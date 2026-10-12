"""Work-scoped process lifetime: scope markers, recorded groups, one census, one reap.

Every process that exists because of work has one custodian that can see it and end
it by measured identity (DEVELOPMENT "Process Custody Rule"). The custodian's view of
its scope on POSIX is the union of three sources, read from live kernel state and
never from what a command line looks like:

* the scope MARKER, an environment entry ``NAME=1`` whose name carries a random
  128-bit hex id (``OURO_INSTALL_SCOPE_<hex>`` for the installation,
  ``OURO_TASK_SCOPE_<hex>`` for a task; the engine's ``CLAUDEXOR_RUN_SCOPE_<hex>`` is
  recognised for shedding). Inherited by every descendant; on Linux readable for every
  process of this user, on macOS only for non-platform binaries — ``ps -E`` shows no
  environment for Apple's own ``/bin/sh``, ``/bin/sleep``, ``/usr/bin/find`` under SIP,
  so the marker alone is blind to exactly a ``find`` orphaned under a killed shell;
* the scope's RECORDED process groups: every own command, executor foreground
  command and task-scoped service is its own group leader (``start_new_session``), so
  the group id with the leader's measured birth is recorded at spawn and a ``cmd &`` or
  ``nohup`` child stays in that group after the shell exits. A recorded group is
  signalled MEMBER BY MEMBER, never ``killpg`` on a bare number, and only under the
  pgid-reuse rule: every candidate with that pgid was born at or after the scope
  started, and a live process whose pid IS the group id carries the recorded leader
  birth; otherwise the id was reused and the group is skipped entirely;
* individually recorded descendants (the ledger and executor records keep theirs).

Inheritance is not a lifetime transfer: a process handed to a longer-lived custodian
sheds every shorter-lived marker at its spawn env (``shed_scopes``). Membership is
re-measured immediately before every signal (birth time), environment bytes are never
logged or persisted (pid, birth, reason only), an unreadable table is unknown and
never an empty scope, and Windows has no environment census: ``end_scope`` reports
``unsupported`` there and the existing Job/TerminateProcess paths stay the truth.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import pathlib
import re
import secrets
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional

from ouroboros import platform_layer as _pl
from ouroboros.utils import atomic_write_json, read_json_dict, update_json_locked, utc_now_iso

log = logging.getLogger(__name__)

SCOPE_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*_SCOPE_[0-9a-f]{32}$")
SCOPE_VALUE = "1"
INSTALL_SCOPE_PREFIX = "OURO_INSTALL_SCOPE_"
TASK_SCOPE_PREFIX = "OURO_TASK_SCOPE_"
RUN_SCOPE_PREFIX = "CLAUDEXOR_RUN_SCOPE_"

INSTALLATION_SCOPE_FILE = "installation_scope.json"
PANIC_SWEEP_PENDING_FILE = "panic_sweep_pending.json"
TASK_SCOPES_DIR = "task_scopes"

# Task-end: TERM, a bounded grace, then KILL; Panic sends KILL at once. Bounded rescans
# catch a fork between the snapshot and the signal; nothing here is a polling monitor.
TASK_END_GRACE_SEC = 2.0
_KILL_CONFIRM_SEC = 1.0
_RESCAN_SETTLE_SEC = 0.1
_CENSUS_TIMEOUT_SEC = 20
_BIRTH_TIMEOUT_SEC = 5
_CLEANUP_RUNNING_STALE_SEC = 60.0

COVERAGE_POSIX = "marker+groups"
COVERAGE_UNSUPPORTED = "unsupported"

# The one disclosure the installation census cannot resolve itself: the engine's
# harness shells and their platform-binary descendants belong to the engine's run
# capture set (reaped at its run barrier or at its next daemon start), and on macOS
# their environment is invisible to this census.
PANIC_CENSUS_RESIDUAL = ("platform-binary orphans of engine harness shells are the engine's capture "
                        "set; this census does not see their environment on macOS")


# --------------------------------------------------------------------------- markers

def mint_marker(prefix: str) -> str:
    """A fresh grammar-shaped scope name: ``<PREFIX><32 random hex digits>``."""
    name = f"{prefix}{secrets.token_hex(16)}"
    if not SCOPE_NAME_RE.match(name):
        raise ValueError(f"scope prefix {prefix!r} does not fit the marker grammar")
    return name


def is_scope_name(name: str) -> bool:
    return bool(SCOPE_NAME_RE.match(str(name or "")))


def scope_entries(env: Mapping[str, str], *, prefix: str = "") -> Dict[str, str]:
    """The grammar-shaped ``NAME=1`` entries of ``env`` (optionally one role's)."""
    return {key: SCOPE_VALUE for key, value in env.items()
            if is_scope_name(key) and str(value) == SCOPE_VALUE and key.startswith(prefix)}


def shed_scopes(env: Mapping[str, str], *, keep_prefixes: Iterable[str] = (INSTALL_SCOPE_PREFIX,)) -> Dict[str, str]:
    """``env`` without the scope markers a longer-lived custodian must not inherit.

    The managed daemon and a keep/session service keep the installation marker and
    drop task and engine-run markers: inheriting one would end them with the first
    task or run that happened to start them.
    """
    kept = tuple(keep_prefixes)
    return {key: value for key, value in env.items()
            if not is_scope_name(key) or key.startswith(kept)}


def installation_scope_entries(env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The installation marker entries present in ``env`` (default: this process)."""
    return scope_entries(os.environ if env is None else env, prefix=INSTALL_SCOPE_PREFIX)


# --------------------------------------------------------------------------- installation scope

def _state_dir(data_dir: Any) -> pathlib.Path:
    return pathlib.Path(data_dir) / "state"


def installation_scope_path(data_dir: Any) -> pathlib.Path:
    return _state_dir(data_dir) / INSTALLATION_SCOPE_FILE


def installation_marker(data_dir: Any, *, create: bool = True) -> str:
    """The persisted installation marker of ``data_dir``: minted once, stable across
    server generations, unique per (temporary) installation. ``""`` when absent and
    ``create`` is false."""
    path = installation_scope_path(data_dir)
    record = read_json_dict(path) or {}
    marker = str(record.get("marker") or "")
    if is_scope_name(marker) and marker.startswith(INSTALL_SCOPE_PREFIX):
        return marker
    if not create:
        return ""
    path.parent.mkdir(parents=True, exist_ok=True)

    def mint(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        existing = str((current or {}).get("marker") or "")
        if is_scope_name(existing) and existing.startswith(INSTALL_SCOPE_PREFIX):
            return None
        return {"marker": mint_marker(INSTALL_SCOPE_PREFIX), "created_at": utc_now_iso()}

    record = update_json_locked(path, mint)
    return str(record.get("marker") or "")


_PLANTED: Dict[str, Any] = {}


def plant_installation_scope(data_dir: Any, environ: Optional[Dict[str, str]] = None) -> str:
    """Put the installation marker into the server's environment BEFORE any child spawns.

    Idempotent: an exec-restarted generation already carries it. Remembers the
    launcher's pid (the parent at planting) so the Panic census can exclude it.
    """
    marker = installation_marker(data_dir, create=True)
    target = os.environ if environ is None else environ
    target[marker] = SCOPE_VALUE
    if environ is None:
        _PLANTED.update(marker=marker, launcher_pid=os.getppid(), server_pid=os.getpid())
    return marker


def panic_census_exclusions() -> set:
    """Never signalled by the installation census: this server, its launcher, pid 1."""
    return {os.getpid(), int(_PLANTED.get("launcher_pid") or os.getppid()), 1}


# --------------------------------------------------------------------------- census

@dataclass
class _Row:
    pid: int
    ppid: int
    pgid: int
    birth: str          # the exact ``ps -o lstart`` text, compared for equality
    epoch: float        # the same instant as seconds, for "born after the scope started"
    markers: Optional[set] = field(default_factory=set)  # None: environment unreadable


_MONTHS = {name: index for index, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}
_LSTART_RE = re.compile(r"^[A-Za-z]{3} ([A-Za-z]{3}) +(\d{1,2}) (\d\d):(\d\d):(\d\d) (\d{4})$")
_ROW_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+([A-Za-z]{3} [A-Za-z]{3} +\d{1,2} \d\d:\d\d:\d\d \d{4})\s*(.*)$")


def _ps_env() -> Dict[str, str]:
    # English month/day names whatever the server's locale: the parser below has no
    # locale tables, and both the snapshot and the per-pid re-measure use this env.
    return {**os.environ, "LC_ALL": "C", "LANG": "C"}


def birth_epoch(text: str) -> float:
    """Seconds since the epoch for one ``lstart`` text; 0.0 when it does not parse."""
    match = _LSTART_RE.match(str(text or "").strip())
    if not match:
        return 0.0
    month, day, hour, minute, second, year = match.groups()
    try:
        return _dt.datetime(int(year), _MONTHS[month], int(day), int(hour), int(minute), int(second)).timestamp()
    except (KeyError, ValueError, OverflowError):
        return 0.0


def birth_text(pid: int) -> str:
    """The birth text of ``pid`` as the table still shows it (``""`` when gone or unmeasurable).

    A just-exited leader not yet waited for is a zombie with its birth intact: that is the
    identity a group RECORD needs (the command may have exited before its row was written)."""
    if _pl.IS_WINDOWS or int(pid) <= 0:
        return ""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True,
                             text=True, timeout=_BIRTH_TIMEOUT_SEC, env=_ps_env())
    except Exception:
        return ""
    text = (out.stdout or "").strip()
    return text if out.returncode == 0 else ""


def measure_birth(pid: int) -> str:
    """The LIVE birth text of ``pid`` (``""`` when gone, a zombie, or unmeasurable).

    The identity re-measured immediately before every signal and every liveness
    answer: a pid that now names another process has another birth; a corpse can
    execute nothing and is gone."""
    text = birth_text(pid)
    if not text:
        return ""
    from ouroboros.process_containment import pid_is_zombie

    return "" if pid_is_zombie(pid) else text


def census(markers: Iterable[str] = ()) -> Optional[Dict[int, _Row]]:
    """One snapshot of the live process table: pid, ppid, pgid, birth and which of
    ``markers`` each process visibly carries. ``None`` when the table could not be read
    (unknown, never empty). macOS reads the environment column of one ``ps -E``;
    Linux reads each ``/proc/<pid>/environ`` as exact NUL-separated entries."""
    if _pl.IS_WINDOWS:
        return None
    wanted = {str(marker) for marker in markers if marker}
    proc_available = os.path.isdir("/proc")
    columns = "pid=,ppid=,pgid=,lstart=" + ("" if proc_available else ",command=")
    argv = ["ps", "-A", "-o", columns] if proc_available else ["ps", "-E", "-ww", "-A", "-o", columns]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                             timeout=_CENSUS_TIMEOUT_SEC, env=_ps_env())
    except Exception:
        return None
    if out.returncode != 0:
        return None
    rows: Dict[int, _Row] = {}
    assignments = {f"{marker}={SCOPE_VALUE}" for marker in wanted}
    for line in (out.stdout or "").splitlines():
        match = _ROW_RE.match(line)
        if not match:
            continue
        pid, ppid, pgid, birth, rest = match.groups()
        row = _Row(int(pid), int(ppid), int(pgid), birth.strip(), birth_epoch(birth))
        if proc_available:
            row.markers = _proc_markers(row.pid, assignments)
        else:
            words = set(rest.split())
            row.markers = {marker for marker in wanted if f"{marker}={SCOPE_VALUE}" in words}
        rows[row.pid] = row
    return rows or None


def _proc_markers(pid: int, assignments: set) -> Optional[set]:
    if not assignments:
        return set()
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            entries = set(handle.read().split(b"\0"))
    except OSError:
        return None  # gone, nondumpable or another user's: unknown, not "no"
    return {entry.decode("utf-8", "replace").split("=", 1)[0]
            for entry in entries if entry.decode("utf-8", "replace") in assignments}


# --------------------------------------------------------------------------- membership

def scope_members(rows: Mapping[int, _Row], *, marker: str = "", groups: Iterable[Mapping[str, Any]] = (),
                  scope_start_epoch: float = 0.0, exclude: Iterable[int] = ()) -> Dict[str, Any]:
    """The scope's live members from one snapshot: marker members ∪ members of the
    recorded groups that pass the pgid-reuse rule. Returns ``{"members": {pid: row},
    "groups_reused": [pgid, ...], "groups_gone": [pgid, ...]}``."""
    excluded = {int(pid) for pid in exclude} | {0, 1}
    members: Dict[int, _Row] = {}
    if marker:
        for row in rows.values():
            if row.pid not in excluded and row.markers and marker in row.markers:
                members[row.pid] = row
    reused: List[int] = []
    gone: List[int] = []
    for group in groups:
        pgid = int(group.get("pgid") or 0)
        if pgid <= 1:
            continue
        candidates = [row for row in rows.values() if row.pgid == pgid]
        if not candidates:
            gone.append(pgid)  # an emptied group is simply gone
            continue
        leader_birth = str(group.get("leader_birth") or "")
        # No member of a recorded group predates its leader (the scope's start at the
        # latest); lstart has one-second resolution, hence the one-second tolerance.
        floor = (birth_epoch(leader_birth) or float(scope_start_epoch or 0.0)) - 1.0
        leader = rows.get(pgid)
        if (leader is not None and leader.birth != leader_birth) or any(
                row.epoch and row.epoch < floor for row in candidates):
            reused.append(pgid)  # the id names another group now: skipped entirely
            continue
        for row in candidates:
            if row.pid not in excluded:
                members[row.pid] = row
    return {"members": members, "groups_reused": reused, "groups_gone": gone}


def _signal_member(pid: int, birth: str, signum: int) -> bool:
    """Signal ``pid`` only when it still carries ``birth``; True when a signal was sent."""
    if measure_birth(pid) != birth:
        return False
    try:
        os.kill(pid, signum)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _await_exit(targets: Mapping[int, str], deadline: float) -> Dict[int, str]:
    """The ``{pid: birth}`` entries still alive with the same birth at ``deadline``."""
    remaining = dict(targets)
    while remaining:
        remaining = {pid: birth for pid, birth in remaining.items() if measure_birth(pid) == birth}
        if not remaining or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    return remaining


def end_scope(*, marker: str = "", groups: Iterable[Mapping[str, Any]] = (), scope_start_epoch: float = 0.0,
              exclude: Iterable[int] = (), grace_sec: float = TASK_END_GRACE_SEC, term_first: bool = True,
              rescans: int = 2) -> Dict[str, Any]:
    """End every live member of a scope by measured identity; report what is left.

    ``ended`` lists members gone after our signal; ``already_gone`` members that left
    between the snapshot and the signal (a vanished, birth-checked member is gone);
    ``survivors`` members alive with their recorded birth after the last pass;
    ``census_failed`` means the table could not be read and NOTHING is claimed clean.
    """
    outcome: Dict[str, Any] = {
        "coverage": COVERAGE_UNSUPPORTED if _pl.IS_WINDOWS else COVERAGE_POSIX,
        "ended": [], "already_gone": [], "survivors": [], "groups_reused": [], "groups_gone": [],
        "census_failed": False, "scans": 0,
    }
    if _pl.IS_WINDOWS:
        return outcome
    groups = list(groups)
    excluded = set(int(pid) for pid in exclude)
    seen: Dict[int, str] = {}
    for scan in range(max(1, int(rescans) + 1)):
        rows = census([marker] if marker else [])
        if rows is None:
            outcome["census_failed"] = True
            break
        outcome["scans"] = scan + 1
        view = scope_members(rows, marker=marker, groups=groups, scope_start_epoch=scope_start_epoch,
                             exclude=excluded)
        outcome["groups_reused"] = sorted(set(outcome["groups_reused"]) | set(view["groups_reused"]))
        outcome["groups_gone"] = sorted(set(outcome["groups_gone"]) | set(view["groups_gone"]))
        fresh = {pid: row.birth for pid, row in view["members"].items() if pid not in seen}
        if not fresh:
            break  # nothing in the scope, or a quiet rescan after the kill pass
        seen.update(fresh)
        signalled: Dict[int, str] = {}
        for pid, birth in fresh.items():
            if _signal_member(pid, birth, signal.SIGTERM if term_first else signal.SIGKILL):
                signalled[pid] = birth
            else:
                outcome["already_gone"].append(pid)
        if term_first:
            left = _await_exit(signalled, time.monotonic() + max(0.0, float(grace_sec)))
            for pid, birth in left.items():
                _signal_member(pid, birth, signal.SIGKILL)
        left = _await_exit(signalled, time.monotonic() + _KILL_CONFIRM_SEC)
        outcome["ended"].extend(pid for pid in signalled if pid not in left)
        outcome["survivors"].extend({"pid": pid, "birth": birth} for pid, birth in left.items())
        time.sleep(_RESCAN_SETTLE_SEC)
    outcome["ended"].sort()
    return outcome


# --------------------------------------------------------------------------- task scopes

def task_scopes_dir(drive_root: Any) -> pathlib.Path:
    return _state_dir(drive_root) / TASK_SCOPES_DIR


def task_scope_path(drive_root: Any, task_id: str) -> pathlib.Path:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(task_id or ""))
    return task_scopes_dir(drive_root) / f"{safe}.json"


def task_scope_record(drive_root: Any, task_id: str) -> Optional[Dict[str, Any]]:
    return read_json_dict(task_scope_path(drive_root, task_id))


def task_scope_records(drive_root: Any) -> List[Dict[str, Any]]:
    folder = task_scopes_dir(drive_root)
    try:
        paths = sorted(folder.iterdir())
    except OSError:
        return []
    records = []
    for path in paths:
        if path.suffix != ".json":
            continue
        record = read_json_dict(path)
        if record and record.get("task_id") and record.get("marker"):
            records.append(record)
    return records


def _fresh_record(task_id: str, groups: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {"task_id": str(task_id), "marker": mint_marker(TASK_SCOPE_PREFIX),
            "minted_at": utc_now_iso(), "minted_epoch": time.time(), "groups": list(groups or [])}


def _rotated(task_id: str, current: Dict[str, Any]) -> Dict[str, Any]:
    """A fresh record for a NEW launch of a task whose cleanup already started: the new
    children get a fresh id so the running cleanup cannot reach them, while the groups the
    old record still owed are carried (rule 2 judges each at the next settlement)."""
    return _fresh_record(task_id, current.get("groups") or [])


def _retire_record(path: pathlib.Path, marker: str) -> bool:
    """Remove the record only while it still names ``marker`` (a rotation in between keeps
    the successor's record), under the record's own writer lock."""
    removed = []

    def tombstone(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if str((current or {}).get("marker") or "") != marker:
            return None
        return {**current, "cleanup": {"state": "done"}}

    def unlink(_written: Dict[str, Any]) -> None:
        path.unlink(missing_ok=True)
        removed.append(True)

    try:
        update_json_locked(path, tombstone, after_write=unlink)
    except (OSError, TimeoutError, ValueError):
        return False
    return bool(removed)


def task_scope_marker(drive_root: Any, task_id: str) -> str:
    """The task's marker, minted and written durably BEFORE the first launch under it.

    A record whose cleanup already started is rotated: the successor's children get a
    fresh id so a late cleanup of the ended attempt cannot reach them."""
    path = task_scope_path(drive_root, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    def ensure(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not current or not is_scope_name(str(current.get("marker") or "")):
            return _fresh_record(task_id)
        return _rotated(task_id, current) if (current.get("cleanup") or {}).get("state") else None

    return str(update_json_locked(path, ensure).get("marker") or "")


def task_scope_env(drive_root: Any, task_id: str) -> Dict[str, str]:
    """The per-launch entry a task's child receives (never put into ``os.environ``)."""
    if not drive_root or not task_id:
        return {}
    return {task_scope_marker(drive_root, task_id): SCOPE_VALUE}


def task_launch_target(binding: Optional[Mapping[str, Any]] = None, *, drive_root: Any = None,
                       task_id: str = "") -> Optional[tuple]:
    """``(drive_root, task_id)`` the launch belongs to, from the context-bound invocation
    binding (``tool_custody.invocation_binding()``) or the caller's own context; ``None``
    for a launch outside any task (nothing is scoped, nothing is recorded)."""
    binding = binding or {}
    root = binding.get("drive_root") or drive_root
    task = str(binding.get("task_id") or task_id or "")
    if not root or not task or task == "manual":
        return None
    return pathlib.Path(root), task


def scoped_child_env(env: Mapping[str, str], target: Optional[tuple]) -> Dict[str, str]:
    """The child's final environment: a task launch adds its task marker; a launch handed to
    a longer-lived custodian (``target=None``: a keep/session service) sheds every task and
    engine-run marker and keeps the installation marker. Never mutates ``env``."""
    if target is None:
        return shed_scopes(env)
    return {**env, **task_scope_env(*target)}


def record_task_group(drive_root: Any, task_id: str, pid: int) -> Optional[Dict[str, Any]]:
    """Record the group a just-spawned task child leads, with its measured birth.

    Groups without a live member are pruned on every append (``killpg(G, 0)`` is one
    syscall), so a task that runs thousands of commands keeps a small record."""
    if _pl.IS_WINDOWS or not drive_root or not task_id or int(pid) <= 0:
        return None
    pgid = _pl.process_group_id(int(pid))
    birth = birth_text(int(pid))  # the leader may already have exited: its corpse still names its birth
    if pgid <= 1 or not birth:
        return None
    entry = {"pgid": pgid, "leader_pid": int(pid), "leader_birth": birth, "recorded_at": utc_now_iso()}
    path = task_scope_path(drive_root, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    def append(current: Dict[str, Any]) -> Dict[str, Any]:
        if not current or not is_scope_name(str(current.get("marker") or "")):
            record = _fresh_record(task_id)
        elif (current.get("cleanup") or {}).get("state"):
            record = _rotated(task_id, current)
        else:
            record = dict(current)
        kept = [group for group in record.get("groups") or []
                if int(group.get("pgid") or 0) != pgid and _pl.process_group_is_alive(int(group.get("pgid") or 0))]
        record["groups"] = [*kept, entry]
        return record

    try:
        update_json_locked(path, append)
    except (OSError, TimeoutError, ValueError):
        log.warning("task scope group for %s could not be recorded; task-end cleanup relies on the marker",
                    task_id, exc_info=True)
        return None
    return entry


def task_scope_terminal_evidence(row: Any) -> bool:
    """Positive durable terminal evidence: a truly terminal status that is not a
    reopenable presence placeholder. Running, interrupted, paused, held, handed off,
    unreadable and placeholder rows are NOT terminal."""
    from ouroboros.task_results import _TRULY_TERMINAL_STATUSES, is_reconciled_presence_placeholder

    if not isinstance(row, dict) or str(row.get("status") or "") not in _TRULY_TERMINAL_STATUSES:
        return False
    return not is_reconciled_presence_placeholder(row)


_CLEANUP_THREADS: List[threading.Thread] = []
_CLEANUP_LOCK = threading.Lock()


def note_task_terminal_transition(drive_root: Any, task_id: str) -> Optional[threading.Thread]:
    """Called by the result writer right after an ACCEPTED transition into a truly
    terminal status landed: the obligation is durable already (the scope record plus
    the terminal row), so it is executed outside the store transaction on a thread.
    Nothing happens for a task that launched nothing (no record)."""
    try:
        if not task_scope_path(drive_root, task_id).exists():
            return None
    except OSError:
        return None
    thread = threading.Thread(target=settle_task_scope, args=(drive_root, task_id),
                              kwargs={"reason": "terminal_transition"}, name=f"task-scope-cleanup-{task_id}",
                              daemon=True)
    with _CLEANUP_LOCK:
        _CLEANUP_THREADS[:] = [item for item in _CLEANUP_THREADS if item.is_alive()]
        _CLEANUP_THREADS.append(thread)
    thread.start()
    return thread


def wait_for_cleanups(timeout_sec: float = 10.0) -> None:
    """Join the cleanup threads this process started (tests and the exit census)."""
    deadline = time.monotonic() + timeout_sec
    with _CLEANUP_LOCK:
        threads = list(_CLEANUP_THREADS)
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))


def settle_task_scope(drive_root: Any, task_id: str, *, reason: str = "sweep") -> Optional[Dict[str, Any]]:
    """Execute one task's cleanup obligation on positive terminal evidence only.

    Reads the record and the durable result; a task that is not truly terminal (or whose
    row is a reopenable placeholder, or unreadable) is left alone. Records the outcome on
    the result (``process_scope``, an identical-status rewrite) and retires the record;
    a failed census keeps the record for the next sweep.
    """
    path = task_scope_path(drive_root, task_id)
    record = read_json_dict(path)
    if not record:
        return None
    try:
        from ouroboros.task_results import load_task_result, write_task_result

        row = load_task_result(drive_root, str(task_id))
    except Exception:
        return None
    if not task_scope_terminal_evidence(row):
        return None
    started_at = time.time()

    def claim(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        cleanup = (current or {}).get("cleanup") or {}
        if cleanup.get("state") == "running" and started_at - float(cleanup.get("started_epoch") or 0) < _CLEANUP_RUNNING_STALE_SEC:
            return None  # another custodian is on it right now
        return {**current, "cleanup": {"state": "running", "started_epoch": started_at, "reason": reason}}

    try:
        claimed = update_json_locked(path, claim)
    except (OSError, TimeoutError, ValueError):
        return None
    if (claimed.get("cleanup") or {}).get("started_epoch") != started_at:
        return None
    outcome = end_scope(marker=str(claimed.get("marker") or ""), groups=claimed.get("groups") or [],
                        scope_start_epoch=float(claimed.get("minted_epoch") or 0.0),
                        exclude={os.getpid(), 1})
    facts = {**outcome, "marker_role": "task", "reason": reason, "settled_at": utc_now_iso()}
    try:
        write_task_result(drive_root, str(task_id), str(row.get("status") or ""), process_scope=facts)
    except Exception:
        log.warning("task scope outcome for %s could not be recorded on its result", task_id, exc_info=True)
    if outcome["census_failed"]:
        try:
            update_json_locked(path, lambda current: {**current, "cleanup": {
                "state": "pending", "reason": "census_failed", "attempted_at": utc_now_iso()}})
        except (OSError, TimeoutError, ValueError):
            pass
        return facts
    _retire_record(path, str(claimed.get("marker") or ""))
    return facts


def settle_task_scopes(drive_root: Any, live_task_ids: Any = None) -> List[Dict[str, Any]]:
    """The startup/periodic sweep's pass over every outstanding obligation: a record whose
    task has positive durable terminal evidence is settled; a live, unknown, retained or
    paused owner keeps its processes. ``live_task_ids`` is a set or a zero-arg callable."""
    records = task_scope_records(drive_root)
    if not records:
        return []
    live = live_task_ids() if callable(live_task_ids) else live_task_ids
    settled = []
    for record in records:
        task_id = str(record["task_id"])
        if live is not None and task_id in live:
            continue
        try:
            facts = settle_task_scope(drive_root, task_id, reason="sweep")
        except Exception:
            log.debug("task scope sweep failed for %s", task_id, exc_info=True)
            continue
        if facts is not None:
            settled.append({"task_id": task_id, **facts})
    return settled


# --------------------------------------------------------------------------- Panic

def panic_sweep_pending_path(data_dir: Any) -> pathlib.Path:
    return _state_dir(data_dir) / PANIC_SWEEP_PENDING_FILE


def _installation_census(data_dir: Any, *, exclude: Iterable[int], rescans: int) -> Dict[str, Any]:
    marker = installation_marker(data_dir, create=False)
    groups = [group for record in task_scope_records(data_dir) for group in (record.get("groups") or [])]
    outcome = end_scope(marker=marker, groups=groups, exclude=exclude, grace_sec=0.0, term_first=False,
                        rescans=rescans)
    outcome["marker_role"] = "installation"
    outcome["recorded_groups"] = len(groups)
    outcome["residual"] = PANIC_CENSUS_RESIDUAL
    return outcome


def panic_scope_census(data_dir: Any, *, exclude: Optional[Iterable[int]] = None) -> Dict[str, Any]:
    """Panic's one more bounded request attempt: SIGKILL every live member of the
    installation scope (marker ∪ recorded task groups) by re-measured identity, excluding
    the server, its launcher and pid 1. The durable ``panic_sweep_pending`` record is
    written FIRST so whatever this process cannot finish before its Panic exit is
    completed by the launcher or by the next start before admission; it is removed only
    after a census that succeeded. Never waits on anything but its own signals."""
    marker = installation_marker(data_dir, create=False)
    if not marker:
        return {"requested": False, "reason": "no installation marker planted"}
    excluded = set(panic_census_exclusions() if exclude is None else {int(pid) for pid in exclude})
    pending = panic_sweep_pending_path(data_dir)
    try:
        pending.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(pending, {"requested_at": utc_now_iso(), "marker_role": "installation",
                                    "server_pid": os.getpid(), "excluded": sorted(excluded)})
    except OSError:
        log.warning("panic sweep pending record could not be written", exc_info=True)
    outcome = _installation_census(data_dir, exclude=excluded, rescans=1)
    if not outcome["census_failed"]:
        try:
            pending.unlink()
        except OSError:
            pass
    return {"requested": True, **outcome}


def complete_pending_panic_sweep(data_dir: Any, *, exclude: Optional[Iterable[int]] = None) -> Optional[Dict[str, Any]]:
    """Finish a Panic sweep a server could not complete before its exit (the launcher's
    Panic branch, or the next start before planting its marker and before any child).
    ``None`` when nothing is pending."""
    pending = panic_sweep_pending_path(data_dir)
    if not pending.exists():
        return None
    excluded = {os.getpid(), 1} if exclude is None else {int(pid) for pid in exclude}
    outcome = _installation_census(data_dir, exclude=excluded, rescans=1)
    if not outcome["census_failed"]:
        try:
            pending.unlink()
        except OSError:
            pass
    log.warning("completed the pending Panic scope sweep: ended=%s survivors=%s census_failed=%s",
                outcome["ended"], outcome["survivors"], outcome["census_failed"])
    return outcome


__all__ = [
    "COVERAGE_POSIX", "COVERAGE_UNSUPPORTED", "INSTALL_SCOPE_PREFIX", "RUN_SCOPE_PREFIX", "SCOPE_NAME_RE",
    "SCOPE_VALUE", "TASK_END_GRACE_SEC", "TASK_SCOPE_PREFIX", "birth_epoch", "census",
    "birth_text", "complete_pending_panic_sweep", "end_scope", "installation_marker", "installation_scope_entries",
    "is_scope_name", "measure_birth", "mint_marker", "note_task_terminal_transition", "panic_census_exclusions",
    "panic_scope_census", "panic_sweep_pending_path", "plant_installation_scope", "record_task_group",
    "scope_entries", "scope_members", "settle_task_scope", "settle_task_scopes", "shed_scopes",
    "task_scope_env", "task_scope_marker", "task_scope_record", "task_scope_records",
    "task_scope_terminal_evidence", "wait_for_cleanups",
]
