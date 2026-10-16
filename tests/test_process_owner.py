"""Finite fault matrix for a test run's owner, guardian and overload stop.

Every fixture has a finite lifetime and an independent outer cleanup owner.
Assertions observe PID plus birth identity before that fallback cleanup runs.
"""
from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest

from ouroboros import platform_layer as pl
from ouroboros.process_containment import ProcessContainer, pid_is_zombie


def _until(predicate, *, within=15):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("owned process did not reach the expected state before the test deadline")


def _identity(pid):
    birth = pl.process_start_time(pid)
    assert birth, f"could not measure birth of test child {pid}"
    return pid, birth


def _live(identity):
    pid, birth = identity
    return pl.process_start_time(pid) == birth and not pid_is_zombie(pid)


def _read(path):
    def complete():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    return _until(complete)


def _child_code(receipt, *, allocate=False):
    return textwrap.dedent(f"""
        import json, os, signal, time
        from pathlib import Path
        from ouroboros.platform_layer import process_start_time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        Path({str(receipt)!r}).write_text(json.dumps([os.getpid(), process_start_time(os.getpid())]), encoding='utf-8')
        if {allocate!r}:
            data = bytearray(80 * 1024 * 1024)
            for i in range(0, len(data), 4096):
                data[i] = 1
        time.sleep(45)
    """)


def _parent_code(child_code):
    return textwrap.dedent(f"""
        import os, subprocess, sys, time
        subprocess.Popen([sys.executable, '-c', {child_code!r}],
                         start_new_session=(os.name != 'nt'))
        time.sleep(45)
    """)


@pytest.fixture
def owned():
    containers = []

    def spawn(code, **options):
        container = ProcessContainer(**options)
        containers.append((container, None))
        proc = container.spawn([sys.executable, "-c", code],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        containers[-1] = (container, proc)
        return container, proc

    yield spawn
    for container, proc in reversed(containers):
        try:
            container.reap()
        finally:
            container.close()
            if proc is not None:
                if proc.poll() is None:
                    pl.kill_process_tree(proc)
                proc.wait(timeout=10)


@pytest.fixture
def sentinel(owned):
    _, proc = owned("import time; time.sleep(45)")
    return _identity(proc.pid)


def test_timeout_reaps_term_ignoring_grandchild_and_spares_sentinel(tmp_path, owned, sentinel):
    receipt = tmp_path / "grandchild.json"
    container, proc = owned(_parent_code(_child_code(receipt)), lifeline=True)
    child = _read(receipt)
    root = _identity(proc.pid)
    with pytest.raises(subprocess.TimeoutExpired):
        proc.wait(timeout=0.05)

    assert container.reap() == ""
    proc.wait(timeout=5)
    _until(lambda: not _live(child) and not _live(root))
    assert _live(sentinel)


def test_abrupt_owner_death_reaps_tree_without_owner_finally(tmp_path, owned, sentinel):
    child_receipt, owner_receipt = tmp_path / "child.json", tmp_path / "owner.json"
    workload = _parent_code(_child_code(child_receipt))
    owner_code = textwrap.dedent(f"""
        import json, subprocess, sys, time
        from pathlib import Path
        from ouroboros.process_containment import ProcessContainer
        from ouroboros.platform_layer import process_start_time
        c = ProcessContainer(lifeline=True)
        p = c.spawn([sys.executable, '-c', {workload!r}],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        guardian = c._lifeline._proc.pid if c._lifeline is not None else 0
        Path({str(owner_receipt)!r}).write_text(json.dumps({{
            'root': [p.pid, process_start_time(p.pid)],
            'guardian': [guardian, process_start_time(guardian)] if guardian else None
        }}), encoding='utf-8')
        time.sleep(45)
    """)
    _, owner = owned(owner_code)
    child = _read(child_receipt)
    facts = _read(owner_receipt)
    owner.kill()  # SIGKILL / TerminateProcess; no finally, signal handler or reap.
    owner.wait(timeout=5)

    _until(lambda: not _live(child) and not _live(facts["root"]))
    if facts["guardian"]:
        _until(lambda: not _live(facts["guardian"]))
    assert _live(sentinel)


def test_normal_release_joins_guardian(tmp_path, owned, sentinel):
    container, proc = owned("pass", lifeline=True)
    guardian = container._lifeline._proc if container._lifeline else None
    proc.wait(timeout=5)
    assert container.reap() == ""
    assert container.reap() == ""
    if guardian is not None:
        assert guardian.returncode == 0
        assert guardian.stdin.closed and guardian.stdout.closed
    assert _live(sentinel)


def test_local_runner_reaps_descendant_after_successful_root(tmp_path, sentinel):
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "run_tests.py"
    spec = importlib.util.spec_from_file_location("owned_test_runner", script)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    receipt = tmp_path / "local-child.json"
    code = textwrap.dedent(f"""
        import os, subprocess, sys, time
        from pathlib import Path
        subprocess.Popen([sys.executable, '-c', {_child_code(receipt)!r}],
                         start_new_session=(os.name != 'nt'),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while not Path({str(receipt)!r}).exists():
            if time.monotonic() > deadline:
                raise RuntimeError('child did not publish its identity')
            time.sleep(0.02)
    """)
    child = None
    try:
        assert runner._run("finite owner", [sys.executable, "-c", code], runner.REPO) == 0
        child = _read(receipt)
        _until(lambda: not _live(child))
        assert _live(sentinel)
    finally:
        if child is None and receipt.exists():
            child = _read(receipt)
        if child and _live(child):
            pl.force_kill_pid(child[0])
            _until(lambda: not _live(child))


def test_watch_thread_start_failure_reaps_started_children(monkeypatch, sentinel):
    container = ProcessContainer(lifeline=True, overload=True)
    captured = []
    original = threading.Thread.start

    def exhausted(thread):
        if thread.name == "test-overload-watch":
            captured.append(_identity(container._root))
            if container._lifeline is not None:
                captured.append(_identity(container._lifeline._proc.pid))
            raise RuntimeError("can't start new thread (injected resource exhaustion)")
        return original(thread)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(threading.Thread, "start", exhausted)
            with pytest.raises(RuntimeError, match="injected resource exhaustion"):
                container.spawn([sys.executable, "-c", "import time; time.sleep(45)"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert captured
        _until(lambda: all(not _live(identity) for identity in captured))
        assert _live(sentinel)
    finally:
        container.reap()
        container.close()


def test_overload_stops_bounded_allocator_with_largest_member(tmp_path, owned, sentinel, monkeypatch):
    from ouroboros import config, test_parallelism

    # 80 MiB maximum allocation, a 32 MiB test budget. No pressure experiment
    # against the machine: only the watch's physical budget is made small.
    monkeypatch.setattr(test_parallelism, "memory_budget", lambda memory: 32 * 1024 * 1024)
    monkeypatch.setattr(config, "TEST_PROCESS_SAMPLE_SEC", 0.05)
    receipt = tmp_path / "allocator.json"
    container, proc = owned(_child_code(receipt, allocate=True), lifeline=True, overload=True)
    child = _read(receipt)
    proc.wait(timeout=15)
    diagnostic = container.reap()
    assert proc.returncode != 0
    assert "overload_stop" in diagnostic
    assert f"largest pid={child[0]}" in diagnostic
    assert "tree_rss=" in diagnostic and "limit=33554432" in diagnostic
    assert container.reap() == diagnostic, "repeated cleanup must not erase an overload stop"
    assert not _live(child)
    assert _live(sentinel)


@pytest.mark.skipif(os.name == "nt", reason="Windows attribution is the held Job, not a POSIX token")
def test_lifeline_does_not_kill_a_recycled_pid(monkeypatch, sentinel):
    from ouroboros import process_containment, process_lifeline

    with monkeypatch.context() as patch:
        patch.setattr(process_containment, "pids_with_env_marker", lambda token: [sentinel[0]])
        process_lifeline.kill_attributed_tree("OURO_PROC_CONTAINER_not_ours", sentinel[0], "old birth")
    assert _live(sentinel)


@pytest.mark.skipif(os.name == "nt", reason="Windows verification uses the held Job")
def test_guardian_reports_unreadable_member_after_root_exit(monkeypatch, capsys):
    from ouroboros import process_containment as pc, process_lifeline as lifeline

    # The old root is gone, but an environment-replaced member remains in its
    # recorded group. Group membership detects a leak; it grants no new kill.
    packet = {"token": "OURO_PROC_CONTAINER_probe", "root": 1001,
              "birth": "old", "pgid": 1001, "since": 7}
    enumerations, signals = [], []

    def census(token, pgid=0, since=0):
        enumerations.append((pgid, since))
        return [1002] if pgid == 1001 else []

    with monkeypatch.context() as patch:
        patch.setattr(lifeline, "kill_attributed_tree", lambda *args: None)
        patch.setattr(pc, "pids_with_env_marker", census)
        patch.setattr(pc, "pid_marker_state", lambda pid, token:
                      pc.MARKER_UNREADABLE if pid == 1002 else pc.MARKER_ABSENT)
        patch.setattr(pc, "pid_is_zombie", lambda pid: False)
        patch.setattr(pl, "process_group_id", lambda pid: 1001 if pid == 1002 else 0)
        patch.setattr(pl, "force_kill_pid", signals.append)
        patch.setattr(pc, "_REAP_DEADLINE_SEC", 0.2)
        patch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(packet).encode() + b"\n")))
        patch.setattr(sys, "stdout", SimpleNamespace(buffer=io.BytesIO()))
        with pytest.raises(SystemExit) as stopped:
            lifeline._guardian_main()
    assert stopped.value.code == 1
    assert enumerations and set(enumerations) == {(1001, 7)}
    assert not signals
    diagnostic = capsys.readouterr().err
    assert "PROCESS_LIFELINE_UNCONFIRMED" in diagnostic and "1002" in diagnostic


def test_node_file_child_is_reaped(tmp_path, sentinel):
    from ouroboros.preflight_node import resolve_node

    node = resolve_node()
    if not node:
        pytest.skip("Node is unavailable; native node --test child custody needs Node")
    receipt = tmp_path / "node-pid.json"
    test_file = tmp_path / "owned.test.cjs"
    test_file.write_text(
        "const fs = require('node:fs'); const test = require('node:test');\n"
        f"fs.writeFileSync({json.dumps(str(receipt))}, JSON.stringify(process.pid));\n"
        "test('finite child', async () => { await new Promise(r => setTimeout(r, 30000)); });\n",
        encoding="utf-8",
    )
    container = ProcessContainer(lifeline=True)
    proc = container.spawn([node, "--test", "--test-concurrency=1", str(test_file)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        child = _identity(_read(receipt))
        assert child[0] != proc.pid, "the case must exercise a per-file Node child"
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=0.05)
        assert container.reap() == ""
        proc.wait(timeout=5)
        _until(lambda: not _live(child))
        assert _live(sentinel)
    finally:
        container.reap()
        container.close()
        proc.wait(timeout=5)
