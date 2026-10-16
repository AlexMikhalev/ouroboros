"""Delegated invocation identity and the canonical first-request projection.

The custody facade re-exports these readers; the chain oracle stays independent
of the store reducer so imports and live publication can be compared directly.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from ouroboros._usage_rows import REVIEW_ATTRIBUTION_KEYS


def invocation_record(drive_root: Any, invocation_id: str, *,
                      rows: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """One invocation's durable fate: who requested it, the EXACT body it sent,
    the resources that attempt bound, and how it resolved.
    ``state`` is ``pending`` (requested, never bound, never definitely refused —
    the only state an explicit retry may replay), ``started`` (a run bound to it;
    carried with its ``run_id`` so the caller can wait instead of re-posting), or
    ``failed_definite`` (the daemon definitively refused; the id is dead — replaying
    it against a since-reconfigured route is how a key wedges into a permanent 409).
    ``request`` is the canonical POST body recorded before the wire attempt: a retry
    replays THESE bytes, never a re-derivation, because the engine's replay match
    digests the request and answers a same-key-different-digest POST with 409
    ``idempotency_conflict``.
    ``route``, ``project_id``, ``project_owned`` and ``idempotency_key`` are the
    attempt facts that never ride the wire, read from the FIRST request row — the
    minting — because the invocation stores its facts ONCE and a retry replays them.
    A retry that re-derived any of these from the current route/model/workspace
    context wrote a durable record contradicting the body it actually POSTed.
    First-request lineage, usage attribution (category/source/skill/wave/slot),
    and isolation facts are likewise replayed rather than re-derived.
    ``rows`` folds a caller's chain snapshot; otherwise the store answers from the
    pending invocation row, the bound run's row or the definite-refusal row.
    """
    from ouroboros.delegate_custody import START_REQUESTED, STARTED, START_FAILED, _unique_rows

    target = str(invocation_id or "").strip()
    if not target:
        return None
    if rows is None:
        from ouroboros.delegate_custody_current import invocation

        return invocation(drive_root, target, _request_facts)
    found: Optional[Dict[str, Any]] = None
    state, run_id = "pending", ""
    for row in _unique_rows(rows):
        if str(row.get("invocation_id") or "") != target:
            continue
        kind = str(row.get("type") or "")
        if kind == START_REQUESTED and found is None:
            found = _request_facts(drive_root, row)
        elif kind == STARTED:
            state, run_id = "started", str(row.get("run_id") or "")
        elif kind == START_FAILED and row.get("definite") is True and state != "started":
            state = "failed_definite"
    if found is None:
        return None
    return {**found, "state": state, "run_id": run_id}


def _request_facts(drive_root: Any, row: Dict[str, Any]) -> Dict[str, Any]:
    """The attempt facts of ONE START_REQUESTED row, the shape ``invocation_record`` returns."""
    from ouroboros.delegate_pending import request_body

    return {
        "task_id": str(row.get("task_id") or ""),
        "surface": str(row.get("surface") or ""),
        "slot_id": str(row.get("slot_id") or ""),
        "operation_id": str(row.get("operation_id") or ""),
        "request": request_body(drive_root, row),
        "route": str(row.get("route") or ""),
        "project_id": str(row.get("project_id") or ""),
        "project_owned": bool(row.get("project_owned")),
        # Absence is a fact: legacy rows fall back to the stored request.
        **({"project_persistent": bool(row["project_persistent"])}
           if "project_persistent" in row else {}),
        "idempotency_key": str(row.get("idempotency_key") or ""),
        "root_task_id": str(row.get("root_task_id") or ""),
        "parent_task_id": str(row.get("parent_task_id") or ""),
        "category": str(row.get("category") or "subagent"),
        "source": str(row.get("source") or "delegated_subagent"),
        **{key: str(row.get(key) or "") for key in REVIEW_ATTRIBUTION_KEYS},
        # The C1 isolation binding: a retry reproduces EXACTLY these — the
        # snapshot, the execution root, the baseline and the authority
        # target the original attempt bound — never a re-derivation.
        "snapshot_id": str(row.get("snapshot_id") or ""),
        "execution_root": str(row.get("execution_root") or ""),
        "execution_binding_fingerprint": str(row.get("execution_binding_fingerprint") or ""),
        "baseline_sha": str(row.get("baseline_sha") or ""),
        "target_root": str(row.get("target_root") or ""),
        "authority_source": str(row.get("authority_source") or ""),
        # Continuation lineage and the same-tree capture identity replay too.
        **{key: str(row.get(key) or "") for key in ("continuation_of", "capture_id", "snapshot_task_id")},
        # A copy: the source rows may be the shared, read-only custody memo.
        "resource_ref": copy.deepcopy(row.get("resource_ref")) if isinstance(row.get("resource_ref"), dict) else {},
        "selected_subagent_id": str(row.get("selected_subagent_id") or ""),
        "config_fingerprint": str(row.get("config_fingerprint") or ""),
        "work_order_fingerprint": str(row.get("work_order_fingerprint") or ""),
        "work_order_coverage": str(row.get("work_order_coverage") or ""),
        "authority_fingerprint": str(row.get("authority_fingerprint") or ""),
        "processing": copy.deepcopy(row.get("processing")) if isinstance(row.get("processing"), dict) else {},
        # #1196: the cap and HOW it was decided replay with the body a
        # retry re-POSTs, so the replayed STARTED row keeps the same basis.
        "max_seconds": int(row.get("max_seconds") or 0) if str(row.get("max_seconds") or "").lstrip("-").isdigit() else 0,
        "requested_at": str(row.get("ts") or ""),  # preserve an unbound start's original window
        "max_seconds_basis": str(row.get("max_seconds_basis") or ""),
        "work_order_source_request": (
            copy.deepcopy(row.get("work_order_source_request"))
            if isinstance(row.get("work_order_source_request"), dict) else {}
        ),
    }
