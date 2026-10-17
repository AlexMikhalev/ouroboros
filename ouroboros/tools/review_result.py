"""Result review — ``review_change(subject="result")``: does this task's result do what was asked?

One subject of the one review machinery. This module composes the subject — what was asked, the
reply, what the task produced and the host-attested verification — and the result brief; the wave
itself (the panel, custody, the paid stamp, $0 reuse, a rejoin, leaving on an addressed message,
settlement and the ledger record) is ``review_change``'s own. Every seat answers one question,
judged on the result, never on the method: the work trace is not part of the subject.

The subject is frozen once, at the call: its exact text is retained in the task's artifact store
and its sha is the wave's identity, so a second call over the same reply, material and request
reuses the settled record at $0. Retrieving seats read the retained text and the produced files in
place; packet seats receive the text inline. Produced files are named by path and content hash,
never copied.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pathlib
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

RESULT_LAYER = "result"
RESULT_KIND = "result"
RESULT_ROOT_KIND = "task_result"
RESULT_USER_TURN = "Review the task result against its request, as the instructions above describe."
RESULT_RUBRIC = """\
You review the RESULT of one task, not a code change. The subject has four parts: what was asked (the
task, its owner's words and the claims it was held to), the reply the task gives, what the task produced
(files and effects, named by path and content hash) and the host-attested verification receipts.

Answer one question: does this result do what was asked?
- Judge the result, never the method: how the work was done is not under review.
- A partial result that says plainly what it did not do is honest, not a defect. A result that silently
  narrows what was asked, claims what it did not do, or contradicts its own material is a defect.
- Read the produced files you can reach; a claim about a file must match its content.
- A finding names the concrete gap, where it is, and what would close it.

Severity: "critical" when the result fails what was asked or misstates what it delivers, so it should not
be delivered as it is; "advisory" for a real improvement that does not block delivery.
"""


def refuse_when_off() -> None:
    """Task review Off means no result review at all, whoever asks; change review and ordinary
    child critics stay available. A typed refusal before anything is built or paid."""
    from ouroboros.config import get_task_review_mode

    if get_task_review_mode() == "off":
        from ouroboros.tools.review_change import ReviewChangeArgumentError

        raise ReviewChangeArgumentError(
            "result review is off on this install (task review mode Off): no result review runs; "
            "change review and ordinary child critics stay available")


def result_rules() -> Dict[str, Any]:
    """The rules a result wave is judged by, in the checklist facts' own shape."""
    digest = hashlib.sha256(RESULT_RUBRIC.encode("utf-8")).hexdigest()
    return {"checklist_hash": digest, "rules_source": {"path": "ouroboros/tools/review_result.py", "sha": digest}}


def latest_reply(ctx: Any) -> str:
    """The reply this task would deliver now (its retained delivery candidate), "" when none."""
    return str(getattr(getattr(ctx, "_delivery_candidate", None), "full_text", "") or "")


def _file_identity(path: pathlib.Path) -> Dict[str, Any]:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return {"size": path.stat().st_size, "sha256": digest.hexdigest()}
    except FileNotFoundError:
        return {"size": 0, "sha256": "deleted"}
    except OSError as exc:
        return {"size": 0, "sha256": f"unreadable ({type(exc).__name__})"}


def produced_material(ctx: Any) -> Dict[str, Any]:
    """What this task produced: its registered artifacts, its workspace changes against the
    task's own baseline, and the effects an extension tool declared (``observed_effect``).
    Inputs, checkpoints and the expected result a task was given are not production. A capture
    that fails is named as a gap, never filled in."""
    out: Dict[str, Any] = {"files": [], "effects": [], "issues": [], "workspace": ""}
    root, task_id = getattr(ctx, "drive_root", None), str(getattr(ctx, "task_id", "") or "")
    if root is None or not task_id:
        return out
    try:
        from ouroboros.artifacts import collect_task_artifact_records, registered_task_artifact

        for row in collect_task_artifact_records(root, task_id, measure=False, strict=True, require_registered=True):
            name = str(row.get("relpath") or row.get("name") or "")
            if not name or not row.get("size") or not registered_task_artifact(root, task_id, name):
                continue
            path = pathlib.Path(str(row["path"]))
            out["files"].append({"path": str(path), "kind": "artifact", **_file_identity(path)})
    except Exception as exc:
        out["issues"].append(f"task artifacts could not be listed ({type(exc).__name__}: {exc})")
    try:
        from ouroboros.mutation_attribution import attributed_git_candidates

        getter = getattr(ctx, "active_repo_dir", None)
        repo = getter() if callable(getter) else getattr(ctx, "repo_dir", None)
        if repo:
            snapshot = attributed_git_candidates(getattr(ctx, "budget_drive_root", None) or root, task_id, repo)
            candidates = [str(c.get("path") if isinstance(c, dict) else c) for c in snapshot.get("candidates") or []]
            for relative in candidates:
                path = pathlib.Path(str(repo)) / relative
                out["files"].append({"path": str(path), "kind": "workspace change", **_file_identity(path)})
            if candidates:
                out["workspace"] = str(pathlib.Path(str(repo)).resolve(strict=False))
            elif snapshot.get("blockers") and snapshot.get("blockers") != ["baseline_missing"]:
                out["issues"].append("workspace changes not attributed: " + ", ".join(map(str, snapshot["blockers"])))
    except Exception as exc:
        out["issues"].append(f"workspace changes could not be attributed ({type(exc).__name__}: {exc})")
    out["effects"] = _declared_effects(ctx, root, task_id, out["issues"])
    return out


def _declared_effects(ctx: Any, root: Any, task_id: str, issues: List[str]) -> List[Dict[str, Any]]:
    """Extension tools' own execution receipts (``PluginAPI.register_tool``): read exactly as the producer
    wrote them, never inferred from a method or backend name."""
    from ouroboros.artifacts import materialize_tool_result_source
    from ouroboros.extension_surface_names import parse_extension_surface_name

    effects: List[Dict[str, Any]] = []
    trace = getattr(ctx, "_execution_trace", None) or {}
    for call in trace.get("tool_calls") or []:
        if not isinstance(call, dict) or call.get("is_error") or not parse_extension_surface_name(str(call.get("tool") or "")):
            continue
        try:
            text, complete, gap = materialize_tool_result_source(root, task_id, call)
            if not complete:
                issues.append(f"effect receipt of {call.get('tool')} unavailable ({gap})")
                continue
            payload = json.loads(text) if isinstance(text, str) else {}
        except (OSError, ValueError, TypeError) as exc:
            issues.append(f"effect receipt of {call.get('tool')} unreadable ({type(exc).__name__}: {exc})")
            continue
        if isinstance(payload, dict) and payload.get("observed_effect") is True:
            effects.append({"tool": str(call.get("tool") or ""), "receipt": text})
    return effects


def _ask(ctx: Any) -> str:
    """What was asked, host-attested: the task's own request fields, the claims it is held to
    (an ingress contract's, else the latest plan revision's) and its owner's words."""
    from ouroboros.owner_words import owner_words_text
    from ouroboros.review_evidence_sections import _accept_effective_claims, _accept_task_contract

    contract = _accept_task_contract(ctx)
    lines: List[str] = []
    for key in ("goal", "objective", "requirements", "interface", "expected_output"):
        value = contract.get(key)
        if value not in (None, "", [], {}):
            lines.append(f"{key}: {value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)}")
    try:
        claims, source, _exhibit = _accept_effective_claims(
            ctx, contract, getattr(ctx, "drive_root", None), str(getattr(ctx, "task_id", "") or ""))
    except Exception as exc:
        claims, source = [], ""
        lines.append(f"claims: unavailable ({type(exc).__name__}: {exc})")
    if claims:
        lines.append(f"claims the result is held to ({source or 'task contract'}):")
        lines.extend(f"- {claim.get('claim') or claim}" + (f" (support: {claim['support']})" if claim.get("support") else "")
                     for claim in claims if isinstance(claim, dict))
    words = owner_words_text(ctx, audience="reviewer")
    if words.strip():
        lines.append(words.strip())
    return "\n".join(lines) or "(the task states no request beyond its reply)"


def _verification(ctx: Any) -> str:
    from ouroboros.outcomes import read_context_verification_receipts
    from ouroboros.review_evidence_sections import _accept_receipt_exhibits, _accept_verification_summary

    task_id = str(getattr(ctx, "task_id", "") or "")
    try:
        receipts = read_context_verification_receipts(ctx, task_id, fallback_root=getattr(ctx, "drive_root", None)) if task_id else []
    except Exception as exc:
        return f"(verification receipts unreadable: {type(exc).__name__}: {exc})"
    if not receipts:
        return "(no host-attested verification receipts)"
    return json.dumps({"summary": _accept_verification_summary(receipts), "receipts": _accept_receipt_exhibits(receipts)},
                      ensure_ascii=False, indent=1, default=str)


def compose_subject(*, ask: str, reply: str, produced: Dict[str, Any], verification: str) -> str:
    files = [f"- {item['path']} ({item['kind']}; sha256 {item['sha256']}; {item['size']} bytes)"
             for item in produced.get("files") or []]
    effects = [f"- effect declared by {item['tool']}: {item['receipt']}" for item in produced.get("effects") or []]
    gaps = [f"- not captured: {issue}" for issue in produced.get("issues") or []]
    return "\n".join([
        "# Task result under review", "",
        "## What was asked", "", ask, "",
        "## The reply", "", reply.strip() or "(no reply text)", "",
        "## What the task produced", "", *(files + effects + gaps or ["(nothing produced beyond the reply)"]), "",
        "## Host-attested verification", "", verification, "",
    ])


def result_subject(ctx: Any, request: Any, *, governance_root: Any, answer: Optional[str] = None) -> Any:
    """The frozen result subject of this call (``review_subject.FrozenSubject``): its text, its sha as
    the wave's identity, and as root the folder its retrieving seats read (the workspace the task
    changed, else the folder holding the retained subject). ``answer`` is the host's delivery text
    (the Required floor); a mind's call names its reply in ``request.answer`` or reviews its latest."""
    from ouroboros.tools.review_change import ReviewChangeArgumentError
    from ouroboros.tools.review_subject import FrozenSubject, ReviewSubjectSpec

    reply = str(answer if answer is not None else (getattr(request, "answer", "") or latest_reply(ctx)) or "")
    produced = produced_material(ctx)
    if not reply.strip() and not produced["files"] and not produced["effects"]:
        raise ReviewChangeArgumentError("subject=result has nothing to review: no reply and nothing this task produced")
    text = compose_subject(ask=_ask(ctx), reply=reply, produced=produced, verification=_verification(ctx))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    source = subject_source(ctx, text)
    root = produced["workspace"] or (str(source.parent) if source else str(pathlib.Path(ctx.drive_root)))
    spec = ReviewSubjectSpec(root_kind=RESULT_ROOT_KIND, root=root, kind=RESULT_KIND, governance_root=str(governance_root),
                             surface="result", body_fact="unknown", body_how="", layer=RESULT_LAYER)
    return FrozenSubject(spec=spec, diff_text=text, diff_sha=digest, tree_sha=digest, parent_sha="",
                         name_status=tuple(("M", str(item["path"])) for item in produced["files"]))


def subject_source(ctx: Any, text: str) -> Optional[pathlib.Path]:
    """The subject's exact text, retained once in the task's artifact store (write-once by digest:
    the same text is the same file); ``None`` when it could not be retained."""
    from ouroboros.artifacts import persist_exact_text_source, task_artifact_dir_path

    root, task_id = getattr(ctx, "drive_root", None), str(getattr(ctx, "task_id", "") or "")
    if root is None or not task_id:
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    exact, ref, gap = persist_exact_text_source(root, task_id, source_id=f"result-subject-{digest[:16]}", text=text)
    if gap or exact != text or not ref.get("path"):
        log.warning("result subject of %s not retained: %s", task_id, gap.get("reason") if gap else "readback mismatch")
        return None
    return task_artifact_dir_path(root, task_id) / str(ref["path"])


def _brief_head(*, goal: str, scope: str, review_rebuttal: str) -> str:
    from ouroboros.triad_review import REVIEW_JSON_ARRAY_CONTRACT

    parts = ["## REVIEW INSTRUCTIONS", "", RESULT_RUBRIC]
    if goal.strip():
        parts += ["## The author's own goal and questions for this review", "", goal.strip(), ""]
    if scope.strip():
        parts += ["## Scope the author names", "", scope.strip(), ""]
    if review_rebuttal.strip():
        parts += ["## The author's answer to the previous review of this result", "", review_rebuttal.strip(), ""]
    return "\n".join([*parts, "## Your answer", "", REVIEW_JSON_ARRAY_CONTRACT])


def prepare_result_review(ctx: Any, subject: Any, *, goal: str = "", scope: str = "",
                          review_rebuttal: str = "") -> Tuple[Optional[Dict[str, Any]], Optional[str], bool]:
    """The result wave assembled for ``review._dispatch_unified_review`` WITHOUT dispatching anything, in
    ``review._prepare_unified_review``'s return shape: every seat of the composed panel answers the one
    result question; a packet seat receives the subject inline, a retrieving seat its own brief naming the
    retained subject and the produced files to read in place."""
    from ouroboros.review_checking import SUBJECT_WORKTREE, wave_checking_source
    from ouroboros.review_model_routes import get_review_enforcement
    from ouroboros.reviewer_slot_config import commit_triad_delivery, row_plan_retrieves
    from ouroboros.tools.review_admission import seat_vectors
    from ouroboros.tools.review_helpers import review_drive_root, review_enforcement_blocks
    from ouroboros.triad_review import REVIEW_JSON_ARRAY_CONTRACT

    blocking_review = review_enforcement_blocks(get_review_enforcement())
    try:
        row_plan = seat_vectors(commit_triad_delivery())
    except ValueError as exc:
        ctx._last_review_block_reason = "infra_failure"
        return None, f"⚠️ REVIEW_BLOCKED: invalid review pool configuration — {exc}", True
    count = len(row_plan.get("models") or [])
    row_plan["parts"] = [("result",)] * count
    head = _brief_head(goal=goal, scope=scope, review_rebuttal=review_rebuttal)
    source = subject_source(ctx, subject.diff_text)
    tasks, policies, shas = [""] * count, [None] * count, [""] * count
    texts: Dict[str, str] = {}
    manifests: List[Dict[str, Any]] = []
    for index in range(count):
        if not row_plan_retrieves(row_plan, index):
            continue
        where = (f"The subject's exact text is retained at {source}; read it whole. " if source else "")
        task = (f"{head}\n\n{where}Every produced file the subject names is at its absolute path: read what you "
                "need in place, nothing is copied for you.\n\n---\n\n# THE SUBJECT\n\n" + subject.diff_text)
        digest = hashlib.sha256(task.encode("utf-8")).hexdigest()
        tasks[index], shas[index], texts[digest] = task, digest, task
        policies[index] = {"output_contract": REVIEW_JSON_ARRAY_CONTRACT}
        manifests.append({"slot_id": str(row_plan["slot_ids"][index]), "delivery": "retrieving", "parts": ["result"],
                          "brief_chars": len(task), "sha": {"brief": digest}})
    row_plan.update(session_tasks=tasks, session_policies=policies, brief_shas=shas)
    routes = list(row_plan.get("routes") or [])
    accesses = list(row_plan.get("session_accesses") or [""] * len(routes))
    seats = [SimpleNamespace(route=route, session_access=access) for route, access in zip(routes, accesses)]
    packet = [str(row_plan["slot_ids"][index]) for index in range(count) if not row_plan_retrieves(row_plan, index)]
    prompt = f"{head}\n\n---\n\n# THE SUBJECT\n\n{subject.diff_text}" if packet else ""
    return {
        "prompt": prompt, "stable_prefix_len": len(head) if packet else 0,
        "models": row_plan["models"], "routes": routes, "row_plan": row_plan,
        "session_task": "", "target_repo": str(subject.spec.root), "blocking_review": blocking_review,
        "task_evidence": {}, "preflight_execution": {}, "layer": RESULT_LAYER,
        "governance_manifest": [], "governance_packet_slots": packet,
        "checking_source": wave_checking_source(subject.spec.root, seats, data_root=review_drive_root(ctx),
                                                subject=SUBJECT_WORKTREE),
        "retrieving_manifests": manifests, "brief_texts": texts, "user_turn": RESULT_USER_TURN,
    }, None, False
