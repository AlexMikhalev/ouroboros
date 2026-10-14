"""Recovery never re-runs an answered prompt (Astra R2, consilium unanimous).

With the answer on the live bus BEFORE the result store (owner 1A(1)), a worker
that dies or stalls inside the store leaves a task whose row is not terminal
while the host already holds its answer (owed in the outbox, delivered with a
receipt, or unreadable evidence). Both recovery owners — the timeout reaper's
retry admission and dead-worker recovery — finalize that attempt from the
outbox's bytes through the lifecycle-fault vocabulary instead of admitting a
fresh model attempt over the original prompt. Without a registered answer the
ordinary retry still proceeds (the guard is two-sided).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ouroboros.task_results import load_task_result, write_task_result
from supervisor import terminal_delivery as td
from tests._cancel_intents_shared import qenv as _qenv

# The fixture is requested by name as a test parameter, so it is re-bound through a
# module attribute (a direct import that reappears as a parameter is an F811 under ruff).
qenv = _qenv


def _event(tid: str, text: str = "the authored answer") -> dict:
    return {"type": "send_message", "task_id": tid, "chat_id": 1, "text": text,
            "delivery_id": td.delivery_id_for(tid, text)}


def _task(tid: str) -> dict:
    return {"id": tid, "type": "task", "chat_id": 1, "depth": 0, "root_task_id": tid,
            "parent_task_id": "", "delegation_role": "root", "_attempt": 1}


def _register(drive, tid: str, state: str) -> dict:
    evt = _event(tid)
    if state == "unreadable":
        registry = drive / "state" / "terminal_deliveries.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text("{bad json", encoding="utf-8")
        return evt
    assert td.register_pending_delivery(drive, evt)
    if state == "delivered":
        assert td.register_delivery(drive, evt["delivery_id"], emitted=evt)
    return evt


def _reaper_retry(qenv, monkeypatch, tid: str):
    from supervisor import task_reaper

    monkeypatch.setattr("ouroboros.artifacts.handoff_task_attachments_for_retry", lambda *_a, **_kw: ({}, ""))
    monkeypatch.setattr("ouroboros.owner_mailbox.copy_owner_mailbox_for_retry", lambda *_a, **_kw: True)
    return task_reaper._enqueue_retry(qenv.q, _task(tid), task_id=tid, retry_task_id=tid + "-retry",
                                      attempt=1, terminal_reason="idle_timeout", recon_fields={})


@pytest.mark.parametrize("state", ["owed", "delivered", "unreadable"])
def test_timeout_retry_finalizes_a_registered_answer_instead_of_rerunning_the_prompt(qenv, monkeypatch, state):
    from ouroboros.headless import prepare_terminal_task_files
    from ouroboros.working_checkpoint import REASON_FINALIZATION_INCOMPLETE, final_answer_registered

    tid = "early-answer-crash"
    write_task_result(qenv.drive, tid, "running", result="working", task_attempt=1)
    evt = _register(qenv.drive, tid, state)
    assert final_answer_registered(qenv.drive, tid)
    assert prepare_terminal_task_files(qenv.drive, _task(tid))["terminal_source_present"] is False

    result = _reaper_retry(qenv, monkeypatch, tid)

    assert result == (False, 1, "registered_answer_finalized",
                      {"kind": "terminal_result", "target": tid, "status": "failed"})
    assert qenv.q.PENDING == [], "the original prompt was placed in PENDING"
    stored = load_task_result(qenv.drive, tid)
    assert stored["status"] == "failed" and stored["reason_code"] == REASON_FINALIZATION_INCOMPLETE
    assert stored["outcome_axes"]["execution"]["status"] == "infra_failed"
    assert stored["finalization_recovery"] == {"cause": "idle_timeout", "answer": "owed" if state == "owed" else state,
                                               "delivery_id": "" if state == "unreadable" else evt["delivery_id"],
                                               "attempt": 1}
    assert "not run again" in stored["terminal_host_notice"]
    if state == "unreadable":
        assert "could not be read" in stored["terminal_host_notice"]
        assert stored["result"] == stored["terminal_host_notice"] and "terminal_origin" not in stored
    else:
        # The outbox's exact bytes (the pending row, or the retained receipt) become the result.
        assert stored["result"] == "the authored answer" and stored["terminal_origin"] == "model_final"
        assert ("owed" if state == "owed" else "delivered") in stored["terminal_host_notice"]
    assert final_answer_registered(qenv.drive, tid), "finalizing never retires the answer's custody"


def test_timeout_retry_without_a_registered_answer_still_admits_the_retry(qenv, monkeypatch):
    tid = "no-answer-yet"
    write_task_result(qenv.drive, tid, "running", result="working", task_attempt=1)
    assert td.terminal_answer_receipts(qenv.drive, tid)["state"] == "unknown"

    result = _reaper_retry(qenv, monkeypatch, tid)

    assert result == (True, 2, "idle_timeout", {})
    assert [row["id"] for row in qenv.q.PENDING] == [tid + "-retry"]
    assert load_task_result(qenv.drive, tid)["status"] != "failed"


@pytest.mark.parametrize("state", ["owed", "delivered", "unreadable"])
@pytest.mark.parametrize("exitcode", [1, -11])
def test_dead_worker_recovery_finalizes_a_registered_answer_without_a_requeue(qenv, monkeypatch, state, exitcode):
    from supervisor import task_reaper, worker_health as health, workers

    tid = "crash-after-answer"
    task = _task(tid)
    write_task_result(qenv.drive, tid, "running", result="working", task_attempt=1)
    evt = _register(qenv.drive, tid, state)
    monkeypatch.setattr(workers, "DRIVE_ROOT", qenv.drive)
    monkeypatch.setattr(workers, "reconstruct_task_cost",
                        lambda *_a, **_k: {"cost_accounting_status": "unavailable", "cost_final": False})
    monkeypatch.setattr(health, "_dead_job_is_current", lambda job: True)
    monkeypatch.setattr(health, "_complete_exact_budget_pause_after_death", lambda *_a, **_k: (False, False))
    finished: list = []
    monkeypatch.setattr(task_reaper, "_finish_self_finalized_task",
                        lambda *args, **kwargs: finished.append((args, kwargs)))
    monkeypatch.setattr(qenv.q, "enqueue_task",
                        lambda *_a, **_k: pytest.fail("the original prompt must not be requeued"))
    sent: list = []
    monkeypatch.setattr(workers, "send_with_budget", lambda *args, **kwargs: sent.append(args), raising=False)
    worker = SimpleNamespace(proc=SimpleNamespace(pid=4321, is_alive=lambda: False), active_capacity=True)
    job = {"worker": worker, "task_id": tid, "task": task, "meta": {"task": task, "attempt": 1},
           "worker_id": 0, "exitcode": exitcode, "drive_root": str(qenv.drive)}

    health._recover_crashed_task_without_terminal(job, qenv.q)

    (args, kwargs), = finished
    assert args[3] == tid and args[5] == "failed" and kwargs == {"worker_id": 0}
    stored = load_task_result(qenv.drive, tid)
    assert args[6]["status"] == stored["status"] == "failed"
    assert stored["reason_code"] == "finalization_incomplete"
    assert stored["finalization_recovery"]["cause"] == "worker_crash"
    if state != "unreadable":
        assert stored["result"] == "the authored answer"
        assert stored["finalization_recovery"]["delivery_id"] == evt["delivery_id"]
    assert sent == [], "no crash toast: the answer and the honest card speak"


def test_a_refused_finalization_write_keeps_the_job_for_the_next_health_tick(qenv, monkeypatch):
    """A registered answer with a store that refuses the write: no retry, no silent drop."""
    from supervisor import task_reaper, worker_health as health, workers

    tid = "store-refuses"
    task = _task(tid)
    write_task_result(qenv.drive, tid, "running", result="working", task_attempt=1)
    _register(qenv.drive, tid, "owed")
    monkeypatch.setattr(workers, "DRIVE_ROOT", qenv.drive)
    monkeypatch.setattr(workers, "reconstruct_task_cost",
                        lambda *_a, **_k: {"cost_accounting_status": "unavailable", "cost_final": False})
    monkeypatch.setattr(health, "_dead_job_is_current", lambda job: True)
    monkeypatch.setattr(health, "_complete_exact_budget_pause_after_death", lambda *_a, **_k: (False, False))
    monkeypatch.setattr(qenv.q, "enqueue_task",
                        lambda *_a, **_k: pytest.fail("the original prompt must not be requeued"))

    def refuse(*_args, **_kwargs):
        raise OSError("store unavailable")

    monkeypatch.setattr("ouroboros.task_results.write_task_result", refuse)
    worker = SimpleNamespace(proc=SimpleNamespace(pid=4321, is_alive=lambda: False), active_capacity=True)
    job = {"worker": worker, "task_id": tid, "task": task, "meta": {"task": task, "attempt": 1},
           "worker_id": 0, "exitcode": 1, "drive_root": str(qenv.drive)}
    with pytest.raises(task_reaper.TerminalFileRecoveryPending):
        health._recover_crashed_task_without_terminal(job, qenv.q)
    assert load_task_result(qenv.drive, tid)["status"] == "running"


def test_registered_answer_reads_the_outbox_never_the_result_prose(tmp_path):
    from ouroboros.working_checkpoint import registered_answer

    tid = "custody-read"
    write_task_result(tmp_path, tid, "running", result="prose the host must not adopt")
    assert registered_answer(tmp_path, tid) == {}
    evt = _event(tid, "owed bytes")
    assert td.register_pending_delivery(tmp_path, evt)
    assert registered_answer(tmp_path, tid) == {"basis": "owed", "text": "owed bytes", "delivery_id": evt["delivery_id"]}
    assert td.register_delivery(tmp_path, evt["delivery_id"], emitted=evt)
    assert registered_answer(tmp_path, tid) == {"basis": "delivered", "text": "owed bytes",
                                                "delivery_id": evt["delivery_id"]}
    # A legacy delivered id proves no bytes; it still refuses regeneration.
    legacy = td.delivery_id_for(tid, "legacy")
    assert td.register_delivery(tmp_path, legacy)
    registry = tmp_path / "state" / "terminal_deliveries.json"
    data = json.loads(registry.read_text(encoding="utf-8"))
    data["receipts"].pop(evt["delivery_id"])
    registry.write_text(json.dumps(data), encoding="utf-8")
    assert registered_answer(tmp_path, tid)["basis"] == "delivered"
    assert registered_answer(tmp_path, tid)["text"] == ""
