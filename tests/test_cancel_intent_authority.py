"""Cancellation authority is the durable intent, and only the intent (invariant 14).

Phantom cancelled-subagent card in Main (14.10.2026): a ``cancel_task`` event drained
51 minutes late ran custody with no open intent, a busy ownership read was reported as
"changed", and the resulting "❌ cancel … did not settle" row named a child without
lineage in the owner's chat. Three owner decisions close the class:

- A1: the event is a silent wake of ``drive_cancel_intent_scope``; no intent is a typed
  no-op that touches nothing.
- A2: ``cancel_task_custody`` refuses without a claimed intent or an explicit root-cascade
  claim; the dead no-intent facade is gone.
- A3: the ownership read is one signed transaction outside the queue lock; under the lock
  the re-check is non-blocking and tri-state, and the retry chain is walked only when the
  task has ``superseded_by``.

Every test here fails with the corresponding change reverted and keeps the still-working
case beside it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ouroboros import cancel_intents as ci
from ouroboros.task_results import STATUS_CANCELLED, load_task_result, write_task_result
from tests._cancel_intents_shared import qenv as _qenv

qenv = _qenv


def _event_ctx(sent):
    return SimpleNamespace(load_state=lambda: {"owner_chat_id": 1},
                           send_with_budget=lambda *a, **k: sent.append((a, k)))


# ---------------------------------------------------------------- A1 -------------------------

def test_a1_cancel_event_without_open_intent_never_tears_down(qenv, monkeypatch):
    """A stale wake for a live task that nobody asked to cancel: nothing happens, nothing is said."""
    from supervisor.events_runtime_controls import _drive_cancel_task_event

    qenv.q.PENDING[:] = [{"id": "live-no-intent", "chat_id": 1}]
    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_k: None)
    write_task_result(qenv.drive, "live-no-intent", "scheduled")
    sent = []
    _drive_cancel_task_event({"task_id": "live-no-intent"}, _event_ctx(sent))
    assert [t["id"] for t in qenv.q.PENDING] == ["live-no-intent"]
    assert load_task_result(qenv.drive, "live-no-intent")["status"] == "scheduled"
    assert sent == []


def test_a1_wake_with_a_closed_intent_is_typed_and_touches_the_queue_not_at_all(qenv, monkeypatch):
    """The incident shape: the watchdog settled the intent an hour ago; the event arrives now.
    ``already_settled`` is the answer; a queued row for that id is NOT scrubbed here (custody
    owns ghost rows, under its own claim)."""
    custody_calls = []
    monkeypatch.setattr(qenv.q, "cancel_task_custody",
                        lambda tid, **kw: custody_calls.append(tid) or qenv.tl.CANCEL_CANCELLED)
    write_task_result(qenv.drive, "settled-child", STATUS_CANCELLED)
    qenv.q.PENDING[:] = [{"id": "settled-child", "chat_id": 1}]

    assert qenv.tl.drive_cancel_intent_scope("settled-child") == qenv.tl.CANCEL_ALREADY_SETTLED
    assert [t["id"] for t in qenv.q.PENDING] == ["settled-child"]
    assert custody_calls == []

    write_task_result(qenv.drive, "live-child", "scheduled")
    qenv.q.PENDING.append({"id": "live-child", "chat_id": 1})
    assert qenv.tl.drive_cancel_intent_scope("live-child") == qenv.tl.CANCEL_NOT_FOUND
    assert [t["id"] for t in qenv.q.PENDING] == ["settled-child", "live-child"]
    assert custody_calls == []


def test_a1_wake_with_an_open_intent_drives_custody_once_and_still_says_nothing(qenv, monkeypatch):
    from supervisor.events_runtime_controls import _drive_cancel_task_event

    custody_calls = []
    monkeypatch.setattr(qenv.q, "cancel_task_custody",
                        lambda tid, **kw: custody_calls.append(tid) or qenv.tl.CANCEL_CANCELLED)
    write_task_result(qenv.drive, "wanted", "scheduled")
    qenv.q.PENDING[:] = [{"id": "wanted", "chat_id": 1}]
    ci.request_cancel(qenv.drive, "wanted", reason="stop")
    sent = []
    _drive_cancel_task_event({"task_id": "wanted"}, _event_ctx(sent))
    assert custody_calls == ["wanted"]
    assert sent == []


def test_a1_wake_claimed_by_another_worker_is_silent_and_leaves_the_claim(qenv, monkeypatch):
    """The eighth phantom row: the event lost the claim race to the watchdog 10 s before
    settlement and announced a failure. A refused claim is a typed outcome the wake ignores."""
    from supervisor.events_runtime_controls import _drive_cancel_task_event

    write_task_result(qenv.drive, "raced", "scheduled")
    qenv.q.PENDING[:] = [{"id": "raced", "chat_id": 1}]
    ci.request_cancel(qenv.drive, "raced", reason="stop")
    owner = ci.claim_intent(qenv.drive, "raced", owner="watchdog")
    assert owner and not owner.get("claim_refused")
    sent = []
    _drive_cancel_task_event({"task_id": "raced"}, _event_ctx(sent))
    assert sent == []
    current = ci.active_intent(qenv.drive, "raced")
    assert (current["state"], current["claim_owner"], current["generation"]) == (
        ci.INTENT_CLAIMED, "watchdog", owner["generation"])
    assert [t["id"] for t in qenv.q.PENDING] == ["raced"]
    assert load_task_result(qenv.drive, "raced")["status"] == "scheduled"
