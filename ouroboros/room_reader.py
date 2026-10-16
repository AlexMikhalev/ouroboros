"""Structural reader facts shared by room intake and addressed question answers.

Queued work remains addressable explicitly but cannot consume ordinary room
input. A live actor waiting for its own answer is still reading. No delivery
here releases an owner, budget, restored-process or project admission hold.
"""
from __future__ import annotations


def is_reading(root, task_id: str, task: dict, *, running: bool, fences=None) -> bool:
    if not running or task.get("_owner_hold") or task.get("_project_admission_hold"):
        return False
    from ouroboros.owner_mailbox import mailbox_drain_ended
    from ouroboros.owner_pause import fence_closed, read_fence
    from supervisor.queue_transitions import budget_pause_fact
    if budget_pause_fact(task, fences) is not None:
        return False
    if fence_closed(read_fence(root, str(task.get("root_task_id") or task_id))):
        return False
    return not mailbox_drain_ended(root, task_id)
