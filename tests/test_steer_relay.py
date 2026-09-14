"""steer_task relays the message it was given (#896).

A direct turn outlives its own origin message: it routes that message onward
and keeps receiving owner text through its mailbox. Two host rules used to
assume "this turn == the routing decision for its origin message": they
substituted the origin bytes for every steer the model issued, and keyed every
mailbox entry by that one origin message id. A turn relaying two new
instructions therefore re-sent a 20-minute-old message once and delivered
neither instruction. The exact-bytes transport now belongs to the turn's FIRST
routing act; after it, or after a later owner message arrives, the turn's own
words are delivered as written.
"""

from __future__ import annotations

import types

import pytest


def _tool_ctx(tmp_path, *, generation=None, task_id="turn-1", metadata=None):
    """A steer-issuing turn: no supervisor, so events land in pending_events."""
    agent = (
        types.SimpleNamespace(_owner_message_generation=generation)
        if generation is not None else None
    )
    return types.SimpleNamespace(
        pending_events=[],
        event_queue=None,
        current_chat_id=1,
        drive_root=tmp_path,
        task_id=task_id,
        task_metadata=dict(metadata or {}),
        owner_message_admission_agent=agent,
    )


def _supervisor_ctx(tmp_path, notices):
    return types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        RUNNING={"t-target": {"task": {"id": "t-target", "chat_id": 1}, "started_at": 1.0}},
        PENDING=[],
        send_with_budget=lambda _chat_id, text, *a, **k: notices.append(text),
    )


def _live_tool_ctx(tmp_path, supervisor_ctx, emitted, **kwargs):
    """A steer-issuing turn wired to a REAL supervisor handler, so the tool's
    durable receipt wait sees exactly what the handler persisted."""
    from supervisor.events import _handle_steer_task

    def _dispatch(event):
        emitted.append(event)
        _handle_steer_task(event, supervisor_ctx)

    ctx = _tool_ctx(tmp_path, **kwargs)
    ctx.event_queue = types.SimpleNamespace(put_nowait=_dispatch)
    return ctx


def test_a_turn_still_on_its_origin_message_steers_the_exact_owner_bytes(tmp_path):
    """Unchanged contract: no later owner message, so the model's paraphrase is
    replaced by the owner's ingress bytes and the receipt names that message."""
    from ouroboros.tools.control import _steer_task

    exact = "  publish the seven skills\nand merge them  "
    ctx = _tool_ctx(tmp_path, generation=0, metadata={
        "client_message_id": "cm-origin",
        "origin_message_text": exact,
    })

    _steer_task(ctx, "t-target", "model paraphrase")

    evt = ctx.pending_events[0]
    assert evt["message"] == exact
    assert evt["client_message_id"] == "cm-origin"


def test_a_turn_with_no_admission_agent_keeps_the_origin_substitution(tmp_path):
    """Internal/unit callers carry no admission agent: generation reads as zero."""
    from ouroboros.tools.control import _steer_task

    ctx = _tool_ctx(tmp_path, metadata={
        "client_message_id": "cm-origin",
        "origin_message_text": "owner bytes",
    })

    _steer_task(ctx, "t-target", "model paraphrase")

    assert ctx.pending_events[0]["message"] == "owner bytes"
    assert ctx.pending_events[0]["client_message_id"] == "cm-origin"


def test_a_turn_that_already_routed_its_origin_speaks_for_itself(tmp_path, monkeypatch):
    """The promote carried the owner's message; the pacing note that follows it
    twenty minutes later is the turn's OWN text, and must reach the task as
    written instead of re-sending the message the promote already delivered.

    Delivery still CONFIRMS: an agent-authored steer earns its receipt under a
    synthetic id, so a landed mailbox write is never reported as unconfirmed.
    The owner's message keeps the promote receipt a later decision turn reads.
    """
    import supervisor.queue as queue
    from ouroboros.owner_mailbox import drain_owner_entries
    from ouroboros.project_dialogue import append_chat_annotation, latest_chat_annotations
    from ouroboros.tools.control import _steer_task

    monkeypatch.setattr(queue, "DRIVE_ROOT", str(tmp_path))
    append_chat_annotation(
        tmp_path, "cm-origin", action="promote_chat_to_task",
        target="t-target", status="scheduled", routing_token="tok-promote",
    )
    notices, emitted = [], []
    ctx = _live_tool_ctx(
        tmp_path, _supervisor_ctx(tmp_path, notices), emitted, generation=0, metadata={
            "client_message_id": "cm-origin",
            "origin_message_text": "publish the seven skills",
        },
    )

    out = _steer_task(ctx, "t-target", "Pacing checkpoint: keep the PR small")

    assert "durably confirmed" in out and "UNCONFIRMED" not in out
    assert emitted[0]["message"] == "Pacing checkpoint: keep the PR small"
    assert emitted[0]["client_message_id"] == f"agent-steer:{emitted[0]['routing_token']}"
    assert [e["text"] for e in drain_owner_entries(tmp_path, "t-target")] == [
        "Pacing checkpoint: keep the PR small",
    ]
    annotations = latest_chat_annotations(tmp_path)
    # The owner's own message still carries the act that DID relay its bytes.
    assert annotations["cm-origin"]["action"] == "promote_chat_to_task"
    assert annotations["cm-origin"]["routing_token"] == "tok-promote"
    # The steer's receipt lives under its own id, on no chat message.
    synthetic = annotations[emitted[0]["client_message_id"]]
    assert synthetic["action"] == "steer_task"
    assert synthetic["status"] == "delivered"
    assert synthetic["target"] == "t-target"
    assert synthetic["routing_token"] == emitted[0]["routing_token"]
    assert notices == []


@pytest.mark.parametrize("status", ["needs_manual_target", "unconfirmed"])
def test_a_refused_first_act_leaves_the_owner_message_unrouted(tmp_path, status):
    """A promote that did not land carried nothing, so the next act still
    relays the owner's exact bytes (the picker dispatches them the same way)."""
    from ouroboros.project_dialogue import append_chat_annotation
    from ouroboros.tools.control import _steer_task

    append_chat_annotation(
        tmp_path, "cm-origin", action="promote_chat_to_task",
        target="rejected-root", status=status, routing_token="tok-promote",
    )
    ctx = _tool_ctx(tmp_path, generation=0, metadata={
        "client_message_id": "cm-origin",
        "origin_message_text": "publish the seven skills",
    })

    _steer_task(ctx, "t-target", "model paraphrase")

    evt = ctx.pending_events[0]
    assert evt["message"] == "publish the seven skills"
    assert evt["client_message_id"] == "cm-origin"


def test_another_messages_routing_receipt_does_not_end_this_turns_window(tmp_path):
    """The receipt must be on THIS turn's origin message, not any routed one."""
    from ouroboros.project_dialogue import append_chat_annotation
    from ouroboros.tools.control import _steer_task

    append_chat_annotation(
        tmp_path, "cm-somebody-else", action="promote_chat_to_task",
        target="other-root", status="scheduled",
    )
    ctx = _tool_ctx(tmp_path, generation=0, metadata={
        "client_message_id": "cm-origin",
        "origin_message_text": "publish the seven skills",
    })

    _steer_task(ctx, "t-target", "model paraphrase")

    assert ctx.pending_events[0]["message"] == "publish the seven skills"
    assert ctx.pending_events[0]["client_message_id"] == "cm-origin"


def test_a_relaying_turn_delivers_the_message_it_was_given(tmp_path):
    """After a later owner message was admitted, the turn is relaying: its own
    text is delivered and the receipt moves off the origin message."""
    from ouroboros.project_dialogue import append_chat_annotation
    from ouroboros.tools.control import _steer_task

    append_chat_annotation(
        tmp_path, "cm-later", action="mailbox_delivery",
        target="turn-1", status="delivered",
    )
    ctx = _tool_ctx(tmp_path, generation=1, metadata={
        "client_message_id": "cm-origin",
        "origin_message_text": "the twenty-minute-old owner message",
    })

    _steer_task(ctx, "t-target", "Anton says you may ask questions")

    evt = ctx.pending_events[0]
    assert evt["message"] == "Anton says you may ask questions"
    assert evt["client_message_id"] == "cm-later"


def test_a_relaying_turn_with_no_delivery_receipt_uses_its_own_receipt_id(tmp_path):
    """No mailbox-delivery receipt for this task: the steer borrows no owner
    message id, and still keeps a confirmable receipt of its own."""
    from ouroboros.tools.control import _steer_task

    ctx = _tool_ctx(tmp_path, generation=2, metadata={
        "client_message_id": "cm-origin",
        "origin_message_text": "the twenty-minute-old owner message",
    })

    _steer_task(ctx, "t-target", "the new instruction")

    evt = ctx.pending_events[0]
    assert evt["message"] == "the new instruction"
    assert evt["client_message_id"] == f"agent-steer:{evt['routing_token']}"


def test_a_mailbox_receipt_for_another_task_is_not_this_turns_message(tmp_path):
    """The receipt must name THIS task; a sibling turn's follow-up is not ours."""
    from ouroboros.project_dialogue import append_chat_annotation
    from ouroboros.tools.control import _steer_task

    append_chat_annotation(
        tmp_path, "cm-sibling", action="mailbox_delivery",
        target="another-turn", status="delivered",
    )
    ctx = _tool_ctx(tmp_path, generation=1, metadata={"client_message_id": "cm-origin"})

    _steer_task(ctx, "t-target", "the new instruction")

    evt = ctx.pending_events[0]
    assert evt["client_message_id"] == f"agent-steer:{evt['routing_token']}"


def test_two_relayed_instructions_reach_the_mailbox_as_two_messages(tmp_path, monkeypatch):
    """Same owner message id and target, two distinct steers: the routing token
    keeps them apart, so the second instruction is no longer deduplicated away."""
    import supervisor.queue as queue
    from ouroboros.owner_mailbox import drain_owner_entries
    from supervisor.events import _handle_steer_task

    monkeypatch.setattr(queue, "DRIVE_ROOT", str(tmp_path))
    notices = []
    ctx = _supervisor_ctx(tmp_path, notices)

    def _evt(message, token):
        return {
            "type": "steer_task", "target_task_id": "t-target", "message": message,
            "chat_id": 1, "client_message_id": "cm-later", "routing_token": token,
        }

    _handle_steer_task(_evt("pacing checkpoint", "token-a"), ctx)
    _handle_steer_task(_evt("Anton may be asked questions", "token-b"), ctx)

    assert [entry["text"] for entry in drain_owner_entries(tmp_path, "t-target")] == [
        "pacing checkpoint", "Anton may be asked questions",
    ]


def test_the_same_steer_retried_is_still_delivered_once(tmp_path, monkeypatch):
    """The token rides the event by value, so a retried emit collides as before."""
    import supervisor.queue as queue
    from ouroboros.owner_mailbox import drain_owner_entries
    from supervisor.events import _handle_steer_task

    monkeypatch.setattr(queue, "DRIVE_ROOT", str(tmp_path))
    notices = []
    ctx = _supervisor_ctx(tmp_path, notices)
    evt = {
        "type": "steer_task", "target_task_id": "t-target", "message": "steer me",
        "chat_id": 1, "client_message_id": "cm-later", "routing_token": "token-a",
    }

    _handle_steer_task(evt, ctx)
    _handle_steer_task(dict(evt), ctx)

    assert [entry["text"] for entry in drain_owner_entries(tmp_path, "t-target")] == ["steer me"]


@pytest.mark.parametrize("client_message_id", ["cm-later", ""])
def test_a_tokenless_steer_keeps_the_legacy_mailbox_key(tmp_path, monkeypatch, client_message_id):
    """An event without a routing token (older producers, direct handler calls)
    keeps the previous client-id+target key and its retry dedup."""
    import supervisor.queue as queue
    from ouroboros.owner_mailbox import drain_owner_entries
    from supervisor.events import _handle_steer_task

    monkeypatch.setattr(queue, "DRIVE_ROOT", str(tmp_path))
    notices = []
    ctx = _supervisor_ctx(tmp_path, notices)
    evt = {
        "type": "steer_task", "target_task_id": "t-target", "message": "steer me",
        "chat_id": 1, "client_message_id": client_message_id,
    }

    _handle_steer_task(evt, ctx)
    _handle_steer_task(dict(evt), ctx)

    delivered = [entry["text"] for entry in drain_owner_entries(tmp_path, "t-target")]
    # A missing client id has always used a fresh unique key (no false dedup).
    assert delivered == (["steer me"] if client_message_id else ["steer me", "steer me"])
