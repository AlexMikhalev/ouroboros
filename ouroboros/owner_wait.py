"""Same-task owner waiting at a completed-tool boundary.

The queue owns admission and active worker capacity. This module preserves the
native continuation through the existing source store. Pooled workers wait on
their command queue; direct actors use the same mailbox and task controls without
holding pooled capacity. A warm wake continues the original stack and browser;
only a confirmed planned-restart handoff may load a cold continuation. Source
bytes outlive their one-use resume authority in the ordinary task result.

An optional bound (``escalate(max_wait_minutes=N)``) rides the checkpoint as an
ABSOLUTE stamp (``wait_deadline_at``), so a planned restart resumes the same
bound instead of starting it over. Both callbacks return ``"owner_input"`` or
``"timeout"``; the existing control axes (Stop, cancel, task deadline, absolute
ceiling) are still checked FIRST, so the soft bound can never overtake them, and
a timeout introduces no new wait state — the row resumes with the additive
``resume_reason: "timeout"``.
"""

from __future__ import annotations

import json
import logging
import pathlib
import queue
import time
import uuid
from dataclasses import asdict
from typing import Any

from ouroboros.artifacts import read_actor_source_bytes, store_actor_source_bytes
from ouroboros.owner_mailbox import OwnerMailboxPeek
from ouroboros.task_results import _TRULY_TERMINAL_STATUSES, load_task_result

log = logging.getLogger(__name__)


def set_owner_wait(root: Any, task_id: str, wait: dict,
                   expected_wait_id: str | None = None) -> dict:
    """Update only the existing continuation projection, preserving siblings."""
    from ouroboros.task_results import (
        require_writable_task_result_schema,
        stamp_task_result_schema, task_result_path,
    )
    from ouroboros.utils import update_json_locked

    def update(current: dict) -> dict:
        require_writable_task_result_schema(current)
        if current.get("status") in _TRULY_TERMINAL_STATUSES:
            raise ValueError("a terminal task cannot continue owner waiting")
        old = current.get("owner_wait") or {}
        if expected_wait_id is not None and old.get("wait_id") != expected_wait_id:
            raise ValueError("owner wait identity changed")
        return stamp_task_result_schema({**current, "owner_wait": dict(wait)})

    update_json_locked(task_result_path(root, task_id), update, strict_existing_dict=True)
    return dict(wait)


def _wait_bound_fields(ctx: Any) -> dict:
    """The optional bound, as an ABSOLUTE instant plus the minutes it named.

    Absent unless a bound was requested: an unbounded wait must not carry a
    field that reads as one. The stamp (not a remaining count) is what lets a
    planned restart resume the SAME bound instead of granting it again.
    """
    deadline = str(getattr(ctx, "_owner_wait_deadline_at", "") or "")
    if not deadline:
        return {}
    return {"wait_deadline_at": deadline,
            "wait_max_minutes": int(getattr(ctx, "_owner_wait_max_minutes", 0) or 0)}


def checkpoint_owner_wait(ctx: Any, messages: list, trace: dict, usage: dict,
                          round_idx: int, tool_schemas: list, seen: set,
                          *, review_binding: str = "") -> dict:
    """Capture only the live loop's continuation values, never Python handles."""
    wait_id = uuid.uuid4().hex
    candidate = getattr(ctx, "_delivery_candidate", None)
    cost_ceiling = getattr(ctx, "_cost_ceiling", None)
    model_wait = getattr(ctx, "model_wait_context", None)
    model_state = model_wait.continuation_state() if model_wait is not None else {}
    state = {
        "task_id": ctx.task_id, "task_attempt": int(ctx.task_attempt or 1),
        "wait_id": wait_id, "quiz_id": getattr(ctx, "_owner_wait_requested", ""),
        **_wait_bound_fields(ctx),
        "reason": "review" if review_binding else "owner",
        "review_binding": review_binding,
        "messages": messages, "trace": trace, "usage": usage,
        "cost_ceiling": asdict(cost_ceiling) if cost_ceiling is not None else None,
        "model_wait": model_state,
        "context_model_role": getattr(getattr(ctx, "context_fit_plan", None), "model_role", ""),
        "round_idx": round_idx, "tool_schemas": tool_schemas,
        "seen": sorted(seen), "owner_directives": getattr(ctx, "_owner_directives", []),
        "route": {key: getattr(ctx, key, None) for key in (
            "active_model", "active_effort", "active_use_local", "active_context_mode",
            "active_model_override", "active_effort_override", "active_use_local_override",
        )},
        "delivery_candidate": asdict(candidate) if candidate is not None else None,
        "delivery": {key: getattr(ctx, key, None) for key in (
            "_delivery_candidate_revision", "_delivery_control_required",
            "_delivery_evidence_revision", "_delivery_evidence_fingerprint",
            "_delivery_effective_criteria", "_delivery_material_tool_indices",
            "_acceptance_ack_source_sha256",
        ) if getattr(ctx, key, None) is not None},
        "acceptance": {
            "_task_acceptance_improvement_passes": int(getattr(ctx, "_task_acceptance_improvement_passes", 0)),
            "_task_acceptance_reviewed": bool(getattr(ctx, "_task_acceptance_reviewed", False)),
            "_task_acceptance_pending": str(getattr(ctx, "_task_acceptance_pending", "")),
            "_task_acceptance_reviewed_subject": str(getattr(ctx, "_task_acceptance_reviewed_subject", "")),
        },
    }
    root = pathlib.Path(ctx.budget_drive_root or ctx.drive_root)
    source = store_actor_source_bytes(root, ctx.task_id, category="context_checkpoints",
                                     source_id="owner-wait-" + wait_id,
                                     data=json.dumps(state, ensure_ascii=False).encode(), extension="json")
    return {
        "wait_id": wait_id, "quiz_id": getattr(ctx, "_owner_wait_requested", ""),
        **_wait_bound_fields(ctx),
        "reason": "review" if review_binding else "owner",
        "review_binding": review_binding,
        "source_ref": source, "task_attempt": int(ctx.task_attempt or 1),
        "execution_drive_root": str(ctx.drive_root),
        "started_at": getattr(ctx, "task_started_at", None),
        "model_wait_quota_clock": model_state.get("quota_clock", {}),
    }


def load_owner_wait(ctx: Any, handoff: dict | None = None) -> dict:
    """Resolve a selected handoff; a stale snapshot cannot revive a spent wait."""
    handoff = handoff or getattr(ctx, "owner_wait_resume", None)
    if not handoff:
        return {}
    root = pathlib.Path(ctx.budget_drive_root or ctx.drive_root)
    row = load_task_result(root, ctx.task_id, strict=True) or {}
    current = row.get("owner_wait") or {}
    if (row.get("status") in _TRULY_TERMINAL_STATUSES
            or current.get("state") != "waiting"
            or current.get("wait_id") != handoff.get("wait_id")
            or not handoff.get("restart_transaction_id")):
        raise ValueError("owner wait continuation is not an active planned-restart handoff")
    state = json.loads(read_actor_source_bytes(root, ctx.task_id, current["source_ref"]))
    if (state.get("task_id") != ctx.task_id
            or state.get("wait_id") != current.get("wait_id")
            or state.get("task_attempt") != int(ctx.task_attempt or 1)):
        raise ValueError("owner wait continuation identity mismatch")
    if state.get("cost_ceiling") is not None:
        from ouroboros.task_pacing import CostCeiling

        ctx._cost_ceiling = CostCeiling(**state["cost_ceiling"])
    return state


def restore_owner_wait_allowed(root: Any, task: dict) -> bool:
    """A snapshot is a locator; current wait and acknowledged restart authorize it."""
    from ouroboros.cancel_intents import has_active_intent
    from ouroboros.deadline_utils import parse_deadline_ts, utc_now
    from ouroboros.delegate_recovery import _ack_direct_exec_successor, _read_restart_transaction
    from ouroboros.config import get_task_abs_ceiling_sec
    from ouroboros.model_wait import quota_waited_seconds
    import time

    handoff = task.get("_owner_wait_resume")
    if not isinstance(handoff, dict):
        return False
    root = pathlib.Path(root)
    if any((root / "state" / name).exists() for name in ("owner_restart_no_resume.flag", "panic_stop.flag")):
        return False
    _ack_direct_exec_successor(root)
    task_id = str(task.get("id") or "")
    transaction = _read_restart_transaction(root, str(handoff.get("restart_transaction_id") or ""))
    if transaction.get("status") != "normal_exit_acknowledged" or task_id not in transaction.get("task_ids", []):
        return False
    row = load_task_result(root, task_id, strict=True) or {}
    wait = row.get("owner_wait") or {}
    if (row.get("status") in _TRULY_TERMINAL_STATUSES
            or wait.get("state") != "waiting" or wait.get("wait_id") != handoff.get("wait_id")):
        return False
    if has_active_intent(root, task_id, strict=True):
        return False
    deadline = parse_deadline_ts(task.get("deadline_at") or (task.get("task_contract") or {}).get("deadline_at"))
    if deadline is not None and deadline <= utc_now():
        return False
    started = float(handoff.get("started_at") or 0)
    now = time.time()
    if started and now - started - quota_waited_seconds(wait, now) >= get_task_abs_ceiling_sec():
        return False
    read_actor_source_bytes(root, task_id, wait["source_ref"])
    return True


def _wait_deadline(checkpoint: dict) -> Any:
    """The bound's absolute instant, or None when the wait is unbounded."""
    from ouroboros.deadline_utils import parse_deadline_ts

    return parse_deadline_ts((checkpoint or {}).get("wait_deadline_at"))


def _bound_expired(deadline: Any) -> bool:
    from ouroboros.deadline_utils import utc_now

    return deadline is not None and utc_now() >= deadline


def worker_owner_wait(wid: int, in_q: Any, out_q: Any, ctx: Any,
                      checkpoint: dict) -> str:
    """Keep the original task process asleep until the pool grants capacity.

    A bound is a second reason to ask for the SAME resume the mailbox already
    triggers — no new scheduler: the request must sit inside the ``parked``
    gate, because the supervisor ignores a resume for a wait that is not
    durably waiting. Disclosed: a resume is a REQUEST, not a guarantee (the
    grant can be refused under a cancel intent or the repo-writer gate); the
    hard axes still end the task in that case.
    """
    import os

    identity = {"type": "owner_wait", "worker_id": wid, "pid": os.getpid(),
                "task_id": ctx.task_id, "task_attempt": int(ctx.task_attempt or 1),
                "wait_id": checkpoint["wait_id"]}
    # The existing deferred buffer must reach the supervisor before parking.
    # Remove only a successfully submitted prefix; its final flush cannot repeat it.
    while ctx.pending_events:
        out_q.put({**ctx.pending_events[0], "worker_id": wid})
        del ctx.pending_events[0]
    out_q.put({**identity, "phase": "park", "checkpoint": checkpoint})
    peek = OwnerMailboxPeek()
    deadline = _wait_deadline(checkpoint)
    parked = resume_requested = False
    outcome = "owner_input"
    while True:
        try:
            command = in_q.get(timeout=1.0)
        except queue.Empty:
            command = None
        if isinstance(command, dict) and command.get("type") == "owner_wait":
            if all(command.get(key) == identity[key] for key in ("task_id", "task_attempt", "wait_id")):
                phase = command.get("phase")
                if phase == "parked":
                    parked = True
                elif phase == "resume_granted":
                    return outcome
                elif phase == "refused":
                    raise RuntimeError(str(command.get("reason") or "owner wait refused"))
        if parked and not resume_requested:
            if peek.pending(
                    pathlib.Path(ctx.drive_root), ctx.task_id,
                    set(getattr(ctx, "_loop_mailbox_seen_ids", set())), ctx.task_attempt or 1):
                out_q.put({**identity, "phase": "resume"})
                resume_requested = True
            elif _bound_expired(deadline):
                outcome = "timeout"
                out_q.put({**identity, "phase": "resume", "resume_reason": outcome})
                resume_requested = True


def direct_owner_wait(ctx: Any, checkpoint: dict) -> str:
    """Retain a registered chat actor's stack; it holds no pooled capacity.

    The existing mailbox still owns input and its loop still owns delivery.
    TaskModelWait supplies the same Stop/deadline clocks as native model calls;
    this owner wait does not enter a quota pause or grant cold restart authority.
    An optional bound releases the loop only AFTER those controls are consulted,
    so Stop, the task deadline and the absolute ceiling keep precedence.
    """
    control = ctx.model_wait_context
    root = pathlib.Path(ctx.budget_drive_root or ctx.drive_root)
    while ctx.pending_events:
        ctx.event_queue.put(dict(ctx.pending_events[0]))
        del ctx.pending_events[0]
    wait = set_owner_wait(root, ctx.task_id, {**checkpoint, "state": "waiting"})
    peek = OwnerMailboxPeek()
    deadline = _wait_deadline(checkpoint)
    outcome = "owner_input"
    while not control.control_reason() and not peek.pending(
            pathlib.Path(ctx.drive_root), ctx.task_id,
            set(getattr(ctx, "_loop_mailbox_seen_ids", set())), ctx.task_attempt or 1):
        if _bound_expired(deadline):
            outcome = "timeout"
            break
        time.sleep(1.0)
    set_owner_wait(root, ctx.task_id,
                   {**wait, "state": "resumed",
                    **({"resume_reason": outcome} if outcome == "timeout" else {})},
                   wait["wait_id"])
    if outcome == "timeout":
        announce_wait_ended(root, ctx.task_id, str(checkpoint.get("quiz_id") or ""),
                            int(getattr(ctx, "current_chat_id", 0) or 0))
    return outcome


def announce_wait_ended(root: Any, task_id: str, quiz_id: str, chat_id: int) -> None:
    """A bound closed and the turn resumed: the card's projection and the live card both
    stop saying "waiting" while the question stays answerable. The direct lane calls it
    here; the pool's supervisor grant calls the same two seams (best effort, never raises)."""
    if not quiz_id:
        return
    try:
        from ouroboros.owner_quiz import mark_wait_ended

        mark_wait_ended(root, task_id, quiz_id)
    except Exception:
        log.debug("owner-wait end not recorded on quiz %s", quiz_id, exc_info=True)
    try:
        from supervisor.message_bus import get_bridge

        get_bridge().send_quiz_state(quiz_id, task_id, "open", chat_id=chat_id, wait_for_answer=False)
    except Exception:
        log.debug("owner-wait end not broadcast for quiz %s", quiz_id, exc_info=True)


def owner_wait_timeout_notice(ctx: Any, checkpoint: dict) -> dict:
    """Host frame for a bound that ended without an owner answer.

    A host notice, never owner-marked content: the model must not read it as
    something the owner said. The card stays open, so the honest instruction
    depends on whether an assumption was recorded — without one, silence is
    explicitly NOT consent.
    """
    minutes = int((checkpoint or {}).get("wait_max_minutes") or 0)
    window = f"within {minutes} minutes" if minutes > 0 else "within the requested window"
    assumption = ""
    try:
        from ouroboros.owner_quiz import quiz_states

        root = pathlib.Path(ctx.budget_drive_root or ctx.drive_root)
        block = quiz_states(root, ctx.task_id).get(str((checkpoint or {}).get("quiz_id") or "")) or {}
        assumption = str(block.get("assumption") or "").strip()
    except Exception:
        assumption = ""
    stance = (f"Proceed under your stated assumption: {assumption}" if assumption
              else "No assumption was recorded; no answer is not consent")
    return {"role": "user", "content": (
        f"[SYSTEM NOTICE]\nNo owner answer arrived {window}. The question card stays open — "
        "a later answer reaches you as an ordinary owner message. "
        f"{stance} — or finish and say what is unresolved.")}


def wait_after_tools(ctx: Any, messages: list, trace: dict, usage: dict,
                     round_idx: int, tool_schemas: list, seen: set,
                     *, review_binding: str = "") -> None:
    """Yield only after complete tool results; no model polling or terminal path."""
    if not getattr(ctx, "_owner_wait_requested", "") and not review_binding:
        return
    callback = getattr(ctx, "owner_wait_callback", None)
    if not callable(callback):
        raise RuntimeError("required owner wait has no worker continuation owner")
    checkpoint = checkpoint_owner_wait(ctx, messages, trace, usage, round_idx, tool_schemas, seen,
                                       review_binding=review_binding)
    outcome = callback(ctx, checkpoint)
    if outcome == "timeout" and not review_binding:
        # The acceptance-review park shares this seam and must never receive a
        # quiz-timeout notice.
        messages.append(owner_wait_timeout_notice(ctx, checkpoint))
    ctx._owner_wait_requested = ""
    ctx._owner_wait_deadline_at = ""
    ctx._owner_wait_max_minutes = 0


def resume_native_loop(tools: Any, state: dict, messages: list, trace: dict,
                       usage: dict, seen: set) -> tuple:
    """Restore the selected cold continuation and await its ordinary input grant."""
    from ouroboros.loop_delivery import DeliveryCandidate
    from ouroboros.loop import _rebind_context_fit_plan, get_context_mode

    ctx = tools._ctx
    messages[:] = state["messages"]
    trace.update(state["trace"])
    usage.update(state["usage"])
    seen.update(state["seen"])
    ctx._loop_mailbox_seen_ids = seen
    ctx._owner_directives = state["owner_directives"]
    for key, value in {**state["route"], **state["delivery"], **state["acceptance"]}.items():
        setattr(ctx, key, value)
    candidate = state.get("delivery_candidate")
    ctx._delivery_candidate = DeliveryCandidate(**candidate) if candidate else None
    cold_checkpoint = ctx.owner_wait_resume
    outcome = ctx.owner_wait_callback(ctx, cold_checkpoint)
    ctx.owner_wait_resume = None
    from ouroboros.model_slots import task_model_binding

    model_wait = getattr(ctx, "model_wait_context", None)
    role, account = task_model_binding(
        {"model_role": state.get("context_model_role"), "task_metadata": ctx.task_metadata},
        context_fit_plan=ctx.context_fit_plan,
        overrides=model_wait.overrides if model_wait is not None else None,
    )
    plan, mode = _rebind_context_fit_plan(
        ctx.context_fit_plan, tools, messages, model=ctx.active_model,
        use_local=ctx.active_use_local, preferred_mode=get_context_mode(),
        tool_schemas=state["tool_schemas"],
        model_role=role, model_route={}, credential_profile_id=account,
    )
    messages.append({"role": "user", "content": (
        "[SYSTEM NOTICE]\nThis task continued from its saved owner wait after a planned restart. "
        "Prior tool results remain recorded; do not repeat completed effects. "
        "The restart ended the previous browser process and task-local services; "
        "their recorded results remain evidence, not proof they are still running.")})
    if outcome == "timeout":
        # The bound survived the restart as an absolute stamp, so the cold
        # continuation can ALSO end on it — and must say so, exactly like warm.
        messages.append(owner_wait_timeout_notice(ctx, cold_checkpoint or {}))
    return (ctx.active_model, ctx.active_effort, ctx.active_use_local,
            mode, state["round_idx"], plan)


def prepare_owner_wait_handoffs(root: Any, running: dict, transaction_id: str) -> set[str]:
    """Select only parked native continuations for the planned restart owner."""
    selected = set()
    for task_id, meta in running.items():
        task = meta.get("task") or {}
        row = load_task_result(root, task_id, strict=True) or {}
        wait = row.get("owner_wait") or {}
        if wait.get("state") != "waiting" or not wait.get("source_ref"):
            continue
        read_actor_source_bytes(root, task_id, wait["source_ref"])
        task["_owner_wait_resume"] = {**wait, "restart_transaction_id": transaction_id}
        selected.add(task_id)
    return selected
