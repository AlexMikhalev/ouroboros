"""Git transport for every git-hosted skill source: one shallow cache clone per repository.

A hub and a plain skills repository are both read from a clone under
``data/state/skill_sources/repos/<key>/repo`` (the two public hub rows share one clone because they read
one repository), fetched with this computer's own git credentials, so a private repository works when
the host can fetch it. Reads are local files; a refresh fetches the newest commit of the configured
branch (or the pinned commit) and is started in the background by whoever shows or searches a catalog.
A pointer entry's author repository is fetched at its exact commit into ``pointers/<key>/repo``.
A source clone keeps the last ``HISTORY_DEPTH`` commits so its catalog can say when each skill last
changed (``last_changed``); a skill unchanged within them has no date. Probes and pointers fetch one.

Git never prompts here (``GIT_TERMINAL_PROMPT=0``, batch SSH): an unreachable or private repository is
an error message on its source row, never a hidden dialog. No credential is written into a URL, an
argument or a log line. Every git process runs through the shared bounded runner, which kills the whole
process tree on timeout.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import shutil
import threading
import uuid
from typing import Any, Dict, Optional

from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso

log = logging.getLogger(__name__)

STATE_DIRNAME = "skill_sources"
FETCH_TIMEOUT_SEC = 180
HISTORY_DEPTH = 100
_LOCK_STALE_SEC = 3 * FETCH_TIMEOUT_SEC
_THREADS: Dict[str, threading.Thread] = {}
_THREADS_LOCK = threading.Lock()


class SkillSourceFetchError(RuntimeError):
    """A git fetch of a source failed; the message names the repository, never a credential."""


def sources_state_root(drive_root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(drive_root) / "state" / STATE_DIRNAME


def _key(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:20]


def repo_cache_dir(drive_root: pathlib.Path, row: Dict[str, Any]) -> pathlib.Path:
    """The cache folder of the repository a git-hosted row reads (shared by rows of one repository)."""
    selector = str(row.get("commit") or row.get("ref") or "")
    return sources_state_root(drive_root) / "repos" / _key(str(row.get("url") or ""), selector)


def clone_dir(drive_root: pathlib.Path, row: Dict[str, Any]) -> pathlib.Path:
    return repo_cache_dir(drive_root, row) / "repo"


def read_meta(drive_root: pathlib.Path, row: Dict[str, Any]) -> Dict[str, Any]:
    return read_json_dict(repo_cache_dir(drive_root, row) / "meta.json") or {}


def _git_env() -> Dict[str, str]:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_LFS_SKIP_SMUDGE": "1"})
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


def _git(args: list[str], *, cwd: pathlib.Path, timeout: float = FETCH_TIMEOUT_SEC) -> str:
    from supervisor.git_ops import _run_git_process_bounded

    code, out, err = _run_git_process_bounded(["git", *args], timeout=timeout, cwd=cwd, env=_git_env())
    if code != 0:
        detail = " ".join(str(err or out or "").split())[:400]
        raise SkillSourceFetchError(detail or f"git {args[0]} failed with exit code {code}")
    return str(out or "")


def _origin_text(url: str) -> str:
    return url if "://" in url or "@" in url else pathlib.Path(url).as_uri()


def fetch_tree(target: pathlib.Path, url: str, selector: str, *, depth: int = 1) -> str:
    """Make ``target`` a shallow checkout of ``selector`` (branch, commit or '' for the default branch)
    holding its last ``depth`` commits."""
    if not (target / ".git").is_dir():
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        _git(["init", "--quiet"], cwd=target)
        _git(["remote", "add", "origin", _origin_text(url)], cwd=target)
    else:
        _git(["remote", "set-url", "origin", _origin_text(url)], cwd=target)
    _git(["fetch", "--depth", str(depth), "--no-tags", "--quiet", "origin", selector or "HEAD"], cwd=target)
    _git(["checkout", "--quiet", "--force", "--detach", "FETCH_HEAD"], cwd=target)
    _git(["clean", "-ffdxq"], cwd=target)
    return _git(["rev-parse", "HEAD"], cwd=target).strip()


def _locked(lock_path: pathlib.Path, wait_sec: float):
    from ouroboros.platform_layer import acquire_exclusive_file_lock

    return acquire_exclusive_file_lock(lock_path, timeout_sec=wait_sec, stale_sec=_LOCK_STALE_SEC,
                                       owner_aware_stale=True)


def update_clone(drive_root: pathlib.Path, row: Dict[str, Any], *, wait_sec: float = FETCH_TIMEOUT_SEC,
                 after_fetch: Any = None) -> Dict[str, Any]:
    """Fetch the row's repository into its cache clone and return the meta record.

    ``after_fetch(clone_dir, commit)`` runs under the same lock, so a caller can derive a snapshot from
    exactly this commit before another refresh moves the files. A busy cache (another process is
    fetching it) returns the current meta with ``busy: true``; a failed fetch keeps the previous clone
    and records the error.
    """
    from ouroboros.platform_layer import release_exclusive_file_lock

    cache = repo_cache_dir(drive_root, row)
    cache.mkdir(parents=True, exist_ok=True)
    lock_path = cache / ".lock"
    fd = _locked(lock_path, wait_sec)
    if fd is None:
        return {**read_meta(drive_root, row), "busy": True}
    try:
        meta = read_meta(drive_root, row)
        meta.update({"url": str(row.get("url") or ""), "ref": str(row.get("ref") or ""),
                     "pinned": str(row.get("commit") or "")})
        try:
            commit = fetch_tree(cache / "repo", str(row.get("url") or ""),
                                 str(row.get("commit") or row.get("ref") or ""), depth=HISTORY_DEPTH)
            if after_fetch is not None:
                after_fetch(cache / "repo", commit)
            meta.update({"commit": commit, "fetched_at": utc_now_iso(), "error": "", "error_at": ""})
        except Exception as exc:  # the previous clone and snapshot stay readable
            meta.update({"error": str(exc)[:500] or type(exc).__name__, "error_at": utc_now_iso()})
            log.info("Skill source fetch failed for %s: %s", row.get("id"), meta["error"])
        atomic_write_json(cache / "meta.json", meta)
        return meta
    finally:
        release_exclusive_file_lock(lock_path, fd)


def is_refreshing(drive_root: pathlib.Path, row: Dict[str, Any]) -> bool:
    with _THREADS_LOCK:
        thread = _THREADS.get(str(repo_cache_dir(drive_root, row)))
        return bool(thread and thread.is_alive())


def refresh_in_background(drive_root: pathlib.Path, row: Dict[str, Any], refresh: Any) -> bool:
    """Run ``refresh(drive_root, row)`` on a daemon thread unless one already runs for this repository."""
    key = str(repo_cache_dir(drive_root, row))
    with _THREADS_LOCK:
        current = _THREADS.get(key)
        if current is not None and current.is_alive():
            return False
        thread = threading.Thread(target=refresh, args=(drive_root, row), daemon=True,
                                  name=f"skill-source-refresh-{row.get('id')}")
        _THREADS[key] = thread
        thread.start()
        return True


def fetch_signals(clone: pathlib.Path, branch: str = "signals") -> Optional[bytes]:
    """The ``signals.json`` of a hub's signals branch, or ``None`` when it is unavailable."""
    try:
        _git(["fetch", "--depth", "1", "--no-tags", "--quiet", "origin",
              f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], cwd=clone, timeout=60)
        return _git(["show", f"refs/remotes/origin/{branch}:signals.json"], cwd=clone, timeout=30).encode("utf-8")
    except SkillSourceFetchError:
        return None


def last_changed(clone: pathlib.Path, paths: Dict[str, str]) -> Dict[str, str]:
    """When each of ``paths`` (``{key: path in the repository}``, ``""`` = all of it) last changed:
    ``{key: ISO time}`` of the newest commit of the fetched history touching it. A path whose newest
    commit is where the shallow history is cut off is left out: its real change may be older."""
    from datetime import datetime, timezone

    wanted: Dict[str, list] = {}
    for key, path in paths.items():
        wanted.setdefault(str(path or "").strip("/"), []).append(key)
    if not wanted:
        return {}
    shallow = clone / ".git" / "shallow"
    cut = set(shallow.read_text(encoding="utf-8").split()) if shallow.is_file() else set()
    out = _git(["log", "-z", "--date-order", "--no-renames", "--no-follow", "--no-color", "--no-show-signature",
                "--name-only", "--format=%x01%H %ct", "HEAD", "--", *([] if "" in wanted else sorted(wanted))],
               cwd=clone, timeout=60)
    seen: Dict[str, str] = {}
    for chunk in out.split("\x01")[1:]:
        header, _, names = chunk.partition("\0")
        sha, _, stamp = header.partition(" ")
        when = "" if sha in cut else datetime.fromtimestamp(int(stamp), timezone.utc).isoformat()
        for name in filter(None, (part.strip("\n") for part in names.split("\0"))):
            path = pathlib.PurePosixPath(name)
            for prefix in (path, *path.parents):  # the newest commit touching a path is listed first
                for key in wanted.get("" if str(prefix) == "." else str(prefix), ()):
                    seen.setdefault(key, when)
    return {key: when for key, when in seen.items() if when}


def checkout_at_commit(drive_root: pathlib.Path, url: str, commit: str) -> pathlib.Path:
    """A local tree of ``url`` at exactly ``commit`` (a pointer entry or a base release), cached by both."""
    from ouroboros.platform_layer import release_exclusive_file_lock
    from ouroboros.skill_sources import validate_git_url

    validate_git_url(url)
    cache = sources_state_root(drive_root) / "pointers" / _key(url, commit)
    target = cache / "repo"
    cache.mkdir(parents=True, exist_ok=True)
    fd = _locked(cache / ".lock", FETCH_TIMEOUT_SEC)
    if fd is None:
        raise SkillSourceFetchError("another process is fetching this release; try again shortly")
    try:
        if (target / ".git").is_dir():
            try:
                if _git(["rev-parse", "HEAD"], cwd=target, timeout=30).strip() == commit:
                    return target
            except SkillSourceFetchError:
                pass
        if fetch_tree(target, url, commit) != commit:
            raise SkillSourceFetchError(f"the repository did not serve commit {commit}")
        return target
    finally:
        release_exclusive_file_lock(cache / ".lock", fd)


def scratch_dir(drive_root: pathlib.Path, label: str) -> pathlib.Path:
    """A fresh private folder under the sources state root (probes, merge inputs)."""
    path = sources_state_root(drive_root) / ".scratch" / f"{label}-{uuid.uuid4().hex[:12]}"
    path.mkdir(parents=True)
    return path


__all__ = [
    "FETCH_TIMEOUT_SEC", "HISTORY_DEPTH", "SkillSourceFetchError", "checkout_at_commit", "clone_dir",
    "fetch_signals", "fetch_tree", "is_refreshing", "last_changed", "read_meta", "refresh_in_background",
    "repo_cache_dir", "scratch_dir", "sources_state_root", "update_clone",
]
