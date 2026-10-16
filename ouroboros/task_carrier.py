"""Actual attempt ownership, separate from historical direct-chat origin."""


def execution_kind(task) -> str:
    """Read the existing host-owned attempt fact; legacy rows retain their origin."""
    if isinstance(task, dict):
        meta = task.get("metadata") or {}
        owner = task.get("_execution_owner") or task.get("execution_owner") or meta.get("_execution_owner") or {}
        direct, presence = task.get("_is_direct_chat"), task.get("_presence_turn")
    else:
        meta = getattr(task, "task_metadata", None) or {}
        owner = meta.get("_execution_owner") or {}
        direct, presence = getattr(task, "is_direct_chat", False), False
    kind = owner.get("kind")
    return kind if kind in {"direct", "presence", "pooled"} else "presence" if presence else "direct" if direct else "pooled"


def inline_execution(task) -> bool:
    return execution_kind(task) in {"direct", "presence"}
