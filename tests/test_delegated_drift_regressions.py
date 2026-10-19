"""Focused authority-drift regressions kept outside the size-ratcheted isolation module."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ouroboros import delegate_custody as custody
from ouroboros.subagent_worktrees import provision_execution_snapshot, find_execution_snapshot
from test_delegated_run_isolation import _isolated_entry, _nanny_ctx, _seed_target


def test_drift_capture_freezes_evidence_and_safe_reject_releases_empty_snapshot(tmp_path, monkeypatch):
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    target = _seed_target(tmp_path)
    ctx = _nanny_ctx(tmp_path, target, monkeypatch)
    handle = provision_execution_snapshot(target_root=target, task_id="t-nanny", snapshot_id="snapDrift")
    entry = _isolated_entry(ctx, target, handle, run_id="run-drift")
    (target / "neighbor.txt").write_text("owner change\n", encoding="utf-8")

    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "ready_no_changes"
    assert capture["target_mutated_during_run"] == ["neighbor.txt"]
    manifest_path = custody.delegated_capture_dir(custody.custody_root(ctx), "t-nanny", "snapDrift") / "workspace_patch.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["authority_drift"]["paths"] == ["neighbor.txt"]
    (target / "neighbor.txt").unlink()

    replay = _capture_terminal_patch(ctx, entry)
    assert replay["status"] == "ready_no_changes"
    assert replay["target_mutated_during_run"] == ["neighbor.txt"]
    assert "Rejected delegated run" in _integrate_delegated_patch(
        ctx, "run-drift", "reject", "preserve for inspection")
    assert find_execution_snapshot("snapDrift") is None
    custody._CUSTODY.clear()


def test_excluded_untracked_baseline_drift_is_not_clean(tmp_path, monkeypatch):
    from ouroboros.tools.delegate import _capture_terminal_patch

    target = _seed_target(tmp_path)
    ctx = _nanny_ctx(tmp_path, target, monkeypatch)
    handle = provision_execution_snapshot(target_root=target, task_id="t-nanny", snapshot_id="snapSecretDrift")
    entry = _isolated_entry(ctx, target, handle, run_id="run-secret-drift")
    assert any(row.get("path") == ".env" and row.get("baseline") for row in handle.excluded_untracked)
    (target / ".env").write_text("SECRET=changed\n", encoding="utf-8")

    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "ready_no_changes"
    assert capture["target_mutated_during_run"] == [".env"]
    assert entry.patch_captured is True
    custody._CUSTODY.clear()


def test_nested_untracked_git_repository_does_not_break_snapshot_provision(tmp_path):
    target = _seed_target(tmp_path)
    nested = target / "vendor" / "inner"
    nested.mkdir(parents=True)
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=nested, check=True)
    (nested / "README").write_text("nested\n", encoding="utf-8")

    handle = provision_execution_snapshot(
        target_root=target, task_id="t-nested", snapshot_id="snapNested")

    assert Path(handle.path).exists()
    assert any(row.get("reason") == "nested_repository" for row in handle.excluded_untracked)


def test_drift_uses_snapshot_registry_when_custody_root_differs(tmp_path, monkeypatch):
    from ouroboros.delegate_target_drift import _target_drift_evidence
    from ouroboros.tools.delegate import _capture_terminal_patch

    target = _seed_target(tmp_path)
    ctx = _nanny_ctx(tmp_path, target, monkeypatch)
    handle = provision_execution_snapshot(
        target_root=target, task_id="t-nanny", snapshot_id="snapSeparateRoots")
    entry = _isolated_entry(ctx, target, handle, run_id="run-separate-roots")
    assert find_execution_snapshot(handle.snapshot_id) is not None
    assert find_execution_snapshot(handle.snapshot_id, data_dir=entry.ledger_root) is None

    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "ready_no_changes"
    assert capture["target_drift_checked"] is True
    assert capture.get("target_mutated_during_run", []) == []
    (target / "untracked.txt").write_text("changed\n", encoding="utf-8")
    assert _target_drift_evidence(entry)["paths"] == ["untracked.txt"]
    custody._CUSTODY.clear()


_DEV_SERVER = ("import sqlite3, sys\n"
               "conn = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
               "conn.execute('PRAGMA journal_mode=WAL')\n"
               "conn.execute('PRAGMA wal_autocheckpoint=0')\n"
               "conn.execute('CREATE TABLE IF NOT EXISTS t(v)')\n"
               "conn.execute(\"INSERT INTO t VALUES ('dev server row')\")\n"
               "print('ready', flush=True)\n"
               "for line in sys.stdin:\n"
               "    conn.execute(\"INSERT INTO t VALUES ('another row')\")\n"
               "    print('committed', flush=True)\n")


@pytest.mark.parametrize("ignored", [False, True], ids=["db-and-log-untracked", "db-and-log-ignored"])
def test_another_process_s_live_wal_index_is_excluded_without_a_baseline(tmp_path, monkeypatch, ignored):
    """A project may hold another process's live WAL database (a dev server): its untracked
    ``app.db-shm`` is excluded from a mutating run's snapshot with no content baseline (its bytes are
    shared memory no raw reader opens), so the snapshot provisions, and the post-run capture and
    drift check pass it unread while it changes underneath (``.gitignore`` of ``*.db`` alone never
    covers it; with the log ignored too, nothing else moves)."""
    import hashlib
    import subprocess

    from ouroboros.delegate_target_drift import _target_drift_evidence
    from ouroboros.tools.delegate import _capture_terminal_patch

    target = _seed_target(tmp_path)
    if ignored:
        (target / ".gitignore").write_text("*.db\n*.db-wal\n", encoding="utf-8")
    ctx = _nanny_ctx(tmp_path, target, monkeypatch)
    index = target / "app.db-shm"
    holder = subprocess.Popen([sys.executable, "-I", "-c", _DEV_SERVER, str(target / "app.db")],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready" and index.exists()
        handle = provision_execution_snapshot(target_root=target, task_id="t-nanny", snapshot_id="snapLiveWal")
        [row] = [row for row in handle.excluded_untracked if row.get("path") == "app.db-shm"]
        assert row.get("wal_index") is True and "baseline" not in row and "WAL index" in row["reason"]
        assert not (Path(handle.path) / "app.db-shm").exists()
        entry = _isolated_entry(ctx, target, handle, run_id="run-live-wal")
        before = hashlib.sha256(index.read_bytes()).hexdigest()  # this test process holds no connection to it
        holder.stdin.write("commit\n")
        holder.stdin.flush()
        assert holder.stdout.readline().strip() == "committed"
        assert hashlib.sha256(index.read_bytes()).hexdigest() != before, "the index changed underneath"
        capture = _capture_terminal_patch(ctx, entry)
        evidence = _target_drift_evidence(entry)
    finally:
        holder.stdin.close()
        try:
            holder.wait(timeout=60)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait()
    assert evidence["checked"] is True and evidence["error"] == "", evidence
    assert capture["status"] == "ready_no_changes" and capture.get("target_drift_checked") is True, capture
    assert "app.db-shm" not in evidence["paths"] and "app.db-shm" not in capture.get("target_mutated_during_run", [])
    if ignored:  # only the index moved, and it is no drift
        assert evidence["paths"] == [] and capture.get("target_mutated_during_run", []) == []
    custody._CUSTODY.clear()
