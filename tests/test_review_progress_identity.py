"""Progress distinguishes requested slots from their own execution receipts."""

import queue
from types import SimpleNamespace

from ouroboros.review_custody import _emit_operation
from ouroboros.review_execution_projection import review_actor_progress_text
from ouroboros.review_records import ReviewSlot
from ouroboros.review_execution import ReviewRouteKind


def test_requested_model_is_never_substituted_for_missing_observed_model():
    slot = ReviewSlot("slot-one", "requested-model", route=ReviewRouteKind.AGENT_SESSION,
                      session_target="cursor=chosen-model", session_profile="requested-profile")
    actor = SimpleNamespace(usage={"provider": "claudexor", "delegated_run_id": "run-one"},
                            operation_state="settled", status="ok")
    message = review_actor_progress_text("plan_review", "finished", slot, actor)
    assert "requested model=requested-model" in message
    assert "target=cursor=chosen-model" in message
    assert "observed execution: harness, model=not reported" in message
    assert "observed execution: harness, model=requested-model" not in message


def test_same_requested_model_keeps_per_task_observed_route_and_profile():
    slot = ReviewSlot("same-slot", "requested-model", route=ReviewRouteKind.AGENT_SESSION)
    messages = []
    for task, model, profile in (("one", "observed-one", "profile-one"), ("two", "observed-two", "profile-two")):
        progress = []
        events = queue.Queue()
        ctx = SimpleNamespace(event_queue=events, emit_progress_fn=progress.append, execution_id=task)
        request = SimpleNamespace(surface="task_acceptance", task_attempt=1)
        actor = SimpleNamespace(usage={"provider": "claudexor", "delegated_route": "cursor",
                                      "resolved_model": model, "applied_profile": profile},
                                operation_state="settled", status="ok")
        _emit_operation(ctx, task_id=task, request=request, entry=SimpleNamespace(operation_id=task),
                        slot=slot, phase="finished", actor=actor)
        assert events.get_nowait()["task_id"] == task
        assert len(progress) == 1
        assert f"harness:cursor, model={model}, profile={profile}" in progress[0]
        messages.append(progress[0])
    assert "observed-two" not in messages[0]
    assert "observed-one" not in messages[1]


def test_refusal_is_not_reported_as_observed_execution():
    slot = ReviewSlot("api-slot", "requested-model")
    actor = SimpleNamespace(usage={}, operation_state="not_dispatched", status="not_dispatched")
    message = review_actor_progress_text("skill_review", "failed", slot, actor)
    assert "observed execution: not reported" in message
    assert "state=not_dispatched" in message


def test_api_sent_model_is_not_promoted_to_provider_observation():
    from ouroboros.llm import LLMClient

    response = {"id": "response", "model": "provider-reported-model",
        "choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0}}
    client = object.__new__(LLMClient)
    _, usage = client._normalize_remote_response(response,
        {"provider": "openai", "usage_model": "openai::sent-route", "resolved_model": "sent-route"},
        skip_cost_fetch=True)
    slot = ReviewSlot("api-slot", "requested-route")
    actor = SimpleNamespace(usage=usage, operation_state="settled", status="ok")
    message = review_actor_progress_text("plan_review", "finished", slot, actor)
    assert "requested model=requested-route" in message
    assert "api execution: sent model=openai::sent-route" in message
    assert "provider-observed model=not reported" in message
    assert "observed execution: api" not in message
    usage["delivery"] = "native_tool_rounds"
    assert "native execution: sent model=openai::sent-route" in review_actor_progress_text("plan_review", "finished", slot, actor)


def test_progress_failure_cannot_drop_started_custody_event():
    observed_queue_sizes = []

    def broken_progress(text):
        observed_queue_sizes.append(events.qsize())
        raise RuntimeError("UI disconnected")

    events = queue.Queue()
    _emit_operation(SimpleNamespace(event_queue=events, emit_progress_fn=broken_progress),
                    task_id="task", request=SimpleNamespace(surface="scope_review"),
                    entry=SimpleNamespace(operation_id="operation"),
                    slot=ReviewSlot("scope", "requested-model"), phase="started")
    assert observed_queue_sizes == [1]
    event = events.get_nowait()
    assert event["type"] == "cognitive_operation"
    assert event["phase"] == "started"
    assert event["operation_id"] == "operation"
