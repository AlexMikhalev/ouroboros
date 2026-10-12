"""Post-task reflection and backlog promotion in ``ouroboros.agent_task_pipeline``.

Split out of ``tests/test_agent_task_pipeline.py`` when that module was divided
by theme; every moved block is verbatim. Covers `_run_reflection` entry
generation, `_update_improvement_backlog`, the project-scoped channel
split (project memory stays project-local while backlog promotion goes to the
global drive through `_run_global_backlog_promotion_only`) and the reflection
stage's nested paid Pattern Register write under the post-task stage protocol,
including a register reply cut at the output ceiling (#1605).
"""

import json
from types import SimpleNamespace

import pytest

import ouroboros.agent_task_pipeline as pipeline
from ouroboros import model_wait
from ouroboros.task_results import load_task_result
from tests.test_post_task_model_wait import _generic_unknown, phase as phase


def test_project_scoped_post_task_processing_feeds_global_backlog_but_project_memory(tmp_path, monkeypatch):
    import ouroboros.post_task_evolution as post_task_evolution

    calls = []
    reflection = {"backlog_candidates": [{"summary": "tool friction"}], "memory_actions": [{"kind": "note"}]}
    monkeypatch.setattr(pipeline, "_record_task_facts", lambda *args, **kwargs: calls.append(("facts",)))
    monkeypatch.setattr(pipeline, "_run_reflection", lambda *args, **kwargs: reflection)
    monkeypatch.setattr(pipeline, "_update_improvement_backlog", lambda _env, entry: calls.append(("backlog", entry)) or 1)
    monkeypatch.setattr(
        pipeline,
        "_apply_reflection_memory_actions",
        lambda _env, entry, project_id="": calls.append(("memory", project_id, entry)) or 1,
    )
    monkeypatch.setattr(post_task_evolution, "maybe_promote", lambda _env, task, entry, _llm: calls.append(("promote", task.get("project_id"), entry)))
    env = SimpleNamespace(repo_dir=tmp_path, drive_root=tmp_path, drive_path=lambda rel: tmp_path / rel)

    pipeline._run_post_task_processing_async(
        env,
        {"id": "task-1", "type": "task", "project_id": "proj-1", "text": "fix workspace"},
        {"rounds": 3, "cost": 0.1},
        {"tool_calls": [], "reasoning_notes": []},
        {},
        tmp_path / "logs",
        blocking=True,
    )

    assert ("backlog", reflection) in calls
    assert ("memory", "proj-1", reflection) in calls
    assert ("promote", "proj-1", reflection) in calls


def test_project_global_promotion_uses_real_maybe_promote_without_project_scope(tmp_path, monkeypatch):
    import ouroboros.post_task_evolution as post_task_evolution

    monkeypatch.setattr("ouroboros.config.get_post_task_evolution_enabled", lambda: True)
    monkeypatch.setattr("ouroboros.config.get_runtime_mode", lambda: "pro")
    monkeypatch.setattr("ouroboros.config.get_post_task_evolution_cadence", lambda: "every_n:1")
    monkeypatch.setattr(
        post_task_evolution,
        "_decide_promotion",
        lambda *_args, **_kwargs: {
            "promote": True,
            "objective": "Improve Ouroboros workspace tool feedback",
            "requires_plan_review": True,
            "backlog_id": "",
        },
    )
    env = SimpleNamespace(drive_root=tmp_path, drive_path=lambda rel: tmp_path / rel)
    reflection = {
        "reflection": "Project-specific detail should not be forwarded.",
        "memory_actions": [{"kind": "note"}],
        "backlog_candidates": [{"summary": "Improve Ouroboros workspace tool feedback"}],
    }

    pipeline._run_global_backlog_promotion_only(
        env,
        {
            "id": "task-project",
            "project_id": "proj-1",
            "workspace_root": "/tmp/project",
            "workspace_mode": "external",
            "metadata": {"workspace_preflight": {"git": {"head": "abc"}}},
        },
        reflection,
        object(),
    )

    req = json.loads((tmp_path / "state" / "post_task_evolution_request.json").read_text(encoding="utf-8"))
    assert req["objective"] == "Improve Ouroboros workspace tool feedback"
    backlog = (tmp_path / "memory" / "knowledge" / "improvement-backlog.md").read_text(encoding="utf-8")
    assert "Project-specific detail" not in backlog


def test_update_improvement_backlog_appends_candidates(tmp_path):
    env = SimpleNamespace(drive_root=tmp_path)

    added = pipeline._update_improvement_backlog(
        env,
        {
            "backlog_candidates": [{
                "summary": "Reduce recurring task friction around REVIEW_BLOCKED",
                "category": "process",
                "source": "execution_reflection",
                "task_id": "task-backlog",
                "evidence": "REVIEW_BLOCKED",
                "context": "The task retried blocked review loops without narrowing scope.",
                "proposed_next_step": "Run plan_task before touching review prompts again.",
            }],
        },
    )

    assert added == 1
    backlog_path = tmp_path / "memory" / "knowledge" / "improvement-backlog.md"
    assert backlog_path.exists()
    text = backlog_path.read_text(encoding="utf-8")
    assert "Reduce recurring task friction around REVIEW_BLOCKED" in text


def test_run_reflection_returns_entry_when_generated(tmp_path, monkeypatch):
    captured = {}
    # Entry generation/persistence is the subject; the Pattern Register has a
    # separate model call and must not consume a real provider in this test.
    monkeypatch.setattr(
        "ouroboros.reflection._update_patterns",
        lambda root, entry: captured.update(pattern_root=root, pattern_entry=entry),
    )

    class FakeLlm:
        def chat(self, *, messages, model, reasoning_effort, max_tokens, model_role, **kwargs):
            captured["prompt"] = messages[0]["content"]
            captured["model_role"] = model_role
            assert {tool["function"]["name"] for tool in kwargs["tools"]} == {"knowledge_read", "knowledge_list", "compact_context", "read_file"}
            return {
                "content": (
                    "Reflection text.\n"
                    "BACKLOG_CANDIDATES_JSON: "
                    "[{\"summary\":\"Reduce recurring task friction around REVIEW_BLOCKED\","
                    "\"category\":\"process\","
                    "\"source\":\"execution_reflection\","
                    "\"evidence\":\"REVIEW_BLOCKED\"}]"
                )
            }, {"cost": 0}

    env = SimpleNamespace(drive_root=tmp_path)
    (tmp_path / "logs").mkdir(parents=True)

    entry = pipeline._run_reflection(
        env,
        FakeLlm(),
        {"id": "task-reflect", "type": "task", "text": "Fix it", "drive_root": str(tmp_path)},
        {"rounds": 2, "cost": 0.01},
        {"tool_calls": [{"tool": "commit_reviewed", "is_error": True, "status": "error",
                         "tool_result_code": "REVIEW_BLOCKED", "result": "⚠️ REVIEW_BLOCKED"}]},
        {"recent_attempts": [], "open_obligations": [{"item": "tests_affected", "reason": "Fix the failing test before commit"}]},
    )

    assert entry is not None
    assert captured["model_role"] == "light"
    assert entry["task_id"] == "task-reflect"
    assert entry["reflection"] == "Reflection text."
    assert len(entry["backlog_candidates"]) == 1
    assert entry["backlog_candidates"][0]["summary"] == "Reduce recurring task friction around REVIEW_BLOCKED"
    stored = [json.loads(line) for line in
              (tmp_path / "logs" / "task_reflections.jsonl").read_text(encoding="utf-8").splitlines()]
    assert stored == [entry]
    assert captured["pattern_root"] == tmp_path and captured["pattern_entry"] == entry


@pytest.mark.parametrize("scope", ["global", "project"])
@pytest.mark.parametrize("outcome", ["reflection_unknown", "pattern_unknown", "pattern_budget",
                                     "pattern_deadline", "pattern_ordinary", "pattern_ok"])
def test_nested_pattern_register_follows_the_post_task_stage_protocol(phase, monkeypatch, tmp_path, scope, outcome):
    """F-R1: the reflection stage's nested paid Pattern Register write ran even when the
    reflection's own call had already recorded an unknown outcome, and both writers
    (global ``append_reflection``, project branch of ``append_reflection_routed``)
    swallowed a budget refusal, an unresolved attempt, a control and an ordinary
    failure alike, so the promotion stage bought its paid calls and the checkpoint
    read ``completed``. Through the REAL reflection, routing and register adapters
    (only the provider dispatch is substituted): an interrupted reflection buys no
    register call; a register interruption stops promotion after the reflection is
    persisted and its free actions are applied once; an ordinary register failure
    degrades while promotion still runs; a register success completes."""
    from ouroboros import consolidator, context_fit, llm_observability, post_task_synthesis, project_facts
    from ouroboros.capability_evidence import CapabilityEvidence
    from ouroboros.usage_accounting import BudgetExceeded

    f = phase
    monkeypatch.setattr(consolidator, "_consolidation_route", lambda: ("test/model", False))
    monkeypatch.setattr(context_fit, "resolve_context_fit_route", lambda task, *, allow_fetch: (
        {"model": task["model"], "provider": "openrouter"},
        CapabilityEvidence(100_000, "confirmed", "test", "route-test", model=task["model"], provider="openrouter")))
    monkeypatch.setattr(context_fit, "_route_calibration_ratio", lambda *_: 1.0)
    monkeypatch.setattr(pipeline, "_run_reflection", post_task_synthesis._run_reflection)
    if scope == "project":
        monkeypatch.setattr(project_facts, "_project_store_root", lambda pid: tmp_path / "projects" / pid)
        f.task["project_id"] = "slime"
    failures = {"reflection_unknown": _generic_unknown("direct"), "pattern_unknown": _generic_unknown("cause"),
                "pattern_budget": BudgetExceeded("root wallet spent"),
                "pattern_deadline": model_wait.ModelWaitInterrupted("deadline"),
                "pattern_ordinary": RuntimeError("pattern provider failed")}

    def dispatch(*_args, call_type="", **_kwargs):
        f.stages.append(call_type)
        failing = "task_reflection" if outcome == "reflection_unknown" else "pattern_register_update"
        if call_type == failing and outcome in failures:
            raise failures[outcome]
        content = ("| Error class | Count | Root cause | Structural fix | Status |\n|---|---|---|---|---|\n"
                   "| run_command | 1 | boom | typed | open |") if call_type == "pattern_register_update" else "Lesson: typed."
        return {"content": content}, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0}

    monkeypatch.setattr(llm_observability, "chat_observed", dispatch)
    applied, entries = [], []
    monkeypatch.setattr(pipeline, "_apply_reflection_memory_actions", lambda *a, **k: applied.append(1))
    trace = {"tool_calls": [{"tool": "run_command", "status": "error", "is_error": True, "result": "boom"}]}
    pipeline._run_post_task_processing_async(
        f.env, f.task, {"rounds": 3}, trace, {}, f.root / "logs", event_queue=f.events,
        on_reflection=lambda entry, _llm: entries.append(entry))
    assert f.done.wait(5)
    checkpoint = load_task_result(f.root, f.task["id"])["root_phase_checkpoint"]
    reflected = ["facts", "scratch", "task_reflection"]
    stop = {"reflection_unknown": "provider_outcome_unknown", "pattern_unknown": "provider_outcome_unknown",
            "pattern_budget": "budget_exhausted", "pattern_deadline": "deadline"}.get(outcome)
    if stop:
        assert checkpoint["post_task_synthesis"] == "degraded"
        assert checkpoint["post_task_stop_reason"] == f"{stop}:skipped=promotion"
        assert f.stages == reflected + ([] if outcome == "reflection_unknown" else ["pattern_register_update"])
        assert entries == [], "no paid promotion after an interruption"
    else:
        assert checkpoint["post_task_synthesis"] == ("degraded" if outcome == "pattern_ordinary" else "completed")
        assert not checkpoint.get("post_task_stop_reason")
        assert f.stages == reflected + ["pattern_register_update", "backlog"]
        [entry] = entries
        kinds = [row["kind"] for row in entry.get("memory_operation_errors") or []]
        assert kinds == (["pattern_register_failed"] if outcome == "pattern_ordinary" else [])
    assert applied == [1], "the completed reflection's free actions are applied exactly once"
    canonical = [json.loads(line) for line in (f.root / "logs" / "task_reflections.jsonl").read_text(
        encoding="utf-8").splitlines()]
    if scope == "project":
        assert [row["type"] for row in canonical] == ["project_reflection_pointer"]
        persisted = (tmp_path / "projects" / "slime" / "logs" / "task_reflections.jsonl").read_text(encoding="utf-8")
        assert len(persisted.splitlines()) == 1
    else:
        assert len(canonical) == 1 and canonical[0]["task_id"] == f.task["id"]
    patterns = f.root / "memory" / "knowledge" / "patterns.md"
    assert patterns.exists() == (outcome == "pattern_ok")


_REGISTER = ("# Pattern Register\n\n| Error class | Count | Root cause | Structural fix | Status |\n"
             "|---|---|---|---|---|\n| old_class | 3 | earlier | kept | open |\n")
_REGISTER_REPLY = ("| Error class | Count | Root cause | Structural fix | Status |\n|---|---|---|---|---|\n"
                   "| old_class | 3 | earlier | kept | open |\n| run_command | 1 | boom | typed | open |")


def _knowledge_files(root):
    knowledge = root / "memory" / "knowledge"
    return {path.relative_to(knowledge).as_posix(): path.read_bytes()
            for path in sorted(knowledge.rglob("*")) if path.is_file()}


@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("usage_fact,message_fact,cut", [
    ({"response_finish_reason": "length"}, {}, "length"),
    ({}, {"stop_reason": "max_tokens"}, "max_tokens"),
    ({}, {"finish_reason": "length"}, "length"),
    ({"response_finish_reason": None}, {"finish_reason": "length"}, ""),  # an explicit null outranks lower fields
    ({"response_finish_reason": "stop"}, {}, ""),
    ({}, {"stop_reason": "end_turn"}, ""),
    ({}, {}, ""),  # no finish fact is not a cutoff
])
def test_a_register_reply_cut_at_the_output_ceiling_keeps_the_previous_register(
        phase, monkeypatch, resumed, usage_fact, message_fact, cut):
    """#1605: a Pattern Register reply the provider marks as cut at its output ceiling
    replaced the whole register with a prefix. Through the REAL post-task coordinator —
    the ordinary reflection stage and a Pause-resumed published reflection
    (``finish_published_reflection``) — with only the provider dispatch substituted:
    the paid call is sent once and accounted, the register, its history and its index
    stay byte-identical, the existing failure owner records ``pattern_register_failed``
    and the stage reads degraded while promotion still runs. A complete, unknown or
    absent finish fact publishes exactly as before."""
    from ouroboros import consolidator, context_fit, llm_observability, post_task_synthesis
    from ouroboros.capability_evidence import CapabilityEvidence

    f = phase
    knowledge = f.root / "memory" / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    (knowledge / "patterns.md").write_text(_REGISTER, encoding="utf-8")
    (knowledge / "patterns_history.jsonl").write_text('{"task_id": "earlier"}\n', encoding="utf-8")
    consolidator._rebuild_knowledge_index(knowledge)
    before = _knowledge_files(f.root)
    monkeypatch.setattr(consolidator, "_consolidation_route", lambda: ("test/model", False))
    monkeypatch.setattr(context_fit, "resolve_context_fit_route", lambda task, *, allow_fetch: (
        {"model": task["model"], "provider": "openrouter"},
        CapabilityEvidence(100_000, "confirmed", "test", "route-test", model=task["model"], provider="openrouter")))
    monkeypatch.setattr(context_fit, "_route_calibration_ratio", lambda *_: 1.0)
    monkeypatch.setattr(pipeline, "_run_reflection", post_task_synthesis._run_reflection)
    pattern_usage = {"prompt_tokens": 7, "completion_tokens": 16384, "total_tokens": 16391, "cost": 0.0, **usage_fact}
    sends, accounted = [], []

    def dispatch(*_args, call_type="", **_kwargs):
        sends.append(call_type)
        if call_type == "pattern_register_update":
            return {"content": _REGISTER_REPLY, **message_fact}, dict(pattern_usage)
        return {"content": "Lesson: typed."}, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0}

    monkeypatch.setattr(llm_observability, "chat_observed", dispatch)
    monkeypatch.setattr("supervisor.state.update_budget_from_usage", accounted.append)
    resume = None
    if resumed:
        published = {"task_id": f.task["id"], "reflection": "Lesson: typed.", "key_markers": ["run_command"],
                     "error_count": 1}
        resume = {"stage": "reflection", "completed_stages": ["memory_fallback_draft", "scratchpad_consolidation"],
                  "result": {"reflection_entry": published}, "usage_snapshot": {"rounds": 3}}
    entries = []
    trace = {"tool_calls": [{"tool": "run_command", "status": "error", "is_error": True, "result": "boom"}]}
    pipeline._run_post_task_processing_async(
        f.env, f.task, {"rounds": 3}, trace, {}, f.root / "logs", event_queue=f.events,
        on_reflection=lambda entry, _llm: entries.append(entry), resume=resume)
    assert f.done.wait(5)

    checkpoint = load_task_result(f.root, f.task["id"])["root_phase_checkpoint"]
    [entry] = entries
    errors = entry.get("memory_operation_errors") or []
    assert sends == ([] if resumed else ["task_reflection"]) + ["pattern_register_update"], "one paid send, no retry"
    assert [row for row in accounted if row.get("prompt_tokens") == 7] == [pattern_usage], "the paid call is accounted"
    assert f.stages[-1] == "backlog", "promotion still runs after the register stage"
    assert not checkpoint.get("post_task_stop_reason")
    if cut:
        assert checkpoint["post_task_synthesis"] == "degraded"
        assert [row["kind"] for row in errors] == ["pattern_register_failed"]
        assert f"finish_reason={cut}" in errors[0]["message"]
        assert "previous register was kept" in errors[0]["message"]
        assert _knowledge_files(f.root) == before, "register, history and index stay byte-identical"
    else:
        assert checkpoint["post_task_synthesis"] == "completed" and errors == []
        assert (knowledge / "patterns.md").read_text(encoding="utf-8") == "# Pattern Register\n\n" + _REGISTER_REPLY + "\n"
        history = (knowledge / "patterns_history.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(history) == 2 and json.loads(history[-1])["old_content"] == _REGISTER
