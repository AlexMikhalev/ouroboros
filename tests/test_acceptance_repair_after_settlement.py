"""The control-repair round after a settled acceptance panel runs; it never parks again."""
from __future__ import annotations

import copy
import json

import pytest

from ouroboros import loop
from tests.test_acceptance_async_loop import ANSWER, call, keep
from tests.test_acceptance_async_loop import full_loop as _full_loop

full_loop = _full_loop  # noqa: F811 - pytest fixture re-export


@pytest.mark.parametrize("owner_followup", [False, True], ids=["same_owner", "owner_followup"])
def test_prose_after_the_settled_verdict_wake_gets_its_repair_round_not_a_second_park(
    full_loop, monkeypatch, owner_followup,
):
    """The wake after a settled panel re-offers the keep/replace control; a prose
    answer there is a malformed control, so the loop queues its ONE repair round.
    That round must run: the panel has already settled, so parking again would
    wait for a settlement that never comes (the keyless E2E lane hung this way
    until the task deadline). One park while the panel ran, then the repair."""
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
            return {"content": reauthored}, 0.0  # prose where the control object was due
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
    assert f.model_step == 4, f.progress
    host = [r for r in trace["review_runs"] if r.get("authority") == "host_root"]
    assert [r.get("aggregate_signal") for r in host] == ["FAIL", "DEGRADED"], host
