"""Port of Ouroboros contribution 03 "An already performed review reaches its author" onto the
planning organ (task d4ce874483ff407a, contribution tree HEAD aaa0bdbc2): already-paid answers
stay available after an interruption, an attempt change and the author's completion; they are
never re-paid; an old approval never passes as approval of a new plan; reading is never a
disposition; UNKNOWN is never cleaned. The organ's entries that collect — the free READ, the
gate pre-collection and the late-result hook — go through the shared recorded-actor collector,
over the real ``{op}_prompt`` / ``{op}_response`` CAS the substrate retains (the fake substrate
here writes the same records the real coordinator writes).
"""
from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

from ouroboros import owner_hurry as oh
from ouroboros import review_operation
from ouroboros.observability import persist_call
from ouroboros.review_dispatch import review_operation_binding, review_reconciliation_identity
from ouroboros.review_records import ReviewActorRecord
from ouroboros.tools import plan_review as pr
from tests.test_plan_task_tool import CLEAN, _call, _record, _reply, _slots, _write, harness as _harness

harness = _harness  # noqa: F811 - pytest fixture re-export


def _persist_prompt(drive, request, slot, operation):
    """What the real coordinator retains before any send: the exact request and slot under the
    operation's binding (``recorded_review_assignment`` proves a later collection against it)."""
    request.reconciliation_identity = review_reconciliation_identity(request, [slot], root_task_id=request.task_id)
    persist_call(drive, task_id=request.task_id, call_id=f"{operation}_prompt", call_type="plan_review_prompt",
                 payload={"request": asdict(request), "slot": asdict(slot)},
                 manifest={"review_operation_binding": review_operation_binding(request, slot, operation)})


def _complete_producer(drive, request, slot, operation, text):
    """The producer's complete outcome, retained after the worker that owned it is gone."""
    binding = review_operation_binding(request, slot, operation)
    actor = ReviewActorRecord(slot_id=slot.slot_id, model=slot.model, status="ok", raw_text=text, operation_id=operation,
                              recovery_binding=binding, usage={"cost": 0.01})
    persist_call(drive, task_id=request.task_id, call_id=f"{operation}_response", call_type="plan_review_response",
                 payload={"producer_outcome": asdict(actor), "message": {"content": text}, "usage": {"cost": 0.01}},
                 manifest={"producer_complete": True, "review_operation_binding": binding})


def _pending_row(slot, operation, **extra):
    return {"slot_id": slot.slot_id, "model": slot.model, "status": "error", "raw_text": "", "error": "", "usage": {},
            "prompt_ref": {}, "response_ref": {}, "operation_id": operation, "operation_state": "pending_dispatch",
            "late_result_pending": True, "awaiting_since": "2026-10-15T00:00:00+00:00", **extra}


def _settled_row(slot, operation, text):
    return {"slot_id": slot.slot_id, "model": slot.model, "status": "ok", "raw_text": text, "error": "", "usage": {"cost": 0.01},
            "prompt_ref": {}, "response_ref": {}, "operation_id": operation, "operation_state": "settled"}


class _Barrier:
    """A substrate that records every send, retains each seat's prompt CAS and leaves the seats
    it is told to hold pending at the barrier (the real custody's released rows)."""

    def __init__(self, drive, *, pending, settled=(), row_extra=None):
        self.drive, self.pending, self.settled, self.row_extra = drive, set(pending), dict(settled), dict(row_extra or {})
        self.calls, self.sent = [], {}

    def __call__(self, request, *, slots, drive_root, llm, usage_ctx=None):
        self.calls.append([s.slot_id for s in slots])
        actors = []
        for slot in slots:
            operation = f"op-{slot.slot_id}-{len(self.calls)}"
            _persist_prompt(drive_root, request, slot, operation)
            self.sent[slot.slot_id] = (request, slot, operation)
            if slot.slot_id in self.pending:
                actors.append(_pending_row(slot, operation, **self.row_extra.get(slot.slot_id, {})))
            else:
                actors.append(_settled_row(slot, operation, self.settled.get(slot.slot_id, CLEAN)))
        return SimpleNamespace(actors=actors)


def _install(harness, barrier):
    import ouroboros.review_substrate as rs

    harness.monkeypatch.setattr(rs, "run_review_request", barrier)
    return barrier


def _rows(harness, task_id="task-1"):
    return {p["seat_id"]: p for p in _record(harness, task_id)["asks"][0]["participants"]}


# ---------------------------------------------------------------- D6: the organ collects through the shared collector

def test_cold_collection_reads_a_delegated_seat_attach_only_and_names_a_dead_api_seat_unknown(harness):
    """03 D6 (probe P3 inverted): after a process loss a paid delegated seat whose answer
    exists in the engine reaches the author attach-only; a dead API seat is unresolved —
    neither a failure nor a reply, never re-sent; nothing is dispatched a second time."""
    harness.state["slots"] = _slots(("s1", "m/a", "session"), ("s2", "m/b"), ("s3", "m/c"))
    barrier = _install(harness, _Barrier(harness.drive, pending={"s1", "s2"}, row_extra={"s1": {"pending_invocation_id": "inv-s1"}}))
    observed = []

    def observe(root, request, slot, row, operation_id, *, controller_known=True):
        observed.append((operation_id, controller_known))
        return ReviewActorRecord(slot_id=slot.slot_id, model=slot.model, status="ok", operation_id=operation_id,
                                 operation_state="late_settled",
                                 raw_text=_reply("The delegated run finished.", objections=[("o1", "late objection")])), review_operation.COLLECTED

    harness.monkeypatch.setattr(review_operation, "_observe_delegated_review", observe)
    ctx = harness.make_ctx()
    _write(ctx, ask="council")
    assert {s: p["status"] for s, p in _rows(harness).items()} == {"s1": "pending", "s2": "pending", "s3": "answered"}
    cold = harness.make_ctx()  # a new process: no in-memory custody, a new attempt
    cold.task_attempt = 2
    text, _result = _call(cold)  # the free READ collects
    rows = _rows(harness)
    assert rows["s1"]["status"] == "answered" and rows["s1"]["reply"]["objections"] == [{"id": "o1", "text": "late objection"}]
    assert observed == [("op-s1-1", True)], "the delegated run was read once, attach-only"
    assert rows["s2"]["status"] == "pending" and rows["s2"]["unresolved"] is True and "never re-sent" in rows["s2"]["error"]
    assert rows["s2"]["failure_code"] == "review_custody_lost" and rows["s3"]["status"] == "answered"
    assert barrier.calls == [["s1", "s2", "s3"]], "one physical dispatch, ever"
    assert pr._decision(cold, _record(harness))["holding"] == ["s1:o1"], "the late objection holds like any other"
    assert "s2 (m/b): pending — outcome unknown" in text and "a new ask to it may pay twice" in text
    # Reading again changes nothing: the unknown seat is never cleaned and never re-sent.
    _call(cold)
    assert _rows(harness)["s2"]["unresolved"] is True and barrier.calls == [["s1", "s2", "s3"]]


def test_a_seat_a_live_controller_still_owns_is_deferred_not_read_as_dead(harness):
    """The other direction of D6: the recorded controller of another live process keeps the
    seat pending (not unresolved); the gate reads the same through its pre-collection."""
    harness.state["slots"] = _slots(("s1", "m/a"), ("s2", "m/b"))
    owner = {"pid": 424242, "birth": "2026-10-15T00:00:00", "custody_session": "other-process"}
    barrier = _install(harness, _Barrier(harness.drive, pending={"s1"}, row_extra={"s1": {"usage": {"review_controller": owner}}}))
    harness.monkeypatch.setattr(review_operation, "controller_state", lambda identity: "alive" if identity == owner else "dead")
    ctx = harness.make_ctx(force_plan=True)
    _write(ctx, ask="council")
    assert _rows(harness)["s1"]["review_controller"] == owner, "the owning controller rides the participant row"
    cold = harness.make_ctx(force_plan=True)
    cold.task_attempt = 2
    _call(cold)
    decision = oh.force_plan_decision(cold, {}, enforcement="blocking")  # the gate pre-collects the same way
    rows = _rows(harness)
    assert rows["s1"]["status"] == "pending" and not rows["s1"].get("unresolved") and rows["s2"]["status"] == "answered"
    assert decision["status"] == "awaiting" and decision["allow"] is True and decision["unknown_outcome"] == []
    assert barrier.calls == [["s1", "s2"]]
    # Once that controller is gone the same seat is read as unknown — and only then.
    harness.monkeypatch.setattr(review_operation, "controller_state", lambda identity: "dead")
    _call(cold)
    row = _rows(harness)["s1"]
    assert row["status"] == "pending" and row["unresolved"] is True and barrier.calls == [["s1", "s2"]]
    assert oh.force_plan_decision(cold, {}, enforcement="blocking")["unknown_outcome"] == ["a1"]
