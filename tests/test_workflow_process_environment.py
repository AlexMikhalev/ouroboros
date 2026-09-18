"""The real foreground consumers share one selected environment and its egress."""
from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

from tests.test_process_environment import process_context as _process_context

process_context = _process_context

pytestmark = pytest.mark.serial


@pytest.mark.parametrize("backend", ["host", "local"])
@pytest.mark.parametrize("tool", ["run_command", "run_script", "verify_and_record"])
def test_registered_foreground_env_reaches_child_and_redacts_egress(process_context, monkeypatch, backend, tool):
    from ouroboros import workspace_executor
    from ouroboros.outcomes import verification_receipts_path
    from ouroboros.loop_tool_execution import _execute_single_tool

    registry, ctx, workspace, data = process_context
    if backend == "local":
        ctx.executor_ref = {"type": "local", "workspace_host_path": str(workspace), "workspace_backend_path": "/workspace"}
    secret = 'private-test-"quoted"\nsecond-line'
    expected_hash = hashlib.sha256(secret.encode()).hexdigest()
    monkeypatch.setattr("ouroboros.config.load_settings", lambda: {"CUSTOM_KEY": secret, "OPENAI_BASE_URL": "ordinary-8080"})
    calls = []
    resolve = workspace_executor.resolve_process_env

    def observe(*args, **kwargs):
        calls.append(1)
        return resolve(*args, **kwargs)

    monkeypatch.setattr(workspace_executor, "resolve_process_env", observe)
    program = "import os,hashlib; print(os.environ['TOKEN']); print(os.environ['PORT']); print(hashlib.sha256(os.environ['TOKEN'].encode()).hexdigest())"
    args = {"cwd": str(workspace), "env_from_settings": {"TOKEN": "CUSTOM_KEY", "PORT": "OPENAI_BASE_URL"}}
    if tool == "run_script":
        args.update(script=program, interpreter=sys.executable)
    elif tool == "verify_and_record":
        args.update(contract_kind="explicit_command", check=[sys.executable, "-c", program], expected=expected_hash)
    else:
        args.update(cmd=[sys.executable, "-c", program])
    logs = data / "logs"
    logs.mkdir(exist_ok=True)
    _execute_single_tool(registry, {"id": "env-proof", "function": {"name": tool, "arguments": json.dumps(args)}}, logs, task_id=ctx.task_id)
    records = (logs / "tools.jsonl").read_text(encoding="utf-8")
    assert expected_hash in records and "ordinary-8080" in records
    assert secret not in records and "private-test-" not in records
    assert calls == [1], "one frozen Settings selection per public invocation"
    if tool == "verify_and_record":
        receipt = json.loads(verification_receipts_path(data, ctx.task_id).read_text(encoding="utf-8").splitlines()[-1])
        assert receipt["status"] == "pass" and receipt["returncode"] == 0
        assert expected_hash in receipt["summary"]
        assert "private-test-" not in json.dumps(receipt)
        assert receipt["runtime_provenance"]["selected_path"] == sys.executable


def test_verify_matches_raw_selected_value_before_receipt_redaction(process_context, monkeypatch):
    from ouroboros.outcomes import verification_receipts_path

    registry, ctx, workspace, data = process_context
    secret = "!"
    monkeypatch.setattr("ouroboros.config.load_settings", lambda: {"CUSTOM_KEY": secret})
    result = registry.execute_result("verify_and_record", {"contract_kind": "explicit_command",
        "check": [sys.executable, "-c", "import os; print(os.environ['TOKEN'])"],
        "cwd": str(workspace), "expected": secret, "env_from_settings": {"TOKEN": "CUSTOM_KEY"}})
    receipt = json.loads(verification_receipts_path(data, ctx.task_id).read_text(encoding="utf-8").splitlines()[-1])
    assert receipt["status"] == "pass" and receipt["matched"] is True
    assert receipt["expected"] == "***" and secret not in result.text


@pytest.mark.parametrize("failure", ["nonzero", "spawn", "timeout"])
def test_selected_secret_does_not_escape_failed_process(process_context, monkeypatch, failure):
    registry, _ctx, workspace, _data = process_context
    secret = "synthetic-failed-process-secret"
    monkeypatch.setattr("ouroboros.config.load_settings", lambda: {"CUSTOM_KEY": secret})
    if failure == "spawn":
        cmd = [str(workspace / "absent")]
    else:
        cmd = [sys.executable, "-c", "import os,sys,time; print(os.environ['TOKEN'],flush=True); " + ("sys.exit(7)" if failure == "nonzero" else "time.sleep(5)")]
    result = registry.execute_result("run_command", {"cmd": cmd, "cwd": str(workspace),
        "timeout_sec": 1, "env_from_settings": {"TOKEN": "CUSTOM_KEY"}})
    assert secret not in result.text
    assert result.status != "ok"
    if failure == "nonzero":
        assert result.meta["exit_code"] == 7 and "***" in result.text


def test_reference_authority_precedes_settings_read_and_spawn(process_context, monkeypatch):
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tools.shell import _run_shell

    _registry, ctx, workspace, _data = process_context
    ctx.task_constraint = TaskConstraint(mode="acting_subagent", surface="external_workspace", write_root=str(workspace))
    monkeypatch.setattr("ouroboros.config.runtime_settings", lambda **kwargs: pytest.fail("read forbidden Settings"))
    result = _run_shell(ctx, [sys.executable, "-c", "raise SystemExit('must not run')"],
        cwd=str(workspace), env_from_settings={"TOKEN": "CUSTOM_KEY"})
    assert "PROCESS_ENV_REFERENCE_BLOCKED" in result


def test_non_run_verify_refuses_environment_instead_of_ignoring_it(process_context):
    registry, _ctx, _workspace, _data = process_context
    result = registry.execute_result("verify_and_record", {"contract_kind": "artifact_observation",
        "env_from_settings": {"TOKEN": "CUSTOM_KEY"}, "artifact_paths": []})
    assert result.code == "TOOL_ARG_ERROR" and "only to run-kind" in result.text


@pytest.mark.skipif(os.name == "nt", reason="POSIX shim fixture; Windows environment merging has separate coverage")
@pytest.mark.parametrize("runtime", ["python", "node"])
def test_runtime_uses_explicit_child_path_not_host_path(process_context, monkeypatch, runtime):
    registry, _ctx, workspace, _data = process_context
    binary_dir = workspace / "chosen-bin"
    binary_dir.mkdir()
    binary = binary_dir / runtime
    if runtime == "python":
        binary.symlink_to(sys.executable)
        command = [runtime, "-c", "print('chosen-runtime')"]
    else:
        binary.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo v22.1.0; else echo chosen-runtime; fi\n", encoding="utf-8")
        binary.chmod(0o755)
        command = [runtime, "-e", "unused"]
    monkeypatch.setattr("ouroboros.config.load_settings", lambda: {"OPENAI_BASE_URL": str(binary_dir)})
    result = registry.execute_result("run_command", {"cmd": command, "cwd": str(workspace),
        "env_from_settings": {"PATH": "OPENAI_BASE_URL"}})
    assert result.status == "ok" and "chosen-runtime" in result.text, result.text
    if runtime == "python":
        assert str(binary) in result.text and '"source": "PATH"' in result.text


def test_unknown_wrapper_provenance_never_invents_a_python_path(process_context):
    registry, _ctx, workspace, _data = process_context
    if os.name == "nt":
        pytest.skip("POSIX shell fixture")
    result = registry.execute_result("run_command", {"cmd": ["sh", "-c", "printf ready"], "cwd": str(workspace)})
    assert result.status == "ok" and "runtime inside wrapper not inspected" in result.text
    assert '"selected_path": null' in result.text


def test_docker_target_env_uses_only_inert_host_aliases(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from ouroboros import workspace_executor as executor

    seen = {}
    class Process:
        pid, returncode = 123, 0
        def __init__(self, command, **kwargs):
            seen.update(command=command, kwargs=kwargs)
        def communicate(self, **kwargs):
            return "done", ""
    monkeypatch.setattr(executor.subprocess, "Popen", Process)
    monkeypatch.setattr(executor, "_register_process", lambda *a: None)
    monkeypatch.setattr(executor, "_forget_process", lambda *a: None)
    ref = SimpleNamespace(network="default", container_name="isolated-fixture", kind="docker_exec", executor_id="fixture")
    target = {"DOCKER_HOST": "target-only", "PATH": "/target/bin", "TOKEN": "fake-$('literal')\nsecret"}
    result = executor._execute_docker(ref, ["python3", "-c", "print('done')"], "/workspace", 5, drive_root=tmp_path, target_env=target)
    assert result.returncode == 0
    host_env = seen["kwargs"]["env"]
    assert host_env.get("DOCKER_HOST") == os.environ.get("DOCKER_HOST")
    assert host_env["PATH"] == os.environ["PATH"]
    assert all(value not in " ".join(seen["command"]) for value in target.values())
    aliases = [seen["command"][i + 1] for i, value in enumerate(seen["command"]) if value == "--env"]
    assert len(aliases) == 3 and {host_env[key] for key in aliases} == set(target.values())
