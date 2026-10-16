"""Mind controls share owner fences, grants and the predecessor CAS."""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from tests._budget_pause_exact_helpers import _install_queue
from tests.test_owner_continue import _interrupted


def _event(action, target, nonce="mind-call-1"):
    return {"action": action, "task_id": target, "actor_task_id": "mind-root", "action_nonce": nonce,
            "reason": "finish the earlier request"}


def test_self_pause_refused_and_nonce_reuses_the_provider_call(tmp_path):
    from ouroboros.tools.control_mind import _continue_task, _pause_task
    from ouroboros.tool_call_log import invocation_scope

    emitted = []
    ctx = SimpleNamespace(task_id="mind-root", pending_events=emitted, event_queue=None)
    assert json.loads(_pause_task(ctx, "mind-root"))["error"] == "self_pause_refused"
    assert json.loads(_continue_task(ctx, "other"))["error"] == "control_call_identity_missing"
    with invocation_scope({"tool_call_id": "provider-call"}):
        first = json.loads(_continue_task(ctx, "other", "interrupted"))
        again = json.loads(_continue_task(ctx, "other", "interrupted"))
    assert first["status"] == "requested" and first["action_nonce"] == again["action_nonce"]
    with invocation_scope({"tool_call_id": "next-call"}):
        assert json.loads(_continue_task(ctx, "other"))["action_nonce"] != first["action_nonce"]


def test_mind_pause_other_root_records_actor_and_resume_keeps_attribution(tmp_path, monkeypatch):
    from ouroboros.owner_pause import read_fence
    from ouroboros.task_results import write_task_result
    from supervisor.events_mind_controls import _apply

    q, _state, workers = _install_queue(tmp_path, monkeypatch)
    task = {"id": "other", "type": "task", "root_task_id": "other", "chat_id": 7, "_attempt": 0}
    workers.PENDING.append(task)
    write_task_result(tmp_path, "other", "scheduled", root_task_id="other")
    result = _apply(_event("pause_task", "other"))
    assert result["ok"]
    fence = read_fence(tmp_path, "other")
    assert fence["actor"] == "mind" and fence["actor_task_id"] == "mind-root"
    assert _apply(_event("pause_task", "other"))["fence_id"] == fence["fence_id"]
    resumed = _apply(_event("resume_task", "other", "resume-call"))
    assert resumed["ok"], resumed
    assert read_fence(tmp_path, "other")["resume_actor"]["actor_task_id"] == "mind-root"


def test_panic_hold_survives_mind_resume_but_owner_can_select_it(tmp_path, monkeypatch):
    from ouroboros.task_results import write_task_result
    from supervisor.events_budget import HOLD_PANIC, hold_budget_row
    from supervisor.events_mind_controls import _apply
    from supervisor.queue_transitions import resume_budget_paused_task

    q, _state, workers = _install_queue(tmp_path, monkeypatch)
    task = {"id": "other", "root_task_id": "other", "_attempt": 0, "type": "task"}
    hold_budget_row(task, reason=HOLD_PANIC)
    workers.PENDING.append(task)
    write_task_result(tmp_path, "other", "scheduled", root_task_id="other")
    assert _apply(_event("resume_task", "other"))["error"] == "panic_requires_owner_resume"
    assert task["_budget_pause_hold"]["reason"] == HOLD_PANIC
    assert resume_budget_paused_task("other")["ok"]


def test_mind_continue_and_owner_button_race_share_one_successor(tmp_path, monkeypatch):
    from ouroboros.task_results import load_task_result
    from supervisor.continuation_admission import admit_continuation
    from supervisor.events_mind_controls import _apply

    _q, _state, workers = _install_queue(tmp_path, monkeypatch)
    _interrupted(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        mind = executor.submit(_apply, _event("continue_task", "pred-1"))
        owner = executor.submit(admit_continuation, "pred-1", action_nonce="owner-button-call")
        answers = [mind.result(), owner.result()]
    accepted = [answer for answer in answers if answer.get("ok")]
    assert len(accepted) == 1, answers
    successor = accepted[0]["successor_task_id"]
    assert len(workers.PENDING) == 1 and workers.PENDING[0]["id"] == successor
    claim = load_task_result(tmp_path, "pred-1")["continued_by"]
    if answers[0].get("ok"):
        assert claim["actor"] == "mind" and claim["actor_task_id"] == "mind-root"
        assert load_task_result(tmp_path, successor)["continuation_admission"]["actor"] == "mind"
    else:
        assert "actor" not in claim


def test_mind_continue_is_attributed_and_idempotent(tmp_path, monkeypatch):
    from ouroboros.task_results import load_task_result
    from supervisor.events_mind_controls import _apply

    _install_queue(tmp_path, monkeypatch)
    _interrupted(tmp_path)
    event = _event("continue_task", "pred-1")
    first, again = _apply(event), _apply(event)
    assert first["ok"] and again["replay"]
    assert first["successor_task_id"] == again["successor_task_id"]
    assert load_task_result(tmp_path, "pred-1")["continued_by"]["actor_task_id"] == "mind-root"


@pytest.mark.parametrize("source", ["owner", "panic"])
def test_continue_after_owner_stop_is_not_authorized_by_a_mind(tmp_path, monkeypatch, source):
    from supervisor.events_mind_controls import _apply

    _install_queue(tmp_path, monkeypatch)
    _interrupted(tmp_path, origin={"source": source, "reason": "owner_stop"})
    assert not _apply(_event("continue_task", "pred-1"))["ok"]
