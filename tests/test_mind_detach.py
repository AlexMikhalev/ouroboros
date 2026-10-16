"""Same work, cold continuation, unchanged authority and actual attempt ownership."""

import json
from types import SimpleNamespace

import pytest

from tests._budget_pause_exact_helpers import _fast_hold, _install_queue, _loop_ctx, _quiet_external


def _arm(tmp_path, monkeypatch):
    from ouroboros import model_detach
    from ouroboros.task_results import write_task_result
    from ouroboros.tools.control_mind import _continue_in_background

    monkeypatch.setattr(model_detach, "pool_available", lambda: {"available": True})
    write_task_result(tmp_path, "head", "running", _is_direct_chat=True)
    ctx, limit = _loop_ctx(tmp_path, "head", direct=True)
    # A completed batch is the cold-sleep checkpoint boundary.
    limit.messages.append({"role": "tool", "tool_call_id": "call_b", "content": "done b"})
    result = json.loads(_continue_in_background(ctx, "keep working"))
    return ctx, limit, result


def test_ready_detach_arms_then_parks_same_id_and_sleep_grant_is_single_use(tmp_path, monkeypatch):
    from ouroboros import budget_pause
    from ouroboros.task_results import load_task_result
    from supervisor.direct_pause import park_direct_pause
    from supervisor.sleep_wake import wake_ready_sleepers

    q, state, workers = _install_queue(tmp_path, monkeypatch)
    from supervisor import message_bus
    monkeypatch.setattr(message_bus, "get_bridge", lambda: SimpleNamespace(push_log=lambda *_a: None))
    monkeypatch.setattr(state, "budget_remaining", lambda *_a, **_k: 5.0)
    ctx, limit, result = _arm(tmp_path, monkeypatch)
    assert result["ok"] and result["status"] == "detach_armed"
    assert "browser" in result["note"] and "REPL" in result["note"]
    assert ctx._model_sleep["intent"] == "continue_in_background"
    _fast_hold(monkeypatch, budget_pause)
    _quiet_external(monkeypatch, budget_pause)
    with pytest.raises(budget_pause.BudgetPauseRequested) as raised:
        budget_pause.enter_cold_sleep(limit)
    pause = raised.value.pause
    assert pause["resume_point"]["unanswered_tool_call_ids"] == []
    task = {"id": "head", "_is_direct_chat": True, "_attempt": 1, "tool_profile": "operator_control"}
    assert park_direct_pause(task, budget_pause.pause_event(task, pause))
    assert len(workers.PENDING) == 1 and workers.PENDING[0]["id"] == "head"
    parked = workers.PENDING[0]
    assert parked["tool_profile"] == "operator_control" and parked["_is_direct_chat"]
    assert parked["detached_by"] == "mind" and parked["detached_at"] > 0
    durable = load_task_result(tmp_path, "head")
    assert durable["metadata"]["detached_at"] == parked["detached_at"]
    assert wake_ready_sleepers(q)[0]["ok"]
    grant = budget_pause.budget_pause_row(tmp_path, "head")["grant"]
    assert grant["selected_by"] == "sleep_wake"
    assert wake_ready_sleepers(q) == []
    assert budget_pause.budget_pause_row(tmp_path, "head")["grant"] == grant


@pytest.mark.parametrize("kind", ["service", "delegated_run", "running_member", "member_custody", "custody_unreadable"])
def test_detach_refuses_blocker_list_without_arming(tmp_path, monkeypatch, kind):
    from ouroboros import model_sleep

    blockers = [{"kind": kind, "detail": "still owned"}]
    monkeypatch.setattr(model_sleep, "cold_blockers", lambda *_a, **_k: blockers)
    ctx, _limit, result = _arm(tmp_path, monkeypatch)
    assert result == {"ok": False, "error": "detach_blocked", "blockers": blockers}
    assert not getattr(ctx, "_model_sleep", None)


def test_detach_does_not_accept_transfer_eligible_runs(tmp_path, monkeypatch):
    from ouroboros import budget_pause, model_sleep, sleep_custody
    from tests.test_model_sleep import _ctx

    monkeypatch.setattr(budget_pause, "observe_task_runs", lambda *_a, **_k: {
        "custody_read": "ok", "runs": [{"run_id": "r", "invocation_id": "i"}]})
    monkeypatch.setattr(sleep_custody, "transferable_pairs", lambda *_a: ({("r", "i")}, False))
    ctx = _ctx(tmp_path)
    assert not model_sleep.cold_blockers(ctx, allow_transfer=True)
    assert model_sleep.cold_blockers(ctx, chosen={"intent": "continue_in_background"}, allow_transfer=True) == [
        {"kind": "delegated_run", "run_id": "r", "invocation_id": "i"}]


def test_pooled_noop_and_pool_failure_keeps_head(tmp_path, monkeypatch):
    from ouroboros import model_detach

    ctx, _limit = _loop_ctx(tmp_path, "head", direct=True)
    ctx.task_metadata = {"_execution_owner": {"kind": "pooled"}}
    assert model_detach.arm_detach(ctx, "")["status"] == "already_in_background"
    ctx.task_metadata = {}
    monkeypatch.setattr(model_detach, "pool_available", lambda: {"available": False, "disabled_reason": "crash_storm"})
    assert model_detach.arm_detach(ctx, "")["error"] == "worker_pool_unavailable"
    assert not getattr(ctx, "_model_sleep", None)


def test_boundary_rechecks_pool_and_custody(tmp_path, monkeypatch):
    from ouroboros import budget_pause, model_detach, model_sleep

    ctx, limit, result = _arm(tmp_path, monkeypatch)
    assert result["ok"]
    monkeypatch.setattr(model_detach, "pool_available", lambda: {"available": False})
    budget_pause.enter_cold_sleep(limit)
    assert ctx._model_sleep is None and "worker_pool_unavailable" in limit.messages[-1]["content"]
    monkeypatch.setattr(model_detach, "pool_available", lambda: {"available": True})
    assert model_detach.arm_detach(ctx, "")["ok"]
    monkeypatch.setattr(model_sleep, "cold_blockers", lambda *_a, **_k: [{"kind": "service", "name": "late"}])
    budget_pause.enter_cold_sleep(limit)
    assert ctx._model_sleep is None and "service late" in limit.messages[-1]["content"]


def test_frozen_profile_survives_carrier_change_but_not_subagent_ceiling():
    from ouroboros.tool_access_roots import active_tool_profile, freeze_tool_profile

    task = {"_is_direct_chat": True}
    assert freeze_tool_profile(task) == "operator_control"
    ctx = SimpleNamespace(task_metadata={"tool_profile": task["tool_profile"]}, is_direct_chat=False)
    assert active_tool_profile(ctx) == "operator_control"
    ctx.task_metadata["delegation_role"] = "subagent"
    assert active_tool_profile(ctx) == "local_readonly_subagent"
    task = {"workspace_root": "/tmp/a", "workspace_mode": "external"}
    assert freeze_tool_profile(task) == "external_workspace_task"
    task.pop("workspace_root")
    assert freeze_tool_profile(task) == "external_workspace_task"


def test_owner_question_stays_warm_in_either_tool_order(tmp_path, monkeypatch):
    from ouroboros import model_detach, owner_wait

    ctx, limit, result = _arm(tmp_path, monkeypatch)
    assert result["ok"]
    ctx._owner_wait_requested = "quiz-created-after-detach"

    class WarmCheckpoint(Exception):
        pass

    def warm(ctx, *_a, **_k):
        assert ctx._model_sleep is None
        assert ctx._owner_wait_requested == "quiz-created-after-detach"
        raise WarmCheckpoint

    monkeypatch.setattr(owner_wait, "checkpoint_owner_wait", warm)
    with pytest.raises(WarmCheckpoint):
        owner_wait.wait_after_tools(ctx, limit.messages, {}, {}, 1, [], set())
    assert model_detach.arm_detach(ctx, "")["error"] == "owner_question_pending"


def test_raise_after_saved_pause_parks_before_unregister_without_error_terminal(tmp_path, monkeypatch):
    from ouroboros import budget_pause
    from supervisor import message_bus, worker_chat_lane

    _q, _state, workers = _install_queue(tmp_path, monkeypatch)
    ctx, limit, _result = _arm(tmp_path, monkeypatch)
    _fast_hold(monkeypatch, budget_pause)
    _quiet_external(monkeypatch, budget_pause)
    task = {"id": "head", "type": "task", "chat_id": 7, "text": "work",
            "project_id": "fixture", "_is_direct_chat": True, "_attempt": 1}
    emitted, released = [], []
    monkeypatch.setattr(workers, "get_event_q", lambda: SimpleNamespace(put=emitted.append))
    monkeypatch.setattr(message_bus, "get_bridge", lambda: SimpleNamespace(push_log=lambda _e: None))
    monkeypatch.setattr(worker_chat_lane, "_report_direct_chat_error", lambda *_a: pytest.fail("pause became terminal"))

    def fail(_task):
        try:
            budget_pause.enter_cold_sleep(limit)
        except budget_pause.BudgetPauseRequested:
            raise OSError("after saved pause")

    def unregister(task_id):
        assert workers.PENDING[0]["id"] == task_id
        released.append(task_id)

    assert worker_chat_lane._execute_chat_task({"task": task, "agent": SimpleNamespace(handle_task=fail),
        "chat_id": 7, "registry": SimpleNamespace(unregister=unregister)})
    assert released == ["head"] and not emitted
    assert not budget_pause.dispatch_fenced("head")
