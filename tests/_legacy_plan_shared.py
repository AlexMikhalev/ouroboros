"""Earlier-schema (v1/v2) plan-review records as finished installs still hold them.

The retired engine that wrote this shape is gone; what stays live reads it
(``task_results.load_plan_review_state``, ``plan_record.import_legacy_v2``,
``plan_review_artifacts.plan_review_dispute_history``, the acceptance binder). These
helpers plant such records directly, in the stored shape, so those readers stay covered.
Plain callables, never fixtures.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ouroboros.artifacts import store_actor_source_bytes
from ouroboros.task_result_schema import SCHEMA_VERSION_KEY, TASK_RESULT_SCHEMA_VERSION
from ouroboros.task_store import patch_document
from ouroboros.tools.plan_spec import spec_hash

DECK_SPEC: Dict[str, Any] = {
    "goal": "Ship the deck", "in_scope": ["slides"], "non_goals": [], "invariants": [],
    "decisions": [], "deferred": [], "evidence": [], "affected_resources": [], "affected_paths": [],
    "acceptance_claims": [{"id": "claim_1", "claim": "The deck has five slides", "surface": "", "support": "count them",
                           "priority": "must"}],
}


def persist_wave(root: Any, task_id: str, wave: Dict[str, Any]) -> Dict[str, Any]:
    """The wave's exact bytes in the task's artifact store, where the retired engine kept them."""
    fingerprint = str(wave.get("request_fingerprint") or "")
    data = json.dumps(wave, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return store_actor_source_bytes(
        root, task_id, category="context_checkpoints",
        source_id=f"plan-review-wave-{int(wave.get('cycle_index') or 0):04d}-{fingerprint}", data=data, extension="json")


def wave(cycle: int = 1, *, fingerprint: Optional[str] = None, spec: Optional[Dict[str, Any]] = None,
         **changes: Any) -> Dict[str, Any]:
    """One full v2 wave (no artifact yet): an open REVIEW_REQUIRED wave unless ``changes`` say otherwise."""
    spec = dict(spec or DECK_SPEC)
    return {"cycle_index": cycle, "request_fingerprint": fingerprint or f"{cycle:064x}", "spec": spec,
            "spec_hash": spec_hash(spec), "aggregate": "REVIEW_REQUIRED", "closed": False, "paid": True,
            "findings": [], "dispositions": [], "actors": [], "reviewer_outputs": [], **changes}


def with_artifact(root: Any, task_id: str, value: Dict[str, Any]) -> Dict[str, Any]:
    """``value`` with its exact copy retained and named by ``wave_artifact``."""
    return {**value, "wave_artifact": persist_wave(root, task_id, value)}


def v2_state(*waves: Dict[str, Any], current: Optional[str] = None, status: str = "open",
             reason: str = "", **extra: Any) -> Dict[str, Any]:
    """A v2 ``plan_review_state`` whose current attempt names ``current`` (default: the last wave)."""
    fingerprint = current if current is not None else (str(waves[-1]["request_fingerprint"]) if waves else "")
    return {"schema_version": 2, "series_id": (fingerprint or "0" * 16)[:16],
            "cycles_paid": sum(1 for w in waves if w.get("paid")), "need_evidence_seen": [],
            "current_attempt": {"fingerprint": fingerprint, "status": status, "reason": reason} if fingerprint else {},
            "waves": list(waves), "waves_omitted": 0, **extra}


def seed_plan_review_state(root: Any, task_id: str, state: Dict[str, Any], **document: Any) -> Dict[str, Any]:
    """Plant ``state`` under the task result's ``plan_review_state`` key verbatim (the rest of the
    document is kept; ``document`` sets other fields)."""
    def merge(current: Dict[str, Any]) -> Dict[str, Any]:
        body = {**dict(current or {}), **document}
        body.setdefault("task_id", str(task_id))
        body.setdefault("status", "running")
        body.setdefault(SCHEMA_VERSION_KEY, TASK_RESULT_SCHEMA_VERSION)
        body["plan_review_state"] = state
        return body

    return patch_document(root, task_id, merge)
