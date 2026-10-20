"""A warm owner-wait Resume persists on the admission lane, outside Q: reserve, write, revalidate, grant.

The real pool fixture (``tests/test_owner_wait_pool.py``) with a real preparation generation. Both
directions: a contended grant retries at the lane without a storage acquisition under Q and lands
once when the store frees; a stack whose worker is gone between the durable write and the grant is
rolled back to ``waiting`` and keeps its capacity lent; a refused authority writes nothing; and with
nothing contending the same tick grants at once.
"""
from __future__ import annotations

import contextlib
import threading
import time
import traceback
from types import SimpleNamespace

import pytest

from ouroboros import store
from ouroboros.task_results import load_task_result
from supervisor import queue, worker_owner_wait, workers
from supervisor import workspace_preparation as prep
from tests._store_journal_shared import use_journal
from tests.test_admission_contention import database_reader
from tests.test_owner_wait_pool import maintain_capacity, park, pool as pool
from tests.test_workspace_preparation import _WAIT_SEC

pytestmark = pytest.mark.serial
_REAL_THREAD = threading.Thread  # the pool fixture stubs the global Thread; the lane needs the real one


@pytest.fixture
def generation(monkeypatch):
    # Only the preparation owner's lane threads and the test's own database holder are real;
    # the pool's spawn stubs stay in place.
    import tests.test_admission_contention as holders

    real = SimpleNamespace(Thread=_REAL_THREAD, Lock=threading.Lock, Event=threading.Event,
                           Condition=threading.Condition, current_thread=threading.current_thread)
    monkeypatch.setattr(prep, "threading", real)
    monkeypatch.setattr(holders, "threading", real)
    prep._reset_for_tests()
    opened = prep.start_generation()
    try:
        yield opened
    finally:
        prep.close_generation(opened)
        assert opened.wait_idle(_WAIT_SEC)
        prep._reset_for_tests()


def wait_for(predicate, what):
    deadline = time.monotonic() + _WAIT_SEC
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def store_opens_under_q(monkeypatch):
    seen = []
    real = store.connection

    @contextlib.contextmanager
    def connection(*args, **kwargs):
        if queue._queue_lock._is_owned():
            seen.append([frame.name for frame in traceback.extract_stack()][-8:])
        with real(*args, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(store, "connection", connection)
    return seen


def request_resume(pool):
    park(pool)
    worker_owner_wait.handle_owner_wait({**pool.event, "phase": "resume"}, workers)


@pytest.mark.parametrize("journal", ["PERSIST", "WAL"])
def test_contended_grant_retries_at_the_lane_and_lands_once(pool, generation, monkeypatch, journal):
    use_journal(monkeypatch, journal)
    request_resume(pool)
    under_q = store_opens_under_q(monkeypatch)
    original = worker_owner_wait.set_owner_wait
    held, attempts, failures = [], [], []

    def resumed_write(root, task_id, wait, **kwargs):
        if wait.get("state") != "resumed":
            return original(root, task_id, wait, **kwargs)
        attempts.append(threading.current_thread().name)
        if not held:
            held.append(stack.enter_context(database_reader(pool.root, write=journal == "WAL")))
        try:
            return original(root, task_id, wait, **kwargs)
        except store.StoreBusy as exc:
            failures.append(exc.reason)
            raise

    monkeypatch.setattr(worker_owner_wait, "set_owner_wait", resumed_write)
    with contextlib.ExitStack() as stack:
        maintain_capacity()  # reserves under Q and hands the grant to the admission lane
        wait_for(lambda: len(failures) >= 2, "the grant retried at the lane")
        assert set(attempts) == {prep.ADMISSION_LANE} and set(failures) == {"contention"}
        assert pool.meta.get("_resume_claim") and not pool.original.active_capacity
        assert pool.original.in_q.empty() and pool.original.resume_hold == "owner"  # the slot hold stands
        maintain_capacity()  # a second tick finds the claim in flight: no second grant, the hold stays
        assert len(set(attempts)) == 1 and pool.original.resume_hold == "owner"
        held[0]()
        assert generation.wait_idle(_WAIT_SEC)
    grant = pool.original.in_q.get_nowait()
    assert grant["phase"] == "resume_granted" and pool.original.in_q.empty()
    assert pool.original.active_capacity and "_resume_claim" not in pool.meta
    assert load_task_result(pool.root, "owner")["owner_wait"]["state"] == "resumed"
    assert "owner_wait_resume_requested" not in pool.meta and under_q == [], under_q
    maintain_capacity()  # granted once: nothing more to resume
    assert pool.original.in_q.empty() or pool.original.in_q.get_nowait()["type"] == "worker_quiescence"


def test_uncontended_grant_lands_in_the_same_tick_off_q(pool, generation, monkeypatch):
    request_resume(pool)
    under_q = store_opens_under_q(monkeypatch)
    maintain_capacity()
    assert generation.wait_idle(_WAIT_SEC)
    assert pool.original.in_q.get_nowait()["phase"] == "resume_granted" and pool.original.active_capacity
    assert load_task_result(pool.root, "owner")["owner_wait"]["state"] == "resumed"
    assert under_q == [], under_q


def test_worker_gone_between_the_durable_grant_and_the_put_rolls_the_wait_back(pool, generation, monkeypatch):
    request_resume(pool)
    original = worker_owner_wait.set_owner_wait
    written, proceed = threading.Event(), threading.Event()

    def resumed_write(root, task_id, wait, **kwargs):
        result = original(root, task_id, wait, **kwargs)
        if wait.get("state") == "resumed":
            written.set()  # the grant is durable; the put has not happened
            assert proceed.wait(_WAIT_SEC)
        return result

    monkeypatch.setattr(worker_owner_wait, "set_owner_wait", resumed_write)
    maintain_capacity()
    assert written.wait(_WAIT_SEC)
    assert load_task_result(pool.root, "owner")["owner_wait"]["state"] == "resumed"
    with queue._queue_lock:
        pool.original.reaping = True  # the stack's worker died meanwhile: custody owns it now
    proceed.set()
    assert generation.wait_idle(_WAIT_SEC)
    assert pool.original.in_q.empty() and not pool.original.active_capacity
    assert pool.meta["owner_wait"]["state"] == "waiting" and "_resume_claim" not in pool.meta
    assert load_task_result(pool.root, "owner")["owner_wait"]["state"] == "waiting"  # rolled back outside Q
    assert pool.meta.get("owner_wait_resume_requested")  # the request stands for custody to settle


def test_refused_authority_writes_nothing(pool, generation, monkeypatch):
    from ouroboros.cancel_intents import request_cancel

    request_resume(pool)
    request_cancel(pool.root, "owner", reason="owner Stop", source="test")
    writes = []
    original = worker_owner_wait.set_owner_wait
    monkeypatch.setattr(worker_owner_wait, "set_owner_wait",
                        lambda root, task_id, wait, **kw: (writes.append(wait.get("state")), original(root, task_id, wait, **kw))[1])
    maintain_capacity()
    assert generation.wait_idle(_WAIT_SEC)
    assert writes == [] and pool.original.in_q.empty() and not pool.original.active_capacity
    assert load_task_result(pool.root, "owner")["owner_wait"]["state"] == "waiting"
