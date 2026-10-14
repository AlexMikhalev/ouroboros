"""An ``index.lock`` belongs to whichever process holds it; its age proves nothing.

No reader, retry or boot step removes it: a live writer finishes its own write, and a
step Git refuses returns, in Git's words, as the failed bootstrap or restart it belonged
to (a refused restart abandons the adoption it carried; a failed bootstrap is told in the
owner chat). An update apply that Git refused at its first tree step is abandoned as
unapplied; every other refusal keeps the fenced rollback. Only an index whose content Git
reports corrupt is backed up and rebuilt; a rebuild Git refuses puts the original back.
Real Git and real writer processes throughout.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import supervisor.git_ops as git_ops

pytestmark = pytest.mark.serial  # real writer processes and pinned module roots


def _git(repo, *args, input=None):
    proc = subprocess.run(["git", *args], cwd=repo, input=input, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"fixture git {args!r} failed: {proc.stderr}"
    return proc.stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "ouroboros")
    (repo / "seed.txt").write_text("committed seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "seed")
    return repo


def _backdate(path, seconds=60):
    """Older than any age gate. Windows refuses to re-time a file another process holds
    open; such a lock stays young there, which changes nothing when no age is read."""
    old = time.time() - seconds
    try:
        os.utime(path, (old, old))
    except PermissionError:
        if sys.platform != "win32":
            raise


def _orphan_lock(repo):
    lock = repo / ".git" / "index.lock"
    lock.write_bytes(b"left by a crashed process\n")
    _backdate(lock)
    return lock


@contextlib.contextmanager
def _live_writer(repo):
    """A real ``git update-index`` that holds ``index.lock`` until its stdin closes."""
    blob = _git(repo, "hash-object", "-w", "--stdin", input="first writer payload\n")
    lock = repo / ".git" / "index.lock"
    proc = subprocess.Popen(["git", "update-index", "--index-info"], cwd=repo,  # bytes: no CRLF in the path
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    writer = SimpleNamespace(lock=lock, stderr="")

    def finish():
        writer.stderr = proc.communicate(timeout=10)[1].decode("utf-8", "replace")
        return proc.returncode

    writer.finish = finish
    try:
        proc.stdin.write(f"100644 {blob}\tfirst.txt\n".encode())
        proc.stdin.flush()
        deadline = time.monotonic() + 10
        while not lock.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert lock.exists() and proc.poll() is None, "the fixture writer never took the index lock"
        yield writer
    finally:
        if proc.returncode is None:  # not collected by the test
            proc.kill()
            proc.communicate(timeout=10)


def _rows(data, kind):
    path = data / "logs" / "supervisor.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [row for row in map(json.loads, lines) if row.get("type") == kind]


def _worker_env(repo):
    return SimpleNamespace(repo_dir=repo, repo_path=lambda rel: repo / rel)


def test_worker_dirty_check_leaves_a_live_writers_old_lock(tmp_path):
    from ouroboros import agent_startup_checks as checks

    repo = _repo(tmp_path)
    with _live_writer(repo) as writer:
        _backdate(writer.lock)  # older than any age gate
        inode = writer.lock.stat().st_ino
        result, _issues = checks.check_uncommitted_changes(_worker_env(repo))
        assert writer.lock.stat().st_ino == inode
        assert writer.finish() == 0, writer.stderr
    assert result["status"] != "error", result
    assert {"seed.txt", "first.txt"} <= set(_git(repo, "ls-files").split())


def test_worker_dirty_check_does_not_refresh_the_index(tmp_path, monkeypatch):
    from ouroboros import agent_startup_checks as checks

    # Production default; the suite's child-environment wrapper would otherwise pin "0".
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")
    repo = _repo(tmp_path)
    seed = repo / "seed.txt"
    info = seed.stat()
    os.utime(seed, ns=(info.st_atime_ns, info.st_mtime_ns - 10_000_000_000))  # same bytes, stale stat cache
    index = repo / ".git" / "index"
    before = index.read_bytes()

    assert checks.check_uncommitted_changes(_worker_env(repo)) == ({"status": "ok"}, 0)
    assert index.read_bytes() == before, "the worker's status took the optional index lock"


def test_generic_git_capture_leaves_a_live_writers_old_lock(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(git_ops, "REPO_DIR", repo)
    blob = _git(repo, "hash-object", "-w", "--stdin", input="contender payload\n")
    with _live_writer(repo) as writer:
        _backdate(writer.lock)
        rc, _out, err = git_ops.git_capture(
            ["git", "update-index", "--add", "--cacheinfo", f"100644,{blob},contender.txt"])
        assert rc != 0 and "index.lock" in err
        assert writer.lock.exists()
        assert writer.finish() == 0, writer.stderr
    entries = set(_git(repo, "ls-files").split())
    assert "first.txt" in entries and "contender.txt" not in entries


@pytest.fixture
def managed_boot(tmp_path, monkeypatch):
    """Real bootstrap -> serialized restart -> safe_restart -> checkout_and_reset -> Git.

    Dependency sync, the import test and remotes are inert."""
    import server

    repo, data = _repo(tmp_path), tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    for name, value in {"REPO_DIR": repo, "DRIVE_ROOT": data, "BRANCH_DEV": "ouroboros",
                        "BRANCH_STABLE": "ouroboros-stable"}.items():
        monkeypatch.setattr(git_ops, name, value)
    monkeypatch.setattr(git_ops, "init", lambda **_kw: None)  # roots pinned above
    monkeypatch.setattr(git_ops, "update_state", lambda mutate, **_kw: mutate({}))
    monkeypatch.setattr(git_ops, "sync_runtime_dependencies", lambda **_kw: (True, "fixture"))
    monkeypatch.setattr(git_ops, "import_test", lambda: {"ok": True})
    for name, value in {"REPO_DIR": repo, "DATA_DIR": data, "_LAUNCHER_MANAGED": True,
                        "_LAUNCHER_MANAGED_REPO_DIR": str(repo)}.items():
        monkeypatch.setattr(server, name, value)
    monkeypatch.setattr(server, "setup_remote_if_configured", lambda *_a: None)
    monkeypatch.setattr(server, "_runtime_branch_defaults", lambda: ("ouroboros", "ouroboros-stable"))
    monkeypatch.setattr(server, "_has_active_evolution_transaction", lambda: False)
    monkeypatch.delenv("OUROBOROS_DISABLE_MANAGED_UPDATES", raising=False)
    return SimpleNamespace(server=server, repo=repo, data=data)


@pytest.mark.parametrize("holder", ["crashed", "live"])
def test_bootstrap_returns_a_refused_checkout_and_keeps_the_lock(managed_boot, holder):
    boot = managed_boot
    head = _git(boot.repo, "rev-parse", "HEAD")
    with contextlib.ExitStack() as stack:
        writer = stack.enter_context(_live_writer(boot.repo)) if holder == "live" else None
        lock = writer.lock if writer else _orphan_lock(boot.repo)
        _backdate(lock)

        ok, message = boot.server._bootstrap_supervisor_repo({}, git_ops)

        assert ok is False and lock.exists()
        assert "git reset --hard HEAD" in message and "index.lock" in message
        assert "untouched" not in message.lower()  # earlier steps of a checkout may have run
        assert writer is None or writer.finish() == 0
    assert _git(boot.repo, "rev-parse", "HEAD") == head
    assert holder == "crashed" or "first.txt" in _git(boot.repo, "ls-files").split()
    [row] = _rows(boot.data, "checkout_step_failed")
    assert row["reason"] == "bootstrap" and row["returncode"] != 0 and "index.lock" in row["stderr"]


@pytest.mark.parametrize("boot_kind", ["managed_updates_disabled", "local_dev"])
def test_a_held_lock_does_not_fail_a_bootstrap_that_writes_no_index(managed_boot, monkeypatch, boot_kind):
    lock = _orphan_lock(managed_boot.repo)
    if boot_kind == "managed_updates_disabled":
        monkeypatch.setenv("OUROBOROS_DISABLE_MANAGED_UPDATES", "1")
    else:
        monkeypatch.setattr(managed_boot.server, "_LAUNCHER_MANAGED", False)

    ok, message = managed_boot.server._bootstrap_supervisor_repo({}, git_ops)

    assert ok is True, message
    assert lock.exists()


def test_owner_restart_reports_a_refused_checkout_and_does_not_exit(managed_boot, monkeypatch):
    from ouroboros import server_restart

    def must_not_run(*_a, **_kw):
        raise AssertionError("a refused checkout must not proceed to the restart")

    monkeypatch.setattr(server_restart, "_stop_owned_work", must_not_run)
    monkeypatch.setattr(server_restart, "_request_restart_exit", must_not_run)
    with _live_writer(managed_boot.repo) as writer:
        ok, message = server_restart._perform_owner_restart(SimpleNamespace(safe_restart=git_ops.safe_restart))
        assert ok is False and "index.lock" in message
        assert writer.finish() == 0, writer.stderr
    assert "first.txt" in _git(managed_boot.repo, "ls-files").split()


def test_a_refused_restart_checkout_abandons_the_authorized_adoption(tmp_path, monkeypatch):
    """`bind_restart` abandons an adoption on a returned refusal; a raised one left it authorized."""
    from ouroboros import body_adoption, body_candidate
    from tests.body_candidate_support import isolate, make_ctx, make_serving, restart_receipt, rich_commit

    serving, data = make_serving(tmp_path), isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")
    ctx = make_ctx(serving, data, "root-refused-checkout")
    body_candidate.prepare(ctx)
    cand = rich_commit(ctx.repo_dir)
    body_candidate.record_reviewed_commit(ctx, cand)
    body_adoption.authorize(ctx, cand, reason="adopt it")
    restart_receipt(data, cand, "adopt it")
    for name, value in {"REPO_DIR": serving, "DRIVE_ROOT": data, "BRANCH_DEV": "ouroboros",
                        "BRANCH_STABLE": "ouroboros-stable"}.items():
        monkeypatch.setattr(git_ops, name, value)
    monkeypatch.setattr(git_ops, "sync_runtime_dependencies", lambda **_kw: (True, "fixture"))
    monkeypatch.setattr(git_ops, "import_test", lambda: {"ok": True})
    lock = _orphan_lock(serving)

    ok, message = body_adoption.bind_restart(git_ops.safe_restart, data, "adopt it")(
        reason="agent_restart_request", unsynced_policy="rescue_and_block")

    assert ok is False and "was not armed" in message and "index.lock" in message
    assert body_adoption.read(data) == {} and lock.exists()  # abandoned: archived, no live handoff


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX modes as a non-root user")
def test_an_unreadable_valid_index_is_reported_not_rebuilt(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(git_ops, "REPO_DIR", repo)
    (repo / "staged.txt").write_text("unique staged intent\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")
    index = repo / ".git" / "index"
    original = index.read_bytes()
    index.chmod(0)
    try:
        rc, _out, err = git_ops.git_capture(["git", "status", "--porcelain"])
    finally:
        index.chmod(0o644)

    assert rc != 0 and "index file open failed" in err
    assert index.read_bytes() == original
    assert not list((repo / ".git").glob("index.corrupt.*.bak"))
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["staged.txt"]


def test_a_rebuild_refused_by_a_held_lock_restores_the_index_and_reports_the_original_failure(tmp_path, monkeypatch):
    """Retrying against the moved-aside index read every tracked file as deleted (a later
    commit recorded an empty tree); the original bytes come back and stay backed up."""
    repo = _repo(tmp_path)
    monkeypatch.setattr(git_ops, "REPO_DIR", repo)
    index = repo / ".git" / "index"
    index.write_bytes(b"invalid index")
    lock = _orphan_lock(repo)

    for _read in range(2):  # a second read meets the same original failure, never deletions
        rc, out, err = git_ops.git_capture(["git", "status", "--porcelain"])
        assert (rc != 0, out) == (True, "") and "index file smaller than expected" in err
        assert index.read_bytes() == b"invalid index"
    assert lock.read_bytes() == b"left by a crashed process\n"
    backups = list((repo / ".git").glob("index.corrupt.*.bak"))
    assert backups and all(path.read_bytes() == b"invalid index" for path in backups)

    lock.unlink()  # the owner (or the agent) resolves the lock; the ordinary repair then proceeds
    assert git_ops.git_capture(["git", "status", "--porcelain"])[0] == 0
    assert _git(repo, "ls-files").split() == ["seed.txt"]


def test_a_malformed_index_is_still_backed_up_and_rebuilt(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(git_ops, "REPO_DIR", repo)
    (repo / ".git" / "index").write_bytes(b"invalid index")

    rc, _out, err = git_ops.git_capture(["git", "status", "--porcelain"])

    assert rc == 0, err
    [backup] = (repo / ".git").glob("index.corrupt.*.bak")
    assert backup.read_bytes() == b"invalid index"
    assert _git(repo, "ls-files").split() == ["seed.txt"]


# --------------------------------------------------------------------------- #
# A UI replace refused before its first tree step is abandoned as unapplied;
# every other refusal keeps the fenced rollback (and names Git's own words).
# --------------------------------------------------------------------------- #
def _commit(repo, message, files=(), delete=()):
    for rel, text in dict(files).items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    for rel in delete:
        (repo / rel).unlink()
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def replace_scene(tmp_path, monkeypatch):
    """The real replace door: preflight prepare (rescue, keep ref, intent), tx, checkout and the
    fenced rollback. Only the fetching plan, the worker fence/revival and the restart callback are
    recorded. The local branch carries one owner commit the target lacks, so a keep ref is made."""
    from ouroboros.gateway import control
    from supervisor import update_merge

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "ouroboros")
    root = _commit(repo, "root", {"BIBLE.md": "constitution\n", "ouroboros/store.py": "# store\n",
                                  "ouroboros/usage_store.py": "# usage\n", "seed.txt": "seed\n"})
    base = _commit(repo, "owner work", {"local.txt": "owner commit\n"})
    _git(repo, "checkout", "-q", "-b", "official", root)
    target = _commit(repo, "official release", {"official.txt": "release\n"})
    storeless = _commit(repo, "store-less release", delete=("ouroboros/store.py",))
    _git(repo, "checkout", "-q", "ouroboros")
    (repo / ".git" / git_ops.MANAGED_REPO_META_NAME).write_text(
        json.dumps({"managed_local_branch": "ouroboros"}), encoding="utf-8")
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    for name, value in {"REPO_DIR": repo, "DRIVE_ROOT": data, "BRANCH_DEV": "ouroboros"}.items():
        monkeypatch.setattr(git_ops, name, value)
    monkeypatch.setattr(git_ops, "update_state", lambda mutate, **_kw: mutate({}))
    monkeypatch.setattr(git_ops, "_managed_update_target", lambda *_a: ("", "", "official"))
    events = {"fenced": [], "respawned": 0, "restarts": []}
    monkeypatch.setattr(control, "_quiesce_repo_writers", lambda _reason: [])
    monkeypatch.setattr(control, "_respawn_workers_after_failed_update",
                        lambda: events.__setitem__("respawned", events["respawned"] + 1))
    real_fenced = control._rollback_fenced_update
    monkeypatch.setattr(control, "_rollback_fenced_update",
                        lambda reason, error, **extra: events["fenced"].append(reason) or real_fenced(reason, error, **extra))
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    app = Starlette(routes=[Route("/api/update/apply", control.api_update_apply, methods=["POST"])])
    app.state.request_restart = lambda **kw: events["restarts"].append(kw)

    def apply(to):
        plan = {"kind": "clean", "base_sha": base, "target_sha": to, "target_ref": "official",
                "update_channel": "stable", "local_dirty_count": 0}
        monkeypatch.setattr(update_merge, "plan_managed_update_merge", lambda **_kw: dict(plan))
        monkeypatch.setattr(git_ops, "_resolve_managed_update_target", lambda *_a: ("official", to, ""))
        with TestClient(app) as client:
            response = client.post("/api/update/apply", json={"strategy": "replace", "confirm_recovery": True,
                                                            "expected_base_sha": base, "expected_target_sha": to})
        return response.status_code, response.json()

    return SimpleNamespace(repo=repo, data=data, base=base, target=target, storeless=storeless,
                           events=events, apply=apply)


def test_a_replace_refused_at_its_first_tree_step_by_a_held_lock_is_abandoned_unapplied(replace_scene):
    """Not a gate_blocked trap: the tx and intent are cleared, writers reopen, Git's refusal is
    named, and the rescue snapshot and keep ref made before the checkout stay (and are reported)."""
    from supervisor import update_merge

    scene = replace_scene
    with _live_writer(scene.repo) as writer:
        inode = writer.lock.stat().st_ino
        status, body = scene.apply(scene.target)

        outcome = (status, body.get("reason"), body.get("abandoned"), body.get("rolled_back"))
        assert outcome == (409, "replace_not_applied", True, False), body
        assert "index.lock" in body["error"] and "File exists" in body["error"]
        assert scene.events == {"fenced": [], "respawned": 1, "restarts": []}
        assert update_merge.read_update_tx_strict()[0] == "absent" and not git_ops._read_update_intent()
        assert _git(scene.repo, "rev-parse", "HEAD") == scene.base and not (scene.repo / "official.txt").exists()
        assert writer.lock.stat().st_ino == inode
        assert writer.finish() == 0, writer.stderr
    assert "first.txt" in _git(scene.repo, "ls-files").split()
    assert body["keep_branch"] and _git(scene.repo, "rev-parse", body["keep_branch"]) == scene.base  # retained
    assert isinstance(body["rescue"], dict)  # the preflight rescue snapshot is retained and reported
    [row] = _rows(scene.data, "managed_update_replace_apply_abandoned")
    assert row["cause"] == "index_lock_refused" and row["cmd"] == "git reset --hard HEAD" and row["head"] == scene.base


def test_a_lock_that_refuses_a_later_tree_step_keeps_the_fenced_rollback(replace_scene, monkeypatch):
    """A refusal after an earlier tree step ran proves nothing about the tree: no abandon."""
    from supervisor import update_merge

    scene, real = replace_scene, git_ops._run_git_resilient

    def lock_after_the_first_step(cmd, **kwargs):
        result = real(cmd, **kwargs)
        if not (scene.repo / ".git" / "index.lock").exists():
            _orphan_lock(scene.repo)  # another process takes the lock between two checkout steps
        return result

    monkeypatch.setattr(git_ops, "_run_git_resilient", lock_after_the_first_step)
    status, body = scene.apply(scene.target)

    assert scene.events["fenced"] == ["replace_checkout_exception"] and "abandoned" not in body
    assert "index.lock" in body["error"] and "earlier checkout steps may already have" in body["error"]
    assert status == 500 and body["restart_required"] is True  # the same lock refuses the rollback
    assert update_merge.read_update_tx_strict()[1]["phase"] == update_merge.GATE_BLOCKED_PHASE
    assert not _rows(scene.data, "managed_update_replace_apply_abandoned")


def test_a_store_switch_deferral_moves_nothing_and_is_untouched_by_a_held_lock(replace_scene):
    from ouroboros import store_switch
    from supervisor import update_merge

    scene = replace_scene
    with _live_writer(scene.repo) as writer:
        status, body = scene.apply(scene.storeless)
        assert (status, body["status"]) == (200, "ok") and scene.events["fenced"] == []
        assert update_merge.read_update_tx_strict()[1]["phase"] == "pending_boot_smoke"
        assert store_switch.read_switch_intent(scene.data)["target_sha"] == scene.storeless
        assert _git(scene.repo, "rev-parse", "HEAD") == scene.base and writer.lock.exists()
        assert writer.finish() == 0, writer.stderr
    assert len(scene.events["restarts"]) == 1


@pytest.mark.parametrize("case", ["every_attempt_refused", "mixed_attempts", "foreign_lock", "not_a_locking_command",
                                  "repair_between"])
def test_only_a_lock_refusal_on_every_attempt_of_a_locking_command_is_typed(tmp_path, monkeypatch, case):
    import time as time_module

    from supervisor import git_ops_reset

    repo = _repo(tmp_path)
    monkeypatch.setattr(git_ops, "REPO_DIR", repo)
    owner = tmp_path / "other" if case == "foreign_lock" else repo
    refusal = f"fatal: Unable to create '{owner}/.git/index.lock': File exists.\n\nAnother git process seems to be running"
    results = [(1, "error: unrelated failure")] if case == "mixed_attempts" else []
    results += [(128, refusal)] * (5 - len(results))
    cmd = ["git", "clean", "-fd"] if case == "not_a_locking_command" else ["git", "reset", "--hard", "HEAD"]
    def fake_run(argv, **_kw):
        returncode, stderr = results.pop(0)
        return subprocess.CompletedProcess(argv, returncode, "", stderr)

    monkeypatch.setattr(git_ops_reset.subprocess, "run", fake_run)
    monkeypatch.setattr(time_module, "sleep", lambda _seconds: None)
    repaired = iter([case == "repair_between"])
    monkeypatch.setattr(git_ops, "_maybe_repair_git_index", lambda *_a, **_k: next(repaired, False))

    with pytest.raises(subprocess.CalledProcessError) as raised:
        git_ops._run_git_resilient(cmd, cwd=str(repo), check=True)

    expected = git_ops_reset.IndexLockRefusal if case == "every_attempt_refused" else subprocess.CalledProcessError
    assert type(raised.value) is expected and results == []
    assert getattr(raised.value, "before_tree_effects", False) is False  # only checkout's first step sets it


@pytest.mark.parametrize("owner_chat", [42, 0])
def test_a_failed_bootstrap_is_told_once_in_the_owner_chat_and_a_deliberate_deferral_is_not(managed_boot, monkeypatch, owner_chat):
    from supervisor import evolution_lifecycle, message_bus, state, update_merge

    sent = []
    monkeypatch.setattr(state, "load_state", lambda: {"owner_chat_id": owner_chat})
    monkeypatch.setattr(message_bus, "send_with_budget", lambda chat, text, **kw: sent.append((chat, text, kw)))
    lock = _orphan_lock(managed_boot.repo)

    ok, message = managed_boot.server._bootstrap_supervisor_repo({}, git_ops)

    assert ok is False and len(sent) == 1
    chat, text, kw = sent[0]
    assert (chat, kw) == (owner_chat, {"role": "system", "system_type": "startup_notice"})
    assert message in text and "index.lock" in text
    assert text.startswith("Startup bootstrap failed: ")  # no decorative glyph to strip at render

    sent.clear()
    lock.unlink()
    monkeypatch.setattr(evolution_lifecycle, "disable_evolution_projection", lambda: None)
    monkeypatch.setattr(evolution_lifecycle, "pause_evolution_campaign", lambda *_a: None)
    head = _git(managed_boot.repo, "rev-parse", "HEAD")
    update_merge.write_update_tx({"phase": "assisted_resolution", "task_id": "resolver",
                                  "pre_update_sha": head, "pre_update_branch": "ouroboros"})
    ok, message = managed_boot.server._bootstrap_supervisor_repo({}, git_ops)
    assert ok is False and "still being resolved" in message and sent == []  # deliberate deferral

    update_merge.clear_update_tx()
    assert managed_boot.server._bootstrap_supervisor_repo({}, git_ops)[0] is True and sent == []


@pytest.mark.parametrize("owner_chat", [None, "", -5, "not-an-id"])
def test_bootstrap_notice_never_invents_an_owner_address(monkeypatch, owner_chat):
    from ouroboros.server_restart import _noticed_bootstrap
    from supervisor import message_bus, state

    sent = []
    monkeypatch.setattr(state, "load_state", lambda: {"owner_chat_id": owner_chat})
    monkeypatch.setattr(message_bus, "send_with_budget", lambda *a, **kw: sent.append((a, kw)))
    assert _noticed_bootstrap(lambda **kw: (False, "Git refusal"))() == (False, "Git refusal")
    assert sent == []


def test_landed_update_keeps_its_own_boot_authority_after_refused_bootstrap(managed_boot, monkeypatch):
    """Real bootstrap and boot-finalizer consumers; deps/import/native probing and feed refresh are inert.

    A usable supervisor plus the landed update identity must not initiate lock-caused
    rollback. Adoption still observes the unsuccessful bootstrap separately.
    """
    from ouroboros import body_adoption, update_letter
    from supervisor import update_merge

    boot = managed_boot
    head = _git(boot.repo, "rev-parse", "HEAD")
    lock = _orphan_lock(boot.repo)
    ok, message = boot.server._bootstrap_supervisor_repo({}, git_ops)
    assert ok is False and "index.lock" in message
    monkeypatch.setattr(boot.server, "_bootstrap_ok", ok)
    monkeypatch.setattr(boot.server, "_wait_for_supervisor_update_finalize", lambda: True)
    monkeypatch.setattr(git_ops, "compute_managed_update_status", lambda **kw: {})
    monkeypatch.setattr(update_letter, "refresh_after_check", lambda *a, **kw: None)
    monkeypatch.setattr(boot.server, "broadcast_ws_sync", lambda *a: None)
    monkeypatch.setattr(update_merge, "external_host_restart_smoke", lambda: {"ok": True})
    adoption = []
    monkeypatch.setattr(body_adoption, "settle_on_boot", lambda *a, **kw: adoption.append(kw))
    update_merge.write_update_tx({"phase": "pending_boot_smoke", "merge_commit": head,
                                  "pre_update_sha": head, "pre_update_branch": "ouroboros"})

    boot.server._boot_managed_update_tasks()

    assert update_merge.read_update_tx_strict()[0] == "absent"
    assert adoption == [{"supervisor_ready": False}]
    assert lock.read_bytes() == b"left by a crashed process\n"
    assert not _rows(boot.data, "managed_update_rollback")


def test_deferred_replace_records_git_refusal_after_export_and_retirement(replace_scene, monkeypatch):
    """The store-less target defers checkout until exit; its real owner still owes Git's cause."""
    from ouroboros import store, store_switch
    from ouroboros.store_import import ensure_current_store
    from supervisor import message_bus

    scene = replace_scene
    ensure_current_store(scene.data)
    assert store.store_ready(scene.data)
    lock = _orphan_lock(scene.repo)
    status, body = scene.apply(scene.storeless)
    assert status == 200 and body["status"] == "ok"
    outcome = store_switch.run_switch_seam(scene.data, repo_dir=scene.repo)
    assert outcome["status"] == "switch_failed" and "index.lock" in outcome["message"]
    receipt = json.loads((scene.data / store_switch.RECEIPT_REL).read_text(encoding="utf-8"))
    assert receipt["export"]["status"] == "completed" and receipt["retired"]
    assert receipt["switched"]["ok"] is False and "index.lock" in receipt["switched"]["message"]
    assert store_switch.read_switch_intent(scene.data) is None
    assert lock.exists() and _git(scene.repo, "rev-parse", "HEAD") == scene.base
    # The current generation re-imports its completed export; the old intent is not replayed.
    assert store_switch.resume_pending_switch(scene.data, repo_dir=scene.repo)["status"] == "nothing_pending"
    ensure_current_store(scene.data)
    sent = []
    monkeypatch.setattr(message_bus, "send_with_budget", lambda chat, text, **kw: sent.append((chat, text, kw)))
    store_switch.report_outcomes(scene.data, 42)
    assert len(sent) == 1 and "index.lock" in sent[0][1]
    assert sent[0][2]["system_type"] == "storage_switch_notice"
