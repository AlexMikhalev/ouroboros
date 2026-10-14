"""Unconsumed idle inputs on the existing supervisor event bus.

The bounded drain cannot conclude idleness while a fact that may change that
decision is still queued. This queue indexes only its pending envelopes, under
the same mutex as put/get; it neither folds activity nor grants a lease. The
single supervisor consumer finishes dispatch before checking timeouts. A later
enqueue is ordinary concurrent activity, not a claim about the earlier snapshot.
"""
from __future__ import annotations

import logging
import queue
from collections import Counter
from multiprocessing.managers import SyncManager
from typing import Any

from supervisor.event_taxonomy import disposition_for

log = logging.getLogger(__name__)


def idle_input_addresses(event: Any) -> frozenset[str]:
    """Routing addresses of an existing idle-input event, without interpreting work.

    ``log_event`` is telemetry, even when its payload names a lifecycle event:
    its handler does not fold activity. Heartbeats and host narration likewise
    never change the idle inputs. Exact attempts remain the handlers' authority;
    a stale frame can delay observation only until it leaves this queue.
    """
    if not isinstance(event, dict):
        return frozenset()
    kind = str(event.get("type") or "")
    disposition = disposition_for(kind)
    if disposition is None or not disposition.idle_input:
        return frozenset()
    if kind == "send_message" and (not event.get("is_progress") or event.get("host_narration")):
        return frozenset()
    return frozenset(str(event[key]) for key in ("task_id", "parent_task_id", "root_task_id")
                     if event.get(key))


class ObservedEventQueue(queue.Queue):
    """The manager's ordinary FIFO with a pending-address index, not another bus."""

    def _init(self, maxsize: int) -> None:
        super()._init(maxsize)
        self._pending: Counter[str] = Counter()

    def _put(self, item: Any) -> None:
        addresses = idle_input_addresses(item)
        # Retain the addresses by value: an in-process producer may keep or
        # mutate its original dict, while the manager transport copies it.
        super()._put((item, addresses))
        self._pending.update(addresses)

    def _get(self) -> Any:
        item, addresses = super()._get()
        for address in addresses:
            self._pending[address] -= 1
            if not self._pending[address]:
                del self._pending[address]
        return item

    def pending_idle_inputs(self) -> set[str]:
        """One atomic snapshot of addresses with an unconsumed idle input."""
        with self.mutex:
            return set(self._pending)


class EventQueueManager(SyncManager):
    """Same process-lifetime manager, with the supervisor's queue factory."""


EventQueueManager.register("Queue", ObservedEventQueue)


def pending_idle_observations(event_queue: Any) -> set[str] | None:
    """Read outside Q; missing/broken observation is unknown, never empty.

    No actor clock or progress fact is advanced. The domain timeout owner uses
    this only to defer its idle inference, never deadline/ceiling/owner controls.
    """
    try:
        pending = event_queue.pending_idle_inputs()
        if not isinstance(pending, (set, frozenset)) or any(not isinstance(v, str) for v in pending):
            raise ValueError("invalid supervisor event observation")
        return set(pending)
    except Exception:
        log.warning("Supervisor idle inputs are unobservable; idle decisions deferred", exc_info=True)
        return None
