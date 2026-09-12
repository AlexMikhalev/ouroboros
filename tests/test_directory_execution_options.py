"""Caller-selected ordinary-folder geometry survives native scheduling transport."""
from __future__ import annotations

import json
import queue
from pathlib import Path
from types import SimpleNamespace

import pytest

from ouroboros.tools.control_subagent_spec import _validated_schedule_fields


@pytest.mark.parametrize("options", [
    {}, {"directory_strategy": "direct"},
    {"directory_strategy": "copy", "scope_paths": ["."]},
    {"directory_strategy": "copy", "scope_paths": ["documents", "images/cover.png"]},
])
def test_directory_options_have_one_public_parameter_surface(options):
    from ouroboros.tools.control_subagent_spec import schedule_subagent_properties, schedule_subagent_param_names

    properties = schedule_subagent_properties()
    assert properties["directory_strategy"]["enum"] == ["direct", "copy"]
    assert properties["scope_paths"]["type"] == "array"
    assert {"directory_strategy", "scope_paths"} <= schedule_subagent_param_names()
    assert "REAL external Git" not in properties["write_root"]["description"]
    fields, error = _validated_schedule_fields({"objective": "Edit", "expected_output": "Files", **options})
    assert not error
    assert {key: fields[key] for key in ("directory_strategy", "scope_paths") if key in fields} == options


@pytest.mark.parametrize("options", [
    {"directory_strategy": "invalid"}, {"directory_strategy": "copy"},
    {"directory_strategy": "copy", "scope_paths": []},
    {"scope_paths": "documents"}, {"scope_paths": [""]}, {"scope_paths": [1]},
    {"scope_paths": ["/absolute"]}, {"scope_paths": ["C:\\absolute"]},
])
def test_directory_options_refuse_an_unusable_scope_shape(options):
    fields, error = _validated_schedule_fields({"objective": "Edit", "expected_output": "Files", **options})
    assert not fields and "TOOL_ARG_ERROR" in error


def _schedule(tmp_path, monkeypatch, *, kind, options):
    from ouroboros.tools.control_scheduling import _schedule_task
    from ouroboros.tools.registry import ToolContext
    from tests._shared import configure_test_subagent

    actor = configure_test_subagent(
        monkeypatch, kind=kind,
        target="codex=gpt-5.6-sol" if kind == "agent_session" else "openai/gpt-5.6-sol",
    )
    monkeypatch.setenv("OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS", "1")
    monkeypatch.setenv("OUROBOROS_MAX_SUBAGENT_DEPTH", "3")
    repo, data, folder = (tmp_path / name for name in ("system", "data", "documents"))
    for directory in (repo, data, folder):
        directory.mkdir()
    (folder / "draft.txt").write_text("owner draft\n", encoding="utf-8")
    event_queue = queue.Queue()
    ctx = ToolContext(repo_dir=repo, drive_root=data, task_id="parent",
                      workspace_root=folder, workspace_mode="external")
    ctx.event_queue = event_queue
    ctx.task_metadata = {"root_task_id": "parent", "budget_drive_root": str(data)}
    response = _schedule_task(
        ctx, subagent_id=actor, objective="Revise draft.txt", expected_output="Updated draft",
        memory_mode="empty", write_surface="external_workspace", write_root=str(folder), **options,
    )
    return ctx, event_queue, response


def test_native_copy_refusal_precedes_child_side_effects(tmp_path, monkeypatch):
    ctx, events, response = _schedule(
        tmp_path, monkeypatch, kind="api_model",
        options={"directory_strategy": "copy", "scope_paths": ["."]},
    )
    assert "TOOL_ARG_ERROR" in response and "directory_strategy=copy is unsupported" in response
    assert events.empty()
    assert not list((ctx.drive_root / "task_results").glob("*.json"))
    assert not (ctx.workspace_root / ".git").exists()


@pytest.mark.parametrize("kind,options", [
    ("api_model", {}), ("api_model", {"directory_strategy": "direct"}),
    ("agent_session", {}), ("agent_session", {"directory_strategy": "direct"}),
    ("agent_session", {"directory_strategy": "copy", "scope_paths": ["draft.txt"]}),
    ("agent_session", {"directory_strategy": "copy", "scope_paths": ["."]}),
])
def test_directory_options_survive_schedule_result_dispatch_and_bootstrap(
    tmp_path, monkeypatch, kind, options,
):
    from ouroboros import subagent_runtime
    from ouroboros.subagent_bootstrap import bootstrap_before_context
    from ouroboros.task_results import load_task_result
    from supervisor import events
    from tests.test_nested_rights_depth import _fake_ctx

    parent, event_queue, response = _schedule(tmp_path, monkeypatch, kind=kind, options=options)
    assert not event_queue.empty(), response
    event = event_queue.get_nowait()
    tid = event["task_id"]
    keys = ("directory_strategy", "scope_paths")
    selected = lambda row: {key: row[key] for key in keys if key in row}
    assert selected(event) == options
    assert selected(load_task_result(parent.drive_root, tid)) == options
    enqueued = []
    supervisor = _fake_ctx(parent.drive_root, enqueued)
    supervisor.REPO_DIR = parent.repo_dir
    events._handle_schedule_task(event, supervisor)
    assert len(enqueued) == 1
    task = enqueued[0]
    assert selected(task) == selected(task["metadata"]) == options
    result = load_task_result(parent.drive_root, tid)
    assert result["status"] == "scheduled" and selected(result) == options
    assert task["workspace_root"] == str(parent.workspace_root)
    assert task["task_constraint"]["write_root"] == str(parent.workspace_root)
    assert not (parent.workspace_root / ".git").exists()

    starts = []
    def exact_start(ctx, prompt, spec):
        starts.append((ctx, prompt, spec))
        return json.dumps({"status": "started", "run_id": "directory-run"})
    monkeypatch.setattr(subagent_runtime, "exact_start", exact_start)
    child = SimpleNamespace(
        task_id=tid, drive_root=Path(task["drive_root"]), budget_drive_root=str(parent.drive_root),
        task_metadata=task["metadata"], workspace_root=Path(task["workspace_root"]),
        workspace_mode=task["workspace_mode"],
    )
    wake = bootstrap_before_context(child, task, SimpleNamespace(blocked=False))
    if kind == "api_model":
        assert wake == "" and starts == []
    else:
        assert json.loads(wake)["status"] == "configured_session_started"
        assert len(starts) == 1 and selected(starts[0][2]) == options
        assert selected(child._configured_actor_bootstrap) == options
        assert starts[0][0].workspace_root == parent.workspace_root
        assert starts[0][1] == child._configured_actor_bootstrap["canonical_work_order"]
        if "scope_paths" in task:
            task["scope_paths"].append("later-change")
            assert child._configured_actor_bootstrap["scope_paths"] == options["scope_paths"]
