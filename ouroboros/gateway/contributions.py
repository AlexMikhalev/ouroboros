"""Live contribution discovery and revision-bound host assets; passive HTTP reads.

The transport-free TypedDicts are re-exported by gateway.contracts. Extension
rows come from one locked registry snapshot; instruction rows use the installed
catalog's enabled/review gate. No GET loads code, reconciles, writes or fetches.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Dict, List, Literal, TypedDict

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

try:
    from typing import NotRequired, Required
except ImportError:  # pragma: no cover
    from typing_extensions import NotRequired, Required


class UiContribution(TypedDict, total=False):
    key: Required[str]
    skill: Required[str]
    skill_label: Required[str]
    id: Required[str]
    placement: str
    provides: str
    label: Required[str]
    title: Required[str]
    icon: Required[str]
    order: Required[int]
    action: Dict[str, Any]
    source: Dict[str, Any]
    runtime: str
    route_url: str
    destination: str
    languages: List[str]
    formats: List[str]
    install_size: str
    max_bytes: int
    max_seconds: int
    render: Required[Dict[str, str]]
    revision: Required[str]


class UiContributionsResponse(TypedDict):
    contributions: List[UiContribution]
    offers: Dict[str, List[Dict[str, Any]]]
    preferences: Dict[str, Any]
    voice: Dict[str, Dict[str, Any]]  # kind -> {key, available, reason}; never falls back
    icons: List[str]
    problems: Dict[str, List[str]]


def contribution_catalog(drive_root, repo_path=None, *, skills=None, live=None) -> dict:
    from ouroboros.extension_contributions import project_contributions
    from ouroboros.extension_registry_state import live_contribution_problems, live_contribution_projection
    from ouroboros.extension_ui_validation import CONTRIBUTION_ICONS, validate_contributions
    from ouroboros.gateway.ui_preferences import _stored_preferences
    from ouroboros.skill_loader import discover_skills, skill_conflict_status

    if skills is None:
        skills = discover_skills(drive_root, repo_path=repo_path)
    rows = live_contribution_projection() if live is None else list(live.get("contributions", []))
    live_names = {row["skill"] for row in rows}
    problems, offers = live_contribution_problems(), []
    for skill in skills:
        if skill.load_error or skill.identity_collision:
            continue
        clean, warnings = validate_contributions(skill.manifest)
        if warnings:
            problems[skill.name] = list(dict.fromkeys(problems.get(skill.name, []) + warnings))
        if (skill.manifest.is_instruction() and skill.enabled
                and skill.review.gate_for(skill.content_hash)["executable_review"]
                and not skill_conflict_status(skill, skills)):
            rows.extend(project_contributions(skill.name, clean, skill.content_hash, skill.manifest.name))
            live_names.add(skill.name)
        if skill.source == "native" and skill.name not in live_names:
            offers.extend({"skill": skill.name, "skill_label": skill.manifest.name, "id": item["id"],
                           "label": item["label"], "destination": item["destination"],
                           "install_size": item.get("install_size", ""), "enabled": False}
                          for item in clean if item.get("provides") == "stt")
    prefs = _stored_preferences(drive_root / "state" / "ui_preferences.json")
    voice = {}
    for kind, key in prefs["voice"].items():
        available = any(row["key"] == key and row.get("provides") == kind for row in rows)
        voice[kind] = {"key": key, "available": available,
                       "reason": "" if available else ("provider_unavailable" if key else "not_selected")}
    return {"contributions": sorted(rows, key=lambda row: (row["order"], row["key"])),
            "offers": {"stt": offers}, "preferences": {key: prefs[key] for key in ("voice", "contributions")},
            "voice": voice, "icons": list(CONTRIBUTION_ICONS), "problems": problems}


async def api_ui_contributions(request: Request) -> Response:
    from starlette.responses import JSONResponse
    from ouroboros.config import get_skills_repo_path
    from ouroboros.gateway._helpers import request_drive_root

    payload = await asyncio.to_thread(contribution_catalog, request_drive_root(request), get_skills_repo_path())
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


async def api_extension_host_asset(request: Request) -> Response:
    from pathlib import PurePosixPath
    from starlette.responses import Response
    from ouroboros.extension_contributions import HOST_ASSET_TYPES
    from ouroboros.extension_registry_state import live_host_asset

    params = request.path_params
    path = str(params.get("path") or "")
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    media_type = HOST_ASSET_TYPES.get(PurePosixPath(path).suffix)
    if (not path.startswith("ui/") or not media_type or "\\" in path or "\0" in path
            or any(part in {"", ".", ".."} or part.startswith(".") for part in path.split("/"))):
        return Response(status_code=404, headers=headers)
    content = live_host_asset(str(params["skill"]), str(params["revision"]), path)
    if content is None:
        return Response(status_code=404, headers=headers)
    return Response(content, media_type=media_type, headers=headers)


class UiPreferencesResponse(TypedDict):
    ok: NotRequired[bool]
    voice: dict[str, str]
    contributions: dict[str, list[str]]
    widget_order: list[str]
    widget_start_mode: dict[str, Literal["auto", "manual", "retain"]]  # owner per-card launch-policy override
    widget_size: dict[str, dict[str, int]]  # owner Widgets card width: {w: masonry columns the card spans, 12 = full width; h: 0}
    nested_subagents_expanded: bool
    sidebar_width: int  # px; 0 = CSS default (resizable side sections, v6.33.0)
    project_panel_width: int  # px; 0 = CSS default
    project_seen_revision: dict[str, int]  # monotonic paint ACK per active Project
    welcome: dict[str, str]  # install-wide empty-Main UI copy: mode default|hidden|custom and plain text

