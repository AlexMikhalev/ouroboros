"""The quiet-wake door: finish_task(silent=true) on a wake's own turn.

An ordinary direct turn would replace an empty final with an error notice
and emit a chat row. Deliberate wake silence instead stays a typed completion
fact; wake-started tasks retain their obligation to answer.
"""

import json
from types import SimpleNamespace

import pytest

from ouroboros.consciousness_authority import CONSCIOUSNESS_CATEGORY, CONSCIOUSNESS_TASK_CATEGORY
from ouroboros.tools.control_runtime import stage_completion_request
from ouroboros.tools.registry import ToolRegistry


def _registry(tmp_path, monkeypatch, *, direct: bool, metadata: dict) -> ToolRegistry:
    registry = ToolRegistry(repo_dir=tmp_path / "repo", drive_root=tmp_path / "data")
    ctx = registry._ctx
    monkeypatch.setattr(ctx, "is_direct_chat", direct, raising=False)
    monkeypatch.setattr(ctx, "task_metadata", metadata, raising=False)
    return registry


def _wake_metadata() -> dict:
    return {"initiator": "consciousness", "usage_category": CONSCIOUSNESS_CATEGORY,
            "wake_reason": "heartbeat"}


def _finish(registry: ToolRegistry, **kwargs) -> str:
    return registry.execute("finish_task", kwargs)


def test_silent_finish_stages_on_wake_own_turn(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch, direct=True, metadata=_wake_metadata())
    result = _finish(registry, action="finish", answer="", silent=True)
    payload = json.loads(result)
    assert payload["status"] == "completion_requested"
    staged = registry._ctx._completion_request
    assert staged["wake_silent"] is True
    assert staged["allow_empty"] is True


def test_silent_finish_refused_on_owner_direct_turn(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch, direct=True,
                         metadata={"initiator": "owner", "usage_category": "chat"})
    result = _finish(registry, action="finish", answer="", silent=True)
    assert "COMPLETION_ARGUMENT" in result
    assert getattr(registry._ctx, "_completion_request", None) is None


def test_silent_stop_refused_even_on_wake(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch, direct=True, metadata=_wake_metadata())
    result = _finish(registry, action="stop", answer="", rationale="unfinished", silent=True)
    assert "COMPLETION_ARGUMENT" in result
    assert getattr(registry._ctx, "_completion_request", None) is None


def test_silent_finish_refused_without_direct_flag(tmp_path, monkeypatch):
    # A pooled task with wake metadata (the tasks a wake starts) must refuse:
    # inheritance keeps initiator=consciousness but never the own-turn category.
    registry = _registry(tmp_path, monkeypatch, direct=False,
                         metadata={"initiator": "consciousness",
                                   "usage_category": CONSCIOUSNESS_TASK_CATEGORY})
    result = _finish(registry, action="finish", answer="", silent=True)
    assert "COMPLETION_ARGUMENT" in result
    assert getattr(registry._ctx, "_completion_request", None) is None


def test_silent_finish_refused_on_wake_started_root(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch, direct=True,
                         metadata={"initiator": "consciousness",
                                   "usage_category": CONSCIOUSNESS_TASK_CATEGORY})
    result = _finish(registry, action="finish", answer="", silent=True)
    assert "COMPLETION_ARGUMENT" in result
    assert getattr(registry._ctx, "_completion_request", None) is None


def test_plain_finish_without_silent_unchanged(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch, direct=True, metadata={"initiator": "owner"})
    result = _finish(registry, action="finish", answer="  ")  # blank without the door
    assert "COMPLETION_ARGUMENT" in result
    assert getattr(registry._ctx, "_completion_request", None) is None
    result = _finish(registry, action="finish", answer="done")
    assert json.loads(result)["status"] == "completion_requested"
    staged = registry._ctx._completion_request
    assert "wake_silent" not in staged
    assert staged["allow_empty"] is False


def test_consume_sets_wake_silent_fact_only_on_accepted_path(tmp_path, monkeypatch):
    """The ctx fact is set by consume_completion_request on the accepted path only."""
    from ouroboros.loop_delivery import completion_observation, consume_completion_request

    registry = _registry(tmp_path, monkeypatch, direct=True, metadata=_wake_metadata())
    tools = registry
    trace = {"tool_calls": []}
    messages: list = []
    tools._ctx._completion_observation = completion_observation(tools._ctx, trace)
    staged = stage_completion_request(tools._ctx, {"action": "finish", "answer": "",
                                                   "wake_silent": True}, allow_empty=True)
    assert json.loads(staged)["status"] == "completion_requested"

    class _Ctx:  # loop-limit context stand-in: what consume's success path reads
        def __init__(self, msgs, tools, tmp):
            self.messages = msgs
            self.tools = tools
            self.task_id = getattr(tools._ctx, "task_id", "t1")
            self.root_task_id = getattr(tools._ctx, "root_task_id", "t1")
            self.drive_root = tmp
            self.status_drive_root = tmp
            self.drive_logs = tmp / "logs"

    accepted = consume_completion_request(tools, _Ctx(messages, tools, tmp_path), trace)
    assert accepted is True
    assert tools._ctx._wake_silent_completion is True


def test_held_silent_finish_then_plain_refinish_delivers_the_real_answer(tmp_path, monkeypatch):
    """An accepted plain re-finish clears a held silent selection."""
    from ouroboros.loop_delivery import completion_observation, consume_completion_request

    registry = _registry(tmp_path, monkeypatch, direct=True, metadata=_wake_metadata())
    tools = registry

    class _Ctx:
        def __init__(self, msgs, t):
            self.messages = msgs
            self.tools = tools
            self.task_id = "t1"
            self.root_task_id = "t1"
            self.drive_root = t
            self.status_drive_root = t
            self.drive_logs = t / "logs"

    trace = {"tool_calls": []}
    # 1) Accepted silent finish — flag latched.
    tools._ctx._completion_observation = completion_observation(tools._ctx, trace)
    stage_completion_request(tools._ctx, {"action": "finish", "answer": "", "wake_silent": True}, allow_empty=True)
    assert consume_completion_request(tools, _Ctx([], tmp_path), trace) is True
    assert tools._ctx._wake_silent_completion is True
    # 2) The turn is held; the model re-finishes with a REAL answer, no silent key.
    tools._ctx._completion_observation = completion_observation(tools._ctx, trace)
    stage_completion_request(tools._ctx, {"action": "finish", "answer": "the real answer"})
    assert consume_completion_request(tools, _Ctx([], tmp_path), trace) is True
    assert tools._ctx._wake_silent_completion is False, \
        "a plain accepted re-finish must clear the stale silence declaration"


@pytest.mark.parametrize("held_answer", ["", "hidden previous answer"])
@pytest.mark.parametrize("fresh_answer", ["my revised answer", ""])
def test_owner_followup_plain_prose_supersedes_held_silence(
    tmp_path, monkeypatch, held_answer, fresh_answer,
):
    """An empty new reply cannot publish a previously hidden DeliveryCandidate."""
    import ouroboros.loop as loop_mod
    import ouroboros.loop_delivery as delivery

    registry = _registry(tmp_path, monkeypatch, direct=True, metadata=_wake_metadata())
    tool_ctx = registry._ctx
    tool_ctx._wake_silent_completion = True  # accepted earlier silent finish, now held
    tool_ctx._delivery_candidate = delivery.DeliveryCandidate(
        full_text=held_answer, content_sha256="a" * 64, revision=1,
        evidence_revision=0, evidence_fingerprint="", acceptance_binding={},
        finalization_control="owner_revision_required")
    tool_ctx._delivery_control_required = False
    monkeypatch.setattr(loop_mod, "_project_child_result_dispositions", lambda *a: None)
    replacements = []
    def replace(_tools, _ctx, _trace, text, **_kwargs):
        replacements.append(text)
        return delivery.DeliveryCandidate(
            full_text=text, content_sha256="b" * 64, revision=2,
            evidence_revision=0, evidence_fingerprint="", acceptance_binding={})
    monkeypatch.setattr(loop_mod, "_replace_delivery_candidate", replace)
    monkeypatch.setattr(delivery.working_checkpoint, "save_or_log", lambda *a: None)
    limit_ctx = SimpleNamespace(messages=[])
    proceed, content = delivery._prepare_delivery_candidate(
        fresh_answer, registry, limit_ctx, {}, explicit=False, resumed=False)
    assert proceed and content == fresh_answer
    assert replacements == [fresh_answer], "blank fresh input must replace a real held candidate"
    assert tool_ctx._wake_silent_completion is False
    from ouroboros.agent import _resolve_intentional_empty
    from ouroboros.agent_task_pipeline import _wake_silent_terminal

    usage = {"execution_status": "ok"}
    replaced, intentional, text = _resolve_intentional_empty(tool_ctx, usage, content)
    if fresh_answer:
        assert not replaced and not intentional and text == fresh_answer
    else:
        assert replaced and not intentional and text.startswith("⚠️ Model returned an empty response")
    assert "wake_silent" not in usage and not _wake_silent_terminal(tool_ctx, False)


def test_consume_refusal_leaves_no_silent_fact(tmp_path, monkeypatch):
    from ouroboros.loop_delivery import completion_observation

    registry = _registry(tmp_path, monkeypatch, direct=True, metadata=_wake_metadata())
    tools = registry
    trace = {"tool_calls": []}
    tools._ctx._completion_observation = completion_observation(tools._ctx, trace)
    # Contradictory request (both answer and selector) is refused at staging.
    staged = stage_completion_request(tools._ctx, {"action": "finish", "answer": "",
                                                   "answer_sha256": "x" * 64,
                                                   "wake_silent": True}, allow_empty=True)
    assert "COMPLETION_ARGUMENT" in staged
    assert getattr(tools._ctx, "_completion_request", None) is None
    assert getattr(tools._ctx, "_wake_silent_completion", False) is False


def test_agent_intentional_empty_covers_wake_silent():
    """The agent-side stamp precedes outcome grading on a silent wake."""
    from ouroboros.agent import _resolve_intentional_empty

    ctx = type("C", (), {"_wake_silent_completion": True})()
    usage = {"execution_status": "ok"}
    replaced, intentional, text = _resolve_intentional_empty(ctx, usage, "")
    assert (replaced, intentional, text) == (False, True, "")
    assert usage["wake_silent"] is True, "the stamp lands BEFORE outcome derivation reads usage"

    # A failed status refuses the door: notice replaced, declarations cleared.
    ctx2 = type("C", (), {"_wake_silent_completion": True})()
    usage2 = {"execution_status": "failed"}
    replaced2, intentional2, text2 = _resolve_intentional_empty(ctx2, usage2, "")
    assert replaced2 is True and intentional2 is False and text2.startswith("⚠️ Model returned an empty response")
    assert ctx2._wake_silent_completion is False
    assert "wake_silent" not in usage2

    # A model-authored final that merely LOOKS like the host notice is never
    # relabelled: replacement is a returned fact, not a string-shape test.
    ctx3 = type("C", (), {"_wake_silent_completion": False})()
    replaced3, _, text3 = _resolve_intentional_empty(ctx3, {}, "⚠️ Model returned an empty response from the sub-agent")
    assert replaced3 is False and text3.startswith("⚠️ Model returned an empty response from")


def test_lineage_final_frame_keeps_model_execution_after_prepare(monkeypatch, tmp_path):
    """The final frame retains lineage and model execution together."""
    from ouroboros.task_finalization import prepare_terminal_send_event
    from tests.test_consciousness_initiator_label import WAKE_META

    task = {"id": "w5", "type": "task", "chat_id": 1, "text": "wake", "_is_direct_chat": True,
            "metadata": dict(WAKE_META)}
    usage = {"execution_status": "ok",
             "initial_model_request": {"model": "fixture/main"},
             "llm_call_refs": [{"llm_call_id": "c1", "usable_solve_response": True,
                                "model": "fixture/main"}]}
    send_event = {"type": "send_message", "chat_id": task["chat_id"], "format": "markdown",
                  "text": "hi", "log_text": "hi", "task_id": "w5", "ts": "2026-10-11T00:00:00Z"}
    send_event["progress_meta"] = {"initiator": "consciousness"}
    merged = prepare_terminal_send_event(tmp_path, task, "hi", usage, send_event, presence=False)
    meta = merged.get("progress_meta") or {}
    assert meta.get("initiator") == "consciousness"
    assert meta.get("model_execution", {}).get("llm_call_id") == "c1", \
        "prepare's setdefault must merge model_execution INTO the seeded lineage meta"


def test_pipeline_suppresses_final_row_and_stamps_fact(monkeypatch, tmp_path):
    """emit_task_results drops ONLY the final send_message row for a silent wake."""
    import ouroboros.agent_task_pipeline as pipeline

    monkeypatch.setattr(pipeline, "_run_post_task_processing_async", lambda *a, **k: None)
    pending: list = []
    task = {"id": "w2", "type": "task", "chat_id": 1, "text": "wake", "_is_direct_chat": True,
            "metadata": dict(_wake_metadata())}

    class _Ctx:
        _wake_silent_completion = True

    # agent.handle_task stamps the fact into usage BEFORE emit_task_results, so
    # outcome derivation grades the deliberate empty final by its own branch.
    usage = {"rounds": 1, "execution_status": "ok", "wake_silent": True}
    pipeline.emit_task_results(
        SimpleNamespace(drive_root=tmp_path, repo_dir=tmp_path), None, None,
        pending, task, "", usage, {"tool_calls": [], "reasoning_notes": []},
        0.0, tmp_path / "logs", ctx=_Ctx(),
    )
    sends = [e for e in pending if isinstance(e, dict) and e.get("type") == "send_message"]
    assert sends == [], "a silent wake must not emit a final chat row"
    assert usage.get("wake_silent") is True
    # Boot replay must not deliver a row the silent wake omitted.
    outbox = json.loads((tmp_path / "state" / "terminal_deliveries.json").read_text(encoding="utf-8")
                        if (tmp_path / "state" / "terminal_deliveries.json").exists() else "{}")
    owed = [row for row in (outbox.get("pending") or []) if row.get("task_id") == "w2"]
    assert owed == []
    # The durable record keeps the typed fact: deliberate silence, not a lost answer.
    from ouroboros.task_results import load_task_result

    stored = load_task_result(tmp_path, "w2")
    assert stored.get("wake_silent") is True


def test_pipeline_emits_row_without_the_door(monkeypatch, tmp_path):
    """Without the ctx fact the final row is emitted exactly as before."""
    import ouroboros.agent_task_pipeline as pipeline

    monkeypatch.setattr(pipeline, "_run_post_task_processing_async", lambda *a, **k: None)
    pending: list = []
    task = {"id": "w3", "type": "task", "chat_id": 1, "text": "wake", "_is_direct_chat": True,
            "metadata": dict(_wake_metadata())}
    usage = {"rounds": 1, "execution_status": "ok"}
    pipeline.emit_task_results(
        SimpleNamespace(drive_root=tmp_path, repo_dir=tmp_path), None, None,
        pending, task, "final answer", usage, {"tool_calls": [], "reasoning_notes": []},
        0.0, tmp_path / "logs",
    )
    sends = [e for e in pending if isinstance(e, dict) and e.get("type") == "send_message"]
    assert len(sends) == 1 and sends[0].get("text") == "final answer"
    assert "wake_silent" not in usage
