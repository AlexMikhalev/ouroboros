"""Wedge detection for the supervisor generation.

The two silent-wedge predicates (a stalled supervisor loop, a heartbeat-silent
in-process chat turn), the owner alert one of them raises, and the dedicated
watchdog thread that evaluates both outside the loop it watches.
"""

from __future__ import annotations

import os
import threading
import time

from ouroboros.server_process import DATA_DIR, log, _restart_requested
from ouroboros.utils import utc_now_iso


def _supervisor_loop_stalled(last_tick: float, now: float, deadline_sec: int) -> bool:
    """True when the supervisor loop has not published a liveness tick within the
    deadline (WS3). deadline_sec<=0 disables the watchdog."""
    return deadline_sec > 0 and (now - last_tick) > deadline_sec


def _chat_turn_wedged(busy: bool, last_activity_ts, now: float, deadline_sec: int) -> bool:
    """True when an IN-PROCESS direct-chat turn is busy but its liveness tick has been
    silent past the deadline (WS3). ``last_activity_ts is None`` => the turn has not
    started its liveness loop yet (not wedged). deadline_sec<=0 disables the check."""
    if not busy or last_activity_ts is None or deadline_sec <= 0:
        return False
    return (now - last_activity_ts) > deadline_sec


def _alert_chat_turn_wedge(task_id, gap: float) -> None:
    """WS3: a direct-chat turn is heartbeat-silent. New messages still get answered
    on independent native actors, but a hung IN-PROCESS turn cannot be killed
    independently of the supervisor process. Surface it +
    recommend /restart, which is the safe full recovery."""
    from supervisor.state import append_jsonl, load_state
    try:
        append_jsonl(DATA_DIR / "logs" / "supervisor.jsonl", {
            "ts": utc_now_iso(), "type": "chat_turn_wedge",
            "task_id": str(task_id or ""), "silent_sec": round(gap, 1),
        })
    except Exception:
        log.debug("chat-turn wedge log failed", exc_info=True)
    try:
        owner_chat = int((load_state() or {}).get("owner_chat_id") or 0)
        if owner_chat:
            from supervisor.message_bus import send_with_budget
            send_with_budget(
                owner_chat,
                f"⚠️ A chat turn looks wedged (~{int(gap)}s with no heartbeat). New messages "
                "still get answered, but the stuck turn can't be cleared in-process — /restart "
                "to fully recover it.",
                is_progress=True,
                task_id=str(task_id or ""),
                progress_meta={
                    "task_incident": "chat_turn_wedge",
                    "toast_once": f"{task_id or 'direct-chat'}:chat_turn_wedge",
                },
            )
    except Exception:
        log.debug("chat-turn wedge owner alert failed", exc_info=True)


def _start_supervisor_liveness_watchdog(liveness: list, stop_event=None) -> None:
    """Dedicated daemon thread (NOT inside the supervisor loop, so it fires even when
    that loop stalls). It ALERTS the owner on two silent-wedge classes — a supervisor
    loop stall (new-message intake starvation) and a heartbeat-silent in-process
    direct-chat turn — converting a multi-hour silent wedge into an immediate signal.
    It deliberately does NOT kill a hung thread; independent native actors keep
    the chat responsive meanwhile. ``stop_event`` is
    a PER-GENERATION token: when the supervisor loop that owns ``liveness`` exits (incl.
    the crash-storm death path, which never sets the global restart flag), it is set so
    this watchdog stops watching a now-stale liveness list (no false post-revival alert)."""
    from ouroboros.config import get_supervisor_liveness_deadline_sec

    deadline = get_supervisor_liveness_deadline_sec()
    if deadline <= 0:
        return

    def _watch() -> None:
        from supervisor.state import append_jsonl, load_state
        interval = min(15, max(1, deadline // 3))
        loop_alerted = False
        wedged_tasks: set[str] = set()
        while not _restart_requested.is_set() and not (stop_event is not None and stop_event.is_set()):
            time.sleep(interval)
            # ONE clock: both halves measure an ELAPSED GAP against stamps taken on
            # the monotonic clock (the loop-liveness tick here, and the chat-turn
            # heartbeat in agent.py), so a wall-clock jump — NTP step, DST/timezone
            # change, manual set, VM resume — can neither fabricate a stall/wedge
            # nor mask a real one on either half.
            now = time.monotonic()
            # (1) Supervisor loop stall — new-message intake starvation.
            if _supervisor_loop_stalled(liveness[0], now, deadline):
                if not loop_alerted:
                    gap = now - liveness[0]
                    log.error(
                        "Supervisor loop STALLED ~%.0fs — new-message intake starved (native "
                        "chat still answers); investigate a blocking step.", gap,
                    )
                    try:
                        append_jsonl(DATA_DIR / "logs" / "supervisor.jsonl", {
                            "ts": utc_now_iso(), "type": "supervisor_loop_stall", "stalled_sec": round(gap, 1),
                        })
                    except Exception:
                        log.debug("loop-stall log failed", exc_info=True)
                    try:
                        owner_chat = int((load_state() or {}).get("owner_chat_id") or 0)
                        if owner_chat:
                            from supervisor.message_bus import send_with_budget
                            send_with_budget(
                                owner_chat,
                                f"⚠️ My supervisor loop stalled for ~{int(gap)}s — new messages may be "
                                "delayed. I recover on the next tick or a restart; investigating.",
                                is_progress=True,
                                progress_meta={
                                    "task_incident": "supervisor_loop_stall",
                                    # pid disambiguates server GENERATIONS: the monotonic stamp alone can
                                    # repeat at a similar uptime offset across restarts, and the browser's
                                    # toast-dedupe set outlives this process while the page stays open.
                                    "toast_once": f"supervisor-loop-stall:{os.getpid()}:{int(liveness[0])}",
                                },
                            )
                    except Exception:
                        log.debug("loop-stall owner alert failed", exc_info=True)
                    loop_alerted = True
            else:
                loop_alerted = False
            # (2) Each native actor has its own liveness and alert identity.
            try:
                from supervisor.workers import chat_turn_liveness
                turns = chat_turn_liveness()
            except Exception:
                turns = []
            live_ids = {task_id for task_id, _ in turns}
            wedged_tasks.intersection_update(live_ids)
            for turn_task, turn_ts in turns:
                if _chat_turn_wedged(True, turn_ts, now, deadline) and turn_task not in wedged_tasks:
                    _alert_chat_turn_wedge(turn_task, now - turn_ts)
                    wedged_tasks.add(turn_task)

    threading.Thread(target=_watch, name="supervisor-liveness-watchdog", daemon=True).start()
