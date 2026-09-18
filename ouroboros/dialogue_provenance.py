"""Neutral rendering of exact actor and conversation facts in dialogue memory."""

from __future__ import annotations

import json
from typing import Any, Mapping


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def is_presence_task(task: Mapping[str, Any]) -> bool:
    metadata = _mapping(task.get("metadata"))
    return bool(
        task.get("_presence_turn")
        or task.get("_presence_origin")
        or isinstance(metadata.get("presence"), Mapping)
    )


def presence_provenance_from_task(task: Mapping[str, Any]) -> dict[str, str]:
    """Return the stable, non-secret presence facts carried by one task.

    The host-authored event owns transport identity while the immutable
    capability ceiling owns the reviewed state/selection fingerprints.  Keep
    this projection small so dialogue and reflection records share one exact
    provenance shape without copying prompt text or arbitrary actor metadata.
    """

    metadata = _mapping(task.get("metadata"))
    presence = _mapping(metadata.get("presence"))
    if not presence:
        return {}
    event = _mapping(presence.get("event"))
    actor = _mapping(event.get("actor"))
    contract = _mapping(task.get("task_contract"))
    ceiling = _mapping(contract.get("capability_ceiling"))
    return {
        "binding_id": _text(presence.get("binding_id")),
        "transport_skill": _text(presence.get("transport_skill")),
        "behavior_skill": _text(presence.get("behavior_skill")),
        "profile_fingerprint": _text(ceiling.get("profile_fingerprint") or presence.get("profile_fingerprint")),
        "state_fingerprint": _text(ceiling.get("state_fingerprint")),
        "selection_fingerprint": _text(ceiling.get("selection_fingerprint")),
        "source_event_id": _text(event.get("source_event_id")),
        "conversation_key": _text(event.get("conversation_key")),
        "provider": _text(event.get("provider")),
        "account_id": _text(event.get("account_id")),
        "conversation_id": _text(event.get("conversation_id")),
        "thread_id": _text(event.get("thread_id")),
        "actor_id": _text(actor.get("platform_actor_id") or actor.get("id")),
    }


def presence_provenance_fields(task: Mapping[str, Any]) -> dict[str, Any]:
    value = presence_provenance_from_task(task)
    return {"presence_provenance": value} if value else {}


def dialogue_speaker(entry: Mapping[str, Any]) -> str:
    transport = entry.get("transport") if isinstance(entry.get("transport"), Mapping) else {}
    actor = transport.get("actor") if isinstance(transport.get("actor"), Mapping) else {}
    return str(
        entry.get("sender_label")
        or entry.get("username")
        or entry.get("author")
        or actor.get("display_name")
        or actor.get("username")
        or actor.get("platform_actor_id")
        or actor.get("id")
        or "User"
    )


def dialogue_provenance(entry: Mapping[str, Any]) -> str:
    transport = entry.get("transport") if isinstance(entry.get("transport"), Mapping) else {}
    facts = []
    for label, key in (
        ("provider", "provider"),
        ("account", "account_id"),
        ("conversation", "conversation_id"),
        ("thread", "thread_id"),
    ):
        value = str(transport.get(key) or "").strip()
        if value:
            facts.append(f"{label}={value}")
    source = str(entry.get("source") or "").strip()
    if source and not facts:
        facts.append(f"source={source}")
    delivery = _mapping(transport.get("delivery"))
    state = _text(delivery.get("state"))
    if state:
        label = {"authored": "authored (delivery unconfirmed)",
                 "accepted": "accepted (provider acceptance only)"}.get(state, state)
        facts.append(f"delivery={label}")
    return "; ".join(facts)


def dialogue_author(entry: Mapping[str, Any]) -> str:
    speaker = dialogue_speaker(entry)
    provenance = dialogue_provenance(entry)
    return f"{speaker} [{provenance}]" if provenance else speaker


def dialogue_text(entry: Mapping[str, Any]) -> str:
    """Keep observed delivery metadata distinct from the quoted message body."""
    text = str(entry.get("text", ""))
    transport = _mapping(entry.get("transport"))
    message = _mapping(transport.get("message"))
    if entry.get("type") == "presence_delivery" and message:
        text += "\n[Delivery details: " + json.dumps(dict(message), ensure_ascii=False, sort_keys=True) + "]"
    return text


__all__ = [
    "dialogue_author",
    "dialogue_provenance",
    "dialogue_speaker",
    "dialogue_text",
    "is_presence_task",
    "presence_provenance_fields",
    "presence_provenance_from_task",
]
