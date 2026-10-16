"""Stored tool names survive public renames; event and receipt names do not change."""

from collections.abc import Iterable
from types import MappingProxyType


TOOL_NAME_ALIASES = MappingProxyType({
    "promote_chat_to_task": "start_task",
    "ensure_project_scope": "move_to_project",
    "steer_task": "tell_task",
})


def canonical_tool_name(name: str) -> str:
    """Resolve a saved name without registering a retired callable alias."""
    return TOOL_NAME_ALIASES.get(name, name)


def expand_disabled_tool_names(names: Iterable[str]) -> list[str]:
    """Retain saved restrictions and withhold their successors as well."""
    return list(dict.fromkeys(name for old in names for name in (old, canonical_tool_name(old))))
