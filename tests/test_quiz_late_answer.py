"""The LATE owner quiz answer (owner decision В17a=A, retiring 30=A).

A card no longer dies with its author: after the task-done seam has flipped the
block to ``expired_terminal``, the ingress still ACCEPTS an answer, stamps
``answered_after_terminal`` for audit, and — because no mailbox will ever be
drained — delivers the recorded answer into the card's own chat as the owner's
own message through the NAMED ingress ``message_bus.accept_local_message``
(idempotent per ``client_message_id``). The ingress-side siblings of these
invariants live in ``tests/test_quiz_answer.py``; the durable evidence half is
``tests/test_quiz_history_evidence.py``.
"""

from __future__ import annotations

import json

import pytest

from ouroboros.owner_quiz import (
    STATE_ANSWERED,
    quiz_states,
    reconcile_terminal,
    record_asked,
)
from tests.test_quiz_answer import _decision_app, _post


def _late_bridge(tmp_path, monkeypatch):
    """A real bridge for the late-answer path: named ingress plus the WS echo."""
    import supervisor.message_bus as mb

    bridge = mb.LocalChatBridge()
    frames = []
    bridge._broadcast_fn = frames.append
    monkeypatch.setattr(mb, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mb, "_BRIDGE", bridge)
    return bridge, frames


def _inbox(bridge):
    queued = []
    while True:
        try:
            queued.append(bridge._inbox.get_nowait())
        except Exception:
            return queued


def _accepted_rows(tmp_path, client_message_id):
    return [row for row in (json.loads(line) for line in
                            (tmp_path / "logs" / "chat.jsonl").read_text().splitlines())
            if row.get("client_message_id") == client_message_id]


def test_ingress_late_answer_is_accepted_and_delivered_as_an_owner_message(tmp_path, monkeypatch):
    """Owner decision В17a=A (retiring 30=A): the card outlives its author.

    A late answer is RECORDED on the expired block (with the audit flag) and,
    because no mailbox will ever be drained, delivered into the card's own chat
    as the owner's own message through the NAMED ingress — whose canonical row
    is the acceptance receipt and whose client_message_id makes a retry rejoin
    instead of enqueueing a second time."""
    record_asked(tmp_path, "task-1", quiz_id="q1", question="Which db?",
                 options=["sqlite", "postgres"], assumption="sqlite meanwhile", chat_id=1)
    reconcile_terminal(tmp_path, "task-1")  # the task settled
    bridge, frames = _late_bridge(tmp_path, monkeypatch)
    app = _decision_app(tmp_path, monkeypatch, live_task=None)
    resp = _post(app, {"request_id": "r1", "decision_id": "quiz:task-1:q1",
                       "option_index": 1, "comment": "prod parity"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == STATE_ANSWERED and body["answered_index"] == 1
    assert body["answered_after_terminal"] is True and body["forwarded"] is True
    block = quiz_states(tmp_path, "task-1")["q1"]
    assert block["state"] == STATE_ANSWERED and block["answered_after_terminal"] is True

    source_id = "quiz_late_answer:task-1:q1"
    [queued] = _inbox(bridge)
    assert queued["chat_id"] == 1 and queued["user_id"] == 1 and queued["source"] == "web"
    assert queued["client_message_id"] == source_id
    # The FULL existing answer frame, nothing trimmed; provenance is its own
    # field and the real transport's client_surface is never substituted.
    assert "[Owner quiz answer]" in queued["text"] and "Which db?" in queued["text"]
    assert "prod parity" in queued["text"] and "postgres" in queued["text"]
    assert queued["task_metadata"]["late_answer"] == {"task_id": "task-1", "quiz_id": "q1"}
    assert "client_surface" not in queued["task_metadata"]
    # The same user bubble the owner's own typing produces.
    echo = [f for f in frames if f.get("type") == "chat" and f.get("role") == "user"]
    assert len(echo) == 1 and echo[0]["client_message_id"] == source_id
    assert echo[0]["chat_id"] == 1 and echo[0]["content"] == queued["text"]
    assert len(_accepted_rows(tmp_path, source_id)) == 1

    # A retry of the SAME request re-enters delivery; the named ingress rejoins.
    again = _post(app, {"request_id": "r1", "decision_id": "quiz:task-1:q1",
                        "option_index": 1, "comment": "prod parity"})
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert again.json()["forwarded"] is True
    assert _inbox(bridge) == [] and len(_accepted_rows(tmp_path, source_id)) == 1
    assert len([f for f in frames if f.get("type") == "chat"]) == 1


def test_an_answer_the_live_task_received_is_not_forwarded_when_retried_after_it_ended(tmp_path, monkeypatch):
    """A lost HTTP response and the UI's retry of the SAME request after the task ended
    must not turn an answer the task already received into a second owner turn: delivery
    follows the persisted acceptance route (answered_after_terminal), not liveness now."""
    record_asked(tmp_path, "task-2", quiz_id="q1", question="Which db?",
                 options=["sqlite", "postgres"], assumption="sqlite meanwhile", chat_id=1)
    bridge, frames = _late_bridge(tmp_path, monkeypatch)
    live = _decision_app(tmp_path, monkeypatch, live_task={"id": "task-2"})
    first = _post(live, {"request_id": "r2", "decision_id": "quiz:task-2:q1", "option_index": 0})
    assert first.status_code == 200, first.text
    assert first.json()["duplicate"] is False and not first.json().get("forwarded")
    assert "answered_after_terminal" not in quiz_states(tmp_path, "task-2")["q1"]
    reconcile_terminal(tmp_path, "task-2")  # the task ended with the answer in its mailbox
    gone = _decision_app(tmp_path, monkeypatch, live_task=None)
    again = _post(gone, {"request_id": "r2", "decision_id": "quiz:task-2:q1", "option_index": 0})
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert not again.json().get("forwarded")
    assert _inbox(bridge) == [] and not [f for f in frames if f.get("type") == "chat"]


def test_a_relayed_late_answer_keeps_the_relaying_skill_as_its_source(tmp_path, monkeypatch):
    """A late answer relayed by a transport skill (Telegram) is the owner's message from THAT
    transport, never a web message (astra round 3)."""
    import asyncio

    from ouroboros.gateway import task_decision as td

    record_asked(tmp_path, "task-3", quiz_id="q1", question="Which db?",
                 options=["sqlite", "postgres"], assumption="sqlite meanwhile", chat_id=1)
    reconcile_terminal(tmp_path, "task-3")
    bridge, frames = _late_bridge(tmp_path, monkeypatch)
    _decision_app(tmp_path, monkeypatch, live_task=None)  # binds the ingress's task lookup
    status, body = asyncio.run(td.answer_decision(
        tmp_path, {"request_id": "r3", "decision_id": "quiz:task-3:q1", "option_index": 1},
        source="skill:telegram"))
    assert status == 200 and body["forwarded"] is True
    [queued] = _inbox(bridge)
    assert queued["source"] == "skill:telegram" and queued["chat_id"] == 1
    echo = [f for f in frames if f.get("type") == "chat" and f.get("role") == "user"]
    assert echo and echo[0].get("source") == "skill:telegram"


def test_ingress_heals_an_unreconciled_quiz_of_a_dead_task(tmp_path, monkeypatch):
    """Crash window: the author died before the task-done seam expired its open
    quiz. The ingress still heals the lifecycle first — so the accepted late
    answer carries answered_after_terminal — and no mailbox control is written
    for a task nobody will drain."""
    from ouroboros.owner_mailbox import drain_owner_entries

    record_asked(tmp_path, "task-1", quiz_id="q1", question="?", options=["A", "B"],
                 assumption="a", chat_id=1)
    _late_bridge(tmp_path, monkeypatch)
    app = _decision_app(tmp_path, monkeypatch, live_task=None)
    resp = _post(app, {"request_id": "r1", "decision_id": "quiz:task-1:q1",
                       "option_index": 0})
    assert resp.status_code == 200, resp.text
    assert resp.json()["answered_after_terminal"] is True
    block = quiz_states(tmp_path, "task-1")["q1"]
    assert block["state"] == STATE_ANSWERED and block["reconciled_at"]
    assert not drain_owner_entries(tmp_path, "task-1", set())


def test_late_answer_addresses_the_task_row_when_the_block_predates_chat_ids(tmp_path, monkeypatch):
    """An older block has no stored chat_id: the delivery falls back to the same
    addressing the answer's own history row already uses."""
    from ouroboros.task_results import write_task_result

    write_task_result(tmp_path, "task-1", "completed", chat_id=1)
    record_asked(tmp_path, "task-1", quiz_id="q1", question="?", options=["A", "B"],
                 assumption="a")
    assert "chat_id" not in quiz_states(tmp_path, "task-1")["q1"]
    bridge, _frames = _late_bridge(tmp_path, monkeypatch)
    app = _decision_app(tmp_path, monkeypatch, live_task=None)
    resp = _post(app, {"request_id": "r1", "decision_id": "quiz:task-1:q1",
                       "option_index": 0})
    assert resp.status_code == 200 and resp.json()["forwarded"] is True
    [queued] = _inbox(bridge)
    assert queued["chat_id"] == 1


@pytest.mark.parametrize("chat_id,reason", [(-1001, "a2a_chat"), (0, "hidden_chat")])
def test_late_answer_to_a_machine_or_hidden_chat_is_recorded_but_not_forwarded(
    tmp_path, monkeypatch, chat_id, reason,
):
    """Canonical chat-id policy: synthetic A2A traffic has no owner turn to
    start and the hidden partition is a destination no surface reads. The answer
    is still recorded; the reply says honestly that nothing was delivered."""
    record_asked(tmp_path, "task-1", quiz_id="q1", question="?", options=["A", "B"],
                 assumption="a", chat_id=chat_id)
    reconcile_terminal(tmp_path, "task-1")
    bridge, frames = _late_bridge(tmp_path, monkeypatch)
    app = _decision_app(tmp_path, monkeypatch, live_task=None)
    resp = _post(app, {"request_id": "r1", "decision_id": "quiz:task-1:q1",
                       "option_index": 0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answered_after_terminal"] is True and body["forwarded"] is False
    assert body["reason_code"] == reason
    assert quiz_states(tmp_path, "task-1")["q1"]["state"] == STATE_ANSWERED
    assert _inbox(bridge) == [] and not [f for f in frames if f.get("type") == "chat"]


def test_a_second_answer_to_a_settled_card_is_still_a_first_wins_409(tmp_path, monkeypatch):
    """First-wins is untouched by the late path: only the transition from
    expired to answered delivers, and a competing request learns the winner."""
    record_asked(tmp_path, "task-1", quiz_id="q1", question="?", options=["A", "B"],
                 assumption="a", chat_id=1)
    reconcile_terminal(tmp_path, "task-1")
    bridge, _frames = _late_bridge(tmp_path, monkeypatch)
    app = _decision_app(tmp_path, monkeypatch, live_task=None)
    assert _post(app, {"request_id": "r1", "decision_id": "quiz:task-1:q1",
                       "option_index": 1}).status_code == 200
    _inbox(bridge)
    loser = _post(app, {"request_id": "r2", "decision_id": "quiz:task-1:q1",
                        "option_index": 0})
    assert loser.status_code == 409
    # Never a fabricated expiry: the true state is what the card settles on.
    assert loser.json()["state"] == STATE_ANSWERED
    assert loser.json()["answered_index"] == 1
    assert _inbox(bridge) == []
