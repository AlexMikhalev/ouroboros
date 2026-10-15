"""Scope review R1 (Astra F1–F3, Opus C1–C3, Fable advisories) — fix batch 1, pinned in both
directions through the real ToolContext, the real plan record, the real ledger and the organ's
own delivery builders (the review substrate is the fake of ``tests/test_plan_task_tool.py``).
Pure record rules live in ``tests/test_plan_record.py``; the web panel in
``web/tests/review_plan_record.test.js``; the gateway projection in ``tests/test_plan_record_gateway.py``."""
from __future__ import annotations

from ouroboros import owner_hurry as oh
from ouroboros.review_records import ReviewActorRecord
from ouroboros.tools import plan_ask, plan_review as pr
from tests.test_plan_task_tool import CLEAN, _call, _record, _reply, _slots, _user_text, _write, harness as _harness

harness = _harness  # noqa: F811 - pytest fixture re-export

REFUSED = {"status": "not_dispatched", "raw_text": "", "error": "delegated review route subscription window is exhausted (resets 2030-01-01)",
           "failure_code": "subscription_window_exhausted", "reset_at": "2030-01-01T00:00:00+00:00", "operation_state": "not_dispatched"}
PENDING = {"status": "pending", "raw_text": "", "operation_state": "pending_dispatch", "late_result_pending": True}


def _content(sub, call, seat):
    return _user_text(sub.calls[call]["request"].slot_messages[seat][-1]["content"])


def _settle(ctx, harness, request, seat, text, *, roster=2):
    slot = next(s for s in harness.state["slots"] if s.slot_id == seat)
    actor = ReviewActorRecord(slot_id=seat, model=slot.model, status="ok", raw_text=text, operation_id=f"op-{seat}-late",
                              operation_state="late_settled", usage={"cost": 0.01, "resolved_model": slot.model})
    plan_ask.settle_released_reply(ctx, request=request, task_id="task-1", actor=actor, slot=slot, settled_wave={}, roster_size=roster)


# ---------------------------------------------------------------- C1: a quoted object in a preamble is not the reply

def test_a_reply_behind_a_quoted_done_when_row_keeps_its_objection_and_holds(harness):
    preamble = ('I re-read the bar:\n```json\n{"id": "c1", "claim": "exactly 5 slides", "support": "count them"}\n```\n'
                + _reply("Too thin.", objections=[("o1", "no story arc")]))
    harness.install({"s1": preamble, "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    text, result = _write(ctx, ask="council")
    assert result.meta["plan_holding"] == 1 and "s1 / o1" in text and "HELD" in text
    s1 = next(p for p in _record(harness)["asks"][0]["participants"] if p["seat_id"] == "s1")
    assert s1["status"] == "answered" and s1["reply"]["prose"] == "Too thin." and s1["reply"]["objections"] == [{"id": "o1", "text": "no story arc"}]
    assert any("s1 answered" in line or "m/a answered — 1 objection" in line for line in harness.progress)
