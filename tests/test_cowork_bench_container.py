"""Cowork entrypoint contracts, without Docker, models, or optional benchmark packages."""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

from devtools.benchmarks.cowork_bench.container import main_ouroboros as adapter


@pytest.mark.serial
def test_probe_retains_original_startup_error_and_producer_exit(tmp_path):
    error = "ModuleNotFoundError: No module named 'psycopg2'\n" + "context\n" * 300
    row = adapter.capture_probe(
        [sys.executable, "-c", f"import sys; print('started'); sys.stderr.write({error!r}); sys.exit(7)"],
        tmp_path / "finance", timeout=10,
    )
    assert row["returncode"] == 7
    assert pathlib.Path(row["stderr_path"]).read_text(encoding="utf-8") == error
    assert pathlib.Path(row["stdout_path"]).read_text(encoding="utf-8") == "started\n"


@pytest.mark.parametrize(("code", "tools", "expected"), [(0, ["read"], 0), (1, ["read"], 1), (0, [], 1)])
def test_inventory_requires_successful_exit_and_real_tools(tmp_path, monkeypatch, code, tools, expected):
    monkeypatch.setattr(adapter, "load_yaml_configs", lambda: [{"name": "fixture", "params": {"command": "fake"}}])
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    stdout.write_text(json.dumps({"tools": tools}), encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    monkeypatch.setattr(adapter, "capture_probe", lambda *a, **kw: {
        "returncode": code, "stdout_path": str(stdout), "stderr_path": str(stderr),
    })
    assert adapter.run_inventory_phase("", tmp_path) == expected


def test_mcp_rendering_keeps_task_paths_and_database_isolation():
    config = [{"name": "db", "params": {"command": "uv", "args": ["${local_servers_paths}/db.py"],
        "env": {"PG_HOST": "wrong", "TASK": "${task_dir}", "WORKSPACE": "${agent_workspace}"}}}]
    rendered = adapter.render_mcp_servers(
        ["db"], config, local_servers="/opt/servers", workspace="/workspace/task",
        task_dir="/workspace/tasks/one", environ={"PGHOST": "this-task-pg", "PGPORT": "5433"},
    )["db"]
    assert rendered["args"] == ["/opt/servers/db.py"]
    assert rendered["env"] == {"PG_HOST": "this-task-pg", "PG_PORT": "5433",
                               "TASK": "/workspace/tasks/one", "WORKSPACE": "/workspace/task"}
    with pytest.raises(adapter.EngineFailure, match="missing"):
        adapter.render_mcp_servers(["missing"], config, local_servers="/opt", workspace="/task",
                                   task_dir="/tasks/one", environ={})


@pytest.mark.parametrize(("result", "expected", "infra"), [
    ({"status": "completed"}, "success", False),
    ({"status": "completed", "reason_code": "round_cap"}, "max_turns_reached", False),
    ({"status": "failed", "reason_code": "provider_unavailable"}, "failed", True),
    ({"status": "failed", "outcome_axes": {"execution": {"status": "infra_failed"}}}, "failed", True),
])
def test_task_outcome_keeps_runtime_rails_and_infrastructure_separate(result, expected, infra):
    outcome = adapter.classify_outcome(result, ["round_cap"])
    assert outcome["bench_status"] == expected
    assert outcome["infra_failed"] is infra


@pytest.mark.parametrize("uid", [0, 1006])
def test_chromium_root_container_can_start_without_altering_other_servers(monkeypatch, uid):
    monkeypatch.setattr(adapter.os, "geteuid", lambda: uid, raising=False)
    original_args = ["/opt/playwright/cli.js", "--headless", "--browser", "chromium"]
    configs = [{"name": name, "params": {"command": "node", "args": original_args[:]}}
               for name in ("playwright_with_chunk", "another-server")]
    rendered = adapter.render_mcp_servers(
        ["playwright_with_chunk", "another-server"], configs, local_servers="/opt",
        workspace="/task", task_dir="/tasks/one", environ={},
    )
    assert rendered["another-server"]["args"] == original_args
    assert rendered["playwright_with_chunk"]["args"] == original_args + (["--no-sandbox"] if uid == 0 else [])
    assert configs[0]["params"]["args"] == original_args
