"""Replay membership end to end on the real two-lane bus.

The replay sweep (``supervisor.terminal_delivery.replay_pending_deliveries``) and the
bus that owns delivery membership (``supervisor.event_observation.ObservedEventQueue``)
were built against one interface; this pins them together on the spawn-context
``EventQueueManager`` the server uses, so a proxy that does not expose the membership
methods, or a sweep that counts attempts before the bus admits a copy, fails here.

Owner (14 Oct 2026, 18:56): «не сжигать попытки повторной доставки, пока сообщение уже
стоит в очереди».
"""
from __future__ import annotations

import multiprocessing

import pytest

from supervisor import terminal_delivery as td
from supervisor.event_observation import EventQueueManager
from tests.test_delivery_replay_membership import _age, _attempts, _owed


@pytest.mark.serial
def test_a_queued_or_handled_answer_is_not_copied_until_the_consumer_releases_it(tmp_path):
    context = multiprocessing.get_context("spawn")
    with EventQueueManager(ctx=context) as manager:
        bus = manager.Queue()
        event = _owed(tmp_path)
        bus.put(event)  # the worker's own live put

        for _ in range(5):  # queued: many due sweeps, no copy, no attempt
            _age(tmp_path)
            assert td.replay_pending_deliveries(tmp_path, event_queue=bus) == []
        assert _attempts(tmp_path, event["delivery_id"]) == 0
        assert bus.qsize() == 1

        served = bus.get_nowait()  # the consumer took it; the handler is running
        assert served["delivery_id"] == event["delivery_id"]
        _age(tmp_path)
        assert td.replay_pending_deliveries(tmp_path, event_queue=bus) == []
        assert bus.qsize() == 0 and _attempts(tmp_path, event["delivery_id"]) == 0

        bus.release_delivery(event["delivery_id"])  # handler returned without a receipt
        assert td.replay_pending_deliveries(tmp_path, event_queue=bus) == [event["delivery_id"]]
        assert bus.qsize() == 1 and _attempts(tmp_path, event["delivery_id"]) == 1
        copy = bus.get_nowait()
        assert copy["delivery_id"] == event["delivery_id"] and copy["text"] == event["text"]


@pytest.mark.serial
def test_the_same_sweep_without_the_membership_methods_admits_nothing(tmp_path):
    """Control: an endpoint that cannot answer membership is unknown, never 'absent'."""

    class _Plain:
        def __init__(self) -> None:
            self.items: list = []

        def put(self, item) -> None:
            self.items.append(item)

        def put_nowait(self, item) -> None:
            self.items.append(item)

    event = _owed(tmp_path)
    plain = _Plain()
    _age(tmp_path)
    assert td.replay_pending_deliveries(tmp_path, event_queue=plain) == []
    assert plain.items == [] and _attempts(tmp_path, event["delivery_id"]) == 0
