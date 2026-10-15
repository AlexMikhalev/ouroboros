"""Retain produced material before root-result admission, then reuse it in review.

Inputs, checkpoints and requested outputs do not establish production. These
sources use the existing artifact/source owners; this module neither dispatches
critics nor writes a terminal/quiescent mutation state. A failed capture stays a
gap and never resurrects the previous selection.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def retain_acceptance_material(ctx: Any, trace: dict, content: str | None = None, *, include_outputs: bool = True) -> dict:
    from ouroboros.artifacts import (
        collect_task_artifact_records, copy_artifact_file, persist_exact_text_source,
        registered_task_artifact, stream_artifact_file,
        task_artifact_dir_path, read_actor_source_bytes,
    )
    from ouroboros.contracts.task_contract import answer_protocol_active
    from ouroboros.outcomes import selected_final_answer
    from ouroboros.artifacts import materialize_tool_result_source
    from ouroboros.extension_surface_names import parse_extension_surface_name
    from ouroboros.review_evidence import _accept_protected_set, _accept_repo_diff

    previous = trace.get("acceptance_material") or {}
    material: dict = {"produced": False, "sources": [], "issues": []}
    trace["acceptance_material"] = material
    root, task_id = getattr(ctx, "drive_root", None), str(getattr(ctx, "task_id", "") or "")
    if root is None or not task_id:
        return material

    def retain(name: str, text: str) -> dict:
        prior = next((row.get("source_ref") for row in previous.get("sources", []) if row.get("name") == name), None)
        if prior and read_actor_source_bytes(root, task_id, prior) == text.encode("utf-8"):
            material["sources"].append({"name": name, "source_ref": prior})
            return prior
        exact, ref, gap = persist_exact_text_source(root, task_id, source_id=name, text=text)
        if gap or exact != text:
            raise OSError(gap.get("reason") or "material source readback mismatch")
        material["sources"].append({"name": name, "source_ref": ref})
        return ref

    if answer_protocol_active(ctx):
        try:
            text = content if content is not None else str(getattr(getattr(ctx, "_delivery_candidate", None), "full_text", ""))
            answer = selected_final_answer(text, trace)
            if answer:
                ref = retain("selected-final-answer", answer)
                material["selected_answer"] = {"text": answer, "source_ref": ref}
                material["produced"] = True
        except Exception as exc:
            material["issues"].append({"status": "source_unavailable", "reason": str(exc), "tool": "selected_answer"})
    if not include_outputs:
        return material  # Auto without nomination / Off still retain a selected answer.
    try:
        protected = _accept_protected_set(ctx)
        base = task_artifact_dir_path(root, task_id)
        for row in collect_task_artifact_records(root, task_id, measure=False, strict=True, require_registered=True):
            name = str(row.get("relpath") or row["name"])
            if not registered_task_artifact(root, task_id, name) or not row.get("size"):
                continue
            if any(name == str(p).lstrip("/") or name.startswith(str(p).strip("/") + "/")
                   or Path(name).name == Path(str(p)).name for p in protected):
                continue  # hidden/restricted evidence is never opened for admission
            source = Path(row["path"])
            identity = stream_artifact_file(source, expected=row)
            rel = "source_handles/context_checkpoints/produced-" + identity["sha256"] + ".bin"
            if not (base / rel).exists():
                copy_artifact_file(source, base / rel, expected=identity)
            ref = {"kind": "task_source", "root": "artifact_store", "path": rel, **identity}
            stream_artifact_file(base / rel, expected=identity)
            material["sources"].append({"name": name, "source_ref": ref})
            material["produced"] = True
    except Exception as exc:
        material["issues"].append({"status": "source_unavailable", "reason": str(exc), "tool": "artifact_manifest"})

    # The extension producer owns execution semantics (PluginAPI.register_tool).
    # Read its exact receipt, never infer effects from a method/backend name.
    for call in trace.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        surface = parse_extension_surface_name(str(call.get("tool") or ""))
        if not surface or call.get("is_error"):
            continue
        try:
            result, complete, _gap = materialize_tool_result_source(root, task_id, call)
            if not complete:
                material["issues"].append({"status": "source_unavailable", "reason": str(_gap),
                                           "tool": str(call.get("tool") or "")})
                continue
            try:
                payload = json.loads(result) if isinstance(result, str) else {}
            except ValueError:
                continue  # Plain extension text carries no declared execution fact.
            if not isinstance(payload, dict) or payload.get("observed_effect") is not True:
                continue
            ref = retain("observed-effect-" + str(call.get("tool_call_id") or len(material["sources"])), result)
            material.setdefault("observed_effects", []).append({"tool": call["tool"], "source_ref": ref})
            material["produced"] = True  # producer execution fact, never goal success
        except Exception as exc:
            material["issues"].append({"status": "source_unavailable", "reason": str(exc), "tool": str(call.get("tool") or "")})

    # Only a real task baseline establishes shell-produced changes. An arbitrary
    # dirty working tree is evidence for a nominated review, not admission proof.
    try:
        from ouroboros.mutation_attribution import attributed_git_candidates, _git_path_fingerprints

        getter = getattr(ctx, "active_repo_dir", None)
        repo = getter() if callable(getter) else getattr(ctx, "repo_dir", None)
        if repo:
            accounting = getattr(ctx, "budget_drive_root", None) or root
            snapshot = attributed_git_candidates(accounting, task_id, repo)
            if snapshot.get("candidates") and not snapshot.get("blockers"):
                fingerprints = _git_path_fingerprints(Path(repo), snapshot["candidates"])
                diff, meta = _accept_repo_diff(ctx, include_recent_commit=False, drive_root=root, task_id=task_id)
                if meta.get("issue") or fingerprints != _git_path_fingerprints(Path(repo), snapshot["candidates"]):
                    raise ValueError("workspace material changed or capture unavailable")
                ref = retain("produced-workspace", json.dumps({**snapshot, "fingerprints": fingerprints}, sort_keys=True))
                diff_ref = retain("produced-workspace-diff", diff)
                material["workspace"] = {"source_ref": ref, "repo_diff": diff, "repo_diff_source_ref": diff_ref,
                                         "repo_diff_capture": meta.get("capture_disclosure") or {}}
                material["produced"] = True
    except Exception as exc:
        material["issues"].append({"status": "source_unavailable", "reason": str(exc), "tool": "repo_diff"})
    return material
