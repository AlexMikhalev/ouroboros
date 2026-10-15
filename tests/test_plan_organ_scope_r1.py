"""Scope review R1 (Astra F1–F3, Opus C1–C3, Fable advisories) — fix batch 1, pinned in both
directions through the real ToolContext, the real plan record, the real ledger and the organ's
own delivery builders (the review substrate is the fake of ``tests/test_plan_task_tool.py``).
Pure record rules live in ``tests/test_plan_record.py``; the web panel in
``web/tests/review_plan_record.test.js``; the gateway projection in ``tests/test_plan_record_gateway.py``."""
from __future__ import annotations

from ouroboros import owner_hurry as oh
from ouroboros import plan_record
from ouroboros.review_records import ReviewActorRecord
from ouroboros.tools import plan_ask
from tests.test_plan_task_tool import CLEAN, _record, _reply, _slots, _user_text, _write, harness as _harness

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


# ---------------------------------------------------------------- F1: a $0 pre-dispatch refusal never counts as a reached reviewer

def test_a_seat_refused_before_dispatch_is_not_reached_and_an_all_refused_ask_is_attempted_not_held(harness):
    harness.state["slots"] = _slots(("s1", "m/a"), ("s2", "m/b"))
    harness.install({"s1": dict(REFUSED), "s2": dict(REFUSED)})
    ctx = harness.make_ctx(force_plan=True)
    text, _result = _write(ctx, ask="council")
    rows = {p["seat_id"]: p for p in _record(harness)["asks"][0]["participants"]}
    assert all(p["status"] == "not_dispatched" and p["dispatched"] is False for p in rows.values())
    assert rows["s1"]["failure_code"] == "subscription_window_exhausted" and "not sent" in text
    decision = oh.force_plan_decision(ctx, {}, enforcement="blocking")
    assert decision["asks_dispatched"] == 0 and decision["asks_unroutable"] == 1 and decision["force_plan_met"] is False
    assert decision["status"] == "force_plan_unmet" and decision["allow"] is True and decision["holding"] == []
    assert oh.unmet_force_plan_obligation(ctx)["unmet"] is True, "unmet (nobody reached), so a promote still carries it"
    # The other direction: one seat refused and one reached is a reached ask; the refused seat never saw the plan.
    other = harness.make_ctx(task_id="task-2", force_plan=True)
    harness.install({"s1": dict(REFUSED), "s2": CLEAN})
    _write(other, ask="council")
    met = oh.force_plan_decision(other, {}, enforcement="blocking")
    assert met["asks_dispatched"] == 1 and met["force_plan_met"] is True and met["asks_unroutable"] == 0
    views = plan_record.participant_views(_record(harness, "task-2"))
    assert views["s1"]["last_seen_revision"] is None and views["s2"]["last_seen_revision"] == 1


def test_a_late_pre_dispatch_refusal_through_the_custody_hook_withdraws_the_dispatch_flag_too(harness):
    harness.state["slots"] = _slots(("s1", "m/a"), ("s2", "m/b"))
    sub = harness.install({"s1": dict(PENDING), "s2": dict(PENDING)})
    ctx = harness.make_ctx(force_plan=True)
    _write(ctx, ask="council")
    assert oh.force_plan_decision(ctx, {}, enforcement="blocking")["asks_dispatched"] == 1, "pending seats were sent"
    request = sub.calls[0]["request"]
    for seat in ("s1", "s2"):
        slot = next(s for s in harness.state["slots"] if s.slot_id == seat)
        actor = ReviewActorRecord(slot_id=seat, model=slot.model, status="not_dispatched", raw_text="", error=REFUSED["error"],
                                  operation_id=f"op-{seat}-refused", operation_state="not_dispatched",
                                  failure_code="subscription_window_exhausted", reset_at=REFUSED["reset_at"])
        plan_ask.settle_released_reply(ctx, request=request, task_id="task-1", actor=actor, slot=slot, settled_wave={}, roster_size=2)
        decision = oh.force_plan_decision(ctx, {}, enforcement="blocking")
        row = next(p for p in _record(harness)["asks"][0]["participants"] if p["seat_id"] == seat)
        assert row["status"] == "not_dispatched" and row["dispatched"] is False and row["reset_at"] == REFUSED["reset_at"]
    assert decision["asks_dispatched"] == 0 and decision["asks_unroutable"] == 1 and decision["allow"] is True
    assert decision["status"] == "force_plan_unmet"


def test_an_exception_during_dispatch_leaves_an_attempted_ask_not_a_hold(harness):
    import ouroboros.review_substrate as rs

    def boom(request, **_kw):
        raise RuntimeError("substrate down")

    harness.monkeypatch.setattr(rs, "run_review_request", boom)
    ctx = harness.make_ctx(force_plan=True)
    _write(ctx, ask="council")
    rows = _record(harness)["asks"][0]["participants"]
    assert rows and all(p["status"] == "failed" and p["dispatched"] is False and "substrate down" in p["error"] for p in rows)
    decision = oh.force_plan_decision(ctx, {}, enforcement="blocking")
    assert decision["asks_unroutable"] == 1 and decision["allow"] is True and decision["status"] == "force_plan_unmet"
