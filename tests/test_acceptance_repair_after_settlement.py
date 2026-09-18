"""An acceptance continuation runs after settlement without another park."""
from __future__ import annotations

import copy
import json

import pytest

from ouroboros import loop
from tests.test_acceptance_async_loop import ANSWER, call, keep
from tests.test_acceptance_async_loop import full_loop as _full_loop

full_loop = _full_loop  # noqa: F811 - pytest fixture re-export


@pytest.mark.parametrize("owner_followup", [False, True], ids=["same_owner", "owner_followup"])
def test_prose_after_settlement_needs_control_only_for_unacknowledged_owner_input(
    full_loop, monkeypatch, owner_followup,
):
    """Complete prose continues directly under N2. A changed owner source still
    needs its exact acknowledgement, whose repair round must run after settlement.
    Neither path may park again on the panel whose one wake was already consumed."""
    f = full_loop
    f.reviewer_verdict = "FAIL"
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "1")
    reauthored = ANSWER + " Budget: $12."
    followup = "Also show the budget as an explicit dollar amount."

    def park(ctx, checkpoint):
        f.park(ctx, checkpoint)
        if owner_followup and len(f.waits) == 1:
            # The owner's new input reaches the same Main wake as the settled
            # verdict. That panel no longer covers the current owner corpus.
            f.incoming.put(followup)

    f.ctx.owner_wait_callback = park

    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return {"content": "", "tool_calls": [call("task_acceptance_review", {"claim": ANSWER}, "first-review")]}, 0.0
        if f.model_step == 2:
            assert f.entered.wait(5) and not f.release.is_set()
            return keep(f), 0.0  # held under the running panel: the one legitimate park
        if f.model_step == 3:
            assert f.settled.is_set() and "FAIL" in str(messages)
            if owner_followup:
                assert followup in str(messages)
            return {"content": reauthored}, 0.0
        if f.model_step == 4:
            assert "[DELIVERY_CONTROL_REPAIR]" in str(messages[-1].get("content"))
            observation = f.ctx._acceptance_observation
            return {"content": json.dumps({"delivery_control": "replace", "full_answer": reauthored,
                                           "acceptance_subject": {"owner_source_sha256": observation["owner_source_sha256"]}})}, 0.0
        assert f.model_step < 7, f.progress
        return keep(f), 0.0

    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, _usage, trace = f.run()
    assert result == reauthored and len(f.review_sends) == 1
    assert [wait.get("reason") for wait in f.waits] == ["review"], f.waits
    assert f.model_step == (4 if owner_followup else 3), f.progress
    assert any("[DELIVERY_CONTROL_REPAIR]" in str(messages) for messages in f.model_inputs) is owner_followup
    host = [r for r in trace["review_runs"] if r.get("authority") == "host_root"]
    assert [r.get("aggregate_signal") for r in host] == ["FAIL", "DEGRADED"], host
