"""Scope markers, recorded groups and the one census/reap (issue #1772, Directive 1).

Real children are confined to this test process's own spawns and ended by exact pid;
nothing here reads or signals another installation. The macOS platform-binary case is
the incident shape: ``/bin/sleep`` shows no environment under ``ps -E`` with SIP, so it
is ended through its recorded group, while a node child is ended through the marker.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from ouroboros import platform_layer as _pl
from ouroboros import process_scope as ps

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the environment census is POSIX-only")


def _sleeper(env=None, *, argv=None, new_session=True) -> subprocess.Popen:
    env = {**os.environ, **(env or {})}
    return subprocess.Popen(argv or [sys.executable, "-c", "import time; time.sleep(120)"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            start_new_session=new_session)


def _end(*procs: subprocess.Popen) -> None:
    for proc in procs:
        if proc.poll() is None:
            try:
                os.kill(proc.pid, 9)
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def _gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not ps.measure_birth(pid):
            return True
        time.sleep(0.05)
    return not ps.measure_birth(pid)


def _group(proc: subprocess.Popen) -> dict:
    return {"pgid": os.getpgid(proc.pid), "leader_pid": proc.pid, "leader_birth": ps.measure_birth(proc.pid)}


# --------------------------------------------------------------------------- markers

def test_marker_grammar_and_roles():
    for prefix in (ps.INSTALL_SCOPE_PREFIX, ps.TASK_SCOPE_PREFIX, ps.RUN_SCOPE_PREFIX):
        name = ps.mint_marker(prefix)
        assert ps.is_scope_name(name) and name.startswith(prefix) and len(name) == len(prefix) + 32
    assert ps.mint_marker(ps.TASK_SCOPE_PREFIX) != ps.mint_marker(ps.TASK_SCOPE_PREFIX)
    assert not ps.is_scope_name("OURO_TASK_SCOPE_/tmp/root")  # a path is never a marker
    assert not ps.is_scope_name("OURO_PROC_CONTAINER_" + "a" * 32)  # the gate token is another grammar
    with pytest.raises(ValueError):
        ps.mint_marker("lower_scope_")


def test_shedding_keeps_the_installation_marker_and_drops_shorter_lived_ones():
    install, task, run = (ps.mint_marker(p) for p in (ps.INSTALL_SCOPE_PREFIX, ps.TASK_SCOPE_PREFIX, ps.RUN_SCOPE_PREFIX))
    env = {install: "1", task: "1", run: "1", "PATH": "/bin", "OURO_PROC_CONTAINER_" + "f" * 32: "1"}
    shed = ps.shed_scopes(env)
    assert install in shed and task not in shed and run not in shed
    assert shed["PATH"] == "/bin" and "OURO_PROC_CONTAINER_" + "f" * 32 in shed  # non-grammar keys untouched
    assert ps.scope_entries(env) == {install: "1", task: "1", run: "1"}
    assert ps.installation_scope_entries(env) == {install: "1"}
    assert ps.scope_entries({task: "0"}) == {}  # the value is exactly "1"


def test_installation_marker_is_persisted_stable_and_unique_per_root(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert ps.installation_marker(a, create=False) == ""
    first = ps.installation_marker(a)
    assert first.startswith(ps.INSTALL_SCOPE_PREFIX) and ps.installation_marker(a) == first
    record = json.loads(ps.installation_scope_path(a).read_text(encoding="utf-8"))
    assert record["marker"] == first and record["created_at"]
    assert ps.installation_marker(b) != first
    env: dict = {}
    assert ps.plant_installation_scope(a, env) == first and env == {first: "1"}


# --------------------------------------------------------------------------- census and reap

def test_census_reads_pid_pgid_birth_and_visible_markers():
    marker = ps.mint_marker(ps.TASK_SCOPE_PREFIX)
    child = _sleeper({marker: "1"})
    try:
        rows = ps.census([marker])
        assert rows is not None and child.pid in rows
        row = rows[child.pid]
        assert row.pgid == child.pid and row.ppid == os.getpid()
        assert row.birth == ps.measure_birth(child.pid) and row.epoch > 0
        assert marker in (row.markers or set()), "a non-platform child's marker is visible"
        assert marker not in (rows[os.getpid()].markers or set())
    finally:
        _end(child)


@pytest.mark.skipif(not _pl.IS_MACOS, reason="the SIP finding is a macOS fact")
def test_on_macos_a_platform_binary_shows_no_environment_so_the_marker_alone_is_blind():
    marker = ps.mint_marker(ps.TASK_SCOPE_PREFIX)
    child = _sleeper({marker: "1"}, argv=["/bin/sleep", "120"])
    try:
        rows = ps.census([marker])
        assert rows is not None and child.pid in rows
        assert marker not in (rows[child.pid].markers or set()), "env invisible: the group is the only source"
    finally:
        _end(child)


def test_end_scope_ends_a_platform_binary_through_its_group_and_a_node_child_through_the_marker(tmp_path):
    node = _pl.resolve_bundled_node()
    if not node:
        pytest.skip("no bundled node for the marker-visible child")
    marker = ps.mint_marker(ps.TASK_SCOPE_PREFIX)
    # The incident shape of a task's own command: a shell in its own group starts a
    # background /bin/sleep and exits; the sleep stays in the recorded group.
    shell = _sleeper({marker: "1"}, argv=["/bin/sh", "-c", "/bin/sleep 120 >/dev/null 2>&1 & /bin/sleep 1"])
    group = _group(shell)  # recorded while the leader lives, as the spawn seams do
    shell.wait(timeout=5)  # the shell is gone; its background child keeps the group
    rows = ps.census([marker])
    orphan = [row.pid for row in rows.values() if row.pgid == group["pgid"] and row.pid != shell.pid]
    assert len(orphan) == 1, "the background sleep stays in the exited shell's group"
    orphan_pid = orphan[0]
    # A node child that left the group (setsid) but carries the marker.
    runaway = _sleeper({marker: "1"}, argv=[node, "-e", "setTimeout(() => {}, 120000)"])
    bystander = _sleeper({ps.mint_marker(ps.TASK_SCOPE_PREFIX): "1"})
    try:
        outcome = ps.end_scope(marker=marker, groups=[group], exclude={os.getpid(), 1}, grace_sec=0.5)
        assert outcome["coverage"] == ps.COVERAGE_POSIX and not outcome["census_failed"]
        assert set(outcome["ended"]) == {orphan_pid, runaway.pid}
        assert outcome["survivors"] == [] and outcome["groups_reused"] == []
        assert _gone(orphan_pid) and _gone(runaway.pid)
        assert bystander.poll() is None, "another scope's child is untouched"
    finally:
        _end(runaway, bystander)
        try:
            os.kill(orphan_pid, 9)
        except ProcessLookupError:
            pass


def test_a_reused_or_emptied_group_is_never_signalled():
    bystander = _sleeper()
    try:
        reused = {"pgid": os.getpgid(bystander.pid), "leader_pid": bystander.pid,
                  "leader_birth": "Mon Jan  1 00:00:00 2001"}  # not the recorded leader: the id was reused
        gone = {"pgid": 2 ** 22 - 7, "leader_pid": 2 ** 22 - 7, "leader_birth": "Mon Jan  1 00:00:00 2001"}
        outcome = ps.end_scope(groups=[reused, gone], exclude={os.getpid(), 1})
        assert outcome["groups_reused"] == [reused["pgid"]] and outcome["groups_gone"] == [gone["pgid"]]
        assert outcome["ended"] == [] and bystander.poll() is None
        # The same group with its true leader birth IS this scope's and ends member by member.
        true = _group(bystander)
        outcome = ps.end_scope(groups=[true], exclude={os.getpid(), 1}, grace_sec=0.2)
        assert outcome["ended"] == [bystander.pid] and _gone(bystander.pid)
    finally:
        _end(bystander)


def test_a_member_whose_identity_changed_is_not_signalled(monkeypatch):
    child = _sleeper()
    try:
        group = _group(child)
        real = ps.measure_birth
        monkeypatch.setattr(ps, "measure_birth", lambda pid: "Mon Jan  1 00:00:00 2001" if pid == child.pid else real(pid))
        outcome = ps.end_scope(groups=[group], exclude={os.getpid(), 1}, grace_sec=0.2)
        assert outcome["ended"] == [] and child.poll() is None, "a re-measured birth that differs refuses the signal"
    finally:
        _end(child)


def test_an_unreadable_table_is_unknown_never_an_empty_scope(monkeypatch):
    child = _sleeper()
    try:
        monkeypatch.setattr(ps, "census", lambda markers=(): None)
        outcome = ps.end_scope(groups=[_group(child)], exclude={os.getpid(), 1})
        assert outcome["census_failed"] is True and outcome["ended"] == [] and child.poll() is None
    finally:
        _end(child)


def test_windows_reports_unsupported_coverage(monkeypatch):
    monkeypatch.setattr(_pl, "IS_WINDOWS", True)
    outcome = ps.end_scope(marker=ps.mint_marker(ps.TASK_SCOPE_PREFIX))
    assert outcome["coverage"] == ps.COVERAGE_UNSUPPORTED and outcome["ended"] == [] and not outcome["census_failed"]
    assert ps.census([]) is None


# --------------------------------------------------------------------------- task scope records

def test_task_scope_marker_is_minted_once_and_rotated_only_after_a_cleanup_started(tmp_path):
    first = ps.task_scope_marker(tmp_path, "t1")
    assert first.startswith(ps.TASK_SCOPE_PREFIX) and ps.task_scope_marker(tmp_path, "t1") == first
    assert ps.task_scope_env(tmp_path, "t1") == {first: "1"}
    assert ps.task_scope_env(tmp_path, "") == {} and ps.task_scope_env(None, "t1") == {}
    record = ps.task_scope_record(tmp_path, "t1")
    assert record["task_id"] == "t1" and record["groups"] == [] and record["minted_epoch"] > 0
    path = ps.task_scope_path(tmp_path, "t1")
    path.write_text(json.dumps({**record, "groups": [{"pgid": 7, "leader_pid": 7, "leader_birth": "x"}],
                                "cleanup": {"state": "running", "started_epoch": time.time()}}), encoding="utf-8")
    rotated = ps.task_scope_marker(tmp_path, "t1")
    assert rotated != first, "a successor's children never share an ending attempt's id"
    assert ps.task_scope_record(tmp_path, "t1")["groups"] == [{"pgid": 7, "leader_pid": 7, "leader_birth": "x"}], \
        "the groups the old record still owed are carried"
    assert ps.task_scope_records(tmp_path) == [ps.task_scope_record(tmp_path, "t1")]


def test_record_task_group_keeps_live_groups_and_prunes_emptied_ones(tmp_path):
    live, short = _sleeper(), _sleeper()
    try:
        entry = ps.record_task_group(tmp_path, "t2", short.pid)
        assert entry["pgid"] == short.pid and entry["leader_birth"] == ps.measure_birth(short.pid)
        _end(short)  # the group empties
        ps.record_task_group(tmp_path, "t2", live.pid)
        groups = ps.task_scope_record(tmp_path, "t2")["groups"]
        assert [group["pgid"] for group in groups] == [live.pid], "an emptied group is pruned on the next append"
        assert ps.record_task_group(tmp_path, "", live.pid) is None
    finally:
        _end(live, short)
