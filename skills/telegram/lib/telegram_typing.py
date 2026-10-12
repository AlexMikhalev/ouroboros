"""Keep Telegram's native typing indicator alive while a task runs.

One ``sendChatAction`` lives about five seconds on Telegram clients, while a
task turn runs for minutes. The host emits ``chat.typing`` exactly once per
turn, so without a refresh the indicator decays and the owner cannot tell a
working agent from a dead one (#1766). This module re-sends the action on an
interval below the client expiry until the turn's terminal row lands on the
outbound subscription, fail-soft by design:

- transient failures (transport, timeout, HTTP 429/5xx) back off exponentially
  up to ``_BACKOFF_MAX_SEC`` and recover to the base cadence on the first
  success — a dead network pauses the indicator, it never busy-loops and never
  blocks polling or delivery;
- a permanent rejection (HTTP 4xx other than 429) stops the loop for that
  chat with one warning line;
- ``_MAX_LIFETIME_SEC`` caps the loop so a lost terminal event cannot keep a
  ghost refresh alive forever;
- one loop per (chat, task): a new ``chat.typing`` for the same task replaces
  the earlier loop, progress rows leave it running (the turn is still working;
  its next tick re-arms the bubble a landed message cleared), and
  ``stop_all_keep_typing`` (skill unload) cancels every loop so nothing
  outlives the plugin.

The loop is NOT a supervised task on purpose: it must never consume restart
budget on Telegram outages — losing the indicator degrades to today's
behavior, nothing more.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Dict

from .telegram_api import (
    TelegramRequestRejected,
    TelegramTransportError,
    is_transient_telegram_error,
)

log = logging.getLogger(__name__)

# Below Telegram's ~5s client expiry; the send timeout keeps one slow
# round-trip from stretching the gap past the expiry for more than one lapse.
_REFRESH_SEC = 3.0
_SEND_TIMEOUT_SEC = 2.0
# Ceiling for the transient-failure backoff; reset on the first success.
_BACKOFF_MAX_SEC = 60.0
# Safety cap: a turn whose terminal event was lost stops refreshing here.
_MAX_LIFETIME_SEC = 30 * 60.0

# chat_id:task_key -> {"task": asyncio.Task}. Guarded by a plain lock: the
# start/stop calls arrive on the event loop, the cancel-on-unload path may not.
_loops: Dict[str, Dict[str, Any]] = {}
_loops_lock = threading.Lock()


def _loop_key(chat_id: int, task_key: str) -> str:
    return f"{int(chat_id or 0)}:{str(task_key or '')}"


def _cancel_quietly(task: Any) -> None:
    """Cancel an asyncio task from any thread; a closed loop needs no cancel."""
    if task is None or task.done():
        return
    try:
        task.get_loop().call_soon_threadsafe(task.cancel)
    except RuntimeError:
        pass  # loop already closed: the task died with it


async def _keep_typing_loop(
    api: Any,
    chat_id: int,
    task_key: str,
    client_factory: Callable[[], Any],
) -> None:
    interval = _REFRESH_SEC
    deadline = time.monotonic() + _MAX_LIFETIME_SEC
    warned_permanent = False
    try:
        while True:
            # Sleep before every send, the first included: the chat.typing
            # handler already fired the immediate action this loop refreshes,
            # so an instant second send would only burn rate limit.
            await asyncio.sleep(interval)
            if time.monotonic() >= deadline:
                log.debug("Telegram keep-typing lifetime cap reached for %s", task_key)
                return
            try:
                client = client_factory()
                await asyncio.wait_for(
                    client.send_chat_action(chat_id, "typing"),
                    timeout=_SEND_TIMEOUT_SEC,
                )
                interval = _REFRESH_SEC  # healthy again: resume the base cadence
            except asyncio.CancelledError:
                raise
            except TelegramTransportError as exc:
                interval = min(interval * 2, _BACKOFF_MAX_SEC)
                log.debug("Telegram keep-typing transient failure: %s", exc)
            except TelegramRequestRejected as exc:
                if is_transient_telegram_error(exc):
                    # 429/5xx backs off like any transient failure: falling
                    # through to the next iteration's sleep is the whole
                    # point — a zero-delay retry would busy-loop the limit.
                    interval = min(interval * 2, _BACKOFF_MAX_SEC)
                    log.debug("Telegram keep-typing rate-limited: %s", exc)
                else:
                    # Permanent rejection (bad token, blocked bot, wrong chat):
                    # stop refreshing this chat once, loudly enough to be seen.
                    if not warned_permanent:
                        warned_permanent = True
                        try:
                            api.log("warning", f"Telegram typing indicator stopped for this chat: {exc}")
                        except Exception:
                            log.warning("Telegram typing indicator stopped: %s", exc)
                    return
            except Exception as exc:  # unknown failure shape: back off, keep trying
                interval = min(interval * 2, _BACKOFF_MAX_SEC)
                log.debug("Telegram keep-typing unexpected failure: %s", exc)
    except asyncio.CancelledError:
        pass  # normal stop: terminal row, replacement, or unload
    finally:
        with _loops_lock:
            entry = _loops.get(_loop_key(chat_id, task_key))
            if entry is not None and entry.get("task") is asyncio.current_task():
                _loops.pop(_loop_key(chat_id, task_key), None)


def start_keep_typing(
    api: Any,
    chat_id: int,
    task_key: str,
    client_factory: Callable[[], Any],
) -> None:
    """Arm (or re-arm) the refresh loop for one (chat, task) pair.

    Called from the ``chat.typing`` handler; a second ``chat.typing`` for the
    same task replaces the earlier loop instead of stacking a second one.
    """
    if not chat_id or not str(task_key or "").strip():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop to schedule on: keep today's single-shot behavior
    key = _loop_key(chat_id, task_key)
    with _loops_lock:
        previous = _loops.pop(key, None)
    if previous is not None:
        _cancel_quietly(previous.get("task"))
    task = running.create_task(_keep_typing_loop(api, chat_id, task_key, client_factory))
    with _loops_lock:
        _loops[key] = {"task": task}


def stop_keep_typing(chat_id: int, task_key: str) -> None:
    """Stop the refresh loop for one (chat, task) pair (terminal row landed)."""
    if not str(task_key or "").strip():
        return
    key = _loop_key(chat_id, task_key)
    with _loops_lock:
        entry = _loops.pop(key, None)
    if entry is not None:
        _cancel_quietly(entry.get("task"))


def stop_all_keep_typing() -> None:
    """Cancel every refresh loop (skill unload / panic; Emergency Stop holds)."""
    with _loops_lock:
        pending = list(_loops.values())
        _loops.clear()
    for entry in pending:
        _cancel_quietly(entry.get("task"))


def active_keep_typing_keys() -> list[str]:
    """Snapshot of the armed (chat, task) keys — test observability only."""
    with _loops_lock:
        return sorted(_loops.keys())
