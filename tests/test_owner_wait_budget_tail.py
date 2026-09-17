"""A cold grant finishes the saved round before another ordinary model/tool call."""

import json
from dataclasses import replace

import pytest

from ouroboros import loop, owner_wait, pricing, task_pacing, usage_accounting as accounting
from ouroboros.contracts.task_contract import normalize_budget_profile
from ouroboros.owner_mailbox import write_owner_message
from tests.test_owner_wait_cold_loop import cold_registry
from tests.test_loop_transport_wait import _loop_kwargs


@pytest.mark.parametrize("queued_override", [False, True])
def test_cold_grant_checks_saved_budget_before_ordinary_dispatch(tmp_path, monkeypatch, queued_override):
    (tmp_path / "logs").mkdir()
    (tmp_path / "fixture.txt").write_text("A real extra tool read")
    scope = accounting.UsageScope(drive_root=tmp_path, task_id="t-wait", root_task_id="t-wait",
                                  global_limit_usd=200.0, root_limit_usd=50.0)
    calls, checkpoints, network = [], [], []
    phase = "warm"
    owner_answer = "Use the existing prepared result and explain the owner choice."

    def settle(amount):
        held = accounting.reserve_attempt(accounting.AttemptRequest(
            model="fixture", provider="fixture", reservation_usd=amount))
        accounting.mark_dispatched(held)
        accounting.settle_attempt(held, {"prompt_tokens": 1, "completion_tokens": 1},
                                  cost_usd=amount, cost_final=True)

    def refuse_network(*args, **kwargs):
        network.append((args, kwargs))
        raise AssertionError("fixture attempted a provider call")

    def forced(call, **kwargs):
        calls.append((phase, "forced", call.round_idx, call.active_model))
        return ("Current owner choice considered." if owner_answer in json.dumps(call.messages)
                else "Stale pre-answer draft.")

    def ordinary(call, disposition, **kwargs):
        calls.append((phase, "ordinary", call.round_idx, call.active_model))
        settle(.2)
        call.accumulated_usage["cost"] = float(call.accumulated_usage.get("cost") or 0) + .2
        if phase == "warm":
            if queued_override:
                # The switch_model tool leaves this pending until the NEXT round.
                call.tools._ctx.active_model_override = "after-budget"
            name, args = "escalate", {"question": "Continue the prepared result?",
                "options": [{"label": "Continue"}, {"label": "Stop"}],
                "stake": "Fixture action", "wait_for_answer": True}
        else:
            name, args = "read_file", {"root": "active_workspace", "path": "fixture.txt"}
        return {"role": "assistant", "content": "", "tool_calls": [{"id": phase, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]}, .2

    monkeypatch.setattr("ouroboros.llm.LLMClient.chat", refuse_network)
    monkeypatch.setattr("ouroboros.llm.LLMClient.chat_async", refuse_network)
    monkeypatch.setattr(pricing, "_fetch_live_rows", lambda provider: {})
    monkeypatch.setattr(loop, "_dispatch_round_model", ordinary)
    monkeypatch.setattr(loop, "_call_forced_model_once", forced)
    with accounting.usage_scope(scope):
        ceiling = task_pacing.resolve_cost_ceiling(200, normalize_budget_profile(None), root_cap_usd=50)
        with accounting.usage_scope(replace(scope, task_id="prior-child", parent_task_id="t-wait")):
            settle(46.9)

        def registry():
            tools = cold_registry(tmp_path, monkeypatch, ceiling)
            tools._ctx._owner_wait_requested = ""
            tools._ctx.current_chat_id, tools._ctx.current_task_type = 1, "task"
            tools._ctx.task_model_override = "same-model"
            return tools

        def park(ctx, checkpoint):
            checkpoints.append(checkpoint)
            owner_wait.set_owner_wait(tmp_path, "t-wait", {**checkpoint, "state": "waiting"})
            write_owner_message(tmp_path, owner_answer, task_id="t-wait", msg_id="warm-answer")

        warm = registry()
        warm._ctx.owner_wait_resume, warm._ctx.owner_wait_callback = None, park
        warm_result, _, warm_trace = loop.run_llm_loop(**{
            **_loop_kwargs(tmp_path, warm, []), "budget_remaining_usd": 200, "drive_logs": tmp_path / "logs"})
        assert len(checkpoints) == 1

        phase = "cold"
        cold = registry()
        owner_wait.set_owner_wait(tmp_path, "t-wait", {**checkpoints[0], "state": "waiting"})
        cold._ctx.owner_wait_resume = {**checkpoints[0], "restart_transaction_id": "observed-restart"}
        cold._ctx.owner_wait_callback = lambda *_: write_owner_message(
            tmp_path, owner_answer, task_id="t-wait", msg_id="cold-answer")
        cold_result, _, cold_trace = loop.run_llm_loop(**{
            **_loop_kwargs(tmp_path, cold, []), "budget_remaining_usd": 152.9, "drive_logs": tmp_path / "logs"})
        held = accounting.reserve_attempt(accounting.AttemptRequest(
            model="fixture", provider="fixture", reservation_usd=.2))
        accounting.release_attempt(held, "test did not send")  # hard50 still has room
    assert network == []
    assert "Current owner choice considered." in warm_result and "Current owner choice considered." in cold_result
    assert not [row for row in calls if row[0:2] == ("cold", "ordinary")]
    assert all(row[2:] == (1, "same-model") for row in calls)
    assert [row["tool"] for row in cold_trace["tool_calls"]] == [row["tool"] for row in warm_trace["tool_calls"]] == ["escalate"]
