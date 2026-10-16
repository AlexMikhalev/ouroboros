"""Durable job facts survive the process that wrote them (``ouroboros/maintenance_jobs.py``)."""

from __future__ import annotations

import pytest

from ouroboros import maintenance_jobs, store_import


def test_job_facts_round_trip_and_survive_a_fresh_read(tmp_path):
    store_import.ensure_current_store(tmp_path)
    assert maintenance_jobs.job(tmp_path, "legacy_reconcile") is None
    imported = maintenance_jobs.job(tmp_path, "current_store_import")
    assert imported["state"] == "done" and imported["done"] == imported["total"] == 1
    assert set(maintenance_jobs.jobs(tmp_path)) == {"current_store_import"}

    maintenance_jobs.record(tmp_path, "legacy_reconcile", state="running", cursor="task-0042", done=42, total=None)
    maintenance_jobs.record(tmp_path, "activity_backfill", state="done", negative=["proj_x"])
    row = maintenance_jobs.record(tmp_path, "legacy_reconcile", state="done", done=100)

    assert row["state"] == "done" and row["cursor"] == "task-0042" and row["done"] == 100 and row["updated_at"]
    assert maintenance_jobs.is_done(tmp_path, "legacy_reconcile")
    assert set(maintenance_jobs.jobs(tmp_path)) == {"current_store_import", "legacy_reconcile", "activity_backfill"}
    assert maintenance_jobs.job(tmp_path, "activity_backfill")["negative"] == ["proj_x"]


def test_job_range_read_ignores_other_meta_keys(tmp_path):
    store_import.ensure_current_store(tmp_path)
    maintenance_jobs.record(tmp_path, "a", state="pending")
    assert set(maintenance_jobs.jobs(tmp_path)) == {"a", "current_store_import"}  # other meta keys are not jobs


@pytest.mark.parametrize("name", ["", "has:colon", None])
def test_invalid_job_names_are_refused(tmp_path, name):
    store_import.ensure_current_store(tmp_path)
    with pytest.raises(ValueError):
        maintenance_jobs.record(tmp_path, name, state="pending")


def test_invalid_state_is_refused(tmp_path):
    store_import.ensure_current_store(tmp_path)
    with pytest.raises(ValueError):
        maintenance_jobs.record(tmp_path, "x", state="almost")
