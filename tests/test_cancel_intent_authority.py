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


# ---------------------------------------------------------------- A2 -------------------------

def test_a2_the_no_intent_boolean_facade_is_gone(qenv, monkeypatch):
    """cancel_task_by_id is cascade-only; the single shape ran custody without any intent."""
    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_k: None)
    qenv.q.PENDING[:] = [{"id": "facade", "chat_id": 1}]
    write_task_result(qenv.drive, "facade", "scheduled")
    with pytest.raises(ValueError):
        qenv.q.cancel_task_by_id("facade", cascade=False)
    assert not hasattr(qenv.q, "_cancel_task_by_id_single")
    assert [t["id"] for t in qenv.q.PENDING] == ["facade"]
    assert load_task_result(qenv.drive, "facade")["status"] == "scheduled"


def test_a2_cascade_stops_a_child_whose_own_mint_failed_only_under_the_root_claim(qenv, monkeypatch):
    """The one authority besides a task's own intent is the root's durable cascade
    intent, passed explicitly by the sweep. Without it a cascade touches nothing; with
    it a child whose intent write failed is still stopped, its forensic row kept."""
    import json

    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_k: None)
    qenv.q.PENDING[:] = [
        {"id": "tree", "chat_id": 1, "root_task_id": "tree"},
        {"id": "kid", "chat_id": 1, "root_task_id": "tree", "parent_task_id": "tree"},
    ]
    write_task_result(qenv.drive, "tree", "scheduled")
    write_task_result(qenv.drive, "kid", "scheduled", parent_task_id="tree", root_task_id="tree")
    real_request = ci.request_cancel

    def _unwritable_for_the_child(root, tid, **kw):
        if tid == "kid":
            raise OSError("intent store unwritable")
        return real_request(root, tid, **kw)

    monkeypatch.setattr(ci, "request_cancel", _unwritable_for_the_child)

    assert qenv.tl.cancel_task_by_id("tree", cascade=True) is False
    assert sorted(t["id"] for t in qenv.q.PENDING) == ["kid", "tree"]
    assert load_task_result(qenv.drive, "kid")["status"] == "scheduled"

    real_request(qenv.drive, "tree", scope=ci.SCOPE_CASCADE, reason="stop the tree")
    assert qenv.tl.cancel_task_by_id("tree", cascade=True) is True
    assert qenv.q.PENDING == []
    assert load_task_result(qenv.drive, "kid")["status"] == STATUS_CANCELLED
    assert load_task_result(qenv.drive, "tree")["status"] == STATUS_CANCELLED
    rows = [json.loads(line) for line in
            (qenv.drive / "logs" / "supervisor.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row.get("type") == "cascade_descendant_intent_write_failed" and row.get("task_id") == "kid"
               for row in rows)


def test_a2_retry_transfer_mints_no_successor_authority_after_an_empty_claim(qenv, monkeypatch):
    """The legacy retry-transfer lane runs before the main claim; an empty claim on the
    logical root must not mint an intent on the physical retry and tear it down."""
    from tests._cancel_intents_shared import _write_root_retry_pair

    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_k: None)
    monkeypatch.setattr(qenv.q, "QUEUE_MAX_RETRIES", 2)
    _write_root_retry_pair(qenv.drive, "old-root", "new-root")
    qenv.q.PENDING[:] = [{
        "id": "new-root", "type": "task", "chat_id": 0, "depth": 0, "root_task_id": "old-root",
        "parent_task_id": "", "delegation_role": "root", "original_task_id": "old-root",
        "timeout_retry_from": "old-root",
    }]

    assert qenv.tl.cancel_task_custody("old-root") == qenv.tl.CANCEL_NOT_FOUND
    assert ci.active_intents(qenv.drive) == {}
    assert [t["id"] for t in qenv.q.PENDING] == ["new-root"]

    minted = ci.request_cancel(qenv.drive, "old-root", reason="stop")
    assert minted["task_id"] == "new-root"
    assert qenv.tl.cancel_task_custody("new-root") == qenv.tl.CANCEL_CANCELLED
    assert qenv.q.PENDING == []
    assert load_task_result(qenv.drive, "new-root")["status"] == STATUS_CANCELLED


def test_a2_http_single_stop_admitted_after_liveness_probe_cannot_tear_down_without_an_intent(tmp_path, monkeypatch):
    """The product Stop path: the HTTP single cancel probes liveness, mints an intent only
    when the probe says live, then calls custody. A task admitted between the probe and
    custody reaches custody with NO durable intent; custody refuses (404, task untouched)."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from ouroboros.gateway.tasks import api_task_cancel
    from supervisor import queue, workers

    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue, "PENDING", [{"id": "late", "chat_id": 1, "root_task_id": "late"}])
    monkeypatch.setattr(queue, "RUNNING", {})
    monkeypatch.setattr(workers, "WORKERS", {}, raising=False)
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": None)
    monkeypatch.setattr(queue, "task_has_live_ownership", lambda *_a, **_k: False)
    monkeypatch.setattr(queue, "task_subtree_is_live", lambda *_a, **_k: False)
    write_task_result(tmp_path, "late", "scheduled")
    monkeypatch.setattr(queue, "_emit_cancel_task_done", lambda *_a, **_k: None)
    minted = []
    real_request = ci.request_cancel
    monkeypatch.setattr(ci, "request_cancel", lambda *a, **k: minted.append(a[1]) or real_request(*a, **k))
    app = Starlette(routes=[Route("/api/tasks/{task_id}/cancel", api_task_cancel, methods=["POST"])])
    app.state.drive_root = tmp_path
    with TestClient(app) as client:
        response = client.post("/api/tasks/late/cancel")
    assert minted == []
    assert response.status_code == 404
    assert load_task_result(tmp_path, "late")["status"] == "scheduled"
    assert [t["id"] for t in queue.PENDING] == ["late"]
