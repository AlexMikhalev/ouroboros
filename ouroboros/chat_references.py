"""Host-owned message addresses and Reply facts, shared by every owner ingress.

References describe what the sender pointed at. They do not select work or grant
authority. Hash the persisted row before adding any history/view fields. A row the
dialogue store cannot answer yet (its archive still importing, or unreadable) leaves
the reference unresolved; it never blocks the message that carries it.
"""
from __future__ import annotations

from ouroboros.chat_chain import format_address, resolve_row, row_address
from ouroboros.store import StoreError


def projection_fields(row: dict) -> dict:
    """Additive fields for live frames and history, from the same stored row."""
    fields = {key: row[key] for key in ("reply_to", "relay") if isinstance(row.get(key), dict)}
    if row.get("reply_to_relay"):
        fields["reply_to_relay"] = row["reply_to_relay"]
    fields["row_address"] = format_address(row_address(row))
    return fields


def reference_from_row(row: dict) -> dict:
    kind = str(row.get("type") or "message")
    # A lifecycle row carrying an authored answer is that answer in Ouroboros's
    # voice wherever Main shows it (project_answer.js::appendProjectRow); the
    # address still hashes the raw System row.
    answer = str(row.get("completion_answer") or "") if kind in {
        "project_started", "project_handoff", "project_completion_summary"} else ""
    kind = ("question" if kind == "quiz" else "completion" if kind in
            {"project_completion_summary", "task_summary"} else
            "system" if row.get("direction") == "system" else "message")
    role = "assistant" if answer else {"in": "user", "out": "assistant", "system": "system"}.get(
        row.get("direction"), "system")
    return {"address": format_address(row_address(row)), "chat_id": int(row.get("chat_id", 1)),
            "role": role, "author_label": str(row.get("sender_label") or
                                               {"user": "You", "assistant": "Ouroboros", "system": "System"}[role]),
            "kind": kind, "snippet": " ".join(str(answer or row.get("text") or row.get("caption") or "").split())[:160],
            "ts": str(row.get("ts") or ""), "task_id": str(row.get("task_id") or "")}


def resolve_reply(root, address: str) -> dict:
    """Resolve a transport-neutral Reply address once, at acceptance."""
    if not isinstance(address, str):
        raise ValueError("reply_to must be an address string")
    if not address:
        return {}
    if address.startswith("task:"):
        from ouroboros.task_status import load_effective_task_result
        task_id = address[5:]
        task = load_effective_task_result(root, task_id, materialize_artifacts=False) if task_id else None
        if task:
            return {"address": address, "chat_id": int(task.get("chat_id") or 1), "role": "assistant",
                    "author_label": "Ouroboros", "kind": "work_card",
                    "snippet": " ".join(str(task.get("title") or task.get("text") or "").split())[:160],
                    "ts": str(task.get("updated_at") or task.get("started_at") or ""), "task_id": task_id}
    else:
        try:
            row, _resolution = resolve_row(root, address)
            if row is None:
                row = resolve_progress_row(root, address)
        except (ValueError, StoreError):
            row = None
        if row:
            return reference_from_row(row)
    return {"address": address, "unresolved": True}


def ingress_reply(root, metadata: dict) -> dict:
    """Shared web/HTTP/skill seam. Raw input is resolved, never trusted as a preview."""
    return resolve_reply(root, metadata.get("reply_to", ""))


def origin_reply(root, task_id: str, metadata=None) -> dict:
    """The answer's host reference to the owner message that began its turn."""
    if not task_id or not root:
        return {}
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.project_dialogue import resolve_owner_message_source
    task = metadata or load_effective_task_result(root, task_id, materialize_artifacts=False) or {}
    ref = task.get("origin_message_ref") or (task.get("metadata") or {}).get("origin_message_ref")
    if not ref:
        return {}
    try:
        row = resolve_owner_message_source(root, ref)
    except StoreError:
        return {}
    return reference_from_row(row) if row else {}


def resolve_progress_row(root, address):
    """Progress uses the same persisted-row hash, found by its indexed chat and time.

    A missing or ambiguous row answers None, as a chat row that does not resolve does."""
    from ouroboros import dialogue_store
    from ouroboros.chat_chain import _normalized, source_row_id
    wanted = _normalized(address)
    candidates = dialogue_store.rows_by_identity(root, wanted["chat_id"], wanted["ts"], stream="progress")
    if wanted["chat_id"] == 1:  # the legacy missing-address convention of chat rows
        candidates += dialogue_store.rows_by_identity(root, None, wanted["ts"], stream="progress")
    matches = {source_row_id(entry.row): entry.row for entry in sorted(candidates, key=lambda entry: entry.seq)
               if source_row_id(entry.row).startswith(wanted["row_sha256"])}
    return next(iter(matches.values())) if len(matches) == 1 else None


def origin_projection(root, ref):
    """A projected origin keeps its real source address, never hashes view fields."""
    from ouroboros.project_dialogue import resolve_owner_message_source
    try:
        row = resolve_owner_message_source(root, ref)
    except StoreError:
        return {}
    return projection_fields(row) if row else {}
