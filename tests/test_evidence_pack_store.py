"""Packing consumes current store facts: one task's status row and its two evidence directories, nothing else."""
import pytest

from ouroboros import evidence_packer as packer, evidence_packs as packs, obligations, store
from ouroboros import observability as obs
from supervisor import terminal_delivery
from tests.test_evidence_packs import _blob, _task, _result
from tests.test_evidence_packs import root as root


def _packable(root, task):
    with store.open_store(root, write=False) as txn:
        return packs.task_packable(txn, task)


def test_a_live_task_waits_and_only_a_status_change_reselects_it(root):
    _task(root, "retry", blobs=1, status="running", updated_at="2026-01-01T00:00:00Z")
    assert packer.run_pass(root)["packed"] == 0 and not _packable(root, "retry")
    assert "retry" not in packer.changed_tasks(root)
    _result(root, "retry", "running", note="a row write that leaves the status")
    assert "retry" not in packer.changed_tasks(root)  # the stamp is the status, not the signature
    _result(root, "retry", updated_at="2026-01-01T00:00:00Z")
    assert not (root / "task_results/retry.json").exists()
    assert "retry" in packer.changed_tasks(root) and _packable(root, "retry")
    assert packer.run_pass(root)["packed"] == 1


def test_the_stamp_is_the_status_and_the_evidence_directories_only(root):
    _task(root, "a", blobs=1)
    initial = packer.task_stamps(root, "a")
    obligations.add(root, "custom_duty", "run:1", {"request": {"task_id": "a"}, "phase": 1})
    obligations.add(root, "pending_drives", "a")
    assert terminal_delivery.register_pending_delivery(root, {"delivery_id": "owed", "task_id": "a", "chat_id": 1})
    _result(root, "a", acceptance_debt={"debt_id": "d1"})  # a body write that keeps the status
    assert packer.task_stamps(root, "a") == initial
    obs.write_call_manifest(root, task_id="a", call_id="late", manifest={"ref": _blob(root, b"late")})
    moved = packer.task_stamps(root, "a")
    assert moved != initial  # the task's own evidence directory changed
    _result(root, "a", "running")
    assert packer.task_stamps(root, "a") not in (initial, moved)  # and so did its status


@pytest.mark.parametrize("status", ["running", "completed"])
@pytest.mark.parametrize("tier", [store.TIER_ENFORCED, store.TIER_NAME])
def test_a_pass_reads_no_obligation_set(root, monkeypatch, status, tier):
    from tests.test_store import publish

    publish(root, tier=tier)
    for number in range(24):
        _task(root, f"t{number}", blobs=1, status=status)
        obligations.add(root, "custom_duty", f"run:{number}", {"task_id": f"t{number}"})
    monkeypatch.setattr(obligations, "members_in", lambda *a: pytest.fail("a packing pass read an obligation set"))
    report = packer.run_pass(root)
    assert report["faults"] == 0
    assert report["packed"] == (24 if status == "completed" else 0)
    assert report["unlinked"] == (48 if status == "completed" else 0)


def test_a_duty_committed_during_compression_no_longer_defers_the_unlink(root, monkeypatch):
    refs, _ = _task(root, "a", blobs=1)
    original = packs.write_pack

    def write(*args, **kwargs):
        result = original(*args, **kwargs)
        obligations.add(root, "custom_duty", "operation", {"task_id": "a"})
        return result

    with monkeypatch.context() as patch:
        patch.setattr(packs, "write_pack", write)
        assert packer.run_pass(root)["unlinked"] == 2
    from pathlib import Path
    assert not Path(refs[0]["path"]).exists() and packs.blob_bytes(root, refs[0])


def test_store_read_failure_defers_the_pass_and_recovers(root, monkeypatch):
    from pathlib import Path
    from ouroboros import maintenance

    refs, _ = _task(root, "a", blobs=1)
    with monkeypatch.context() as patch:
        def unavailable(*args, **kwargs):
            raise store.StoreBusy("simulated unavailable facts", reason="contention")
        patch.setattr(packer, "task_rows", unavailable)
        with pytest.raises(store.StoreBusy):
            packer.run_pass(root)
        assert maintenance.run(root)["jobs"]["evidence_packing"] == {"state": "pending", "items": 0}
    assert Path(refs[0]["path"]).exists()  # nothing packed, nothing unlinked: the job stays due
    assert "evidence_packing" in maintenance.due(root)
    assert packer.run_pass(root)["packed"] == 1


def test_a_pass_that_left_work_unfinished_stays_due_until_the_store_moves(root):
    """Both directions. A pass whose unlink waited for a held custody lock (a copy-back or settlement
    publishing beside it) published its pack but left the loose members; it records ``pending`` and is
    not quiet forever: the server's look spawns the child again once anything is written to the store
    (the contender's own receipt), and the child, running for any reason, finishes it at once. A pass
    that left nothing unfinished rests: a later unrelated write wakes no look."""
    from pathlib import Path
    from ouroboros import maintenance, maintenance_jobs
    from ouroboros.task_custody import task_custody_lock

    refs, handle = _task(root, "held", blobs=1)
    with task_custody_lock(root, "held") as acquired:
        assert acquired
        assert maintenance.run_job(root, "evidence_packing") == {"state": "done", "items": 1}
    assert packs.published_generations(root, "held") and Path(refs[0]["path"]).exists()  # packed, not unlinked
    row = maintenance_jobs.job(root, "evidence_packing")
    assert (row["pending"], row["quiet"], row["state"]) == (1, True, "done")
    assert "evidence_packing" not in maintenance.due(root)  # nothing written since the pass: no spawn
    _result(root, "other", "running")  # any store write re-arms the unfinished pass (a live row moves no terminal fact)
    assert "evidence_packing" in maintenance.due(root)
    assert maintenance.run_job(root, "evidence_packing")["state"] == "done"
    assert not Path(refs[0]["path"]).exists() and packs.blob_bytes(root, refs[0])
    row = maintenance_jobs.job(root, "evidence_packing")
    assert (row["pending"], row["quiet"]) == (0, False)  # the unlink owes one more look
    assert maintenance.run_job(root, "evidence_packing")["state"] == "done"
    assert maintenance_jobs.job(root, "evidence_packing")["quiet"] is True
    assert "evidence_packing" not in maintenance.due(root)
    _result(root, "other", "running", step=2)
    assert "evidence_packing" not in maintenance.due(root)  # nothing unfinished: a write alone wakes nothing
    with task_custody_lock(root, "later") as acquired:  # the child finishes unfinished work whenever it runs
        _task(root, "later", blobs=1)
        assert acquired and maintenance.run_job(root, "evidence_packing")["state"] == "done"
    assert maintenance_jobs.job(root, "evidence_packing")["pending"] == 1
    assert maintenance.run_job(root, "evidence_packing") is not None  # due in the child without any write
    assert maintenance_jobs.job(root, "evidence_packing")["pending"] == 0
