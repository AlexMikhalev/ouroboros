"""Supervisor-side source admission for promoted conversation work."""

from __future__ import annotations

import pathlib
from typing import Any, Tuple


def _source_project_id(source: str, is_git: bool) -> str:
    from ouroboros.project_facts import project_id_from_display_name
    from ouroboros.project_sources import derive_repo_dir_name

    base = derive_repo_dir_name(source) if is_git else pathlib.Path(source.rstrip("/")).name
    return project_id_from_display_name(base or "project")


def resolve_promote_source(
    ctx: Any, source: str, project_id: str,
) -> Tuple[str, str, str, str, bool]:
    """Attach/clone only after the supervisor has admitted an executor.

    Returns ``(workspace_root, note, error, effective_project_id,
    created_project)``.  Keeping this side effect on the authoritative handler
    side prevents a stale tool snapshot from cloning/registering a project
    after the worker pool was disabled but before the queued event was
    rejected. ``created_project`` is True only when THIS resolution registered
    the project row — the promote continuation carries it so the one
    workers-side ``project_started`` announce still fires for a creation that
    happened in this off-loop half (owner 2=A: it is the same agent-initiated
    promote flow).
    """
    from ouroboros.config import DATA_DIR
    from ouroboros.project_sources import clone_project_repo, valid_git_url, validate_attach_path

    src = str(source or "").strip()
    pid = str(project_id or "").strip()
    drive_root = pathlib.Path(getattr(ctx, "DRIVE_ROOT", DATA_DIR))
    if not src:
        return "", "", "", pid, False
    is_git = valid_git_url(src)
    pid = pid or _source_project_id(src, is_git)
    try:
        from ouroboros.projects_registry import get_reserved_project

        existing = get_reserved_project(drive_root, pid)
    except Exception as exc:
        return "", "", f"project_lookup_failed: {type(exc).__name__}: {exc}", pid, False
    lifecycle = str((existing or {}).get("lifecycle") or "active")
    if existing is not None and lifecycle != "active":
        return "", "", f"project_routing_fence: {pid!r} is {lifecycle}", pid, False
    if is_git:
        if str((existing or {}).get("working_dir") or "").strip():
            return "", "", (
                f"conflict: project {pid!r} already has folder {existing.get('working_dir')}; "
                "use another project id or omit source"
            ), pid, False
        cloned, code, detail = clone_project_repo(src, pid)
        if code:
            return "", "", f"{code}: {detail}", pid, False
        folder, provenance, clone_url = cloned, "cloned", src
        note = f"cloned {src} -> {cloned}"
    else:
        from ouroboros.workspace_admission import WorkspaceRootError, validate_workspace_root

        resolved, err = validate_attach_path(
            src,
            system_repo_dir=getattr(ctx, "REPO_DIR", getattr(ctx, "repo_dir", "")),
            drive_root=drive_root,
        )
        if err:
            return "", "", f"attach: {err}", pid, False
        try:
            resolved = validate_workspace_root(
                resolved,
                system_repo_dir=getattr(ctx, "REPO_DIR", getattr(ctx, "repo_dir", "")),
                drive_root=drive_root,
            )
        except WorkspaceRootError as exc:
            return "", "", f"attach: {exc}", pid, False
        folder, provenance, clone_url = str(resolved), "attached", ""
        note = f"attached {resolved}"
    prior_wd = str((existing or {}).get("working_dir") or "").strip()
    if prior_wd and prior_wd != folder:
        return "", "", (
            f"conflict: project {pid!r} already has folder {prior_wd}; use another project id "
            "or omit source"
        ), pid, False
    if prior_wd == folder and str((existing or {}).get("provenance") or "").strip() not in ("", "none"):
        return folder, note, "", pid, False
    try:
        from ouroboros.projects_registry import create_project, update_project
        from ouroboros.utils import utc_now_iso

        created = bool(create_project(
            drive_root, pid, origin="promote_chat_to_task",
        ).get("created"))
        update_project(
            drive_root,
            pid,
            working_dir=folder,
            provenance=provenance,
            clone_url=clone_url,
            trusted_at=utc_now_iso(),
        )
    except Exception as exc:
        return "", "", f"register: {type(exc).__name__}: {exc}", pid, False
    return folder, note, "", pid, created


__all__ = ["resolve_promote_source"]
