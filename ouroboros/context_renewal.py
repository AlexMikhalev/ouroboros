"""The floor of the refusal path: the same task continues in a fresh view of its own record.

Owner decision Q1=A (17.10.2026). When the model-free refusal passes and the configured
fallback could not bring a refused request under the window, the task does not end: its
conversation is rebuilt as a seed of what continuity needs, and everything else stays one
address away in an exact checkpoint of the whole transcript. The task id, attempt,
children, budget, route, pending questions and custody are those of the running loop, so
only the view changes. No model is called and nothing is summarized; the mind's own notes
are never dropped for age.

The seed keeps, in their original order: the system view and the assignment; every delivered
message (the owner's and principals' words typed at delivery, review feedback, notices, and
any row written before host state was bound to its text, which may hold merged owner words);
Ouroboros's outward dialogue; the mind's authored notes (actor capsules) and a predecessor's
notes; the current review decisions; and the mind's own prose since its newest note,
including the text it wrote beside a tool call. Complete tool units, replies the notes
already cover, host state the host sends afresh (facts, clock, roster: bound and unchanged),
superseded review copies and earlier address records leave the view; one host record names
the checkpoint that holds them exactly. A renewal that would remove no tool unit and no
covered reply, or a seed not smaller than the refused request, is never sent: the round then
ends in the named refusal.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Sequence, Tuple

from ouroboros.context_budget import HOST_CONTEXT_KIND_KEY, OWNER_ROW_KINDS, bound_host_row

RENEWAL_RECORD_NAME = "ouroboros_view_renewal"


def renewal_seed(messages: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """``(kept rows in order, counts of the rows that leave the view)``; pure, no I/O."""
    from ouroboros.context_budget import host_row_intact
    from ouroboros.context_compaction import _capsule_metadata, _plain_text
    from ouroboros.loop_messages import CONTEXT_FACTS_NAME
    from ouroboros.main_context_authority import PREDECESSOR_NOTES_NAME
    from ouroboros.peer_roster import ROSTER_SNAPSHOT_NAME, ROSTER_UPDATE_NAME
    from ouroboros.review_history_view import REVIEW_CONTEXT_INDEX_KEY, obsolete_review_index_positions
    from ouroboros.send_clock import CLOCK_ROW_NAME

    resent = {CONTEXT_FACTS_NAME, CLOCK_ROW_NAME, ROSTER_SNAPSHOT_NAME, ROSTER_UPDATE_NAME, RENEWAL_RECORD_NAME}

    def note(row: Dict[str, Any]) -> bool:
        capsule, meta = _capsule_metadata(row)
        return bool(capsule and meta and meta.get("authorship") == "actor")

    first_user = next((i for i, row in enumerate(messages) if row.get("role") == "user"), -1)
    newest_note = max((i for i, row in enumerate(messages) if note(row)), default=-1)
    superseded = set(obsolete_review_index_positions(messages))
    kept: List[Dict[str, Any]] = []
    left = {"tool_rows": 0, "covered_replies": 0, "host_rows": 0}
    for i, row in enumerate(messages):
        role, kind = row.get("role"), row.get(HOST_CONTEXT_KIND_KEY)
        if ((i == 0 and role == "system") or i == first_user or kind in OWNER_ROW_KINDS or note(row)
                or kind == PREDECESSOR_NOTES_NAME or (row.get(REVIEW_CONTEXT_INDEX_KEY) and i not in superseded)):
            kept.append(row)
        elif role == "tool" or row.get("tool_calls"):
            left["tool_rows"] += 1
            prose = _plain_text(row.get("content")) if role == "assistant" and i > newest_note else None
            if prose and prose.strip():
                kept.append({"role": "assistant", "content": prose})  # my words beside the call stay
        elif (_capsule_metadata(row)[0] or row.get(REVIEW_CONTEXT_INDEX_KEY)
              or (kind in resent and host_row_intact(row))):
            left["host_rows"] += 1  # addresses, superseded review copies, state the host sends afresh
        elif role == "assistant" and i <= newest_note:
            left["covered_replies"] += 1
        else:
            kept.append(row)  # every delivered message and my replies since my newest note
    return kept, left


def renew_after_refusal(limit_ctx: Any, tools: Any) -> bool:
    """Publish the renewal seed for a round that ended in a size refusal; False when renewal cannot help."""
    from ouroboros.artifacts import store_actor_source_bytes
    from ouroboros.context_fit import estimate_context_prompt_tokens
    from ouroboros.loop_llm_call import TRANSPORT_DEATHS_KEY
    from ouroboros.loop_round_limits import _publish_reclaimed_transcript

    usage = limit_ctx.accumulated_usage
    if (str(usage.get("_last_llm_error_kind") or "") != "context_overflow" or usage.get("resource_refusal")
            or isinstance(usage.get(TRANSPORT_DEATHS_KEY), dict) or usage.get("_pending_transport_outcome")):
        return False
    messages = limit_ctx.messages
    before = len(messages)
    seed, left = renewal_seed(messages)
    if not (left["tool_rows"] or left["covered_replies"]):
        return _cannot_help(limit_ctx)  # only host rows would leave: a renewal cannot make this conversation fit
    raw = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    try:
        source = store_actor_source_bytes(limit_ctx.drive_root or limit_ctx.drive_logs.parent, limit_ctx.task_id,
                                          category="context_checkpoints", source_id=hashlib.sha256(raw).hexdigest(),
                                          data=raw, extension="json")
    except (OSError, ValueError):
        return False  # no exact source, no renewal: every address must resolve (BIBLE P1)
    seed.append(bound_host_row(RENEWAL_RECORD_NAME, (
        "[Host record: working view renewed after the provider refused this conversation's size. Same task; "
        "nothing summarized; no model called.] Kept, in order: the assignment, typed owner and outward dialogue "
        "rows, plain user rows, my notes, my replies since my newest note and the current review decisions. "
        f"Left the view: {left['tool_rows']} tool rows, {left['covered_replies']} replies before my newest note, "
        f"{left['host_rows']} host rows. The exact transcript of {before} rows: "
        f"{json.dumps(source, ensure_ascii=False, sort_keys=True)}.")))
    schemas = getattr(limit_ctx, "tool_schemas", None)
    if estimate_context_prompt_tokens(seed, schemas) >= estimate_context_prompt_tokens(messages, schemas):
        return _cannot_help(limit_ctx)  # never send a candidate that is not smaller than the refused one
    _publish_reclaimed_transcript(SimpleNamespace(messages=messages, task_id=limit_ctx.task_id, tools=tools), seed)
    from ouroboros.loop import _emit_checkpoint_event

    _emit_checkpoint_event(limit_ctx.event_queue, limit_ctx.task_id, limit_ctx.drive_logs, {
        "checkpoint_kind": "context_view_renewed", "round": limit_ctx.round_idx, "owner_visible": True,
        "source": source, "rows_before": before, "rows_after": len(seed), **left})
    return True


def _cannot_help(limit_ctx: Any) -> bool:
    """Name the frame's largest parts for the named refusal (``loop_transport``), then decline."""
    from ouroboros.loop_messages import _context_components

    view = SimpleNamespace(messages=limit_ctx.messages, tool_schemas=getattr(limit_ctx, "tool_schemas", None) or [],
                           accumulated_usage=limit_ctx.accumulated_usage)
    try:
        limit_ctx.accumulated_usage["_context_refusal_frame"] = [list(part) for part in _context_components(
            view, measured=False)]
    except Exception:  # the refusal text is facts only; an unreadable frame stays unnamed
        pass
    return False
