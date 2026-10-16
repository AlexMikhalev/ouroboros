"""Finish an inline exact park before releasing the direct activity registration."""

from __future__ import annotations

import logging
from types import SimpleNamespace

log = logging.getLogger(__name__)


def park_direct_pause(task: dict, event: dict) -> bool:
    from ouroboros.budget_pause import end_dispatch_fence
    from ouroboros.utils import append_jsonl, utc_now_iso
    from supervisor import queue as q, workers as pool
    from supervisor.events_budget import install_exact_budget_pause
    from supervisor.message_bus import get_bridge

    task_id = str(task["id"])
    checkpoint = event["resource_limit"]["checkpoint"]
    try:
        shim = SimpleNamespace(
            RUNNING=pool.RUNNING, PENDING=pool.PENDING, WORKERS=pool.WORKERS, DRIVE_ROOT=pool.DRIVE_ROOT,
            sort_pending=q.sort_pending, persist_queue_snapshot=q.persist_queue_snapshot, bridge=get_bridge())
        install_exact_budget_pause(shim, task_id, checkpoint, evt=event, source="direct_turn_inline_park")
        append_jsonl(pool.DRIVE_ROOT / "logs" / "supervisor.jsonl", {
            "ts": utc_now_iso(), "type": "direct_turn_budget_pause_parked_inline",
            "task_id": task_id, "chat_id": event.get("chat_id"), "pause_id": checkpoint.get("pause_id")})
        return True
    except Exception:
        log.error("Direct turn %s could not be parked inline; its event takes the ordinary path",
                  task_id, exc_info=True)
        return False
    finally:
        end_dispatch_fence(task_id)


def recover_direct_pause(task: dict) -> bool:
    """A raise after pausing cannot turn the same saved attempt into a terminal."""
    from ouroboros.budget_pause import budget_pause_row, death_returns_to_pause, pause_event, end_dispatch_fence
    from supervisor import workers as pool

    try:
        row = budget_pause_row(task.get("budget_drive_root") or pool.DRIVE_ROOT, str(task["id"]))
        if not death_returns_to_pause(row, int(task.get("_attempt") or 1)):
            return False
        if row.get("source_ref"):
            event = pause_event(task, row)
            if not park_direct_pause(task, event):
                pool.get_event_q().put(event)
        else:
            log.error("Direct turn %s ended during pausing before its source was saved; retained for recovery",
                      task["id"])
        return True
    except Exception:
        log.error("Direct turn %s pause evidence unreadable; recovery decides its exception", task["id"], exc_info=True)
        return True
    finally:
        end_dispatch_fence(str(task["id"]))
