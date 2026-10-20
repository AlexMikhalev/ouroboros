"""A worker launch persists on the admission lane, outside Q: reserve, persist, revalidate, put.

The real assignment pass over real queue maps, a real preparation generation and real SQLite
holds. Both directions: a contended launch never waits under Q and dispatches once when the store
frees; a Stop or Pause arriving between the reservation and the put wins and nothing is sent; a
refused or failed persist releases the slot and keeps the row as residual demand; no worker ever
holds two reservations; and with nothing contending the same pass launches at once.
"""
from __future__ import annotations

import contextlib
import threading
import time
import traceback
from types import SimpleNamespace

import pytest

from ouroboros import store
from ouroboros.task_results import STATUS_SCHEDULED, write_task_result
from supervisor import queue, task_admission, task_lifecycle, worker_assignment, workers
from supervisor import workspace_preparation as prep
from tests._store_journal_shared import use_journal
from tests.test_admission_contention import database_reader
from tests.test_scheduled_receipt_contention import install_worker
from tests.test_workspace_preparation import _WAIT_SEC
from tests.test_workspace_preparation import env as env
from tests.worker_pool_support import affirm_ready

pytestmark = pytest.mark.serial

# Storage opened while Q is owned by paths this change does not move, each its own owner's: the
# pre-assignment cancel consult and the Project hold revalidation read task rows under Q at zero
# wait, and the pass's money reading is the usage store's bounded display read.
_UNTOUCHED_UNDER_Q = {"_drop_cancelled_pending", "revalidate_project_holds",
                      "_retry_terminalization_pending_for_assignment", "budget_remaining"}


def wait_for(predicate, what):
    deadline = time.monotonic() + _WAIT_SEC
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def store_opens_under_q(monkeypatch):
    """Every store connection opened while Q is owned, as the innermost supervisor function; an
    entry outside the untouched allow-list is the old defect come back."""
    seen = []
    real = store.connection

    @contextlib.contextmanager
    def connection(*args, **kwargs):
        if queue._queue_lock._is_owned():
            names = [frame.name for frame in traceback.extract_stack()]
            seen.append(next((name for name in reversed(names) if name in _UNTOUCHED_UNDER_Q), tuple(names[-10:])))
        with real(*args, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(store, "connection", connection)
    return seen


def scheduled_child(env, tid="launch-child", root="root"):
    """A queued child with its SCHEDULED receipt, as admission leaves it (project-scope-none)."""
    for task_id in ("root", "parent"):
        write_task_result(env.data, task_id, "running", root_task_id="root")
    task = {"id": tid, "type": "task", "chat_id": 7, "text": "work", "description": "work",
            "root_task_id": root, "parent_task_id": "parent", "delegation_role": "subagent",
            "memory_mode": "empty", "budget_drive_root": str(env.data), "_project_scope_none": True}
    write_task_result(env.data, tid, STATUS_SCHEDULED, parent_task_id="parent", root_task_id=root,
                      delegation_role="subagent", result="Subagent accepted and scheduled.")
    with queue._queue_lock:
        admitted = queue.enqueue_task(task)
    assert not admitted.get("_admission_blocked"), admitted
    return admitted


def hold_at_marks(env, monkeypatch, stack, *, write):
    """Begin the hold at the first dispatch-marks write, as a reader (rollback journal) or a writer (WAL)."""
    original = task_admission.write_task_result
    held, attempts, failures = [], [], []

    def marks(root, task_id, status, **fields):
        if task_id != "launch-child" or fields.get("_field_projector") is None:
            return original(root, task_id, status, **fields)
        attempts.append(threading.current_thread().name)
        if not held:
            held.append(stack.enter_context(database_reader(env.data, write=write)))
        try:
            return original(root, task_id, status, **fields)
        except store.StoreBusy as exc:
            failures.append((exc.reason, getattr(exc, "rollback_confirmed", None)))
            raise

    monkeypatch.setattr(task_admission, "write_task_result", marks)
    return held, attempts, failures


@pytest.mark.parametrize("journal", ["PERSIST", "WAL"])
def test_contended_launch_never_waits_under_q_and_dispatches_once_when_the_store_frees(env, monkeypatch, journal):
    use_journal(monkeypatch, journal)
    worker, delivered = install_worker(env, monkeypatch)
    row = scheduled_child(env)
    under_q = store_opens_under_q(monkeypatch)
    with contextlib.ExitStack() as stack:
        held, attempts, failures = hold_at_marks(env, monkeypatch, stack, write=journal == "WAL")
        workers.assign_tasks()  # reserves under Q and hands the launch to the admission lane
        assert worker.busy_task_id == "launch-child" and row.get("_launch_claim") and env.pending_ids() == ["launch-child"]
        wait_for(lambda: len(failures) >= 2, "the marks retried at the lane")
        assert set(attempts) == {prep.ADMISSION_LANE} and all(reason == "contention" for reason, _rb in failures)
        assert delivered == [] and "launch-child" not in queue.RUNNING
        workers.assign_tasks()  # a second pass finds the slot taken and the row claimed: no second launch
        assert len([t for t in delivered]) == 0 and sum(1 for w in workers.WORKERS.values() if w.busy_task_id) == 1
        held[0]()
        assert env.gen.wait_idle(_WAIT_SEC)
    assert [task["id"] for task in delivered] == ["launch-child"]
    assert "launch-child" in queue.RUNNING and env.pending_ids() == [] and "_launch_claim" not in row
    assert worker.busy_task_id == "launch-child"
    stored = env.row("launch-child")
    # The marks landed before the put; without a fork drive the worker's own start publishes RUNNING.
    assert stored["admitted_dispatch"] == "possible" and stored["status"] == STATUS_SCHEDULED
    assert all(isinstance(entry, str) for entry in under_q), [entry for entry in under_q if not isinstance(entry, str)]
    # The other direction: nothing contending, the same pass launches a second child at once.
    second = affirm_ready(SimpleNamespace(wid=1, busy_task_id=None,
                                          in_q=SimpleNamespace(put=lambda task: delivered.append(dict(task)))))
    workers.WORKERS[1] = second
    scheduled_child(env, "launch-second")
    workers.assign_tasks()
    assert env.gen.wait_idle(_WAIT_SEC)
    assert [task["id"] for task in delivered] == ["launch-child", "launch-second"] and second.busy_task_id == "launch-second"
    assert all(isinstance(entry, str) for entry in under_q), [entry for entry in under_q if not isinstance(entry, str)]


@pytest.mark.parametrize("control", ["stop", "pause"])
def test_control_between_reservation_and_put_wins_and_frees_the_slot(env, monkeypatch, control):
    from supervisor.events_budget import _set_root_budget_pause_locked

    worker, delivered = install_worker(env, monkeypatch)
    row = scheduled_child(env)
    committed, proceed = threading.Event(), threading.Event()
    original = worker_assignment._commit_launch

    def after_marks(queue_module, candidate, unsubmitted=None):
        projections = original(queue_module, candidate, unsubmitted)
        if candidate.get("id") == "launch-child" and projections:
            committed.set()  # the marks are durable; the put has not been entered
            assert proceed.wait(_WAIT_SEC)
        return projections

    monkeypatch.setattr(worker_assignment, "_commit_launch", after_marks)
    workers.assign_tasks()
    assert committed.wait(_WAIT_SEC) and worker.busy_task_id == "launch-child"
    with queue._queue_lock:
        if control == "stop":
            task_lifecycle.CANCELLED_ROOT_FENCES["root"] = "2026-10-20T00:00:00Z"
        else:
            _set_root_budget_pause_locked("root", {"cause": "owner_pause", "fence_id": "between-marks-and-put"})
    proceed.set()
    assert env.gen.wait_idle(_WAIT_SEC)
    assert delivered == [] and "launch-child" not in queue.RUNNING
    assert worker.busy_task_id is None and "_launch_claim" not in row  # the slot and the row are free again
    assert env.pending_ids() == ["launch-child"] and env.row("launch-child")["admitted_dispatch"] == "possible"
    assert worker_assignment._unsent(row) is not None  # committed, never put: inspected by this process, never re-sent
    workers.assign_tasks()  # the fence still stands: no launch, no second reservation
    assert env.gen.wait_idle(_WAIT_SEC) and delivered == [] and worker.busy_task_id is None


@pytest.mark.parametrize("failure", ["refused", "raised"])
def test_persist_refused_or_failed_releases_the_slot_and_keeps_residual_demand(env, monkeypatch, failure):
    worker, delivered = install_worker(env, monkeypatch)
    row = scheduled_child(env)
    calls = []

    def commit(queue_module, candidate, unsubmitted=None):
        calls.append(candidate.get("id"))
        if failure == "raised":
            raise OSError("store unavailable")
        return False  # refused: an owner's predicate wrote nothing

    monkeypatch.setattr(worker_assignment, "_commit_launch", commit)
    workers.assign_tasks()
    assert env.gen.wait_idle(_WAIT_SEC)
    assert calls == ["launch-child"] and delivered == []
    assert worker.busy_task_id is None and "_launch_claim" not in row and env.pending_ids() == ["launch-child"]
    assert "launch-child" not in queue.RUNNING and env.row("launch-child")["status"] == STATUS_SCHEDULED
    workers.assign_tasks()  # still residual demand: the next pass claims it again, once
    assert env.gen.wait_idle(_WAIT_SEC) and calls == ["launch-child", "launch-child"]
    assert sum(1 for w in workers.WORKERS.values() if w.busy_task_id) == 0


def test_uncertain_marks_commit_is_inspected_by_token_and_never_resent(env, monkeypatch):
    worker, delivered = install_worker(env, monkeypatch)
    row = scheduled_child(env)
    original = task_admission.commit_dispatch_marks
    outcomes = []

    def unknown_once(task, projections):
        result = original(task, projections)
        outcomes.append(result)
        return None if len(outcomes) == 1 else result  # the first commit's readback is unknown

    monkeypatch.setattr(task_admission, "commit_dispatch_marks", unknown_once)
    workers.assign_tasks()
    assert env.gen.wait_idle(_WAIT_SEC)
    assert outcomes == [True] and delivered == [] and worker.busy_task_id is None
    witness = worker_assignment._unsent(row)
    assert witness is not None and row["admitted_dispatch"] == "possible"  # owned by its witness, not sent
    workers.assign_tasks()  # the same row and attempt re-read their own marks and put once
    assert env.gen.wait_idle(_WAIT_SEC)
    assert [task["id"] for task in delivered] == ["launch-child"] and "launch-child" in queue.RUNNING
    assert len(outcomes) == 1  # the marks were never rewritten
