"""The maintenance child's named jobs: the rest law, durable resume, and no history walk in the server.

Each job's second run over unchanged inputs opens no history file, decodes no task body and
commits nothing; a run killed after k items resumes at k from its durable row; a fresh server
process (a new dispatcher object, no module memory) finds every job where the rows left it.
"""
from __future__ import annotations

import builtins
import io
import json
import os
import pathlib

import pytest

from ouroboros import maintenance, maintenance_jobs, store
from ouroboros import projects_registry as pr


class _Killed(BaseException):
    """A hard stop between two items: no handler of the job runs (a SIGKILL's shape)."""


def _write_rows(path, chat_id, *, count=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for index in range(count):
            handle.write(json.dumps({"chat_id": chat_id, "text": f"row {index}"}) + "\n")


def _archive(data, segments, chat_id=7):
    """SEGMENTS chat and progress segments of unrelated chats (none mentions a candidate)."""
    for index in range(segments):
        _write_rows(data / "archive" / f"chat_20260101T{index:06d}.jsonl", chat_id)
        _write_rows(data / "archive" / f"progress_20260101T{index:06d}.jsonl", chat_id)


@pytest.fixture
def opened(monkeypatch):
    """Every path opened through ``open``/``io.open``/``os.open`` while the fixture is active."""
    seen = []
    real_open, real_io, real_os = builtins.open, io.open, os.open

    def record(path):
        try:
            seen.append(pathlib.Path(os.fsdecode(path)).resolve(strict=False))
        except TypeError:
            pass  # a file descriptor

    def py_open(file, *args, **kwargs):
        record(file)
        return real_open(file, *args, **kwargs)

    def io_open(file, *args, **kwargs):
        record(file)
        return real_io(file, *args, **kwargs)

    def os_open(path, *args, **kwargs):
        record(path)
        return real_os(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", py_open)
    monkeypatch.setattr(io, "open", io_open)
    monkeypatch.setattr(os, "open", os_open)
    return seen


def _under(paths, directory):
    directory = pathlib.Path(directory).resolve()
    return [path for path in paths if path.is_relative_to(directory)]


def _decodes(monkeypatch):
    """Task-body reads (single documents and batched fetches)."""
    from ouroboros import task_store

    reads = []
    body_text, decode = store.Txn.body_text, task_store._decode

    def text(txn, task_id):
        reads.append(task_id)
        return body_text(txn, task_id)

    def batch(rows, handoffs):
        reads.extend(row["task_id"] for row in rows)
        return decode(rows, handoffs)

    monkeypatch.setattr(store.Txn, "body_text", text)
    monkeypatch.setattr(task_store, "_decode", batch)
    return reads


def _revision(data):
    with store.open_store(data, write=False) as txn:
        return txn.revision()


@pytest.fixture
def data(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    return root


def test_reconcile_projects_opens_no_archive_while_the_job_reads_it_once(data, opened):
    """``reconcile_projects`` (boot and every reconcile pass) never reads the chain for a
    never-active project; the job that does is the only reader, once per installation."""
    pr.create_project(data, "never-active", origin="reconcile")
    _archive(data, 300)
    opened.clear()
    pr.reconcile_projects(data)
    pr.reconcile_projects(data)
    assert _under(opened, data / "archive") == []
    # The guard counts what it claims to: the job does open the same 600 segments.
    assert maintenance.run_job(data, "activity_backfill")["items"] == 602  # 600 segments + 2 live files
    assert len(set(_under(opened, data / "archive"))) == 600
    assert maintenance_jobs.job(data, "activity_backfill")["empty"] == ["never-active"]


def test_activity_backfill_rests_on_its_second_run(data, opened, monkeypatch):
    pr.create_project(data, "never-active", origin="reconcile")
    _archive(data, 5)
    assert maintenance.run_job(data, "activity_backfill")["state"] == "done"
    before, decodes = _revision(data), _decodes(monkeypatch)
    opened.clear()
    assert maintenance.run_job(data, "activity_backfill") == {"state": "done", "items": 0}
    assert _under(opened, data / "archive") == [] and _under(opened, data / "logs") == []
    assert decodes == [] and _revision(data) == before
    assert "activity_backfill" not in maintenance.due(data)


def test_activity_backfill_killed_after_k_sources_resumes_at_k(data, opened, monkeypatch):
    legacy = pr.create_project(data, "legacy", origin="reconcile")
    pr.create_project(data, "never-active", origin="reconcile")
    _archive(data, 6)
    _write_rows(data / "archive" / "progress_20260101T000005.jsonl", int(legacy["chat_id"]))  # the newest
    scan, scanned = maintenance._activity_scan, []

    def killed_after_three(path, wanted):
        if len(scanned) == 3:
            raise _Killed()
        scanned.append(path)
        return scan(path, wanted)

    monkeypatch.setattr(maintenance, "_activity_scan", killed_after_three)
    with pytest.raises(_Killed):
        maintenance.run_job(data, "activity_backfill")
    row = maintenance_jobs.job(data, "activity_backfill")
    assert row["state"] == "running" and row["done"] == 3
    # A fresh server process: a new dispatcher, no module memory; the row says the job waits.
    assert "activity_backfill" in maintenance.due(data)
    monkeypatch.setattr(maintenance, "_activity_scan", lambda path, wanted: scanned.append(path) or scan(path, wanted))
    assert maintenance.run_job(data, "activity_backfill")["state"] == "done"
    assert len(scanned) == len(set(scanned)) == 14, "a source read before the kill was read again"
    row = maintenance_jobs.job(data, "activity_backfill")
    assert row["flagged"] == ["legacy"] and row["empty"] == ["never-active"] and row["total"] == 14
    assert pr.get_reserved_project(data, "legacy")["thread_activity_seen"] is True
