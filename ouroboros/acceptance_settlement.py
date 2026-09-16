"""What happens to a paid acceptance panel that outlives the answer it reviewed.

A reviewer panel is advice for its author, never a signature on bytes the
reviewers did not read (owner decision D4=A, 2026-09-16). The three moments
below share one fact — the panel keeps its custody and paid identity while
Main moves on — so they live together:

* the wave wakes Main at its own quorum and again when the last slot settles,
  and the wake carries each reviewer's own verdict
  (``announce_acceptance_settlement``);
* a final answer delivered while that panel still runs neither buys a second
  panel nor is refused one (``_deliver_under_running_panel``): Main waits — the
  default, and the only option under blocking enforcement — or consciously
  finishes; a panel that PASSED the earlier revision accepts the task and the
  owner row says so (fork 1=B), while any other settled verdict hands the
  delivery to the ordinary acceptance path with the collected verdicts in its
  dialogue history;
* a panel that settles after its task ended is collected at $0, republished on
  the task's own review projection and announced once in the task's room
  (``attach_late_acceptance_settlement``); no model turn starts (fork 2=A).
"""
from __future__ import annotations

import logging
import pathlib
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# The host acceptance reason for "the paid panel approved the answer Main has
# since rewritten": the task is accepted on the reviewers' word about the
# earlier revision and the owner row says so. Not a member of
# ``outcomes._ACCEPTANCE_BLOCKED_TERMINAL_REASONS``: nothing was refused and no
# review cycle was spent.
REASON_PREVIOUS_REVISION_ACCEPTED = "previous_revision_accepted"

# The chat row a panel writes when it settles after its task already ended.
LATE_SETTLEMENT_SYSTEM_TYPE = "acceptance_late_settlement"

# The mailbox row a settling acceptance wave writes. It carries the reviewers'
# OWN verdicts: a wake that only said results existed, and asked Main to reply
# ``keep`` to read them, made the verdicts reachable only by not having moved
# on. The reducer's quorum verdict still lands through the ordinary collection
# at the next acceptance entry; these are the individual reviewers, unreduced.
ACCEPTANCE_SETTLEMENT_WAKE = (
    "Acceptance review {retry_key}: {settled} of {total} reviewer slot(s) have answered on "
    "the answer that was under review. These are their own verdicts — advice for you, not "
    "a signature on your current draft, and they do not stop you from finishing. A rewritten "
    "answer gets a verdict of its own only when you nominate it again."
)


def _reviewer_lines(wave: Dict[str, Any]) -> List[str]:
    """One line per roster slot: the reviewer's own verdict and note, or pending."""
    slots = wave.get("slots") or {}
    verdicts = wave.get("verdicts") or {}
    lines: List[str] = []
    for slot_id, status in slots.items():
        row = verdicts.get(slot_id) if isinstance(verdicts.get(slot_id), dict) else {}
        verdict = str(row.get("verdict") or "") or str(status or "pending")
        note = str(row.get("note") or "")
        lines.append(f"- {slot_id}: {verdict}" + (f" — {note}" if note else ""))
    return lines


def acceptance_settlement_message(request: Any, wave: Dict[str, Any]) -> str:
    """Render the settled wave as the reviewers' own lines, bounded per slot."""
    slots = wave.get("slots") or {}
    head = ACCEPTANCE_SETTLEMENT_WAKE.format(
        retry_key=str(getattr(request, "retry_key", "") or ""),
        settled=sum(1 for status in slots.values() if status),
        total=max(int(wave.get("total") or 0), len(slots)),
    )
    return "\n".join([head, *_reviewer_lines(wave)])


def announce_acceptance_settlement(usage_ctx: Any, request: Any, wave: Dict[str, Any]) -> None:
    """Deliver a settled acceptance wave to whoever can still act on it.

    While the turn is alive the reviewers' verdicts go to Main's existing
    mailbox, which is drained before every round and also ends an acceptance
    park. Once the task is terminal there is nobody to wake: the same verdicts
    are attached to the task result and announced once in the task's own room,
    so the owner sees them and the next turn reads them in chat history. Never a
    model turn, never a second paid dispatch. Runs on the settlement thread,
    outside custody locks.
    """
    if usage_ctx is None or not getattr(usage_ctx, "drive_root", None):
        return
    task_id = str(getattr(request, "task_id", "") or "")
    try:
        from ouroboros.task_results import _TRULY_TERMINAL_STATUSES, load_task_result

        root = pathlib.Path(usage_ctx.drive_root)
        row = load_task_result(root, task_id) or {}
        if str(row.get("status") or "") in _TRULY_TERMINAL_STATUSES:
            attach_late_acceptance_settlement(usage_ctx, request, wave, result=row)
            return
        from ouroboros.owner_mailbox import write_task_message

        write_task_message(root, acceptance_settlement_message(request, wave),
                           task_id, source_task_id=task_id, provenance="system")
    except Exception:
        log.warning("Acceptance settlement delivery failed for %s", task_id, exc_info=True)


def panel_awaiting_this_turn(tools_ctx: Any, llm_trace: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The panel THIS turn released, found by the binding the host recorded when
    it went pending — the one identity a re-authored answer cannot move."""
    binding = str(getattr(tools_ctx, "_task_acceptance_pending", "") or "")
    if not binding:
        return None
    return next((run for run in reversed(llm_trace.get("review_runs") or [])
                 if isinstance(run, dict) and run.get("authority") == "host_root"
                 and str(run.get("binding_hash") or "") == binding), None)


def acceptance_choice_offered() -> bool:
    """Whether the host can honour a wait/finish choice on this install.

    Blocking enforcement always waits; Cyber Pro never does (Main's final
    response is its decision there, unchanged). Only advisory enforcement on an
    ordinary runtime mode leaves the choice to Main, so only there is it offered.
    """
    from ouroboros.config import get_review_enforcement, get_runtime_mode
    from ouroboros.runtime_mode_policy import runtime_mode_at_least

    return (get_review_enforcement() != "blocking"
            and not runtime_mode_at_least(get_runtime_mode(), "cyber_pro"))


def acceptance_wait_chosen(tools_ctx: Any) -> bool:
    """Waiting is the default; only an explicit, current ``finish`` releases an
    answer over a panel that is still running where the install lets Main choose."""
    from ouroboros.config import get_review_enforcement, get_runtime_mode
    from ouroboros.runtime_mode_policy import runtime_mode_at_least

    if runtime_mode_at_least(get_runtime_mode(), "cyber_pro"):
        return False
    if get_review_enforcement() == "blocking":
        return True
    return str(getattr(tools_ctx, "_acceptance_pending_review_choice", "") or "") != "finish"


def _deliver_under_running_panel(ctx: Any, prior_run: Any) -> Optional[bool]:
    """A DELIVERY is not a nomination: it neither buys a panel nor is refused one.

    The panel this turn already paid for keeps its custody and identity. While
    it runs, Main waits (the default, and the only option under blocking
    enforcement) or consciously finishes. Once it has settled on the earlier
    revision: a PASS accepts the task on the reviewers' word and the owner row
    says so (fork 1=B); any other verdict is not a verdict on this answer, so the
    ordinary path decides — a new panel while the review cap allows, otherwise
    its typed capacity refusal — with the collected verdicts already in its
    dialogue history. ``None`` means "not this case". A NEW panel is bought on a
    rewritten answer only when Main nominates it again (``_acceptance_review_only``).
    """
    from ouroboros import loop
    from ouroboros.loop_acceptance_review import (
        _end_acceptance_terminal, _finish_cyber_acceptance,
        _set_applied_host_acceptance_impact, acceptance_run_pending,
    )
    from ouroboros.outcomes import ACCEPTANCE_ACCEPTED

    tools_ctx = ctx.tools._ctx
    if prior_run is not None or getattr(tools_ctx, "_acceptance_review_only", False):
        return None
    run = panel_awaiting_this_turn(tools_ctx, ctx.llm_trace)
    if run is None:
        return None
    if acceptance_run_pending(run):
        if acceptance_wait_chosen(tools_ctx):
            ctx.emit_progress("Task acceptance review is still running; holding the answer for its verdict.")
            return True
        return _finish_cyber_acceptance(ctx, SimpleNamespace(**run))
    tools_ctx._task_acceptance_pending = ""
    if str(run.get("aggregate_signal") or "").upper() != "PASS":
        return None
    result = SimpleNamespace(**run)
    _set_applied_host_acceptance_impact(run, result, requires_revision=False)
    ctx.llm_trace.setdefault("review_decision", {}).update({
        "panel_id": str(run.get("panel_id") or ""),
        "binding_hash": str(run.get("binding_hash") or ""),
    })
    _end_acceptance_terminal(ctx, "pass")
    loop._set_acceptance_decision(ctx.llm_trace, {
        "status": ACCEPTANCE_ACCEPTED,
        "reason": REASON_PREVIOUS_REVISION_ACCEPTED,
        "source": "task_acceptance_review",
        "reviewer_signal": "PASS",
        "reviewed_panel_id": str(run.get("panel_id") or ""),
        "reviewed_candidate_hash": str(run.get("candidate_hash") or ""),
    })
    ctx.emit_progress(
        "Task acceptance review: PASS on the earlier revision of this answer; accepted on the "
        "reviewers' word (the rewrite itself was not re-reviewed)."
    )
    return False


def _late_settlement_text(run: Dict[str, Any], wave: Dict[str, Any]) -> str:
    """The owner row: which verdict, which revision, and the reviewers' own lines."""
    signal = str(run.get("aggregate_signal") or "").upper()
    head = {"PASS": "Reviewers later passed this answer.",
            "FAIL": "Reviewers later rejected this answer."}.get(
        signal, "Reviewers later returned no settled verdict on this answer.")
    which = (" They reviewed the earlier version, which was rewritten before delivery."
             if run.get("superseded_by_revision") else " They reviewed the answer that was delivered.")
    return "\n".join([head + which, *_reviewer_lines(wave)])


def attach_late_acceptance_settlement(usage_ctx: Any, request: Any, wave: Dict[str, Any],
                                      *, result: Dict[str, Any]) -> bool:
    """A panel that settled after its task ended is a supplement, never a new turn.

    Its verdicts are collected at $0 over the recorded operation, republished on
    the task's own review projection through the existing locked writer, and
    announced ONCE in the task's room through the existing terminal-delivery
    outbox (durably owed, keyed by ``delivery_id``; a second settlement of the
    same wave finds nothing left to reconcile and announces nothing). The owner
    sees it; the next turn reads it in chat history. The acceptance twin of plan
    review's historical supplement (docs/architecture/06-agent-core.md).
    """
    trace = getattr(usage_ctx, "_execution_trace", None)
    retry_key = str(getattr(request, "retry_key", "") or "")
    task_id = str(getattr(request, "task_id", "") or "")
    if not isinstance(trace, dict) or not retry_key:
        return False
    runs = [run for run in (trace.get("review_runs") or [])
            if isinstance(run, dict) and run.get("authority") == "host_root"
            and isinstance(run.get("request"), dict)
            and str(run["request"].get("retry_key") or "") == retry_key]
    if not runs:
        return False  # the worker was rebound to another task; the record stays as published
    from ouroboros.review_dispatch import reconcile_pending_acceptance_runs
    from ouroboros.review_projection import publish_acceptance_checkpoint
    from supervisor.terminal_delivery import enqueue_terminal_delivery

    root = pathlib.Path(usage_ctx.drive_root)
    if not reconcile_pending_acceptance_runs(trace, drive_root=root, usage_ctx=usage_ctx):
        return False
    publish_acceptance_checkpoint(usage_ctx, trace, task_id=task_id, drive_root=root,
                                  chat_id=result.get("chat_id"))
    return bool(enqueue_terminal_delivery(root, {
        "type": "send_message", "chat_id": int(result.get("chat_id") or 0), "task_id": task_id,
        "text": _late_settlement_text(runs[-1], wave),
        "role": "system", "system_type": LATE_SETTLEMENT_SYSTEM_TYPE,
        "delivery_id": f"acceptance-late:{retry_key}",
    }, event_queue=getattr(usage_ctx, "event_queue", None)))
