"""The root budget fence reads the dispatch document of the last successful publication.

A send needs the dispatch document's generation certified by ``queue.alive`` — never a
fresh stamp: a stalled supervisor tick leaves the certificate in place and must not
stop a single call. A publication in flight (the publisher unlinks the stamp first) is
re-read briefly; a certificate that stays missing or mismatched (a failed publication)
refuses typed, and an active fence still refuses whatever the stamp's age.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import threading
from types import SimpleNamespace

import pytest

from ouroboros import queue_observation
from ouroboros import usage_accounting as ua

ROOT, CHILD = "root-1", "child-1"
OLD_TS = "2020-01-01T00:00:00+00:00"


def _write(root, name, payload):
    path = pathlib.Path(root) / "state" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _publish(root, *, fences=(), alive_generation="g1"):
    _write(root, "queue_dispatch.json", {"generation": "g1", "ts": OLD_TS, "budget_root_fences": list(fences), "members": {}})
    if alive_generation is not None:
        _write(root, "queue.alive", {"generation": alive_generation, "ts": OLD_TS, "heartbeats": {}})


def _reserve(root, task_id):
    with ua.usage_scope(ua.UsageScope(drive_root=root, task_id=task_id, root_task_id=ROOT)):
        return ua.reserve_attempt(ua.AttemptRequest(model="m", provider="p", task_id=task_id))


@pytest.fixture(autouse=True)
def _tree(tmp_path, monkeypatch):
    from ouroboros.task_results import STATUS_RUNNING, write_task_result

    monkeypatch.setattr(ua, "_reservation_cost", lambda _request: 0.01)
    monkeypatch.setattr(queue_observation, "_sleep", lambda _sec: None)
    write_task_result(tmp_path, ROOT, STATUS_RUNNING, chat_id=0, root_task_id=ROOT)
    write_task_result(tmp_path, CHILD, STATUS_RUNNING, chat_id=0, root_task_id=ROOT, parent_task_id=ROOT)


def test_an_old_but_certified_publication_admits_an_unfenced_send(tmp_path):
    _publish(tmp_path)  # both stamps are years old: age never gates a send
    assert _reserve(tmp_path, CHILD).attempt_id


def test_an_active_fence_refuses_whatever_the_stamp_age(tmp_path):
    _publish(tmp_path, fences=[{"root_task_id": ROOT, "status": "active", "fence_id": "F1"}])
    with pytest.raises(ua.BudgetExceeded, match="paused pending explicit resume"):
        _reserve(tmp_path, CHILD)


@pytest.mark.parametrize("alive_generation", [None, "g0"])
def test_a_failed_publication_refuses_typed(tmp_path, alive_generation):
    _publish(tmp_path, alive_generation=alive_generation)  # unlinked stamp, or one of an older publication
    with pytest.raises(ua.UsageAccountingError, match="root budget fence authority unavailable"):
        _reserve(tmp_path, CHILD)


@pytest.mark.parametrize("alive_generation", [None, "g0"])
def test_a_publication_in_flight_is_waited_out(tmp_path, monkeypatch, alive_generation):
    _publish(tmp_path, alive_generation=alive_generation)
    waits = []

    def publisher_finishes(_sec):
        waits.append(_sec)
        _write(tmp_path, "queue.alive", {"generation": "g1", "ts": OLD_TS, "heartbeats": {}})

    monkeypatch.setattr(queue_observation, "_sleep", publisher_finishes)
    assert _reserve(tmp_path, CHILD).attempt_id and waits


def test_a_publication_slower_than_the_former_window_is_waited_out(tmp_path, monkeypatch):
    _publish(tmp_path, alive_generation=None)  # issue #1754: 20 reads failed sends on a loaded host
    waits = []

    def slow_publisher(_sec):
        waits.append(_sec)
        if len(waits) == 25:
            _write(tmp_path, "queue.alive", {"generation": "g1", "ts": OLD_TS, "heartbeats": {}})

    monkeypatch.setattr(queue_observation, "_sleep", slow_publisher)
    assert _reserve(tmp_path, CHILD).attempt_id and len(waits) == 25


@pytest.mark.parametrize("damage", ["malformed", "non_object", "missing", "no_generation", "unreadable"])
def test_an_unreadable_dispatch_document_refuses_typed(tmp_path, monkeypatch, caplog, damage):
    _publish(tmp_path)
    path = tmp_path / "state/queue_dispatch.json"
    if damage == "missing":
        path.unlink()
    elif damage == "unreadable":
        read = pathlib.Path.read_text
        def refuse(target, *args, **kwargs):
            if target == path:
                raise PermissionError("fixture dispatch read failed")
            return read(target, *args, **kwargs)
        monkeypatch.setattr(pathlib.Path, "read_text", refuse)
    else:
        path.write_text({"malformed": "{not json", "non_object": "[]", "no_generation": "{}"}[damage], encoding="utf-8")
    reads, waits, load = [], [], queue_observation.load_queue_document
    def observe(*args):
        reads.append(args)
        return load(*args)
    monkeypatch.setattr(queue_observation, "load_queue_document", observe)
    monkeypatch.setattr(queue_observation, "_sleep", waits.append)
    with pytest.raises(ua.UsageAccountingError, match="root budget fence authority unavailable"):
        _reserve(tmp_path, CHILD)
    assert len(reads) == 1 and waits == []
    assert len([record for record in caplog.records if record.exc_info]) == int(damage in {"malformed", "unreadable"})


@pytest.mark.parametrize("operation", ["reserve", "dispatch"])
@pytest.mark.parametrize("sliced", [False, True])
@pytest.mark.parametrize("publication", ["stable", "before_lock", "under_lock"])
def test_publication_waits_only_outside_the_money_lock(tmp_path, monkeypatch, operation, sliced, publication):
    from ouroboros import _usage_wait

    _publish(tmp_path)
    reservation = _reserve(tmp_path, CHILD) if operation == "dispatch" else None
    stamp, held, waits, acquisitions = tmp_path / "state/queue.alive", [], [], []
    original = ua._locked

    @contextlib.contextmanager
    def locked(*args, **kwargs):
        with original(*args, **kwargs) as view:
            if publication == "under_lock" and not acquisitions:
                stamp.unlink()
            acquisitions.append(True)
            held.append(True)
            try:
                yield view
            finally:
                held.pop()

    def finish_publication(seconds):
        assert not held, "queue publication wait held the money lock"
        waits.append(seconds)
        _publish(tmp_path)

    monkeypatch.setattr(ua, "_locked", locked)
    monkeypatch.setattr(queue_observation, "_sleep", finish_publication)
    if publication == "before_lock":
        stamp.unlink()
    token = _usage_wait._CANCEL.set(threading.Event() if sliced else None)
    try:
        def act():
            return _reserve(tmp_path, CHILD) if operation == "reserve" else ua.mark_dispatched(reservation)
        if publication == "under_lock" and operation == "dispatch":
            with pytest.raises(ua.UsageAccountingError, match="root budget fence authority unavailable"):
                act()
        elif operation == "reserve":
            assert act().attempt_id
        else:
            act()
    finally:
        _usage_wait._CANCEL.reset(token)
    assert len(waits) == int(publication == "before_lock" or (publication == "under_lock" and operation == "reserve"))
    if reservation:
        with ua.usage_store.read(tmp_path) as view:
            assert view.attempt(reservation.attempt_id)["state"] == ("reserved" if publication == "under_lock" else "dispatched")


@pytest.fixture
def reservation_race(tmp_path, monkeypatch):
    """Revoke certification after real acquisition, before the first row write."""
    _publish(tmp_path)
    held, acquisitions = [], []
    original = ua._locked

    @contextlib.contextmanager
    def locked(*args, **kwargs):
        with original(*args, **kwargs) as view:
            if not acquisitions:
                (tmp_path / "state/queue.alive").unlink()
            acquisitions.append(True)
            held.append(True)
            try:
                yield view
            finally:
                held.pop()

    monkeypatch.setattr(ua, "_locked", locked)
    return held, acquisitions


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("provider_fails", [False, True])
def test_reservation_race_keeps_one_physical_send(tmp_path, monkeypatch, reservation_race, asynchronous, provider_fails):
    held, _ = reservation_race
    waits, prepared, sent = [], [], []

    def publish(_seconds):
        assert not held
        assert ua.read_usage_records(tmp_path) == []  # no reservation or charge before retry
        waits.append(True)
        _publish(tmp_path)

    def send():
        sent.append(True)
        if provider_fails:
            raise TimeoutError("provider already received the call")
        return {"usage": {"cost": 0.02}}

    async def send_async():
        return send()

    monkeypatch.setattr(queue_observation, "_sleep", publish)
    with ua.usage_scope(ua.UsageScope(drive_root=tmp_path, task_id=CHILD, root_task_id=ROOT)), ua.physical_attempt_limit(1):
        request = ua.AttemptRequest(model="m", provider="p", task_id=CHILD)
        with pytest.raises(TimeoutError) if provider_fails else contextlib.nullcontext():
            if asynchronous:
                asyncio.run(ua.execute_physical_attempt_async(request, send_async, before_dispatch=lambda r: prepared.append(r.attempt_id)))
            else:
                ua.execute_physical_attempt(request, send, before_dispatch=lambda r: prepared.append(r.attempt_id))
        assert ua._PHYSICAL_LIMIT.get().used == 1
    rows = ua.read_usage_records(tmp_path)
    assert len(waits) == len(prepared) == len(sent) == len(rows) == 1
    assert rows[0]["attempt_id"] == prepared[0]
    assert rows[0]["state"] == ("unresolved" if provider_fails else "settled")
    assert rows[0].get("cost_usd") == (None if provider_fails else 0.02)


@pytest.mark.parametrize("alive_generation", [None, "g0"])
@pytest.mark.parametrize("sliced", [False, True])
def test_reservation_race_with_persistent_absence_still_refuses(tmp_path, monkeypatch, reservation_race, alive_generation, sliced):
    from ouroboros import _usage_wait

    held, acquisitions = reservation_race
    waits = []
    monkeypatch.setattr(queue_observation, "DISPATCH_PUBLICATION_WAIT_SEC", 0.2)

    def unavailable(seconds):
        assert not held
        waits.append(seconds)
        _publish(tmp_path, alive_generation=alive_generation)

    monkeypatch.setattr(queue_observation, "_sleep", unavailable)
    token = _usage_wait._CANCEL.set(threading.Event() if sliced else None)
    try:
        with pytest.raises(ua.UsageAccountingError, match="root budget fence authority unavailable"):
            _reserve(tmp_path, CHILD)
    finally:
        _usage_wait._CANCEL.reset(token)
    assert len(waits) == 3 and len(acquisitions) == 1
    assert ua.read_usage_records(tmp_path) == []


def test_reservation_race_observes_a_new_fence(tmp_path, monkeypatch, reservation_race):
    held, acquisitions = reservation_race

    def publish_fence(_seconds):
        assert not held
        _publish(tmp_path, fences=[{"root_task_id": ROOT, "status": "active", "fence_id": "F2"}])

    monkeypatch.setattr(queue_observation, "_sleep", publish_fence)
    with pytest.raises(ua.BudgetExceeded, match="paused pending explicit resume"):
        _reserve(tmp_path, CHILD)
    assert len(acquisitions) == 1 and ua.read_usage_records(tmp_path) == []


@pytest.mark.parametrize("reason", ["cancelled", "deadline"])
def test_reservation_publication_wait_honors_owner_controls(tmp_path, monkeypatch, reservation_race, reason):
    from ouroboros.llm_attempt import PhysicalDispatchInterrupted
    from ouroboros.model_wait import task_model_wait_scope

    held, acquisitions = reservation_race
    waits, control = [], []

    def stop(_seconds):
        assert not held
        waits.append(True)
        control.append(reason)

    monkeypatch.setattr(queue_observation, "_sleep", stop)
    with task_model_wait_scope(task={"id": CHILD}, drive_root=tmp_path, event_queue=None,
                               worker_slot_held=False, owner_control=lambda: control[0] if control else None):
        with pytest.raises(PhysicalDispatchInterrupted) as error:
            _reserve(tmp_path, CHILD)
    assert error.value.control_reason == reason
    assert len(waits) == len(acquisitions) == 1 and ua.read_usage_records(tmp_path) == []


def test_async_cancellation_joins_the_publication_wait_without_a_send(tmp_path, monkeypatch, reservation_race):
    from ouroboros import _usage_wait

    held, acquisitions = reservation_race
    waiting, release = threading.Event(), threading.Event()
    waits, cancellations = [], []

    def pause(_seconds):
        assert not held
        waits.append(True)
        cancellations.append(_usage_wait._CANCEL.get())
        waiting.set()
        assert release.wait(3)

    async def send():
        pytest.fail("cancelled reservation sent a provider call")

    async def exercise():
        with ua.usage_scope(ua.UsageScope(drive_root=tmp_path, task_id=CHILD, root_task_id=ROOT)):
            task = asyncio.create_task(ua.execute_physical_attempt_async(ua.AttemptRequest(model="m", provider="p"), send))
            try:
                assert await asyncio.to_thread(waiting.wait, 3)
                task.cancel()
                assert await asyncio.to_thread(cancellations[0].wait, 3)
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    monkeypatch.setattr(queue_observation, "_sleep", pause)
    asyncio.run(exercise())
    assert len(waits) == len(acquisitions) == 1 and ua.read_usage_records(tmp_path) == []


@pytest.mark.parametrize("stage", ["write", "exit"])
def test_reservation_mutation_or_exit_error_is_never_retried(tmp_path, monkeypatch, stage):
    from ouroboros._usage_wait import DispatchAuthorityUnavailable

    _publish(tmp_path)
    acquisitions, writes = [], []
    original_lock, original_write = ua._locked, ua.usage_store.Txn.write

    def write(view, row, *args, **kwargs):
        result = original_write(view, row, *args, **kwargs)
        writes.append(row["attempt_id"])
        if stage == "write":
            raise DispatchAuthorityUnavailable("after mutation")
        return result

    @contextlib.contextmanager
    def locked(*args, **kwargs):
        with original_lock(*args, **kwargs) as view:
            acquisitions.append(True)
            yield view
            raise DispatchAuthorityUnavailable("during exit")

    monkeypatch.setattr(ua.usage_store.Txn, "write", write)
    monkeypatch.setattr(ua, "_locked", locked)
    with pytest.raises(DispatchAuthorityUnavailable, match="after mutation|during exit"):
        _reserve(tmp_path, CHILD)
    assert len(acquisitions) == len(writes) == 1
    assert ua.read_usage_records(tmp_path) == []  # the transaction rolled back


def test_repeated_reservation_publication_races_share_one_wait_budget(tmp_path, monkeypatch):
    from ouroboros import _usage_wait

    _publish(tmp_path)
    clock, held, acquisitions, waits = [0.0], [], [], []
    original = ua._locked
    monkeypatch.setattr(queue_observation, "DISPATCH_PUBLICATION_WAIT_SEC", 0.2)
    timer = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(queue_observation, "_monotonic", timer.monotonic)
    monkeypatch.setattr(_usage_wait, "time", timer)

    @contextlib.contextmanager
    def locked(*args, **kwargs):
        with original(*args, **kwargs) as view:
            clock[0] += 0.1
            acquisitions.append(True)
            assert len(acquisitions) <= 3, "publication retries reset their deadline"
            (tmp_path / "state/queue.alive").unlink()
            held.append(True)
            try:
                yield view
            finally:
                held.pop()

    def publish(seconds):
        assert not held
        waits.append(seconds)
        clock[0] += seconds
        _publish(tmp_path)

    monkeypatch.setattr(ua, "_locked", locked)
    monkeypatch.setattr(queue_observation, "_sleep", publish)
    with pytest.raises(ua.UsageAccountingError, match="root budget fence authority unavailable"):
        _reserve(tmp_path, CHILD)
    assert len(acquisitions) == 3 and len(waits) == 2
    assert ua.read_usage_records(tmp_path) == []
