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

import logging
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.call_protocol import CallSpec
from ouroboros.gateways.realtime_engine import ENGINE_GOOGLE, ENGINE_OPENAI, EngineRoute
from ouroboros.tool_capabilities import COGNITIVE_MEMORY_TOOL_NAMES

log = logging.getLogger(__name__)

# The call's initial Nano view: memory of the mind, the web, and the room's chat (what
# reads better than it sounds — links, code, lists — the voice writes there itself);
# everything else is one ``enable_tools`` away (growth reaches the engine as ``UpdateTools``).
CALL_INITIAL_TOOL_NAMES: Tuple[str, ...] = tuple(sorted(COGNITIVE_MEMORY_TOOL_NAMES | {"web_search", "send_user_message"}))
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


def initial_call_schemas(tools: Any, spec: Optional[CallSpec] = None) -> List[Dict[str, Any]]:
    """The call's initial view of the room's permitted schemas.

    An owner call opens with the Nano path and the call's initial names. A
    Presence call opens with every schema its binding's ceiling permits: the
    binding author chose that set, and the registry already narrowed it.
    """
    from ouroboros.tool_policy import initial_tool_schemas

    if spec is not None and spec.presence_binding_id:
        return initial_tool_schemas(tools, context_mode="max")
    return initial_tool_schemas(tools, context_mode="nano", schema_names=CALL_INITIAL_TOOL_NAMES)


def call_context_plan(task: Dict[str, Any], tools: Any, drive_root: Any) -> Optional[CallContextPlan]:
    """Resolve the route BEFORE the context is fitted; ``None`` for a text turn.

    The engine's documented window rides into the ordinary preparation as its
    ``known_window_tokens``, so the fit measures against the realtime window; an
    unknown window leaves the fit as for any route without capacity evidence.
    """
    spec = call_spec_of(task)
    if spec is None:
        return None
    route = resolve_call_route(spec, drive_root)
    fit_kwargs: Dict[str, Any] = {"known_window_tokens": route.window_tokens} if route.window_tokens else {}
    return CallContextPlan(spec, route, initial_call_schemas(tools, spec), fit_kwargs)


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


def recover_call_context(ctx: Any, messages: List[Dict[str, Any]], schemas: List[Dict[str, Any]], error: Any) -> bool:
    """An actual size refusal earns a strictly smaller ordinary projection.

    Max keeps its books until the provider refuses, just like a text turn. The
    provider's instruction-field limit is not substituted for the model window:
    Low/Nano retain tier zero and address the same captured reference sources.
    Estimates alone never refuse a call or trigger this recovery.
    """
    from ouroboros.live_tool_batch import text_of
    from ouroboros.loop_model_call import _adopt_view_facts

    plan = getattr(ctx, "context_fit_plan", None)
    if error.kind != "context_length" or plan is None:
        return False
    try:
        evidence = json.loads(error.message)
        deficit = int(evidence["input_tokens"]) - int(evidence["limit_tokens"])
        if evidence["field"] != "session.instructions" or int(evidence["limit_tokens"]) <= 0 or deficit <= 0:
            return False
    except (ValueError, TypeError, KeyError):
        return False
    mode = str(getattr(ctx, "active_context_mode", "") or plan.initial_mode)
    next_mode = {"max": "low", "low": "nano"}.get(mode)
    if not next_mode or not messages or messages[0].get("role") != "system":
        return False
    rebound = plan.reproject_for_route(
        window_tokens=plan.window_tokens, known_window=bool(plan.window_tokens),
        ratio=plan.projection(mode).calibration_ratio, output_reserve=plan.output_reserve_tokens,
        tool_schemas=schemas, start_mode=mode, current_messages=messages,
        refused_tokens=deficit, select_mode=next_mode)
    candidate = rebound.reproject_transcript(messages, next_mode)
    before, after = (len(text_of(rows[0]["content"]).encode("utf-8")) for rows in (messages, candidate))
    if after >= before:
        return False
    messages[:] = candidate
    ctx.context_fit_plan, ctx.active_context_mode = rebound, next_mode
    _adopt_view_facts(ctx, rebound, next_mode)
    trace = getattr(ctx, "_execution_trace", None)
    if isinstance(trace, dict):
        trace.setdefault("live_context_recovery", []).append({**evidence, "from": mode, "to": next_mode,
                                                              "before_bytes": before, "after_bytes": after})
    return True
