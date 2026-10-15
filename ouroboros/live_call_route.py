"""The realtime route of a live call and what the host knows about it before the call opens.

A call's engine route is resolved BEFORE the turn's context is fitted, because the
fit must measure against the realtime window, not Main's. That window is not a
table the host trusts blindly (BIBLE: external capabilities are sourced,
auditable evidence): the documented window of a known realtime route is written
into the capability-evidence store with its source URL and read date, so the
ordinary context fit, the record and a later audit all read the same fact; an
unknown route stays unknown and the fit keeps its own unknown-window behaviour.
The call's initial Nano tool view and the owner's duration ceiling live here too,
so the agent (context preparation) and the driver (the session) read one answer.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ouroboros.call_protocol import CallSpec
from ouroboros.gateways.realtime_engine import ENGINE_GOOGLE, ENGINE_OPENAI, EngineRoute
from ouroboros.tool_capabilities import COGNITIVE_MEMORY_TOOL_NAMES

log = logging.getLogger(__name__)

# The call's initial Nano view: memory of the mind and the web; everything else is
# one ``enable_tools`` away (growth reaches the engine as ``UpdateTools``).
CALL_INITIAL_TOOL_NAMES: Tuple[str, ...] = tuple(sorted(COGNITIVE_MEMORY_TOOL_NAMES | {"web_search"}))
REALTIME_EVIDENCE_KIND = "realtime_windows"   # capability_evidence.json namespace of the documented windows


@dataclass(frozen=True)
class RouteFact:
    """A documented fact about a realtime route, with its source and read date."""

    window_tokens: int
    max_output_tokens: int
    session_cap_minutes: Optional[int]
    source_url: str
    read_on: str


REALTIME_ROUTE_FACTS: Dict[Tuple[str, str], RouteFact] = {
    (ENGINE_OPENAI, "gpt-realtime-2.1"): RouteFact(
        128_000, 32_000, 60, "https://developers.openai.com/api/docs/models/gpt-realtime-2.1", "2026-10-16"),
    (ENGINE_GOOGLE, "gemini-3.8-live"): RouteFact(
        131_072, 65_536, None, "https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live", "2026-10-16"),
}


@dataclass(frozen=True)
class CallRoute:
    engine: EngineRoute
    fact: Optional[RouteFact]
    evidence_fp: str

    @property
    def window_tokens(self) -> Optional[int]:
        return self.fact.window_tokens if self.fact else None


@dataclass(frozen=True)
class CallContextPlan:
    spec: CallSpec
    route: CallRoute
    tool_schemas: List[Dict[str, Any]]
    fit_kwargs: Dict[str, Any]


def call_spec_of(task: Dict[str, Any]) -> Optional[CallSpec]:
    """The call this task is, or ``None`` for a text turn; malformed call metadata raises."""
    meta = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    raw = meta.get("live_call")
    return CallSpec.from_metadata(raw) if isinstance(raw, dict) and raw else None


def resolve_call_route(spec: CallSpec, drive_root: Any) -> CallRoute:
    """The engine route of the call; a known route's documented window becomes evidence."""
    from ouroboros.capability_evidence import route_fingerprint

    route = EngineRoute(engine=spec.engine, model=spec.model, voice=spec.voice)
    fact = REALTIME_ROUTE_FACTS.get((spec.engine, spec.model))
    fp = route_fingerprint(provider=spec.engine, model=spec.model)
    if fact is not None:
        _record_window_evidence(drive_root, route, fact, fp)
    return CallRoute(route, fact, fp)


def _record_window_evidence(drive_root: Any, route: EngineRoute, fact: RouteFact, fp: str) -> None:
    from ouroboros.capability_evidence import (
        SOURCE_PROVIDER_METADATA, STATUS_CONFIRMED, CapabilityEvidence, _store_evidence, canonical_evidence_root,
    )

    observed = f"{fact.read_on}T00:00:00Z"
    evidence = CapabilityEvidence(
        window_tokens=fact.window_tokens, status=STATUS_CONFIRMED, source=SOURCE_PROVIDER_METADATA, route_fp=fp,
        model=route.model, provider=route.engine, ts=observed,
        detail=f"documented context window; {fact.source_url}; read {fact.read_on}", provenance="provider_documentation",
        response_limit={"max_output_tokens": fact.max_output_tokens, "status": STATUS_CONFIRMED,
                        "source": SOURCE_PROVIDER_METADATA, "observed_at": observed, "route_fp": fp},
    )
    _store_evidence(drive_root if drive_root is not None else canonical_evidence_root(),
                    REALTIME_EVIDENCE_KIND, fp, evidence.to_json())


def window_evidence(drive_root: Any, spec: CallSpec) -> Optional[Dict[str, Any]]:
    """The recorded window evidence of the call's route (``None`` when unknown)."""
    from ouroboros.capability_evidence import _load, route_fingerprint

    record = _load(drive_root).get(REALTIME_EVIDENCE_KIND, {}).get(route_fingerprint(provider=spec.engine, model=spec.model))
    return dict(record) if isinstance(record, dict) else None


def initial_call_schemas(tools: Any) -> List[Dict[str, Any]]:
    """The room's permitted schemas through the existing Nano path, with the call's initial names."""
    from ouroboros.tool_policy import initial_tool_schemas

    return initial_tool_schemas(tools, context_mode="nano", schema_names=CALL_INITIAL_TOOL_NAMES)


def call_context_plan(task: Dict[str, Any], tools: Any, drive_root: Any, prepare: Callable[..., Any]) -> Optional[CallContextPlan]:
    """Resolve the route BEFORE the context is fitted; ``None`` for a text turn.

    ``prepare`` is the context builder the agent is about to call: the engine
    window rides as its ``known_window_tokens`` when it takes one (the context
    lane's entry); otherwise the fit runs as today and the window still bounds
    the driver's own view.
    """
    spec = call_spec_of(task)
    if spec is None:
        return None
    route = resolve_call_route(spec, drive_root)
    fit_kwargs: Dict[str, Any] = {}
    try:
        accepts_window = "known_window_tokens" in inspect.signature(prepare).parameters
    except (TypeError, ValueError):
        accepts_window = False
    if route.window_tokens and accepts_window:
        fit_kwargs["known_window_tokens"] = route.window_tokens
    return CallContextPlan(spec, route, initial_call_schemas(tools), fit_kwargs)


def _call_setting(key: str) -> str:
    from ouroboros.settings_integrity import runtime_setting

    return str(runtime_setting(key, "") or "").strip()


def call_minutes_ceiling(spec: CallSpec) -> Optional[int]:
    """The owner's ceiling for this call: the spec's, else the configured one for its kind."""
    if spec.max_minutes:
        return int(spec.max_minutes)
    raw = _call_setting("OUROBOROS_PRESENCE_CALL_MAX_MINUTES" if spec.presence_binding_id else "OUROBOROS_CALL_MAX_MINUTES")
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def native_search_enabled(spec: CallSpec) -> bool:
    """Gemini's own search grounding, the owner's choice; OpenAI sessions never ask for it."""
    if spec.engine != ENGINE_GOOGLE:
        return False
    raw = (_call_setting("OUROBOROS_CALL_NATIVE_SEARCH") or "true").lower()
    return raw in ("1", "true", "yes", "on")
