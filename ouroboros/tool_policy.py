"""Task-local schema residency over the registry's permitted capability envelope.

Low/Max start complete. Nano selects canonical schemas while retaining the
existing discovery/reclaim transport. Residency never grants execution authority.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Protocol, Sequence

from ouroboros.tool_capabilities import META_TOOL_NAMES


NANO_SCHEMA_META_NAMES = META_TOOL_NAMES | frozenset({"compact_context"})


@dataclass(frozen=True)
class ToolSchemaSelection:
    schemas: tuple[Dict[str, Any], ...]
    chosen: tuple[str, ...]
    unavailable: Mapping[str, str]
    unknown: tuple[str, ...]
    missing_meta: tuple[str, ...]


@dataclass(frozen=True)
class ToolSchemaRequest:
    status: str  # pending | no_op | full_envelope_unchanged
    selection: ToolSchemaSelection


class ToolSchemaProvider(Protocol):
    """Minimal registry contract needed by the loop/discovery helpers."""

    def schemas(self, core_only: bool = False) -> List[Dict[str, Any]]:
        ...


def _schema_names(names: Iterable[str] | None) -> tuple[str, ...]:
    if names is None:
        return ()
    if isinstance(names, str):
        raise ValueError("schema_names must be a collection of exact tool names")
    result: list[str] = []
    for name in names:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("schema_names must contain nonempty tool names")
        name = name.strip()
        if name not in result:
            result.append(name)
    return tuple(result)


def select_tool_schemas(
    canonical_schemas: Sequence[Dict[str, Any]], *, context_mode: str = "max",
    schema_names: Iterable[str] | None = None,
    unavailable_reasons: Mapping[str, str] | None = None,
) -> ToolSchemaSelection:
    """Pure selection from one permitted snapshot, retaining its canonical order."""
    requested = _schema_names(schema_names)
    available = {schema["function"]["name"] for schema in canonical_schemas}
    selected = set(requested) | NANO_SCHEMA_META_NAMES
    schemas = tuple(deepcopy(schema) for schema in canonical_schemas
                    if context_mode != "nano" or schema["function"]["name"] in selected)
    reasons = unavailable_reasons or {}
    absent = tuple(name for name in requested if name not in available)
    return ToolSchemaSelection(
        schemas, tuple(schema["function"]["name"] for schema in schemas),
        {name: reasons[name] for name in absent if reasons.get(name)},
        tuple(name for name in absent if not reasons.get(name)),
        tuple(sorted(NANO_SCHEMA_META_NAMES - available)) if context_mode == "nano" else (),
    )


def initial_tool_schemas(
    registry: ToolSchemaProvider, context_mode: str = "max",
    schema_names: Iterable[str] | None = None,
) -> List[Dict[str, Any]]:
    """Return full Low/Max or selected Nano schemas from the current registry.

    Visibility is selected by the registry context: ordinary top-level tasks
    expose all available first-party built-ins plus live extension/MCP schemas;
    delegated-child, repair, credential, resource, and contract
    filters narrow independently. A parent suggestion is an initial Nano view,
    not an allowlist; the actor can request any other permitted schema later.
    """
    return list(select_tool_schemas(registry.schemas(), context_mode=context_mode,
                                    schema_names=schema_names).schemas)


def compact_tool_catalog(
    canonical_schemas: Sequence[Dict[str, Any]], *, schema_names: Iterable[str],
) -> List[Dict[str, Any]]:
    """Complete name/purpose list; bounded purpose is presentation, never policy.

    Full argument contracts remain in the canonical schemas, obtainable by
    ``enable_tools``. Registry capability omissions travel separately unchanged.
    """
    resident = set(_schema_names(schema_names))
    rows = []
    for schema in canonical_schemas:
        function = schema["function"]
        purpose = " ".join(str(function.get("description") or "").split())
        rows.append({
            "name": function["name"], "description": purpose[:120],
            "description_truncated": len(purpose) > 120,
            "residency": "loaded" if function["name"] in resident else "not_loaded",
        })
    return rows


def request_tool_schema_selection(
    ctx: Any, registry: ToolSchemaProvider, names: Iterable[str], *,
    context_mode: str, current_schemas: Sequence[Dict[str, Any]], extend: bool = False,
) -> ToolSchemaRequest:
    """The one pending-name writer for enable and actor-authored context changes.

    It never changes the active wire schemas or registry. The existing loop/view
    owner applies a pending request only at a complete tool boundary after fitting
    the actual candidate. It re-resolves names against current registry policy.
    """
    requested = _schema_names(names)
    pending = getattr(ctx, "_pending_tool_schema_names", None)
    if extend:
        prior = pending if pending is not None else tuple(s["function"]["name"] for s in current_schemas)
        requested = _schema_names((*prior, *requested))
    canonical = registry.schemas()
    available = {schema["function"]["name"] for schema in canonical}
    hidden_reason = getattr(registry, "policy_hidden_reason", lambda name: None)
    reasons = {name: reason for name in requested if name not in available
               if (reason := hidden_reason(name))}
    selection = select_tool_schemas(canonical, context_mode=context_mode,
                                    schema_names=requested, unavailable_reasons=reasons)
    if context_mode != "nano":
        if pending is not None:
            ctx._pending_tool_schema_names = None
        return ToolSchemaRequest("full_envelope_unchanged", selection)
    if list(selection.schemas) == list(current_schemas):
        if pending is not None:
            ctx._pending_tool_schema_names = None
        return ToolSchemaRequest("no_op", selection)
    if pending != selection.chosen:
        ctx._pending_tool_schema_names = selection.chosen
    return ToolSchemaRequest("pending", selection)


def list_non_core_tools(
    registry: ToolSchemaProvider, context_mode: str = "max",
    schema_names: Iterable[str] | None = None,
) -> List[Dict[str, Any]]:
    """Compatibility discovery view: only Nano has intentionally unloaded tools."""
    if context_mode != "nano":
        return []
    canonical = registry.schemas()
    selected = select_tool_schemas(canonical, context_mode=context_mode, schema_names=schema_names)
    return [row for row in compact_tool_catalog(canonical, schema_names=selected.chosen)
            if row["residency"] == "not_loaded"]


CAPABILITY_OMISSION_HEADER = "[CAPABILITY_OMISSION_MANIFEST]"


def format_capability_omissions(
    omissions: Any, *, header: str = CAPABILITY_OMISSION_HEADER,
) -> List[str]:
    """Render the capability-omission manifest — ONE formatter (v6.78.0, owner Q20).

    Replaces five divergent copies (two in ``tools/tool_discovery.py``, three in
    ``loop.py``) so a withheld capability is never rendered with a different amount
    of truth depending on which path the agent hit. Detail is the richest available
    fact: the loader ``error``, else the blocked ``resource``, else the REAL withheld
    tool NAMES (the four thinner copies printed "no detail" for exactly the rows that
    carry names — ``disabled_by_contract``/``missing_credential``). Never raises: an
    unrenderable row is skipped rather than breaking tool discovery.
    """

    lines: List[str] = [header] if header else []
    for item in omissions or []:
        if not isinstance(item, dict):
            continue
        names = item.get("tools")
        detail = (
            item.get("error")
            or item.get("resource")
            or (", ".join(str(name) for name in names) if isinstance(names, list) and names else "")
            or "no detail"
        )
        lines.append(
            f"- {item.get('surface', 'unknown')}: {item.get('reason', 'unknown')} ({detail})"
        )
    return lines
