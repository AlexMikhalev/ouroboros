"""``plan_task`` — one tool, three effects over the task's plan record (DESIGN §2).

* WRITE: any of ``goal`` / ``done_when`` / ``affected_paths`` / ``plan`` records
  revision N+1 — free, nobody is called, omitted fields carry over, identical content
  creates no revision. ``affected_paths`` is REQUIRED on the first revision (``[]`` =
  the work changes no files; owner 9=A); the body flag is computed at write.
* ASK: ``ask="council"`` or ``ask=[participant ids]`` is the ONLY paid act; it sends
  the current revision (the one this call wrote, or the latest) to exactly the named
  participants (``plan_ask``). Answers settle into the record and wake the mailbox;
  a body plan's first ask includes every owner-marked reviewer.
* READ: no arguments → the free state: the current revision, every participant's open
  objections (``not_adjudicated`` flagged), who last saw which revision, the latest
  contributions and the pending asks; pending seats whose producer finished while
  nobody listened are settled at $0 first.

Every refusal — a schema error, an old-form call (``spec``, ``review_disposition``,
``author_action``, …: a typed $0 refusal naming the new form, no shim), a missing first
``affected_paths``, an unknown participant, nothing sendable, the budget or deadline
rail — writes nothing and engages no gate. There is no cycle counter, no verdict, no
quorum and no fingerprint routing; the only hold is the finalization gate's
(``owner_hurry.force_plan_decision`` over ``plan_record.gate_projection``).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Mapping, Optional

from ouroboros import plan_record
from ouroboros.config import (
    get_finalization_grace_sec,
    get_llm_transport_read_timeout_sec,
    get_review_enforcement,
    get_task_abs_ceiling_sec,
    operation_window_sec,
)
from ouroboros.tools import plan_ask
from ouroboros.tools.plan_review_runtime import REVIEWER_EFFORT_SCHEMA as _REVIEWER_EFFORT_SCHEMA
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.tool_result import TOOL_CODE_SPECS, ToolResult, _publish_tool_result
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

WRITE_FIELDS = ("goal", "done_when", "affected_paths", "plan")
ASK_FIELDS = ("ask", "message", "attach", "reviewer_effort")
# The retired engine's arguments: a typed $0 refusal names the new form (no shim).
RETIRED_FIELDS = frozenset({"spec", "review_disposition", "author_action", "author_disposition", "confirm_fresh_panel",
                            "review_fingerprint", "items", "acceptance_claims", "evidence", "in_scope", "non_goals",
                            "invariants", "decisions", "deferred", "affected_resources"})


def _plan_task_tool_timeout_sec() -> float:
    """The outer settlement envelope of one call (never a cognition cutoff): an
    ``agent_session`` participant inherits the task's operation window, an API
    participant one transport read plus one finalization grace, and the entry covers
    either plus one grace."""
    return (max(float(get_llm_transport_read_timeout_sec() + get_finalization_grace_sec()),
                operation_window_sec(get_task_abs_ceiling_sec()))
            + get_finalization_grace_sec())


_DONE_WHEN_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "id": {"type": "string", "description": "Your stable name for this outcome (minted claim_N when absent); link verify_and_record receipts via criterion_id."},
            "claim": {"type": "string", "description": "The observable outcome acceptance will judge — what is true when the work is done, not a method."},
            "support": {"type": "string", "description": "How it will be shown, in your words: a command and its expected output, a screenshot and what it must show, a judgment and by whom, the owner confirming."},
            "surface": {"type": "string", "description": "Optional: the interface or place where the claim is observed."},
        },
        "required": ["claim"],
    },
    "description": ("What acceptance will judge, in the acceptance claim grammar (id / claim / support). "
                    "The LATEST revision's done_when binds acceptance; ingress claims from the task's own contract win over it."),
}

_SCHEMA = {
    "name": "plan_task",
    "description": (
        "Your plan for this work and the council that weighs it — code, research, a deliverable, a computer-use flow or "
        "an action in the world. Three effects, one tool. WRITE (free): goal, done_when, affected_paths and/or plan record "
        "a new revision; omitted fields carry over; identical content records nothing; affected_paths is required on the "
        "first revision ([] = the work changes no files; a path under Ouroboros's own body makes it a body plan, whose "
        "participants receive the constitutional pack). ASK (the only paid act): ask='council' sends the current revision "
        "to the review pool (Main alone when no reviewer is marked), ask=[ids] to exactly those participants — pool seats "
        "or any enabled catalog row (its objections are answered but never hold); a body plan's first ask includes every "
        "owner-marked reviewer. Participants answer in parallel the first time and see each other's answers on later "
        "asks; each receives the plan or its delta, the council so far, its own open objections, your message and your "
        "attachments. Answers arrive in your mailbox; READ (no arguments, free) shows the current revision, every open "
        "objection with who raised it, who saw which revision, the latest contributions and the pending asks. An "
        "objection stands until the participant who raised it withdraws it (ask that participant again after you "
        "revise or argue); nobody votes and there is no verdict. Under Blocking an open objection of an owner-marked "
        "reviewer holds your final answer; under Advisory and in Cyber Pro it is disclosed. The old spec/"
        "review_disposition form is refused at $0."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "goal": {"type": "string", "description": "The outcome the work serves, in your words (what the owner's ask means)."},
            "done_when": _DONE_WHEN_SCHEMA,
            "affected_paths": {
                "type": "array", "items": {"type": "string"},
                "description": ("The FILES the work will CHANGE — paths relative to the subject workspace root, absolute, or "
                                "file://; [] when it changes no files. Required on the first revision. The only field read as paths."),
            },
            "plan": {"type": "string", "description": ("Prose: your reading of the ask and the bar, the approach, constraints, "
                                                        "expensive-to-reverse decisions with their alternatives, risks.")},
            "ask": {
                "anyOf": [{"type": "string", "enum": ["council"]}, {"type": "array", "items": {"type": "string"}, "minItems": 1}],
                "description": ("Whom to ask now: 'council' = the review pool; or participant ids (pool seat ids, catalog row ids/handles). "
                                "Named recipients only — nothing is sent unless you ask."),
            },
            "message": {"type": "string", "description": "What you want from the asked participants now (what changed, what you decided and why, what to look at)."},
            "attach": {"type": "array", "items": {"type": "string"},
                       "description": "Evidence locators for the asked participants (paths with optional ::lines=A-B, task:<id>, chat:<id>); attached bounded, every absence named."},
            "reviewer_effort": {**_REVIEWER_EFFORT_SCHEMA, "description": "Optional panel strength for THIS ask (Auto rows, clamped into the owner's effort range)."},
        },
        "required": [],
    },
}


def get_tools():
    return [ToolEntry(name="plan_task", schema=_SCHEMA, handler=_handle_plan_task, timeout_sec=_plan_task_tool_timeout_sec())]


def _legacy_facts(state_root: Any, task_id: str) -> Dict[str, Any]:
    """The read-only projection of a v1/v2 ``plan_review_state`` (an in-flight task at
    upgrade keeps its open blocking findings as objections of their seats)."""
    from ouroboros.task_results import load_plan_review_state

    try:
        state = load_plan_review_state(state_root, task_id)
    except Exception:
        log.debug("legacy plan state unreadable for %s", task_id, exc_info=True)
        return {}
    author_plan = None
    if state.get("current_attempt"):
        from ouroboros.tools.plan_review_artifacts import current_author_plan

        try:
            author_plan = current_author_plan(state_root, task_id, state)
        except (OSError, ValueError):
            author_plan = None
    return plan_record.import_legacy_v2(state, author_plan=author_plan)


# ------------------------------------------------------------------ refusals

def _refuse(ctx: ToolContext, code: str, text: str) -> str:
    return _publish_tool_result(ctx, ToolResult(status=TOOL_CODE_SPECS[code].status, code=code, text=text))


def _arg_error(ctx: ToolContext, text: str) -> str:
    return _refuse(ctx, "TOOL_ARG_ERROR", "ERROR: PLAN_ARGS_INVALID: " + text + " Nothing was written and nobody was called.")


def _retired_form_refusal(ctx: ToolContext, keys: List[str]) -> str:
    return _refuse(ctx, "TOOL_ARG_ERROR", (
        "ERROR: PLAN_FORM_RETIRED: " + ", ".join(sorted(keys)) + " belong to the retired plan_task form (spec / "
        "review_disposition / author_action). The tool now has three effects: WRITE a revision with goal, done_when "
        "[{id, claim, support}], affected_paths ([] for no files; required on the first revision) and plan prose; ASK "
        "with ask='council' or ask=[participant ids] plus message/attach/reviewer_effort; READ with no arguments. "
        "Nothing was written and nobody was called."))


def _validate_write(params: Mapping[str, Any]) -> Optional[str]:
    for key in ("goal", "plan", "message"):
        if params.get(key) is not None and not isinstance(params[key], str):
            return f"{key} must be text."
    for key in ("affected_paths", "attach"):
        value = params.get(key)
        if value is not None and (not isinstance(value, list) or any(not isinstance(item, str) for item in value)):
            return f"{key} must be an array of strings."
    done_when = params.get("done_when")
    if done_when is not None:
        if not isinstance(done_when, list):
            return "done_when must be an array of {id, claim, support} objects."
        for index, row in enumerate(done_when):
            if not isinstance(row, Mapping) or not str(row.get("claim") or "").strip():
                return f"done_when[{index}] must be an object with a non-empty claim."
            if set(row) - {"id", "claim", "support", "surface"}:
                return f"done_when[{index}] carries unknown keys {sorted(set(row) - {'id', 'claim', 'support', 'surface'})}."
    ask = params.get("ask")
    if ask is not None and ask != "council" and (not isinstance(ask, list) or not ask
                                                or any(not isinstance(item, str) or not item.strip() for item in ask)):
        return "ask must be 'council' or a non-empty array of participant ids."
    effort = params.get("reviewer_effort")
    if effort not in (None, "") and str(effort) not in _REVIEWER_EFFORT_SCHEMA["enum"]:
        return f"reviewer_effort is not on the effort scale {list(_REVIEWER_EFFORT_SCHEMA['enum'])}."
    return None


# ------------------------------------------------------------------ handler

def _handle_plan_task(ctx: ToolContext, **params) -> str:
    given = {key: value for key, value in params.items() if value is not None}
    retired = sorted(key for key in given if key in RETIRED_FIELDS)
    if retired:
        return _retired_form_refusal(ctx, retired)
    unknown = sorted(key for key in given if key not in WRITE_FIELDS and key not in ASK_FIELDS)
    if unknown:
        return _arg_error(ctx, f"unknown fields {unknown}.")
    error = _validate_write(given)
    if error:
        return _arg_error(ctx, error)
    writes = any(key in given for key in WRITE_FIELDS)
    asks = given.get("ask") is not None
    if not writes and not asks and any(key in given for key in ("message", "attach", "reviewer_effort")):
        return _arg_error(ctx, "message, attach and reviewer_effort travel with an ask (ask='council' or ask=[ids]).")
    try:
        state_root, task_id = plan_ask.state_location(ctx)
    except ValueError as exc:
        return _refuse(ctx, "TOOL_ERROR", f"ERROR: {exc}. Nothing was written.")
    try:
        record = plan_record.load_plan_record(state_root, task_id)
    except plan_record.PlanRecordInvalid as exc:
        return _refuse(ctx, "TOOL_ERROR", f"ERROR: PLAN_RECORD_UNREADABLE: {exc}. Nothing was written; the recorded plan state stands.")
    legacy = _legacy_facts(state_root, task_id)
    latest = plan_record.latest_revision(record)
    revision_fields: Optional[Dict[str, Any]] = None
    body, body_note = False, ""
    if writes:
        if latest is None and given.get("affected_paths") is None:
            return _refuse(ctx, "TOOL_ARG_ERROR", (
                "ERROR: PLAN_FIRST_REVISION_NEEDS_AFFECTED_PATHS: the first revision must declare affected_paths — the files "
                "the work will change, as their own list; send [] when it changes no files. Nothing was written and nobody was called."))
        revision_fields = plan_record.compose_revision(
            latest, goal=given.get("goal"), plan=given.get("plan"), done_when=given.get("done_when"),
            affected_paths=given.get("affected_paths"))
        try:
            system_root, active_root = plan_ask.planning_roots(ctx)
        except ValueError as exc:
            return _refuse(ctx, "TOOL_ERROR", f"ERROR: PLAN_SUBJECT_ROOT_INVALID: {exc}. Nothing was written.")
        body, body_note = plan_ask.plan_is_body(ctx, revision_fields["affected_paths"], system_root=system_root, active_root=active_root)
    elif asks and latest is None:
        return _refuse(ctx, "TOOL_ARG_ERROR", (
            "ERROR: PLAN_ASK_NEEDS_A_PLAN: there is no revision to ask about yet — write one first (goal, done_when, "
            "affected_paths, plan), in this call or an earlier one. Nothing was written and nobody was called."))
    prepared = None
    if asks:
        candidate = dict(revision_fields, n=(int(latest.get("n") or 0) if latest else 0) + 1, body=body, body_note=body_note,
                         ts=utc_now_iso()) if revision_fields is not None else dict(latest)
        if revision_fields is not None and latest is not None and latest.get("content_sha256") == revision_fields["content_sha256"]:
            candidate = dict(latest)
        try:
            prepared = plan_ask.prepare_ask(
                ctx, record=record, revision=candidate, ask=given["ask"], message=str(given.get("message") or ""),
                attach=list(given.get("attach") or []), reviewer_effort=str(given.get("reviewer_effort") or ""),
                state_root=state_root, task_id=task_id, legacy=legacy)
        except plan_ask.PlanAskRefused as exc:
            if exc.attempt is None:
                return _refuse(ctx, exc.code, exc.text)
            return _refuse_unroutable(ctx, exc, revision_fields=revision_fields, body=body, body_note=body_note,
                                      state_root=state_root, task_id=task_id)
        except Exception as exc:
            log.error("plan_task ask preparation failed: %s", exc, exc_info=True)
            return _refuse(ctx, "TOOL_ERROR", f"ERROR: PLAN_ASK_FAILED: {exc}. Nothing was written and nobody was called.")
    notes: List[str] = []
    created = False
    if revision_fields is not None:
        try:
            record, latest, created = plan_record.append_revision(state_root, task_id, revision_fields, body=body, body_note=body_note)
        except (plan_record.PlanRecordInvalid, OSError, ValueError) as exc:
            return _refuse(ctx, "TOOL_ERROR", f"ERROR: PLAN_RECORD_WRITE_FAILED: {exc}.")
        if created:
            plan_ask._progress(ctx, f"📐 Plan: revision {latest.get('n')} recorded" + (" (body plan)" if body else "") + ".")
            notes.append(f"Revision {latest.get('n')} recorded" + (f" — {body_note}" if body_note else "") + ".")
        else:
            notes.append(f"Identical content: revision {latest.get('n')} stands, nothing new was recorded.")
        plan_ask._reference(ctx, task_id, record, state_root)
    effect = "write+ask" if (writes and asks) else "ask" if asks else "write" if writes else "read"
    if prepared is not None:
        record = plan_ask.dispatch_ask(ctx, prepared, revision=latest, state_root=state_root, task_id=task_id)
        notes.append(_ask_summary(prepared, record))
    if effect == "read":
        try:
            record = plan_ask.collect_pending(ctx, record, state_root=state_root, task_id=task_id)
        except Exception:
            log.warning("plan_task read: pending seats could not be collected", exc_info=True)
    return _publish(ctx, record, effect=effect, notes=notes, legacy=legacy)


def _refuse_unroutable(ctx: ToolContext, exc: Any, *, revision_fields: Optional[Dict[str, Any]], body: bool, body_note: str,
                       state_root: Any, task_id: str) -> str:
    """The one refusal that leaves a fact: the mind asked and NO named route could take the
    ask. The revision the call carried is recorded first (the plan exists), then the typed
    attempt (nobody called, nothing held); a Swarm obligation reads it as attempted."""
    written = ""
    revision = plan_record.latest_revision(plan_record.load_plan_record(state_root, task_id))
    try:
        if revision_fields is not None:
            record, revision, created = plan_record.append_revision(state_root, task_id, revision_fields, body=body, body_note=body_note)
            written = f" Revision {revision.get('n')} {'was recorded' if created else 'stands (identical content)'}."
            if created:
                plan_ask._progress(ctx, f"📐 Plan: revision {revision.get('n')} recorded" + (" (body plan)" if body else "") + ".")
        record = plan_record.append_attempt(state_root, task_id, {**exc.attempt, "revision": (revision or {}).get("n")})
    except (plan_record.PlanRecordInvalid, OSError, ValueError) as err:
        return _refuse(ctx, "TOOL_ERROR", f"ERROR: PLAN_RECORD_WRITE_FAILED: {err}.")
    plan_ask._progress(ctx, f"📐 Plan: ask on revision {(revision or {}).get('n')} reached nobody — "
                            + "; ".join(f"{sid}: {why}" for sid, why in (exc.attempt.get("not_sendable") or {}).items()) + ".")
    plan_ask._reference(ctx, task_id, record, state_root)
    return _refuse(ctx, exc.code, exc.text + written + " The attempt is on the plan record (asked, no route): a planning "
                   "obligation reads it as attempted — disclosed in your final answer, not held. Ask again when a route is available.")


def _ask_summary(prepared: Any, record: Mapping[str, Any]) -> str:
    ask = next((a for a in record.get("asks") or [] if a.get("ask_id") == prepared.ask_id), {})
    rows = ask.get("participants") or []
    sent = [p for p in rows if p.get("dispatched")]
    lines = [f"Ask {prepared.ask_id} on revision {ask.get('revision')}: sent to "
             + ", ".join(f"{p['seat_id']} ({p.get('model') or '?'}{'' if p.get('owner_marked') else ', added by you — its objections never hold'}"
                         f"{', continued' if p.get('continued') else ''})" for p in sent)
             + ("; not sent: " + "; ".join(f"{p['seat_id']} — {p.get('not_dispatched_reason') or p.get('error') or 'not sendable'}"
                                             for p in rows if not p.get("dispatched")) if any(not p.get("dispatched") for p in rows) else "")
             + "."]
    if ask.get("host_added"):
        lines.append("This is the first ask on a body plan, so every owner-marked reviewer was included: the host added "
                     + ", ".join(ask["host_added"]) + " beside the participants you named.")
    pending = [p["seat_id"] for p in sent if p.get("status") == "pending"]
    if pending:
        lines.append("Answers settle in the background and arrive in your mailbox (await_messages / wait_task); plan_task() reads them. "
                     f"Still working: {', '.join(pending)}.")
    return "\n".join(lines)


# ------------------------------------------------------------------ rendering

def _decision(ctx: ToolContext, record: Mapping[str, Any], legacy: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    from ouroboros.owner_hurry import latched
    from ouroboros.tools.review_helpers import review_enforcement_blocks

    metadata = getattr(ctx, "task_metadata", {})
    metadata = metadata if isinstance(metadata, Mapping) else {}
    return plan_record.gate_projection(
        record, enforcement=str(get_review_enforcement() or ""), cyber_pro=not review_enforcement_blocks("blocking"),
        hurry=latched(ctx) is not None, force_plan=metadata.get("force_plan") is True, legacy=legacy)


def _render_done_when(rows: List[Mapping[str, Any]]) -> str:
    if not rows:
        return "  (none stated yet)"
    return "\n".join(f"  - {r.get('id')}: {r.get('claim')}" + (f" — shown by: {r.get('support')}" if r.get("support") else "")
                     for r in rows)


def _failure_tag(participant: Mapping[str, Any]) -> str:
    """A failed seat by its TYPED facts — the code, the reset instant, the engine's reported
    sentence — never the raw error prose with its retry coach (BIBLE P5: the host puts no
    engine template in the mind's mouth). A row that reported nothing typed keeps a bounded
    first line of the error."""
    from ouroboros.utils import truncate_review_artifact

    code = str(participant.get("failure_code") or "")
    cause = str(participant.get("reported_cause") or "")
    reset = str(participant.get("reset_at") or "")
    tag = "failed" + (f"[{code}]" if code else "")
    if cause:
        tag += f' — reported cause: "{cause}"'
    elif participant.get("error"):
        first_line = str(participant["error"]).strip().splitlines()[0] if str(participant["error"]).strip() else ""
        tag += " — " + truncate_review_artifact(first_line, limit=160)
    if reset:
        tag += f" (reset at {reset})"
    return tag


def render_record(record: Mapping[str, Any], decision: Mapping[str, Any], legacy: Optional[Mapping[str, Any]] = None) -> str:
    """The read view as text: current revision, council, open objections, gate facts."""
    view = plan_record.read_view(record, legacy)
    revision = view.get("revision")
    if revision is None:
        imported = [o for o in (decision.get("open_objections") or []) if o.get("legacy")]
        lead = "No plan revision recorded for this task yet. Write one: goal, done_when, affected_paths ([] for no files), plan."
        if imported:
            lead += ("\n\nImported from this task's previous plan review (still open until their raisers withdraw them):\n- "
                     + "\n- ".join(f"{o['seat_id']} / {o['id']}: {o.get('text')}" for o in imported))
        return lead
    lines = [f"# Plan — revision {revision.get('n')} of {view['revisions']}"
             + (" (body plan: " + str(revision.get("body_note") or "changes Ouroboros's own body") + ")" if revision.get("body") else "")]
    lines += [f"goal: {revision.get('goal') or '(none)'}", "done_when:", _render_done_when(revision.get("done_when") or []),
              f"affected_paths: {json.dumps(revision.get('affected_paths') or [], ensure_ascii=False)}",
              "plan:", (str(revision.get("plan") or "").strip() or "(no prose)")
              + ("" if revision.get("plan_inline", True) else "  [prose moved to its exact source: " + str((revision.get("text_ref") or {}).get("path") or "") + "]"),
              ""]
    lines.append("## Council")
    if not view["asks"]:
        lines.append("No ask yet — nobody has seen this plan. plan_task(ask='council', message=...) sends it.")
    for ask in view["asks"]:
        parts = []
        for p in ask["participants"]:
            tag = p["status"] + (f" ({p['ignored_items']} malformed item{'' if p['ignored_items'] == 1 else 's'} ignored)"
                                 if p.get("ignored_items") else "")
            if p["status"] == "not_dispatched":
                tag = f"not sent — {p.get('not_dispatched_reason') or 'not sendable'}"
            elif p["status"] == "failed":
                tag = _failure_tag(p)
            elif p["status"] == "unparseable":
                tag = "unparseable" + (f" — {p.get('parse_error')}" if p.get("parse_error") else "")
            parts.append(f"{p['seat_id']} ({p.get('model') or '?'}{'' if p.get('owner_marked') else ', added'}): {tag}")
        lines.append(f"- ask {ask['ask_id']} on revision {ask['revision']}" + (f" — «{ask['message']}»" if ask.get("message") else "")
                     + ": " + "; ".join(parts))
    for attempt in view.get("attempts") or []:  # asks no route could take: nobody was called, nothing holds
        lines.append(f"- attempt at {attempt.get('ts')} on revision {attempt.get('revision')}: no route — "
                     + "; ".join(f"{sid}: {why}" for sid, why in (attempt.get("not_sendable") or {}).items()) + " (nobody was called)")
    seen = [f"{sid}: saw revision {v['last_seen_revision']}" + (f", last answered in ask {v['last_answered_ask']}" if v["last_answered_ask"] else ", never answered")
            for sid, v in view["participants"].items() if v.get("last_seen_revision") is not None]
    if seen:
        lines.append("who saw what: " + "; ".join(seen))
    lines.append("")
    lines.append("## Open objections")
    open_rows = decision.get("open_objections") or []
    if not open_rows:
        lines.append("none")
    for row in open_rows:
        flags = []
        if row.get("holds"):
            flags.append("holds your final answer under Blocking" if decision.get("enforcement") == "blocking" and not decision.get("decision_authority")
                         else "owner-marked reviewer — disclosed, not held under " + ("Cyber Pro" if decision.get("decision_authority") else "Advisory"))
        else:
            flags.append("added participant — never holds")
        if row.get("not_adjudicated"):
            flags.append("NOT ADJUDICATED in its raiser's last reply")
        if row.get("legacy"):
            flags.append("imported from the previous plan review")
        lines.append(f"- {row['seat_id']} / {row['id']} (ask {row.get('ask_id')}, revision {row.get('revision')}) [{'; '.join(flags)}]: {row.get('text')}")
    lines.append("Only the participant who raised an objection can withdraw it: ask it again (ask=[its id]) after you revise or answer it.")
    withdrawn = view.get("withdrawn") or []
    if withdrawn:
        lines.append("withdrawn: " + "; ".join(f"{w['seat_id']}/{w['id']} in ask {w['withdrawn_in']} — {w.get('why') or ''}" for w in withdrawn))
    if view.get("unknown_withdrawals"):
        lines.append("ignored withdrawals of objections the seat never raised: "
                     + "; ".join(f"{w['seat_id']}/{w['id']} in ask {w['ask_id']}" for w in view["unknown_withdrawals"]))
    contributions = [(sid, v["latest_prose"]) for sid, v in view["participants"].items() if v.get("latest_prose")]
    if contributions:
        lines.append("")
        lines.append("## Latest contributions")
        for sid, prose in contributions:
            lines.append(f"### {sid}\n{prose}")
    lines.append("")
    lines.append("## Gate")
    status = str(decision.get("status") or "")
    if status == "held":
        lines.append("HELD: under Blocking the open objections marked above hold your normal final answer until their raisers withdraw "
                     "them, the owner hurries or changes enforcement, or you stop honestly with unfinished work (finish_task action=stop).")
    elif status == "objections_open":
        lines.append("Open objections are disclosed in your final answer; nothing holds it.")
    elif status == "awaiting":
        lines.append(f"Answers still on their way for ask(s) {', '.join(decision.get('pending_asks') or view['pending_asks'])}; silence never holds.")
    elif status == "force_plan_unmet" and decision.get("allow") and decision.get("asks_unroutable"):
        lines.append("This task was admitted with a planning obligation; your ask reached nobody (no route could take it, "
                     f"{decision['asks_unroutable']} attempt{'' if decision['asks_unroutable'] == 1 else 's'} recorded) — disclosed in "
                     "your final answer, not held. Ask again when a route is available.")
    elif status == "force_plan_unmet":
        lines.append("This task was admitted with a planning obligation: ask the council before you finish.")
    else:
        lines.append("Nothing holds your final answer.")
    return "\n".join(lines)


def _publish(ctx: ToolContext, record: Mapping[str, Any], *, effect: str, notes: List[str],
             legacy: Optional[Dict[str, Any]] = None) -> str:
    state_root, task_id = plan_ask.state_location(ctx)
    legacy = _legacy_facts(state_root, task_id) if legacy is None else legacy
    decision = _decision(ctx, record, legacy)
    text = ("\n".join(notes) + "\n\n" if notes else "") + render_record(record, decision, legacy)
    latest = plan_record.latest_revision(record)
    return _publish_tool_result(ctx, ToolResult(status="ok", code="OK", text=text, meta={
        "plan_effect": effect,
        "plan_revision": int(latest.get("n") or 0) if latest else 0,
        "plan_open_objections": len(decision.get("open_objections") or []),
        "plan_holding": len(decision.get("holding") or []),
        "plan_pending_asks": len(decision.get("pending_asks") or []),
    }))


# The retired form reaches the handler through the registry's hidden-legacy seam so the
# refusal can name the new form; the model-visible schema never lists these keys.
setattr(_handle_plan_task, "_hidden_legacy_params", tuple(sorted(RETIRED_FIELDS)))
