"""Named ingress acceptance: one canonical inbound row per message id, its dispatch an obligation.

The owner of a named owner/skill message's acceptance (ARCHITECTURE 04 "WebSocket chat", 12
"Operation correlation"). The accepted row, its receipt and the dispatch it owes are ONE
transaction in ``state/dialog.sqlite`` (``dialogue_store.append``): the row lands with the
obligation ``pending``, ``claim_dispatch`` is the durable "entered" mark taken by exactly one
caller -- the acceptor, a same-id retry, or ``recover_pending_dispatches`` after a crash -- and
``complete_dispatch`` records the handoff's return. A same-id redelivery rejoins the accepted row
when its words and ordered attachment content match and is refused otherwise; it dispatches only
by taking a dispatch still owed (a crash between the commit and the handoff), never by guessing.
``_INGRESS_LOCK`` serializes check -> row -> dispatch inside one process; the store's UNIQUE index
settles the race between processes. Which row an id names is the store's answer
(``dialogue_store.accepted_row``): while the chat archive is still importing, a miss is checked
in the segments not yet imported, so unknown is never "new". ``supervisor.message_bus``
re-exports the public names; its ``DATA_DIR`` and ``accept_chat`` are read at call time.

Dispatch facts are durable now, so an ended process's row keeps its state: ``pending`` means
the handoff never happened (the recovery or a retry performs it once), ``dispatched`` means it
began and its outcome is ambiguous (never replayed), ``completed`` that it returned. The old
"undispatched" proof (a write that raised after landing) cannot exist: the write is atomic.
"""

from __future__ import annotations

import functools
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from ouroboros import dialogue_store
from ouroboros.chat_uploads import attachment_views, same_message, stored_refs
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

_INGRESS_LOCK = threading.Lock()
# The dispatchers an obligation can name: replayable from the recorded input and the stored row.
_DISPATCHERS: Dict[str, Callable[..., Any]] = {}


def _bus_dispatch(bridge: Any, text: str, **message: Any) -> None:
    bridge.enqueue_local_message(text, **message)


def _dispatchers() -> Dict[str, Callable[..., Any]]:
    if not _DISPATCHERS:
        from ouroboros.server_control import dispatch_accepted_restart

        _DISPATCHERS.update({"bus": _bus_dispatch, "accepted_restart": dispatch_accepted_restart})
    return _DISPATCHERS


def dispatcher_name(dispatch: Any) -> Optional[str]:
    """The name an obligation records for ``dispatch``: the bus (``None``), the host's accepted
    Restart dispatcher, or ``None`` for a caller's own callback (dispatched now, never replayed)."""
    if dispatch is None:
        return "bus"
    target = dispatch.func if isinstance(dispatch, functools.partial) else dispatch
    return next((name for name, known in _dispatchers().items() if known is target and name != "bus"), None)


def obligation(name: Optional[str], text: str, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The recorded input of a replayable dispatch (bytes ride as the base64 text they already are)."""
    if name is None:
        return None
    return {"dispatcher": name, "text": text,
            "message": {key: value for key, value in message.items() if key not in ("accepted_source_ref", "accepted_source_row")}}


def _root(drive_root: Any = None) -> Optional[Path]:
    from supervisor import message_bus

    root = drive_root if drive_root is not None else message_bus.DATA_DIR
    return None if root is None else Path(root)


def _named(row: Dict[str, Any]) -> Tuple[Optional[int], str]:
    try:
        chat_id = int(row.get("chat_id")) if row.get("chat_id") not in (None, "") else None
    except (TypeError, ValueError):
        chat_id = None
    return chat_id, str(row.get("client_message_id") or "")


def _process() -> str:
    from ouroboros.process_custody import current_custody_session_id

    return current_custody_session_id()


def dispatch_facts(row: Dict[str, Any], drive_root: Any = None) -> Optional[Dict[str, Any]]:
    """The row's durable dispatch facts (``dialogue_store.dispatch_facts``): ``state``, the
    ``process`` that took it, its ``dispatcher``; ``None`` for a row that never owed one."""
    root = _root(drive_root)
    if root is None or not isinstance(row, dict):
        return None
    chat_id, client_message_id = _named(row)
    return dialogue_store.dispatch_facts(root, chat_id, client_message_id)


def _entered_here(facts: Optional[Dict[str, Any]]) -> bool:
    return bool(facts) and facts["state"] in ("dispatched", "completed") and facts["process"] == _process()


def dispatch_entered(row: dict, drive_root: Any = None) -> bool:
    """The handoff began in THIS process (whose queue holds it); another process's entry is
    unknown here, as it was before the store."""
    return _entered_here(dispatch_facts(row, drive_root))


def dispatch_completed(row: dict, drive_root: Any = None) -> bool:
    facts = dispatch_facts(row, drive_root)
    return _entered_here(facts) and facts["state"] == "completed"


def dispatch_taken_over_here(row: dict, drive_root: Any = None) -> bool:
    """THIS process took the row's dispatch after another process accepted it (a retry of an owed
    dispatch, or the recovery): the work is live here whatever session accepted the row."""
    facts = dispatch_facts(row, drive_root)
    return _entered_here(facts) and facts["process"] != str((row or {}).get("ingress_process") or "")


def dispatch_lost(row: dict, drive_root: Any = None) -> bool:
    """The row's dispatch began in a process that ended: its queue died with it. A row still
    ``pending`` is not lost (the recovery or a retry performs it); a legacy row without a dispatch
    fact is lost when the process that accepted it ended."""
    facts = dispatch_facts(row, drive_root)
    if facts is None:
        return bool((row or {}).get("ingress_process")) and not accepted_here(row)
    return facts["state"] in ("dispatched", "completed") and facts["process"] != _process()


def accepted_here(row: dict) -> bool:
    """True when THIS host process wrote that named acceptance row (``accept_chat`` stamps the process
    generation as ``ingress_process``). This proves acceptance only; the dispatch fact is the row's."""
    stamp = str((row or {}).get("ingress_process") or "")
    return bool(stamp) and stamp == _process()


def delivery_facts(row: dict, chat_id: int) -> dict:
    """What the store knows of an accepted row's dispatch, as its echo and history row say it.

    ``ingress_pending``: the dispatch is still owed (the acceptor is between the commit and the
    handoff, or a crash left it: the recovery or a same-id retry performs it once), or this
    process accepted a row for its own dispatcher and has not entered it yet;
    ``ingress_dispatched``: the handoff began in THIS process. Nothing: a row that owes no bus
    dispatch, an imported one, an abandoned obligation, or a dispatch an ended process took
    (its outcome is unknown here, as before the store)."""
    facts = dispatch_facts({**(row or {}), "chat_id": chat_id})
    if facts is not None and (facts["state"] == "pending" or (facts["state"] == "none" and accepted_here(row))):
        return {"ingress_pending": True}
    if _entered_here(facts):
        return {"ingress_dispatched": True}
    return {}


# --- which accepted row an id already names --------------------------------------------------

def accepted_chat_message(drive_root, chat_id: int, client_message_id: str) -> Optional[dict]:
    """The named inbound row ``(chat_id, client_message_id)`` already names (the newest; the answer
    a full scan of the retained chain gave). A store that cannot answer raises typed
    (``store.StoreError``), never None."""
    found = dialogue_store.accepted_row(Path(drive_root), chat_id, client_message_id)
    return None if found is None else dict(found.row)


def rejoin_or_refuse(row: dict, source: str, logged: str, refs: Any) -> None:
    """A redelivered frame may rejoin the accepted row only as the same message of the same source."""
    if row.get("source") != source:
        raise ValueError("client_message_id is already bound to another source")
    if not same_message(row, logged, refs):  # text AND ordered attachment content
        raise ValueError("client_message_id was already used for a different message")


def hand_over(root: Path, chat_id: int, message_id: str, name: Optional[str]) -> bool:
    """The durable "entered" mark before the handoff, stamped with this process: the owed
    dispatch (one winner) or the caller's own dispatcher's entry."""
    if name is None:
        return dialogue_store.note_dispatched(root, chat_id, message_id, process=_process())
    return dialogue_store.claim_dispatch(root, chat_id, message_id, process=_process())


def accept_local_message(bridge, drive_root, text: str, *, retain_inputs=None, dispatch=None, **message) -> tuple[dict, bool]:
    """Accept a named skill delivery once, then schedule/queue its exact source: ``(row, rejoined)``.
    The ingress lock serializes receipt-before-dispatch. A same-id retry rejoins without dispatch
    unless the row's dispatch is still owed (``pending``: the acceptor died before the handoff):
    then it dispatches, not ``rejoined``. ``retain_inputs`` takes custody of the request's copies
    once a write is attempted or a row is handed over; a refusal before that adopts nothing.
    """
    from ouroboros.project_dialogue import build_owner_message_ref
    from ouroboros.chat_uploads import attachment_placeholder
    from ouroboros.owner_quiz import late_answer_ref
    from supervisor import message_bus

    root = Path(drive_root)
    chat_id = int(message["chat_id"])
    message_id = str(message["client_message_id"])
    source = str(message["source"])
    logged = text.strip() or str(message.get("image_caption") or "").strip() or attachment_placeholder(
        message.get("image_base64"), message.get("task_metadata")
    )
    if not logged:
        raise ValueError("message is empty")
    refs = stored_refs((message.get("task_metadata") or {}).get("chat_attachments"))
    late_answer = late_answer_ref((message.get("task_metadata") or {}).get("late_answer"))
    placeholder = not text.strip() and not str(message.get("image_caption") or "").strip()
    name = dispatcher_name(dispatch)
    with _INGRESS_LOCK:
        row = accepted_chat_message(root, chat_id, message_id)
        fresh = attempted = False
        if row is None:
            attempted = True
            try:
                receipt = message_bus.accept_chat(
                    chat_id, int(message.get("user_id") or 0), logged, ts=utc_now_iso(), source=source,
                    client_message_id=message_id, sender_label=str(message.get("sender_label") or ""),
                    transport=message.get("transport"), drive_root=root,
                    message_meta={"attachments": refs, "text_placeholder": placeholder,
                                  **({"late_answer": late_answer} if late_answer else {})},
                    dispatch=obligation(name, text, message))
            finally:
                # Once the write is attempted, failure may have left the canonical row: transfer input
                # custody without claiming acceptance or queue success.
                if retain_inputs is not None:
                    retain_inputs()
            row, fresh = receipt.row, not receipt.duplicate
        if fresh:
            hand_over(root, chat_id, message_id, name)
        else:
            # The row an earlier delivery landed (or the race lost by a hair): rejoin it, or take its owed dispatch.
            rejoin_or_refuse(row, source, logged, refs)
            saved_answer = late_answer_ref(row.get("late_answer"))
            if saved_answer:
                message["task_metadata"] = {**(message.get("task_metadata") or {}), "late_answer": saved_answer}
            if not dialogue_store.claim_dispatch(root, chat_id, message_id, process=_process()):
                return row, True
            # Its dispatch was still owed: this retry's identical copies feed it; the echo shows the row's refs.
            if retain_inputs is not None and not attempted:
                retain_inputs()
            if (message.get("task_metadata") or {}).get("chat_attachments"):
                message["task_metadata"] = {**message["task_metadata"], "chat_attachments": row.get("attachments")}
        ts = str(row.get("ts") or "")
        ref = build_owner_message_ref(chat_id=chat_id, client_message_id=message_id, ts=ts, text=logged)
        # record_inbound_message gets the row witness and receipt time.
        # Dispatch only schedules/queues; slow work must not hold the ingress lock.
        (dispatch or bridge.enqueue_local_message)(text, **message, accepted_source_ref=ref, accepted_source_row=row, received_at=ts)
        dialogue_store.complete_dispatch(root, chat_id, message_id)
        return row, False


def recover_pending_dispatches(bridge, drive_root) -> int:
    """Perform every dispatch the store still owes (a crash between the commit and the handoff),
    exactly once each: the partial index names them, no chain is read. An obligation naming a
    dispatcher this body lacks is abandoned with that reason. Returns the number dispatched."""
    from ouroboros.project_dialogue import build_owner_message_ref

    root = Path(drive_root)
    dispatched = 0
    for stored, payload in dialogue_store.pending_dispatches(root):
        row = stored.row
        chat_id, message_id = _named(row)
        name = str(payload.get("dispatcher") or "")
        dispatcher = _dispatchers().get(name)
        if dispatcher is None or chat_id is None:
            dialogue_store.abandon_dispatch(root, chat_id, message_id, f"dispatcher_unknown:{name or 'none'}")
            log.warning("Owed dispatch of %s:%s abandoned: dispatcher %r unknown", chat_id, message_id, name)
            continue
        with _INGRESS_LOCK:
            if not dialogue_store.claim_dispatch(root, chat_id, message_id, process=_process()):
                continue  # a retry took it meanwhile
            ts = str(row.get("ts") or "")
            text = str(payload.get("text") or "")
            message = dict(payload.get("message") or {})
            ref = build_owner_message_ref(chat_id=chat_id, client_message_id=message_id, ts=ts, text=str(row.get("text") or text))
            try:
                dispatcher(bridge, text, **message, accepted_source_ref=ref, accepted_source_row=row, received_at=ts)
            except Exception:
                log.error("Owed dispatch of %s:%s raised after entry; its outcome is unknown", chat_id, message_id, exc_info=True)
                continue
            dialogue_store.complete_dispatch(root, chat_id, message_id)
            dispatched += 1
    return dispatched


def record_inbound_message(bridge, message: dict, *, chat_id: int, user_id: int,
                           client_message_id: str, text: str, ts: str) -> Optional[dict]:
    """Keep one canonical ingress writer for dequeued and preaccepted messages."""
    from ouroboros.project_dialogue import (
        _text_sha256, build_owner_message_ref, entry_matches_source_ref, owner_message_ref_is_valid,
    )
    from supervisor import message_bus

    source = str(message.get("source") or "web")
    ref = message.get("accepted_source_ref")
    if ref:
        # A web acceptance (socket or late quiz answer) wrote this row in-process
        # before enqueue; its returned row is the item's exact witness. A skill
        # source, or a web item without a witness (the queue's empty default is
        # absent, not a row to match), re-reads the retained row.
        accepted_row = message.get("accepted_source_row")
        row = (accepted_row if source == "web" and isinstance(accepted_row, dict) and accepted_row
               else accepted_chat_message(message_bus.DATA_DIR, chat_id, client_message_id)) if owner_message_ref_is_valid(ref) else None
        if (not row or row.get("source") != source or row.get("chat_id") != chat_id
                or row.get("client_message_id") != client_message_id or not entry_matches_source_ref(row, [ref])
                or ref["text_sha256"] != _text_sha256(text)):
            raise ValueError("accepted source does not match the queued message")
        ref = dict(ref)
        ts = ref["ts"]
        placeholder = row.get("text_placeholder") is True
    elif message.get("suppress_chat_log"):
        return None
    else:
        metadata = message.get("task_metadata") or {}
        # no words or caption from the sender: TEXT is the host's placeholder
        placeholder = not str(message.get("text") or "").strip() and not str(message.get("image_caption") or "").strip()
        message_bus.log_chat(
            "in", chat_id, user_id, text, ts=ts, source=source,
            sender_label=str(message.get("sender_label") or ""),
            sender_session_id=str(message.get("sender_session_id") or ""),
            client_message_id=client_message_id, transport=message.get("transport"),
            client_surface=(metadata.get("client_surface") if isinstance(metadata, dict)
                            and isinstance(metadata.get("client_surface"), dict) else None),
            message_meta={"attachments": metadata.get("chat_attachments") if isinstance(metadata, dict) else None,
                          "text_placeholder": placeholder},
        )
        ref = build_owner_message_ref(chat_id=chat_id, client_message_id=client_message_id, ts=ts, text=text)
    if source != "web":
        # A parked inline photo is one of the views: one bubble, never a second photo frame;
        # its text is the row's, placeholder mark included, as history replays it.
        views = attachment_views((message.get("task_metadata") or {}).get("chat_attachments"))
        bridge.broadcast({
            "type": "photo" if message.get("image_base64") and not views else "chat", "role": "user",
            "content": text if views and placeholder else str(message.get("text") or ""),
            "caption": str(message.get("image_caption") or ""), **({"text_placeholder": True} if views and placeholder else {}),
            "image_base64": "" if views else str(message.get("image_base64") or ""), **({"attachments": views} if views else {}),
            "mime": str(message.get("image_mime") or "image/jpeg"), "ts": ts, "source": source,
            "sender_label": str(message.get("sender_label") or ""),
            "sender_session_id": str(message.get("sender_session_id") or ""),
            "client_message_id": client_message_id, "transport": message.get("transport") or {},
            "chat_id": chat_id,
        })
    return ref
