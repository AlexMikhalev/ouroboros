"""Panic ends every live member of the installation scope by identity (owner 4A, #1772).

Today's owner requests come first and unchanged; the census is one more bounded request
attempt that never delays the hard exit, works with the daemon already dead, leaves a
durable pending record for whatever it could not finish, and never signals the server,
its launcher, pid 1 or another installation's members. Real children belong to this test
process and are ended by exact pid.
"""
from __future__ import annotations

import copy
import logging
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from ouroboros import platform_layer as _pl
from ouroboros import process_scope as ps

pytestmark = [pytest.mark.serial, pytest.mark.skipif(os.name == "nt", reason="the environment census is POSIX-only")]

REPO = pathlib.Path(__file__).resolve().parents[1]


def _sleeper(env=None, *, argv=None) -> subprocess.Popen:
    return subprocess.Popen(argv or [sys.executable, "-c", "import time; time.sleep(120)"],
                            env={**os.environ, **(env or {})}, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)


def _end(*procs) -> None:
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


def test_the_census_ends_marker_members_and_recorded_task_groups_with_no_daemon_anywhere(tmp_path):
    marker = ps.installation_marker(tmp_path)
    member = _sleeper({marker: "1"})  # a worker/command/service of this installation (env visible)
    platform_child = _sleeper(argv=["/bin/sleep", "120"])  # a task command's platform-binary leftover
    ps.record_task_group(tmp_path, "t-ended", platform_child.pid)
    foreign = _sleeper({ps.mint_marker(ps.INSTALL_SCOPE_PREFIX): "1"})  # another installation
    try:
        outcome = ps.panic_scope_census(tmp_path, exclude={os.getpid(), 1})
        assert outcome["requested"] is True and not outcome["census_failed"]
        assert set(outcome["ended"]) == {member.pid, platform_child.pid}
        assert outcome["survivors"] == [] and outcome["recorded_groups"] == 1
        assert outcome["residual"] == ps.PANIC_CENSUS_RESIDUAL  # the engine's harness shells are disclosed
        assert _gone(member.pid) and _gone(platform_child.pid)
        assert foreign.poll() is None, "another installation's member is never signalled"
        assert not ps.panic_sweep_pending_path(tmp_path).exists(), "a finished census owes nothing"
    finally:
        _end(member, platform_child, foreign)


def test_without_a_planted_marker_the_census_requests_nothing(tmp_path):
    assert ps.panic_scope_census(tmp_path) == {"requested": False, "reason": "no installation marker planted"}
    assert ps.complete_pending_panic_sweep(tmp_path) is None


def test_the_census_excludes_the_server_its_launcher_and_pid_one(tmp_path, monkeypatch):
    marker = ps.installation_marker(tmp_path)
    rows = {
        1: ps._Row(1, 0, 1, "Sat Oct 10 20:10:23 2026", 1.0, {marker}),
        4000: ps._Row(4000, 1, 4000, "Sat Oct 10 20:10:23 2026", 1.0, {marker}),   # the launcher
        4001: ps._Row(4001, 4000, 4001, "Sat Oct 10 20:10:23 2026", 1.0, {marker}),  # the server
        4002: ps._Row(4002, 4001, 4002, "Sat Oct 10 20:10:23 2026", 1.0, {marker}),  # a worker
    }
    signalled = []
    monkeypatch.setattr(ps, "census", lambda markers=(): dict(rows))
    monkeypatch.setattr(ps, "measure_birth", lambda pid: "Sat Oct 10 20:10:23 2026" if pid in rows else "")
    monkeypatch.setattr(ps.os, "kill", lambda pid, sig: signalled.append((pid, sig)))
    monkeypatch.setattr(ps, "_await_exit", lambda targets, deadline: {})
    outcome = ps.panic_scope_census(tmp_path, exclude={4001, 4000, 1})
    assert [pid for pid, _sig in signalled] == [4002]
    assert outcome["ended"] == [4002]


def test_a_stalled_census_leaves_a_durable_pending_record_that_the_next_start_completes(tmp_path, monkeypatch):
    marker = ps.installation_marker(tmp_path)
    member = _sleeper({marker: "1"})
    release = threading.Event()
    real_census = ps.census

    def stalled(markers=()):
        release.wait(10)
        return None  # the table never came back: unknown, never clean

    monkeypatch.setattr(ps, "census", stalled)
    results = []
    thread = threading.Thread(target=lambda: results.append(ps.panic_scope_census(tmp_path, exclude={os.getpid(), 1})),
                              daemon=True)
    try:
        thread.start()
        deadline = time.monotonic() + 5
        while not ps.panic_sweep_pending_path(tmp_path).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ps.panic_sweep_pending_path(tmp_path).exists(), "the record is written BEFORE the census"
        assert thread.is_alive() and member.poll() is None
        release.set()
        thread.join(timeout=5)
        assert results and results[0]["census_failed"] is True
        assert ps.panic_sweep_pending_path(tmp_path).exists(), "an unfinished census keeps its obligation"
        monkeypatch.setattr(ps, "census", real_census)
        completed = ps.complete_pending_panic_sweep(tmp_path, exclude={os.getpid(), 1})
        assert completed is not None and completed["ended"] == [member.pid] and _gone(member.pid)
        assert not ps.panic_sweep_pending_path(tmp_path).exists()
        assert ps.complete_pending_panic_sweep(tmp_path) is None
    finally:
        release.set()
        _end(member)


class _ExitCalled(RuntimeError):
    pass


def _panic(monkeypatch, tmp_path, *, census):
    """Neutralize the other owners; keep the census as the one real request under test."""
    from ouroboros import server_control
    from ouroboros.startup_historical_audit import audit

    monkeypatch.setattr(audit, "stop", lambda: None)
    monkeypatch.setattr("ouroboros.tools.shell.kill_all_tracked_subprocesses", lambda **kw: [])
    monkeypatch.setattr("ouroboros.workspace_executor.kill_all_foreground", lambda *a, **kw: [])
    monkeypatch.setattr("ouroboros.tools.services.kill_all_services", lambda *a, **kw: [])
    monkeypatch.setattr("ouroboros.extension_companion.panic_kill_all", lambda **kw: [])
    monkeypatch.setattr("ouroboros.local_model.get_manager", lambda **kw: None)
    monkeypatch.setattr("ouroboros.claudexor_daemon.get_owned_daemon",
                        lambda **kw: None if kw.get("create") is False else SimpleNamespace(stop_outcome=lambda: "nothing_to_stop"))
    monkeypatch.setattr(server_control, "_persist_panic_controls", lambda _root: None)
    monkeypatch.setattr("multiprocessing.active_children", lambda: [])
    monkeypatch.setattr(ps, "panic_scope_census", census)
    monkeypatch.setattr(server_control.os, "_exit", lambda code: (_ for _ in ()).throw(_ExitCalled(code)))
    diagnostics = []

    def report(message, *args):
        diagnostics.append({"requests": copy.deepcopy(args[0]), "settlements": copy.deepcopy(args[1])})
        logging.getLogger(__name__).critical(message, *args)

    started = time.monotonic()
    with pytest.raises(_ExitCalled) as exit_info:
        server_control.execute_panic_stop(consciousness=None, kill_workers_fn=lambda **kw: None,
                                          data_dir=tmp_path, panic_exit_code=120, log=SimpleNamespace(critical=report))
    assert exit_info.value.args == (120,)
    return diagnostics[0], time.monotonic() - started


def test_panic_runs_the_census_as_one_more_request_after_the_owner_requests(tmp_path, monkeypatch):
    calls = []

    def census(data_dir, **kw):
        calls.append(pathlib.Path(data_dir))
        return {"requested": True, "ended": [7], "survivors": [], "census_failed": False}

    diagnostics, _elapsed = _panic(monkeypatch, tmp_path, census=census)
    assert calls == [tmp_path]
    assert diagnostics["requests"]["scope-census"]["ended"] == [7]
    assert diagnostics["requests"]["daemon"] == {"requested": False, "error": "no captured daemon identity"}
    assert "scope-census" not in diagnostics["settlements"], "a request attempt, never a settlement to wait on"


def test_a_census_that_stalls_never_delays_the_panic_exit(tmp_path, monkeypatch):
    release = threading.Event()

    def census(data_dir, **kw):
        release.wait(30)
        return {"requested": True}

    try:
        diagnostics, elapsed = _panic(monkeypatch, tmp_path, census=census)
        assert diagnostics["requests"]["scope-census"] == "unfinished"
        assert elapsed < 5.0, "the request join is the existing quarter second; nothing waits on the census"
    finally:
        release.set()
        for thread in threading.enumerate():
            if thread.name == "panic-scope-census":
                thread.join(timeout=5)


def test_a_failing_census_is_disclosed_and_the_other_requests_still_run(tmp_path, monkeypatch):
    def census(data_dir, **kw):
        raise RuntimeError("table unreadable")

    diagnostics, _elapsed = _panic(monkeypatch, tmp_path, census=census)
    assert diagnostics["requests"]["scope-census"] == {"requested": False, "error": "RuntimeError: table unreadable"}
    assert "commands" in diagnostics["requests"] and "daemon" in diagnostics["settlements"]


def test_the_launcher_panic_exit_and_the_next_start_complete_a_pending_sweep_before_any_child():
    launcher = (REPO / "launcher.py").read_text(encoding="utf-8")
    panic_branch = launcher[launcher.index("if exit_code == PANIC_EXIT_CODE:"):]
    assert (panic_branch.index("complete_pending_panic_sweep(DATA_DIR, exclude={os.getpid(), 1})")
            < panic_branch.index('_kill_orphaned_children(reason="panic_stop")'))
    server = (REPO / "server.py").read_text(encoding="utf-8")
    main = server[server.index("def main() -> int:"):]
    assert (main.index("admit_data_root(DATA_DIR)") < main.index("complete_pending_panic_sweep(DATA_DIR)")
            < main.index("plant_installation_scope(DATA_DIR)") < main.index("automatic_launch_allowed("))
    # Ordinary doors never sweep the installation scope: close, quit, planned and manual Restart.
    for path in ("ouroboros/server_restart.py", "ouroboros/owned_shutdown.py"):
        text = (REPO / path).read_text(encoding="utf-8")
        assert "panic_scope_census" not in text and "complete_pending_panic_sweep" not in text


def test_the_daemon_spawn_env_keeps_the_installation_marker_and_sheds_task_and_run_markers(monkeypatch, tmp_path):
    from ouroboros import claudexor_daemon as owned
    from ouroboros.gateways.claudexor import ClaudexorUnavailable
    from tests.test_claudexor_owned_daemon import _UnpublishedChild, _point_owned_home

    data_dir = tmp_path / "data"
    config_dir = data_dir / "claudexor"
    _point_owned_home(monkeypatch, config_dir, data_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OUROBOROS_CLAUDEXOR_BIN", sys.executable)
    monkeypatch.setattr(owned, "_SPAWN_WAIT_SEC", 0.05)
    monkeypatch.setattr(owned, "_SPAWN_POLL_SEC", 0.01)
    install, task, run = (ps.mint_marker(p) for p in (ps.INSTALL_SCOPE_PREFIX, ps.TASK_SCOPE_PREFIX, ps.RUN_SCOPE_PREFIX))
    for name in (install, task, run):
        monkeypatch.setenv(name, "1")
    captured = {}

    def _capture_spawn(command, **kwargs):
        captured["env"] = kwargs["env"]
        return _UnpublishedChild(424245)

    monkeypatch.setattr(_pl, "kill_process_tree", lambda proc: proc.terminate())
    import ouroboros.process_custody as custody_mod

    monkeypatch.setattr(custody_mod, "spawn_supervised", _capture_spawn)
    with pytest.raises(ClaudexorUnavailable):
        owned.OwnedClaudexorDaemon().ensure_running()
    env = captured["env"]
    assert env.get(install) == "1", "the installation-owned daemon stays in the installation's scope"
    assert task not in env and run not in env, "inheritance is not a lifetime transfer"


def test_the_allowlisted_service_env_and_mcp_stdio_env_keep_the_installation_marker(monkeypatch):
    from ouroboros import mcp_client
    from ouroboros.tools.services import _service_env
    from ouroboros.workspace_executor import _executor_service_env, service_env

    install, task = ps.mint_marker(ps.INSTALL_SCOPE_PREFIX), ps.mint_marker(ps.TASK_SCOPE_PREFIX)
    captured = []
    monkeypatch.setattr(mcp_client, "_MCP_SDK_AVAILABLE", True)
    monkeypatch.setattr(mcp_client, "StdioServerParameters", lambda **kwargs: captured.append(kwargs) or SimpleNamespace(**kwargs))
    monkeypatch.setattr(mcp_client, "stdio_client", lambda params, **kw: SimpleNamespace(params=params))
    cfg = mcp_client.normalize_server_config({"id": "local", "transport": "stdio", "command": "python3", "args": []})

    for build in (service_env, _service_env, _executor_service_env):  # nothing planted: byte-identical allowlist
        assert not ps.scope_entries(build())
    mcp_client._transport_factory(cfg)
    assert "env" not in captured[-1], "no marker planted: the SDK default environment, as before"

    monkeypatch.setenv(install, "1")
    monkeypatch.setenv(task, "1")
    for build in (service_env, _service_env, _executor_service_env):
        env = build()
        assert env.get(install) == "1" and task not in env, "a service keeps the installation marker only"
    mcp_client._transport_factory(cfg)
    assert captured[-1]["env"] == {install: "1"}, "a stdio server rides in the installation's scope"


def test_markers_are_names_only_and_never_logged_or_persisted_with_environment_bytes(tmp_path, caplog):
    source = (REPO / "ouroboros" / "process_scope.py").read_text(encoding="utf-8")
    assert not re.search(r"log\.\w+\([^)]*(?:rest|words|entries|environ)", source), "no environment bytes in logs"
    marker = ps.installation_marker(tmp_path)
    member = _sleeper({marker: "1"})
    try:
        with caplog.at_level(logging.DEBUG):
            outcome = ps.panic_scope_census(tmp_path, exclude={os.getpid(), 1})
        assert outcome["ended"] == [member.pid]
        assert marker not in caplog.text
    finally:
        _end(member)
