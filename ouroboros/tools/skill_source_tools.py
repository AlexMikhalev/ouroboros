"""Ouroboros's own hands on the skill sources: the same operations the Skills page uses.

``skill_sources`` reads the source list (and, on the owner's instruction or in Cyber Pro, adds,
re-trusts or removes a source). ``skill_search`` finds catalog ids in the connected sources (ClawHub
joins a text search). ``skill_install`` installs one by catalog id, never by URL: from a trusted source
the skill gets the keys it asks for (under the owner's auto-grant setting) and turns on when ready; from
a review source it is installed at once, its review starts, and it turns on only when the review leaves
no blockers. ``skill_update`` applies one update choice. Skipping a review stays the owner's act.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry

_OWNER_SOURCE = {
    "type": "object",
    "description": (
        "The owner's instruction this change carries out: {kind:'chat',ref:{chat_id,client_message_id,ts,"
        "text_sha256}}; {kind:'quiz',task_id,quiz_id}; or {kind:'mailbox',task_id,msg_id}. Not needed in Cyber Pro."
    ),
}
_MAX_RESULTS = 40


def _dump(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1)


def _drive(ctx: ToolContext) -> pathlib.Path:
    from ouroboros.tool_access import canonical_data_root

    return canonical_data_root(ctx)


def _cyber() -> bool:
    from ouroboros.config import get_runtime_mode
    from ouroboros.runtime_mode_policy import mode_has_unrestricted_agency

    return mode_has_unrestricted_agency(get_runtime_mode())


def _owner_instruction_error(ctx: ToolContext, owner_source: Any) -> str:
    if _cyber():
        return ""
    from ouroboros.owner_source import resolve_owner_source

    if resolve_owner_source(ctx, owner_source):
        return ""
    return ("this change needs the owner's instruction: pass owner_source pointing at the owner's message, "
            "answered quiz or mailbox entry in this task")


def _handle_skill_sources(ctx: ToolContext, action: str = "list", url: str = "", path: str = "", name: str = "",
                          trust: str = "", source_id: str = "", owner_source: Any = None, **_kw: Any) -> str:
    from ouroboros.skill_source_catalog import probe_source, snapshot_for
    from ouroboros.skill_sources import (
        SkillSourceError,
        load_sources,
        save_sources,
        with_added_source,
        with_changed_source,
        without_source,
    )

    drive = _drive(ctx)
    if action not in {"list", "add", "trust", "remove"}:
        return "⚠️ SKILL_SOURCES_ERROR: action is list, add, trust or remove."
    if action != "list":
        problem = _owner_instruction_error(ctx, owner_source)
        if problem:
            return f"⚠️ SKILL_SOURCES_REFUSED: {problem}."
        try:
            rows = load_sources()
            if action == "add":
                probe = probe_source(drive, url=url, path=path)
                if probe.get("error"):
                    return f"⚠️ SKILL_SOURCES_ERROR: {probe['error']}"
                rows, _row = with_added_source(rows, kind=str(probe["kind"]), url=url, path=path,
                                               name=name or str(probe.get("suggested_name") or "source"),
                                               trust=trust or "review")
            elif action == "trust":
                rows = with_changed_source(rows, source_id, trust=trust, confirmed=True)
            else:
                rows = without_source(rows, source_id)
            save_sources(rows)
        except SkillSourceError as exc:
            return f"⚠️ SKILL_SOURCES_ERROR: {exc}"
    out: List[Dict[str, Any]] = []
    for row in load_sources():
        _snapshot, status = snapshot_for(drive, row)
        out.append({**row, **status})
    return _dump({"sources": out, "note": "trusted installs without review; review reviews each skill. "
                                          "A trust change never rewrites an existing install."})


def _compact(item: Dict[str, Any]) -> Dict[str, Any]:
    keep = ("id", "name", "source_name", "trust", "official", "recommended", "category", "version", "withdrawn",
            "installed", "needs_keys", "type")
    out = {key: item.get(key) for key in keep if item.get(key) not in (None, "", [], False)}
    out["description"] = str(item.get("description") or "")[:240]
    return out


def _handle_skill_search(ctx: ToolContext, query: str = "", source: str = "", category: str = "", **_kw: Any) -> str:
    from ouroboros.skill_source_catalog import catalog_view, read_snapshot, refresh_source, registry_items
    from ouroboros.skill_sources import load_sources

    drive = _drive(ctx)
    rows = load_sources()
    for row in rows:  # a source never read on this computer is read once now; others refresh in the background
        if row["kind"] in {"hub", "git"} and (not source or row["id"] == source) and read_snapshot(drive, row) is None:
            refresh_source(drive, row, wait_sec=120)
    view = catalog_view(drive, rows=rows, refresh=True)
    words = [word for word in str(query or "").lower().split() if word]
    items = [item for item in view["items"]
             if (not source or item["source_id"] == source) and (not category or category in item.get("category", []))
             and all(word in f"{item['name']} {item.get('description', '')}".lower() for word in words)]
    notes = [f"{s['name']}: {s.get('error') or s.get('state')}" for s in view["sources"]
             if s.get("state") not in {"ready", "search"}]
    if words:
        for row in rows:
            if row["kind"] == "registry" and (not source or row["id"] == source):
                try:
                    items.extend(registry_items(row, " ".join(words)))
                except Exception as exc:
                    notes.append(f"{row['name']}: {exc}")
    return _dump({"count": len(items), "items": [_compact(item) for item in items[:_MAX_RESULTS]],
                  "omitted": max(0, len(items) - _MAX_RESULTS), "notes": notes,
                  "categories": view["categories"],
                  "hint": "Install with skill_install(id, reason); the id is <source>/<name>."})


def _handle_skill_install(ctx: ToolContext, id: str = "", reason: str = "", replace: bool = False,
                          owner_source: Any = None, **_kw: Any) -> str:
    from ouroboros.skill_source_install import SkillInstallError, install_item, run_in_lane, start_review

    item_id = str(id or "").strip()
    if "/" not in item_id or "://" in item_id or item_id.startswith(("git@", "/", "~")):
        return "⚠️ SKILL_INSTALL_ERROR: id is a catalog id <source>/<name> from skill_search, never a URL."
    if not str(reason or "").strip():
        return "⚠️ SKILL_INSTALL_ERROR: reason is required: say why this work needs the skill."
    if replace:
        problem = _owner_instruction_error(ctx, owner_source)
        if problem:
            return f"⚠️ SKILL_INSTALL_REFUSED: replacing another skill of the same name: {problem}."
    drive = _drive(ctx)
    task_id = str(getattr(ctx, "task_id", "") or "")
    try:
        result = run_in_lane(drive, "install", item_id, lambda: install_item(
            drive, item_id, replace=bool(replace), actor="ouroboros", reason=str(reason), task_id=task_id,
            turn_on_when_ready=True), message=f"Ouroboros installs {item_id}: {str(reason)[:160]}",
            chat_id=int(getattr(ctx, "current_chat_id", 0) or 0))
    except SkillInstallError as exc:
        return "⚠️ SKILL_INSTALL_REFUSED: " + _dump(exc.payload())
    if result.get("needs_review"):
        start_review(drive, result["name"])
        result["next"] = ("The review runs now in the skill lifecycle lane; the skill turns on when it leaves no "
                          "blockers. list_skills(name=...) shows its state.")
    acceptance = result.get("acceptance") or {}
    result["message"] = (f"Installed {result.get('name')} from {result['source'].get('name')} "
                         f"({'installs without review' if result.get('basis') == 'trusted' else 'reviewed before it runs'})"
                         f"; reason: {reason}. Keys granted: {', '.join(acceptance.get('granted_keys') or []) or 'none'}.")
    return _dump(result)


def _handle_skill_update(ctx: ToolContext, name: str = "", mode: str = "", digest: str = "", **_kw: Any) -> str:
    from ouroboros.skill_source_install import SkillInstallError, run_in_lane, start_review, update_installed

    skill = str(name or "").strip()
    if not skill or mode not in {"replace", "keep", "merge"}:
        return "⚠️ SKILL_UPDATE_ERROR: name and mode (replace, keep or merge) are required."
    drive = _drive(ctx)
    try:
        result = run_in_lane(drive, "update", skill, lambda: update_installed(
            drive, skill, mode=mode, digest=str(digest or ""), actor="ouroboros",
            task_id=str(getattr(ctx, "task_id", "") or "")), message=f"Ouroboros updates {skill} ({mode})",
            chat_id=int(getattr(ctx, "current_chat_id", 0) or 0))
    except SkillInstallError as exc:
        return "⚠️ SKILL_UPDATE_REFUSED: " + _dump(exc.payload())
    if result.get("needs_review"):
        start_review(drive, skill)
    return _dump(result)


_SCHEMAS = {
    "skill_sources": {
        "description": (
            "List the skill sources this install reads, with each one's trust (trusted: installs without review; "
            "review: each skill is reviewed) and read state. action add|trust|remove changes the list: only on the "
            "owner's instruction (owner_source), except in Cyber Pro."
        ),
        "properties": {
            "action": {"type": "string", "enum": ["list", "add", "trust", "remove"], "description": "Default list."},
            "url": {"type": "string", "description": "add: a git repository address (https or ssh)."},
            "path": {"type": "string", "description": "add: a folder on this computer."},
            "name": {"type": "string", "description": "add: the source's name."},
            "trust": {"type": "string", "enum": ["trusted", "review"]},
            "source_id": {"type": "string", "description": "trust/remove: the source id."},
            "owner_source": _OWNER_SOURCE,
        },
    },
    "skill_search": {
        "description": (
            "Search the skills offered by the connected sources (ClawHub joins a text search). Returns catalog ids "
            "for skill_install with source, trust, Official, category, version and installed state."
        ),
        "properties": {
            "query": {"type": "string", "description": "Words to match in names and descriptions; empty lists all."},
            "source": {"type": "string", "description": "Optional source id."},
            "category": {"type": "string", "description": "Optional category id."},
        },
    },
    "skill_install": {
        "description": (
            "Install one skill by catalog id from skill_search (never a URL). From a trusted source it gets the keys "
            "it asks for under the owner's auto-grant setting and turns on when ready; from a review source it "
            "installs now, its review starts, and it turns on only without blocker findings. The reason is recorded "
            "for the reviewer and the owner."
        ),
        "properties": {
            "id": {"type": "string", "description": "Catalog id <source>/<name>."},
            "reason": {"type": "string", "description": "Why this work needs the skill."},
            "replace": {"type": "boolean", "description": "Replace another skill of the same name (owner's instruction)."},
            "owner_source": _OWNER_SOURCE,
        },
        "required": ["id", "reason"],
    },
    "skill_update": {
        "description": (
            "Apply an update an installed skill's source offers: replace (keeps the previous copy), keep (skip this "
            "release) or merge (a task merges the release into local changes). digest names the release you saw."
        ),
        "properties": {
            "name": {"type": "string"},
            "mode": {"type": "string", "enum": ["replace", "keep", "merge"]},
            "digest": {"type": "string", "description": "The candidate release digest from skill_search."},
        },
        "required": ["name", "mode"],
    },
}
def _schema(name: str) -> Dict[str, Any]:
    spec = _SCHEMAS[name]
    return {"name": name, "description": spec["description"],
            "parameters": {"type": "object", "properties": spec["properties"],
                           "required": spec.get("required", [])}}


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("skill_sources", _schema("skill_sources"), _handle_skill_sources,
                  is_code_tool=False, timeout_sec=300, safety_policy="check"),
        ToolEntry("skill_search", _schema("skill_search"), _handle_skill_search,
                  is_code_tool=False, timeout_sec=300, safety_policy="skip"),
        ToolEntry("skill_install", _schema("skill_install"), _handle_skill_install,
                  is_code_tool=False, timeout_sec=1800, safety_policy="check"),
        ToolEntry("skill_update", _schema("skill_update"), _handle_skill_update,
                  is_code_tool=False, timeout_sec=1800, safety_policy="check"),
    ]


__all__ = ["get_tools"]
