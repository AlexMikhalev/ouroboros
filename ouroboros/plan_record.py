"""The per-task plan record: the mind's revisions, its asks to the council and
each participant's own replies, with every projection the host derives from them.

One intention, one record, three kinds of fact the mind writes through ``plan_task``:

* a REVISION — goal, ``done_when`` (the acceptance claim grammar: id / claim /
  support), ``affected_paths`` and the plan prose; omitted fields carry over,
  identical content creates no revision; the body flag is computed at write;
* an ASK — the revision it carried, the mind's message, the attachments, the
  participants it was dispatched to (each with the recorded fact whether it was an
  owner-marked reviewer OF THAT ASK) and the review-ledger record that retains the
  exact texts;
* a REPLY — one participant's parsed ``{prose, objections, withdrawn}``, or its
  typed absence (pending, failed, not dispatched, unparseable).

Everything else is derived here and never stored: the open objections (a fold over
EACH SEAT'S OWN replies — an objection stands until the same seat lists it under
``withdrawn``; omission keeps it and flags ``not_adjudicated``; an unparseable reply
changes nothing), who last saw which revision, the gate projection
(``gate_projection``) and the acceptance view (``acceptance_view``). There is no
cycle counter, no quorum, no aggregate and no fingerprint routing for plans.

Durable form: the task result's ``plan_record`` key (schema 3). The exact text of
every revision is retained once in the task's artifact store (``retain_revision``);
the index keeps the prose inline while it fits and names the exact source otherwise.
Finished v1/v2 ``plan_review_state`` records are never rewritten:
``import_legacy_v2`` reads them (open blocking findings of an in-flight task become
open objections of their seats; a closed wave's or an Advisory author's selected
claims become ``done_when`` with legacy provenance).
"""

from __future__ import annotations

import copy
import difflib
import json
import re
from hashlib import sha256
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from ouroboros.store import StoreError
from ouroboros.task_result_schema import require_writable_task_result_schema, stamp_task_result_schema
from ouroboros.task_store import patch_document, read_document
from ouroboros.utils import utc_now_iso

PLAN_RECORD_KEY = "plan_record"
PLAN_RECORD_VERSION = 3
# The bounded hot index: past it, the oldest prose moves out to its exact source.
_PLAN_RECORD_MAX_BYTES = 1_000_000
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
PARTICIPANT_STATUSES = frozenset({"pending", "answered", "unparseable", "failed", "not_dispatched"})
GATE_STATUSES = frozenset({"not_required", "force_plan_unmet", "held", "objections_open", "awaiting", "clear",
                           "rail_degraded", "unreadable"})
LEGACY_ASK_ID = "legacy"
_DONE_WHEN_KEYS = ("id", "claim", "support", "surface")


class PlanRecordInvalid(ValueError):
    """The durable plan record cannot be read as written."""


# ------------------------------------------------------------------ record shape

def empty_record() -> Dict[str, Any]:
    return {"schema_version": PLAN_RECORD_VERSION, "revisions": [], "asks": []}


def _as_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def normalize_done_when(raw: Any) -> List[Dict[str, str]]:
    """``done_when`` rows in the acceptance claim grammar (``normalize_acceptance_claims``
    mints ids, dedups them and drops empty claims); the stored row keeps id / claim /
    support (+ surface when given) and no priority."""
    from ouroboros.contracts.task_contract import normalize_acceptance_claims

    rows = []
    for row in normalize_acceptance_claims(raw if isinstance(raw, list) else []):
        kept = {key: str(row.get(key) or "") for key in _DONE_WHEN_KEYS if key != "surface"}
        if str(row.get("surface") or ""):
            kept["surface"] = str(row["surface"])
        rows.append(kept)
    return rows


def content_sha256(fields: Mapping[str, Any]) -> str:
    """Identity of a revision's content (provenance only, never routing)."""
    payload = {
        "goal": _as_text(fields.get("goal")), "plan": str(fields.get("plan") or ""),
        "done_when": [dict(row) for row in fields.get("done_when") or []],
        "affected_paths": [str(p) for p in fields.get("affected_paths") or []],
    }
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def compose_revision(latest: Optional[Mapping[str, Any]], *, goal: Any = None, plan: Any = None,
                     done_when: Any = None, affected_paths: Any = None) -> Dict[str, Any]:
    """The next revision's content: the given fields over the latest revision's
    (omitted fields carry over). ``done_when`` is normalized; paths are stripped."""
    base = latest or {}
    fields = {
        "goal": _as_text(goal) if goal is not None else str(base.get("goal") or ""),
        "plan": str(plan) if plan is not None else str(base.get("plan") or ""),
        "done_when": normalize_done_when(done_when) if done_when is not None else [dict(r) for r in base.get("done_when") or []],
        "affected_paths": ([_as_text(p) for p in affected_paths if _as_text(p)] if affected_paths is not None
                           else [str(p) for p in base.get("affected_paths") or []]),
    }
    fields["content_sha256"] = content_sha256(fields)
    return fields


def latest_revision(record: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    revisions = record.get("revisions") or []
    return revisions[-1] if revisions else None


def revision_by_number(record: Mapping[str, Any], n: Any) -> Optional[Dict[str, Any]]:
    return next((r for r in record.get("revisions") or [] if r.get("n") == n), None)


def revision_delta(previous: Optional[Mapping[str, Any]], current: Mapping[str, Any]) -> Dict[str, Any]:
    """What changed between two revisions: field flags, ``done_when`` by id (added /
    removed / reworded with both texts) and a unified diff of the plan prose."""
    if previous is None:
        return {"first": True}
    before = {str(r.get("id") or ""): r for r in previous.get("done_when") or []}
    after = {str(r.get("id") or ""): r for r in current.get("done_when") or []}
    reworded = [{"id": key, "before": before[key], "after": after[key]}
                for key in after if key in before and after[key] != before[key]]
    prose_before, prose_after = str(previous.get("plan") or ""), str(current.get("plan") or "")
    diff = list(difflib.unified_diff(prose_before.splitlines(), prose_after.splitlines(),
                                     fromfile=f"revision {previous.get('n')}", tofile=f"revision {current.get('n')}",
                                     lineterm="", n=2)) if prose_before != prose_after else []
    return {
        "first": False, "from": previous.get("n"), "to": current.get("n"),
        "goal_changed": str(previous.get("goal") or "") != str(current.get("goal") or ""),
        "plan_changed": prose_before != prose_after,
        "done_when_added": [dict(after[k]) for k in after if k not in before],
        "done_when_removed": [dict(before[k]) for k in before if k not in after],
        "done_when_reworded": reworded,
        "affected_paths_added": [p for p in current.get("affected_paths") or [] if p not in (previous.get("affected_paths") or [])],
        "affected_paths_removed": [p for p in previous.get("affected_paths") or [] if p not in (current.get("affected_paths") or [])],
        "body_changed": bool(previous.get("body")) != bool(current.get("body")),
        "plan_diff": diff,
    }


def validate_record(value: Any) -> Dict[str, Any]:
    """The stored record, validated; absent → empty; malformed → ``PlanRecordInvalid``."""
    if value in (None, {}):
        return empty_record()
    if not isinstance(value, dict) or value.get("schema_version") != PLAN_RECORD_VERSION:
        raise PlanRecordInvalid("PLAN_RECORD_INVALID: unsupported or malformed schema")
    revisions, asks = value.get("revisions"), value.get("asks")
    if not isinstance(revisions, list) or not isinstance(asks, list):
        raise PlanRecordInvalid("PLAN_RECORD_INVALID: revisions and asks must be lists")
    for index, revision in enumerate(revisions, start=1):
        if not isinstance(revision, dict) or revision.get("n") != index:
            raise PlanRecordInvalid("PLAN_RECORD_INVALID: revisions must be numbered 1..N in order")
        if not isinstance(revision.get("done_when"), list) or not isinstance(revision.get("affected_paths"), list):
            raise PlanRecordInvalid("PLAN_RECORD_INVALID: revision done_when and affected_paths must be lists")
    seen: set = set()
    for ask in asks:
        if not isinstance(ask, dict) or not _ID_RE.fullmatch(str(ask.get("ask_id") or "")) or ask["ask_id"] in seen:
            raise PlanRecordInvalid("PLAN_RECORD_INVALID: ask ids must be unique")
        seen.add(ask["ask_id"])
        if revision_by_number(value, ask.get("revision")) is None:
            raise PlanRecordInvalid("PLAN_RECORD_INVALID: an ask names a revision the record does not hold")
        if not isinstance(ask.get("participants"), list):
            raise PlanRecordInvalid("PLAN_RECORD_INVALID: ask participants must be a list")
        for participant in ask["participants"]:
            if not isinstance(participant, dict) or not str(participant.get("seat_id") or ""):
                raise PlanRecordInvalid("PLAN_RECORD_INVALID: a participant needs a seat id")
            if str(participant.get("status") or "") not in PARTICIPANT_STATUSES:
                raise PlanRecordInvalid("PLAN_RECORD_INVALID: unknown participant status")
    return {**empty_record(), **copy.deepcopy(value)}


# ------------------------------------------------------------------ reply parsing

def _first_json_object(text: str) -> Optional[Dict[str, Any]]:
    """The reply's one JSON object: the whole text, one fenced block, or the first
    balanced ``{…}`` the text contains (prose around it is tolerated)."""
    raw = str(text or "").strip()
    candidates = [raw]
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    start = raw.find("{")
    if start >= 0:
        depth, in_string, escape = 0, False, False
        for index in range(start, len(raw)):
            char = raw[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(raw[start:index + 1])
                    break
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


def parse_reply(text: str) -> tuple[Optional[Dict[str, Any]], str]:
    """A participant's reply → ``({prose, objections[{id,text}], withdrawn[{id,why}]}, "")``
    or ``(None, error)``. A missing list is an empty list (an omitted ``withdrawn``
    keeps every objection); a malformed item is an error for the whole reply, so a
    reply that changes anything is one the host could read entirely."""
    payload = _first_json_object(text)
    if payload is None:
        return None, "no JSON object found in the reply (prose-only or unparseable)"
    prose = payload.get("prose", "")
    if prose is not None and not isinstance(prose, str):
        return None, "prose must be text"
    reply: Dict[str, Any] = {"prose": str(prose or ""), "objections": [], "withdrawn": []}
    for key, text_key in (("objections", "text"), ("withdrawn", "why")):
        items = payload.get(key)
        if items is None:
            continue
        if not isinstance(items, list):
            return None, f"{key} must be an array"
        by_id: Dict[str, Dict[str, str]] = {}
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                return None, f"{key}[{index}] must be an object"
            item_id = _as_text(item.get("id"))
            if not item_id or not _ID_RE.fullmatch(item_id):
                return None, f"{key}[{index}].id must be a short identifier"
            body = item.get(text_key, "")
            if body is not None and not isinstance(body, str):
                return None, f"{key}[{index}].{text_key} must be text"
            by_id[item_id] = {"id": item_id, text_key: str(body or "")}
        reply[key] = list(by_id.values())
    return reply, ""


# ------------------------------------------------------------------ folds

def _reply_rows(record: Mapping[str, Any], legacy: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
    """Every participant row that carries a reply, in ask order, legacy first."""
    rows: List[Dict[str, Any]] = []
    for objection in (legacy or {}).get("open_objections") or []:
        rows.append({"ask_id": LEGACY_ASK_ID, "revision": 0, "seat_id": str(objection.get("seat_id") or ""),
                     "owner_marked": bool(objection.get("owner_marked", True)),
                     "reply": {"objections": [{"id": str(objection.get("id") or ""), "text": str(objection.get("text") or "")}],
                               "withdrawn": []}, "legacy": True})
    for ask in record.get("asks") or []:
        for participant in ask.get("participants") or []:
            if participant.get("status") == "answered" and isinstance(participant.get("reply"), dict):
                rows.append({"ask_id": ask.get("ask_id"), "revision": ask.get("revision"),
                             "seat_id": str(participant.get("seat_id") or ""),
                             "owner_marked": bool(participant.get("owner_marked")),
                             "reply": participant["reply"], "legacy": False})
    return rows


def fold_objections(record: Mapping[str, Any], legacy: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
    """Every objection ever raised, with its current standing: the fold over each seat's
    OWN replies. Raising or restating sets the text and the ask it answered in (and the
    owner-marked fact OF THAT ASK); only the same seat's ``withdrawn`` closes it; a reply
    that mentions neither leaves it open and flags ``not_adjudicated`` until the seat
    speaks to it again. A withdrawal of an id the seat never raised is recorded as
    ``unknown_withdrawal`` and changes nothing."""
    objections: Dict[tuple, Dict[str, Any]] = {}
    for row in _reply_rows(record, legacy):
        seat = row["seat_id"]
        reply = row["reply"]
        mentioned: set = set()
        for item in reply.get("objections") or []:
            key = (seat, str(item.get("id") or ""))
            mentioned.add(key)
            current = objections.get(key)
            if current is None:
                objections[key] = current = {"seat_id": seat, "id": key[1], "raised_in": row["ask_id"],
                                             "raised_revision": row["revision"], "legacy": row["legacy"]}
            current.update(text=str(item.get("text") or ""), ask_id=row["ask_id"], revision=row["revision"],
                           owner_marked=row["owner_marked"], open=True, not_adjudicated=False,
                           withdrawn_in="", why="")
        for item in reply.get("withdrawn") or []:
            key = (seat, str(item.get("id") or ""))
            mentioned.add(key)
            current = objections.get(key)
            if current is None or not current.get("open"):
                objections.setdefault(("unknown", f"{seat}:{key[1]}:{row['ask_id']}"), {
                    "seat_id": seat, "id": key[1], "unknown_withdrawal": True, "ask_id": row["ask_id"],
                    "open": False, "why": str(item.get("why") or "")})
                continue
            current.update(open=False, not_adjudicated=False, withdrawn_in=row["ask_id"], why=str(item.get("why") or ""))
        for key, current in objections.items():
            if key[0] == seat and current.get("open") and key not in mentioned and not current.get("unknown_withdrawal"):
                current["not_adjudicated"] = True
    out = []
    for key, current in objections.items():
        if current.get("unknown_withdrawal"):
            out.append(current)
            continue
        out.append({**current, "holds": bool(current.get("open") and current.get("owner_marked"))})
    return out


def open_objections(record: Mapping[str, Any], legacy: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
    return [o for o in fold_objections(record, legacy) if o.get("open")]


def participant_views(record: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Per seat: the revision it last saw (the newest ask that dispatched it), the ask
    it last answered, and its latest prose."""
    views: Dict[str, Dict[str, Any]] = {}
    for ask in record.get("asks") or []:
        for participant in ask.get("participants") or []:
            seat = str(participant.get("seat_id") or "")
            view = views.setdefault(seat, {"seat_id": seat, "model": str(participant.get("model") or ""),
                                           "owner_marked": bool(participant.get("owner_marked")),
                                           "last_seen_revision": None, "last_seen_ask": "", "last_answered_ask": "",
                                           "latest_prose": "", "status": ""})
            view["model"] = str(participant.get("model") or view["model"])
            view["status"] = str(participant.get("status") or "")
            if participant.get("dispatched"):
                view["last_seen_revision"], view["last_seen_ask"] = ask.get("revision"), str(ask.get("ask_id") or "")
            if participant.get("status") == "answered" and isinstance(participant.get("reply"), dict):
                view["last_answered_ask"] = str(ask.get("ask_id") or "")
                view["latest_prose"] = str(participant["reply"].get("prose") or "")
    return views


def pending_asks(record: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Asks with at least one participant still pending (dispatched, no settled answer)."""
    return [ask for ask in record.get("asks") or []
            if any(p.get("status") == "pending" for p in ask.get("participants") or [])]


def asks_dispatched(record: Mapping[str, Any]) -> int:
    """How many asks reached at least one reviewer (the force_plan fact)."""
    return sum(1 for ask in record.get("asks") or []
               if any(p.get("dispatched") for p in ask.get("participants") or []))


def engaged(record: Mapping[str, Any]) -> bool:
    return bool(record.get("revisions") or record.get("asks"))


# ------------------------------------------------------------------ gate

def gate_projection(record: Mapping[str, Any], *, enforcement: str, cyber_pro: bool, hurry: bool,
                    force_plan: bool, hard_rail: str = "", legacy: Optional[Mapping[str, Any]] = None,
                    unreadable: bool = False) -> Dict[str, Any]:
    """The ONE finalization decision over the plan record (pure).

    ``held`` ⟺ Blocking, not Cyber Pro, no owner hurry, and some open objection was
    raised by an owner-marked reviewer of the ask it answered — for any task. Silence,
    pending asks, refusals and mind-added critics never hold. A Swarm ``force_plan``
    stays unmet until one ask reached at least one reviewer: under Blocking that holds
    the final answer; Advisory, the owner's hurry and Cyber Pro disclose it instead once
    a plan exists (a task that wrote no plan at all is still held to write one). An
    unreadable record is engaged, never absent.
    """
    policy = "blocking" if str(enforcement or "").lower() == "blocking" else "advisory"
    legacy_engaged = bool((legacy or {}).get("engaged"))
    if not force_plan and not unreadable and not engaged(record) and not legacy_engaged:
        return {"required": False, "allow": True, "status": "not_required", "enforcement": policy,
                "open_objections": [], "holding": [], "pending_asks": [], "revision": 0}
    objections = open_objections(record, legacy)
    holding = [o for o in objections if o.get("holds")]
    pending = [str(a.get("ask_id") or "") for a in pending_asks(record)] + [str(x) for x in (legacy or {}).get("pending_asks") or []]
    dispatched = asks_dispatched(record) + int((legacy or {}).get("asks_dispatched") or 0)
    latest = latest_revision(record)
    decision: Dict[str, Any] = {
        "required": True, "self_opened": not force_plan, "enforcement": policy,
        "open_objections": [{k: o.get(k) for k in ("seat_id", "id", "text", "holds", "not_adjudicated", "ask_id",
                                                     "revision", "owner_marked", "legacy")} for o in objections],
        "holding": [f"{o['seat_id']}:{o['id']}" for o in holding],
        "pending_asks": pending, "asks_dispatched": dispatched,
        "force_plan_met": not force_plan or dispatched > 0,
        "revision": int(latest.get("n") or 0) if latest else 0,
        "unreadable": bool(unreadable), "reason": str(hard_rail or ""),
        "source": "legacy_v2" if (legacy_engaged and not engaged(record)) else "plan_record",
    }
    if cyber_pro:
        decision["decision_authority"] = "cyber_pro"
    if hurry and policy == "blocking":
        decision["owner_hurry_local_advisory"] = True
        decision["configured_enforcement"] = "blocking"
    if hard_rail:
        status, allow = "rail_degraded", True
    elif cyber_pro:
        status, allow = ("objections_open" if objections else "awaiting" if pending else "clear"), True
    elif unreadable:
        status, allow = "unreadable", False  # a gate that cannot read its authority is engaged, not absent
    elif force_plan and dispatched == 0:
        # Blocking holds until one ask reached a reviewer. Advisory and the owner's hurry never
        # hold a plan that exists: an unasked (or refused) plan is disclosed; a task that wrote no
        # plan at all is still held to write one (the Swarm admitted it on that condition).
        tried = engaged(record) or legacy_engaged
        status, allow = "force_plan_unmet", not ((policy == "blocking" and not hurry) or not tried)
    elif holding and policy == "blocking" and not hurry:
        status, allow = "held", False
    elif objections:
        status, allow = "objections_open", True
    elif pending:
        status, allow = "awaiting", True
    else:
        status, allow = "clear", True
    decision.update(status=status, allow=allow, review_only_awaited=status == "awaiting")
    return decision


# ------------------------------------------------------------------ acceptance

def acceptance_view(record: Mapping[str, Any], ingress_claims: List[Dict[str, Any]], *,
                    legacy: Optional[Mapping[str, Any]] = None) -> tuple[List[Dict[str, str]], str, Dict[str, Any]]:
    """``(claims, source, exhibit)`` for task acceptance: ingress claims win; otherwise
    the LATEST revision's ``done_when`` binds, open objections or not (legacy selected
    claims when the task only has a v1/v2 record). The exhibit carries the ACTUAL text
    of every revision's ``done_when``, which revisions the council saw, and the
    objections open now — and, when ingress wins, the adopted promises stay visible."""
    from ouroboros.contracts.task_contract import normalize_acceptance_claims

    revisions = list(record.get("revisions") or [])
    seen_by: Dict[int, List[str]] = {}
    for ask in record.get("asks") or []:
        if any(p.get("dispatched") for p in ask.get("participants") or []):
            seen_by.setdefault(int(ask.get("revision") or 0), []).append(str(ask.get("ask_id") or ""))
    history = [{
        "n": r.get("n"), "ts": r.get("ts"), "goal": r.get("goal"), "done_when": [dict(x) for x in r.get("done_when") or []],
        "affected_paths": list(r.get("affected_paths") or []), "body": bool(r.get("body")),
        "seen_by_asks": seen_by.get(int(r.get("n") or 0), []),
    } for r in revisions]
    objections = [{k: o.get(k) for k in ("seat_id", "id", "text", "holds", "not_adjudicated", "ask_id", "revision")}
                  for o in open_objections(record, legacy)]
    latest = latest_revision(record)
    exhibit: Dict[str, Any] = {"revisions": history, "open_objections": objections}
    if not revisions and legacy:
        exhibit["legacy"] = {k: legacy.get(k) for k in ("done_when_source", "fingerprint") if legacy.get(k)}
    if ingress_claims:
        exhibit["binding"] = "ingress_contract"
        if latest:
            exhibit["adopted_promises"] = [dict(x) for x in latest.get("done_when") or []]
        return ingress_claims, "ingress_contract", exhibit
    if latest is not None:
        exhibit["binding"] = f"latest_revision:{latest.get('n')}"
        return normalize_acceptance_claims(latest.get("done_when") or []), "plan_review", exhibit
    legacy_claims = normalize_acceptance_claims((legacy or {}).get("done_when") or [])
    if legacy_claims:
        exhibit["binding"] = str((legacy or {}).get("done_when_source") or "legacy")
        return legacy_claims, "plan_review", exhibit
    exhibit["binding"] = "none"
    return [], "", exhibit


# ------------------------------------------------------------------ legacy import

def import_legacy_v2(state: Any, *, author_plan: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Read-only projection of a v1/v2 ``plan_review_state``: ``engaged`` (the task once
    entered the old gate), the OPEN blocking findings of the current open wave as
    objections of their seats (pool rows of that wave were owner-marked), and the
    claims the old organ had selected (a CLOSED current wave's spec claims, or an
    Advisory author ``finish`` selection) as ``done_when`` with legacy provenance, and
    an open wave whose reviewers had not all answered as one pending ask (``legacy wave
    <fingerprint>``: silence never holds, but the final answer discloses it).
    Malformed attempts and waves are history, never new objections or promises."""
    if not isinstance(state, dict):
        return {"engaged": False, "open_objections": [], "done_when": [], "done_when_source": "", "asks_dispatched": 0,
                "pending_asks": []}
    waves = [w for w in state.get("waves") or [] if isinstance(w, dict)]
    attempt = state.get("current_attempt") if isinstance(state.get("current_attempt"), dict) else {}
    legacy_v1 = state.get("legacy_v1_projection") if isinstance(state.get("legacy_v1_projection"), dict) else {}
    engaged_fact = bool(waves or attempt or legacy_v1.get("status") not in (None, "", "absent"))
    fingerprint = str(attempt.get("fingerprint") or "")
    matched = next((w for w in waves if str(w.get("request_fingerprint") or "") == fingerprint), None) if fingerprint else None
    # Claims come only from the wave the attempt still points at; objections and owed answers come
    # from the LATEST wave even when the author moved on (a changed spec at the cap, an author plan):
    # a reviewer's standing objection is withdrawn by its raiser, never by a revision (DESIGN §8).
    current = matched if matched is not None else (waves[-1] if waves else None)
    objections: List[Dict[str, Any]] = []
    done_when: List[Dict[str, Any]] = []
    source = ""
    pending: List[str] = []
    if isinstance(current, dict) and not current.get("closed"):
        actors = [a for a in current.get("actors") or [] if isinstance(a, dict)]
        if current.get("custody_pending") or any(
                a.get("late_result_pending") or str(a.get("operation_state") or "") == "pending_dispatch" for a in actors):
            pending.append(f"legacy wave {str(current.get('request_fingerprint') or fingerprint)[:12] or '?'}")
        for finding in current.get("findings") or []:
            if not isinstance(finding, dict) or str(finding.get("class") or "") != "blocking":
                continue
            seat = str(finding.get("slot") or "")
            fid = str(finding.get("finding_id") or finding.get("id") or "")
            if seat and fid:
                objections.append({"seat_id": seat, "id": fid, "owner_marked": True,
                                   "text": str(finding.get("summary") or ""), "fingerprint": str(current.get("request_fingerprint") or fingerprint)})
    author = (author_plan or {}).get("author_disposition") if isinstance(author_plan, Mapping) else None
    if isinstance(author, Mapping) and str(author.get("action") or "finish") == "finish" and str(author.get("enforcement") or "") == "advisory":
        spec = author_plan.get("spec") if isinstance(author_plan.get("spec"), Mapping) else {}
        done_when, source = normalize_done_when(spec.get("acceptance_claims")), "legacy_author_plan"
    elif isinstance(matched, dict) and matched.get("closed"):
        spec = matched.get("spec") if isinstance(matched.get("spec"), Mapping) else {}
        done_when, source = normalize_done_when(spec.get("acceptance_claims") or matched.get("acceptance_claims")), "legacy_closed_wave"
    elif legacy_v1.get("status") == "closed":
        done_when, source = normalize_done_when(legacy_v1.get("acceptance_claims")), "legacy_closed_wave"
    return {"engaged": engaged_fact, "open_objections": objections, "done_when": done_when, "done_when_source": source,
            "fingerprint": fingerprint, "pending_asks": pending,
            # a wave reached its reviewers unless the old engine marked it unpaid with nothing to show
            # for it (a free replay, an all-skipped dispatch); roster rows, findings or owed custody prove the send
            "asks_dispatched": sum(1 for w in waves if w.get("paid") is not False or w.get("actors") or w.get("findings")
                                   or w.get("custody_pending"))}


# ------------------------------------------------------------------ read view

def read_view(record: Mapping[str, Any], legacy: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The free state the mind reads: the current revision, open objections per
    participant (with ``not_adjudicated``), who last saw which revision, the latest
    contributions and the pending asks."""
    latest = latest_revision(record)
    objections = fold_objections(record, legacy)
    by_seat: Dict[str, List[Dict[str, Any]]] = {}
    for objection in objections:
        if objection.get("open"):
            by_seat.setdefault(objection["seat_id"], []).append(objection)
    views = participant_views(record)
    return {
        "revision": copy.deepcopy(latest) if latest else None,
        "revisions": len(record.get("revisions") or []),
        "asks": [{"ask_id": a.get("ask_id"), "ts": a.get("ts"), "revision": a.get("revision"), "message": a.get("message"),
                  "participants": [{k: p.get(k) for k in ("seat_id", "model", "owner_marked", "status", "dispatched",
                                                            "not_dispatched_reason", "error", "parse_error",
                                                            "failure_code", "reported_cause", "reset_at")}
                                   for p in a.get("participants") or []],
                  "record_id": a.get("record_id")} for a in record.get("asks") or []],
        "participants": views,
        "open_objections": {seat: [{k: o.get(k) for k in ("id", "text", "ask_id", "revision", "holds", "not_adjudicated", "legacy")}
                                   for o in rows] for seat, rows in by_seat.items()},
        "withdrawn": [{k: o.get(k) for k in ("seat_id", "id", "withdrawn_in", "why")}
                      for o in objections if not o.get("open") and not o.get("unknown_withdrawal")],
        "unknown_withdrawals": [{k: o.get(k) for k in ("seat_id", "id", "ask_id", "why")} for o in objections if o.get("unknown_withdrawal")],
        "pending_asks": [str(a.get("ask_id") or "") for a in pending_asks(record)],
    }


# ------------------------------------------------------------------ durable index

def load_plan_record(root: Any, task_id: str) -> Dict[str, Any]:
    """The task's plan record (empty when the task has none); malformed → ``PlanRecordInvalid``."""
    from ouroboros.task_results import validate_task_id

    document = read_document(root, validate_task_id(task_id))
    if document is None:
        return empty_record()
    return validate_record(document.get(PLAN_RECORD_KEY))


def _size(record: Mapping[str, Any]) -> int:
    return len(json.dumps(record, ensure_ascii=False, default=str).encode("utf-8"))


def _fit(record: Dict[str, Any]) -> Dict[str, Any]:
    """Move the oldest inline prose out to its exact source while the index is over
    its bound; the latest revision and the newest ask stay whole."""
    if _size(record) <= _PLAN_RECORD_MAX_BYTES:
        return record
    for revision in record.get("revisions", [])[:-1]:
        if revision.get("plan_inline", True) and revision.get("text_ref"):
            revision.update(plan="", plan_inline=False)
            if _size(record) <= _PLAN_RECORD_MAX_BYTES:
                return record
    for ask in record.get("asks", [])[:-1]:
        for participant in ask.get("participants") or []:
            reply = participant.get("reply")
            if isinstance(reply, dict) and reply.get("prose_inline", True) and reply.get("prose"):
                reply.update(prose="", prose_inline=False)
                if _size(record) <= _PLAN_RECORD_MAX_BYTES:
                    return record
    return record


def _update_plan_record(root: Any, task_id: str, mutator: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    """Strict transactional update of the task's plan record (no fallback write)."""
    from ouroboros.task_results import STATUS_RUNNING, validate_task_id

    path = f"task:{validate_task_id(task_id)}"

    def _merge(existing: Dict[str, Any]) -> Dict[str, Any]:
        require_writable_task_result_schema(existing, path)
        record = validate_record(_fit(mutator(validate_record(existing.get(PLAN_RECORD_KEY)))))
        now = utc_now_iso()
        return stamp_task_result_schema({
            **existing, PLAN_RECORD_KEY: record, "task_id": task_id,
            "status": str(existing.get("status") or STATUS_RUNNING),
            "ts": str(existing.get("ts") or now), "updated_at": now,
        })

    try:
        updated = patch_document(root, task_id, _merge)
    except StoreError as exc:
        raise PlanRecordInvalid(f"PLAN_RECORD_UNAVAILABLE: {exc}") from exc
    return validate_record(updated.get(PLAN_RECORD_KEY))


def retain_revision(root: Any, task_id: str, fields: Mapping[str, Any], *, n: int, ts: str) -> Dict[str, Any]:
    """The exact text of one revision as a task source (write-once by content)."""
    from ouroboros.artifacts import store_actor_source_bytes

    payload = {"kind": "plan_revision", "task_id": str(task_id), "n": int(n), "ts": ts,
               **{key: fields.get(key) for key in ("goal", "plan", "done_when", "affected_paths", "content_sha256", "body", "body_note")}}
    return store_actor_source_bytes(
        root, task_id, category="context_checkpoints", source_id=f"plan-revision-{int(n):04d}",
        data=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"),
        extension="json")


def append_revision(root: Any, task_id: str, fields: Mapping[str, Any], *, body: bool, body_note: str = "") -> tuple[Dict[str, Any], Dict[str, Any], bool]:
    """Record revision N+1 with the given CONTENT (``compose_revision`` output) unless it
    is byte-identical to the latest → ``(record, revision, created)``."""
    current = load_plan_record(root, task_id)
    latest = latest_revision(current)
    if latest is not None and str(latest.get("content_sha256") or "") == str(fields.get("content_sha256") or ""):
        return current, latest, False
    n, ts = (int(latest.get("n") or 0) if latest else 0) + 1, utc_now_iso()
    revision = {"n": n, "ts": ts, "goal": str(fields.get("goal") or ""), "plan": str(fields.get("plan") or ""),
                "plan_inline": True, "done_when": [dict(r) for r in fields.get("done_when") or []],
                "affected_paths": [str(p) for p in fields.get("affected_paths") or []],
                "body": bool(body), "body_note": str(body_note or ""),
                "content_sha256": str(fields.get("content_sha256") or content_sha256(fields))}
    revision["text_ref"] = retain_revision(root, task_id, revision, n=n, ts=ts)

    def _append(record: Dict[str, Any]) -> Dict[str, Any]:
        head = latest_revision(record)
        if head is not None and str(head.get("content_sha256") or "") == revision["content_sha256"]:
            return record  # a concurrent identical write landed first
        record["revisions"] = [*record.get("revisions", []), {**revision, "n": (int(head.get("n") or 0) if head else 0) + 1}]
        return record

    record = _update_plan_record(root, task_id, _append)
    stored = latest_revision(record) or revision
    return record, stored, stored.get("content_sha256") == revision["content_sha256"] and stored.get("ts") == ts


def next_ask_id(record: Mapping[str, Any]) -> str:
    used = {str(a.get("ask_id") or "") for a in record.get("asks") or []}
    n = len(used) + 1
    while f"a{n}" in used:
        n += 1
    return f"a{n}"


def append_ask(root: Any, task_id: str, ask: Mapping[str, Any]) -> Dict[str, Any]:
    """Record one dispatched ask (participants with their typed dispatch state)."""
    def _append(record: Dict[str, Any]) -> Dict[str, Any]:
        if any(str(a.get("ask_id") or "") == str(ask.get("ask_id") or "") for a in record.get("asks", [])):
            raise PlanRecordInvalid(f"PLAN_RECORD_INVALID: ask {ask.get('ask_id')!r} is already recorded")
        record["asks"] = [*record.get("asks", []), copy.deepcopy(dict(ask))]
        return record

    return _update_plan_record(root, task_id, _append)


def update_participant(root: Any, task_id: str, *, ask_id: str, seat_id: str, facts: Mapping[str, Any]) -> Dict[str, Any]:
    """Merge settlement facts (status, reply, error, cost, refs…) into one participant row."""
    def _merge(record: Dict[str, Any]) -> Dict[str, Any]:
        for ask in record.get("asks", []):
            if str(ask.get("ask_id") or "") != str(ask_id):
                continue
            for participant in ask.get("participants") or []:
                if str(participant.get("seat_id") or "") == str(seat_id):
                    participant.update(copy.deepcopy(dict(facts)))
                    return record
        raise PlanRecordInvalid(f"PLAN_RECORD_INVALID: no participant {seat_id!r} in ask {ask_id!r}")

    return _update_plan_record(root, task_id, _merge)


def pending_participants(record: Mapping[str, Any]) -> Iterable[tuple[Dict[str, Any], Dict[str, Any]]]:
    """``(ask, participant)`` pairs still pending, oldest first."""
    for ask in record.get("asks") or []:
        for participant in ask.get("participants") or []:
            if participant.get("status") == "pending":
                yield ask, participant
