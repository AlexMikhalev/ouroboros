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

import pytest

from ouroboros import owner_hurry as oh
from ouroboros import plan_record, review_operation
from ouroboros.observability import persist_call
from ouroboros.review_dispatch import review_operation_binding, review_reconciliation_identity
from ouroboros.review_records import ReviewActorRecord
from ouroboros.tools import plan_review as pr
from tests._review_session_route_shared import fake_route  # noqa: F401 - pytest fixture (the offline delegated route)
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


# ---------------------------------------------------------------- D7 (option A): a v2 wave still owed at upgrade reaches the author as history

def test_a_v2_wave_still_owed_at_upgrade_reaches_the_author_as_history_without_writing_state(harness):
    """03 D7, the coordinator's option A: the earlier schema's owed wave is read through the same
    collector by the free READ and the gate pre-collection. A delegated seat still running in the
    engine stays owed (disclosed, holds nothing); once its run has finished its paid answer shows as
    read-only history ("not approval of the current revision") and the disclosed pending ask settles
    in memory; an API seat of the dead old process is unknown — named, never cleaned, never re-sent.
    The v2 state is never written; no objection or claim is created; nothing is sent."""
    import json

    from ouroboros.review_custody import _frozen_actor
    from ouroboros.review_records import ReviewRequest, ReviewRouteKind, ReviewSlot
    from tests._store_shared import read_result, seed_result
    from tests.test_plan_gate import _v2

    session = ReviewSlot(slot_id="old_a", model="cursor=grok", route=ReviewRouteKind.AGENT_SESSION, session_target="cursor=grok",
                         max_tokens=65536, default_temperature=0.2)
    api = ReviewSlot(slot_id="old_b", model="m/old", route=ReviewRouteKind.API_CHAT, max_tokens=65536, default_temperature=0.2)
    request = ReviewRequest(surface="plan_review", goal="the earlier plan review", task_id="task-1",
                            retry_key="plan:task-1:cycle-1", call_type="plan_review")
    rows = []
    for slot, operation, extra in ((session, "op-old-a", {"pending_invocation_id": "inv-old-a"}), (api, "op-old-b", {})):
        _persist_prompt(harness.drive, request, slot, operation)
        rows.append({"slot_id": slot.slot_id, "model": slot.model, "route": str(slot.route.value), "ok": False, "error": "",
                     "operation_id": operation, "operation_state": "pending_dispatch", "late_result_pending": True,
                     "recovery_binding": review_operation_binding(request, slot, operation), **extra})
    state = _v2(closed=False, blocking=())
    state["waves"][0].update(retry_key="plan:task-1:cycle-1", custody_pending=True, actors=rows)
    seed_result(harness.drive, "task-1", {"task_id": "task-1", "status": "running", "plan_review_state": state})
    before = json.dumps(read_result(harness.drive, "task-1")["plan_review_state"], sort_keys=True)
    finished = {"text": ""}

    def observe(root, request, slot, row, operation_id, *, controller_known=True):  # the engine's run, attach-only
        if not finished["text"]:
            return _frozen_actor(row, slot), review_operation.DEFERRED  # still running
        return ReviewActorRecord(slot_id=slot.slot_id, model=slot.model, status="ok", raw_text=finished["text"],
                                 operation_id=operation_id, operation_state="late_settled"), review_operation.COLLECTED

    harness.monkeypatch.setattr(review_operation, "_observe_delegated_review", observe)
    sub = harness.install({})
    ctx = harness.make_ctx()
    _write(ctx)  # revision 1 on the organ; the earlier wave is still owed
    text, _result = _call(ctx)
    owed = oh.force_plan_decision(ctx, {}, enforcement="blocking")
    assert "Legacy contributions" not in text and "legacy wave ffffffffffff: owed: old_a; unknown: old_b" in text
    assert owed["pending_asks"] == ["legacy wave " + "f" * 12] and owed["status"] == "awaiting" and owed["allow"] is True
    finished["text"] = "[]\nNO_FINDINGS: the earlier plan read fine."
    text, result = _call(ctx)
    assert "## Legacy contributions (history; not approval of the current revision)" in text
    assert "### old_a (cursor=grok) — answered the earlier plan review; read back from its retained source" in text
    assert "NO_FINDINGS: the earlier plan read fine." in text and "legacy wave ffffffffffff: unknown: old_b" in text
    assert result.meta["plan_open_objections"] == 0
    decision = oh.force_plan_decision(ctx, {}, enforcement="blocking")
    assert decision["pending_asks"] == [] and decision["open_objections"] == [] and decision["status"] == "clear"
    assert json.dumps(read_result(harness.drive, "task-1")["plan_review_state"], sort_keys=True) == before, "the v2 state is never written"
    assert sub.calls == [], "nothing was sent"
    legacy, _error = plan_record.legacy_facts(harness.drive, "task-1")
    assert legacy["done_when"] == [] and legacy["open_objections"] == [], "history creates no claim and no objection"


# ---------------------------------------------------------------- the organ leg of 03 P2: a refusal before the paid stamp never reached a reviewer

def _fail(message):
    def raise_error(*args, **kwargs):
        raise OSError(message)
    return raise_error


@pytest.mark.serial
@pytest.mark.parametrize("stage", ["gateway", "source", "checking", "budget"])
def test_a_refusal_raised_before_the_paid_stamp_is_never_a_reached_reviewer_on_the_organ(tmp_path, monkeypatch, fake_route, stage):  # noqa: F811
    """03 P2's organ leg, after M1–M4: the gateway, source, checking and budget refusals used to
    settle as a performed-but-failed seat (live) and recover as UNKNOWN (cold), so a Swarm
    force_plan counted them as a reached reviewer. Both readings are now the positive
    not_dispatched; the organ's facts withdraw the dispatch flag; the record reads the ask as
    attempted — disclosed, never held — and never as reached."""
    from dataclasses import replace

    from ouroboros import acceptance_retrieving, budget_pause, review_checking, review_execution, review_substrate, subagents
    from ouroboros.tools.plan_ask import reply_facts_from_row
    from ouroboros.tools.plan_review_runtime import _plan_row_from_actor
    from tests._review_session_route_shared import FakeLLM, _agent_request, _agent_slot

    request = _agent_request(task_id="presend", task_attempt=1, retry_key="one-review", session_root=str(tmp_path),
                             session_task="Weigh the plan.")
    slot = _agent_slot(session_access="readonly")
    if stage == "gateway":
        monkeypatch.setattr("ouroboros.claudexor_daemon.ensure_owned_gateway", _fail("gateway unavailable"))
    elif stage == "source":
        monkeypatch.setattr(acceptance_retrieving, "prepare_session_source", _fail("source unavailable"))
    elif stage == "checking":
        slot = replace(slot, session_access="workspace_write")
        monkeypatch.setattr(review_checking, "session_checking", _fail("checking unavailable"))
        monkeypatch.setattr(subagents, "route_health", lambda *a, **k: ("", ""))
    else:
        monkeypatch.setattr(budget_pause, "dispatch_fenced", lambda *_: True)
    stamps = []
    monkeypatch.setattr(review_execution, "invoke_review_paid_stamp", lambda stamp: stamps.append("paid"))
    ctx = SimpleNamespace(task_id="presend", root_task_id="presend", task_attempt=1, task_metadata={}, drive_root=tmp_path,
                          budget_drive_root=tmp_path)
    result = review_substrate.run_review_request(request, slots=[slot], drive_root=tmp_path, llm=FakeLLM(), usage_ctx=ctx)
    row = asdict(result.actors[0]) if not isinstance(result.actors[0], dict) else result.actors[0]
    assert stamps == [] and row["operation_state"] == "not_dispatched", "nothing was paid; the refusal is positive"
    original, original_slot = review_operation.recorded_review_assignment(
        tmp_path, task_id="presend", surface=request.surface, row=row, retry_key="one-review")
    recovered = review_operation.recover_review_producer(tmp_path, original, original_slot, row)
    assert recovered.operation_state == "not_dispatched" and sum(len(g.start_requests) for g in fake_route.instances) == 0
    live, cold = reply_facts_from_row(_plan_row_from_actor(row, slot)), reply_facts_from_row(_plan_row_from_actor(asdict(recovered), original_slot))
    for facts in (live, cold):
        assert facts["status"] == "not_dispatched" and facts["dispatched"] is False and facts["not_dispatched_reason"]
    seat = {"seat_id": slot.slot_id, "model": slot.model, "route": "agent_session", "owner_marked": True, "dispatched": True,
            "status": "pending", **cold}
    record = {**plan_record.empty_record(), "revisions": [{"n": 1, "ts": "t", "goal": "g", "plan": "p", "done_when": [],
                                                           "affected_paths": [], "body": False, "content_sha256": "0" * 64}],
              "asks": [{"ask_id": "a1", "ts": "t", "revision": 1, "message": "", "participants": [seat]}]}
    assert plan_record.asks_dispatched(record) == 0 and plan_record.unroutable_asks(record) == 1
    gate = plan_record.gate_projection(record, enforcement="blocking", cyber_pro=False, hurry=False, force_plan=True)
    assert gate["status"] == "force_plan_unmet" and gate["allow"] is True and gate["force_plan_met"] is False
    # The other direction: a seat that WAS dispatched and merely pending keeps the ask reached.
    reached = {**record, "asks": [{**record["asks"][0], "participants": [{**seat, "status": "pending", "dispatched": True}]}]}
    assert plan_record.asks_dispatched(reached) == 1 and plan_record.unroutable_asks(reached) == 0
