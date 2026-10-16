"""Project thread-activity derivation + the legacy activity backfill job (perf sprint P1).

``projects_summary`` derives ``has_thread_activity`` from registry facts alone
(origin / bindings / ``visible_revision`` / the write-once backfill flag) —
the per-request chat/progress log scan is retired. Legacy projects whose
activity predates the ``visible_revision`` counter are covered by the maintenance
child's ``activity_backfill`` job: one pass over the chat chain per installation,
persisting ``thread_activity_seen`` through the registry's own write path and the
negative result in its job row. Neither the GET path nor ``reconcile_projects`` scans.
"""
from __future__ import annotations

import json

import pytest

from ouroboros import maintenance, maintenance_jobs
from ouroboros import projects_registry as pr
from ouroboros.contracts.chat_id_policy import project_chat_id


def _backfill(data):
    return maintenance.run_job(data, "activity_backfill")


def _write_rows(path, chat_id, *, count=2, key="text"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for index in range(count):
            handle.write(json.dumps({"chat_id": chat_id, key: f"row {index}"}) + "\n")


def _summary_row(data, pid):
    return next(row for row in pr.projects_summary(data) if row["id"] == pid)


def test_backfill_seeds_flag_for_legacy_project_with_old_chat_rows(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    entry = pr.create_project(data, "legacy", origin="reconcile")
    _write_rows(data / "logs" / "chat.jsonl", int(entry["chat_id"]))
    # Unrelated chat rows must not activate other projects.
    quiet = pr.create_project(data, "quiet", origin="reconcile")

    pr.reconcile_projects(data)
    assert "thread_activity_seen" not in pr.get_reserved_project(data, "legacy"), "reconcile never scans"
    assert _backfill(data)["state"] == "done"

    assert pr.get_reserved_project(data, "legacy")["thread_activity_seen"] is True
    assert "thread_activity_seen" not in pr.get_reserved_project(data, "quiet")
    assert _summary_row(data, "legacy")["has_thread_activity"] is True
    assert _summary_row(data, "quiet")["has_thread_activity"] is False
    assert int(quiet["chat_id"]) != int(entry["chat_id"])
    row = maintenance_jobs.job(data, "activity_backfill")
    assert row["flagged"] == ["legacy"] and row["empty"] == ["quiet"]  # the negative result is a fact
    assert row["coverage"]["through"]["live"] == "1-progress.jsonl"


def test_backfill_is_archive_aware(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    entry = pr.create_project(data, "rotated", origin="reconcile")
    # Activity lives ONLY in a rotated archive segment (live logs empty/absent).
    _write_rows(data / "archive" / "chat_20260101T000000.jsonl", int(entry["chat_id"]))

    _backfill(data)

    assert pr.get_reserved_project(data, "rotated")["thread_activity_seen"] is True
    assert _summary_row(data, "rotated")["has_thread_activity"] is True


def test_telemetry_only_new_project_reads_inactive_and_get_never_scans(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    # The one-time backfill ran first (empty root): done, nothing to look for.
    assert _backfill(data)["state"] == "done"

    # A NEW project whose thread carries ONLY telemetry rows afterwards: no
    # owner-visible canonical row, no binding, revision 0 => inactive (the
    # disclosed micro-delta of the derivation).
    entry = pr.create_project(data, "telem", origin="reconcile")
    _write_rows(data / "logs" / "progress.jsonl", int(entry["chat_id"]), key="content")

    def _no_scan(*_args, **_kwargs):
        raise AssertionError("projects_summary must never scan logs on the GET path")

    monkeypatch.setattr(maintenance, "_activity_scan", _no_scan)
    registry_before = (data / "state" / "projects.json").read_bytes()
    assert _summary_row(data, "telem")["has_thread_activity"] is False
    assert (data / "state" / "projects.json").read_bytes() == registry_before, (
        "GET path must never write the registry"
    )
    assert _backfill(data) is None, "a project created later is never a candidate: the job rests"


def test_derivation_paths_owner_ui_binding_and_revision(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _backfill(data)  # backfill done; everything below is pure derivation

    pr.create_project(data, "fresh", name="Fresh", origin="owner_ui")
    assert _summary_row(data, "fresh")["has_thread_activity"] is True

    bound = pr.create_project(data, "bound", origin="reconcile")
    assert _summary_row(data, "bound")["has_thread_activity"] is False
    pr.bind_task_to_project(data, "t1", "bound", bound["chat_id"], origin={"absent": "system"})
    assert _summary_row(data, "bound")["has_thread_activity"] is True

    pr.create_project(data, "revised", origin="reconcile")
    assert _summary_row(data, "revised")["has_thread_activity"] is False
    pr.increment_project_visible_revision(data, project_id="revised")
    assert _summary_row(data, "revised")["has_thread_activity"] is True
    assert _summary_row(data, "revised")["visible_revision"] == 1


def test_backfill_runs_once_per_installation_not_per_process(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    pr.create_project(data, "empty", origin="reconcile")
    assert _backfill(data)["state"] == "done"

    late = pr.create_project(data, "late", origin="reconcile")
    _write_rows(data / "logs" / "chat.jsonl", int(late["chat_id"]))
    scans = []
    monkeypatch.setattr(maintenance, "_activity_scan", lambda path, wanted: scans.append(path) or set())
    # The periodic reconcile tick, a later look of the job and a fresh process (the job row is
    # durable; no module state remembers anything): none rescans the chain.
    pr.reconcile_projects(data)
    assert "activity_backfill" not in maintenance.due(data)
    assert _backfill(data) is None
    assert scans == []
    assert "thread_activity_seen" not in pr.get_reserved_project(data, "late")
    assert _summary_row(data, "late")["has_thread_activity"] is False
    assert maintenance_jobs.job(data, "activity_backfill")["empty"] == ["empty"]


def test_failed_backfill_waits_for_its_inputs_then_retries(tmp_path, monkeypatch):
    """A failed run records the inputs it waits on (the registry, the archive); it is retried
    when they change, never after a count of failures or a timer."""
    data = tmp_path / "data"
    data.mkdir()
    legacy = pr.create_project(data, "legacy", origin="reconcile")
    _write_rows(data / "logs" / "chat.jsonl", int(legacy["chat_id"]))

    def _boom(_root, **_kwargs):
        raise OSError("transient bindings read failure")

    monkeypatch.setattr(pr, "_load_bindings", _boom)
    with pytest.raises(OSError):
        _backfill(data)
    row = maintenance_jobs.job(data, "activity_backfill")
    assert row["state"] == "failed" and row["error"] == "OSError" and row["waiting_on"]["registry"]
    assert "thread_activity_seen" not in pr.get_reserved_project(data, "legacy")
    monkeypatch.undo()
    assert "activity_backfill" not in maintenance.due(data), "unchanged inputs: not due"
    pr.create_project(data, "other", origin="owner_ui")  # the registry changed
    assert "activity_backfill" in maintenance.due(data)
    assert _backfill(data)["state"] == "done"
    assert pr.get_reserved_project(data, "legacy")["thread_activity_seen"] is True


def test_backfill_skips_projects_already_active_by_derivation(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    owner = pr.create_project(data, "mine", name="Mine", origin="owner_ui")
    revised = pr.create_project(data, "rev", origin="reconcile")
    pr.increment_project_visible_revision(data, project_id="rev")
    _write_rows(data / "logs" / "chat.jsonl", int(owner["chat_id"]))
    _write_rows(data / "logs" / "chat.jsonl", int(revised["chat_id"]))

    def _no_scan(*_args, **_kwargs):
        raise AssertionError("backfill must not scan when every candidate derives active")

    monkeypatch.setattr(maintenance, "_activity_scan", _no_scan)
    assert _backfill(data)["state"] == "done"
    assert "thread_activity_seen" not in pr.get_reserved_project(data, "mine")
    assert "thread_activity_seen" not in pr.get_reserved_project(data, "rev")
    assert _summary_row(data, "mine")["has_thread_activity"] is True
    assert _summary_row(data, "rev")["has_thread_activity"] is True


def test_project_chat_id_helper_matches_registry_rows(tmp_path):
    # Guard the assumption the backfill scan rests on: registry rows carry the
    # deterministic chat id the log rows were written with.
    data = tmp_path / "data"
    data.mkdir()
    entry = pr.create_project(data, "det", origin="reconcile")
    assert int(entry["chat_id"]) == project_chat_id("det")
