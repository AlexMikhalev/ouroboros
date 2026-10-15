"""Evidence completion failures and retries through real checking consumers."""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from ouroboros import review_checking as rc
from ouroboros.gateways.claudexor import ClaudexorUnavailable
from ouroboros.review_execution import (
    ReviewRouteUnavailable,
    SessionInvocation,
    review_session_route,
    run_delegated_review_session,
)
from tests._review_session_route_shared import (  # noqa: F401 - autouse owned-gateway transport
    FakeGateway,
    _owned_gateway_uses_each_test_transport,
)
from tests.test_review_checking import author  # noqa: F401
from tests.test_review_session_rights import writable_engine  # noqa: F401
from tests.test_review_thread_checking import (  # noqa: F401
    ThreadGateway,
    _run,
    fake_route,
    thread_gateway,
)

_LOCKABLE = pytest.mark.skipif(os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
                               reason="needs POSIX permission bits that bind the test user")


@pytest.mark.parametrize("detail", [
    {},
    {"state": "succeeded"},
    {"summary": None, "runFacts": {"state": "succeeded"}},
    {"summary": {"state": "running"}, "result": {"final": True}},
    {"summary": {"state": "interrupted", "failure": {"cause": "host_restart"}}},
])
def test_early_or_unobserved_details_retain_original_named_outputs(tmp_path, detail):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "subject.txt").write_bytes(b"frozen subject\n")
    drive = tmp_path / "drive"
    source = rc.freeze_checking_source(root, data_root=drive)
    area = Path(rc.prepare_seat_area(drive, "original-task", "original-op", source))
    coverage = area / rc.EVIDENCE_DIRNAME / "coverage.xml"
    coverage.parent.mkdir()
    coverage.write_bytes(b"<coverage>still observable</coverage>\r\n")

    evidence = rc.capture_seat_evidence(
        drive, "original-task", "original-op", release=rc.checking_writers_settled(detail))

    assert evidence["status"] == "retained"
    assert not evidence["refresh_safe"] and area.is_dir()
    retained = Path(evidence["evidence_dir"]) / "files" / rc.EVIDENCE_DIRNAME / "coverage.xml"
    assert retained.read_bytes() == coverage.read_bytes()
    assert (root / "subject.txt").read_bytes() == b"frozen subject\n"


@pytest.mark.parametrize("state", ["succeeded", "failed"])
def test_summary_write_failure_preserves_copy_and_original_outcome(
    author, tmp_path, thread_gateway, monkeypatch, state,  # noqa: F811
):
    drive = tmp_path / "drive"
    source = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    thread_gateway.terminal_state = state
    original_write = rc.atomic_write_json

    def fail_summary(path, value, *args, **kwargs):
        if Path(path).name == "summary.json":
            raise OSError("evidence summary volume unavailable")
        return original_write(path, value, *args, **kwargs)

    monkeypatch.setattr(rc, "atomic_write_json", fail_summary)
    if state == "succeeded":
        result = _run(author, drive, source)
        assert result["text"] == '{"findings": []}'
        assert result["run_id"] == "run-1"
        evidence = result["checking_evidence"]
    else:
        with pytest.raises(ClaudexorUnavailable) as caught:
            _run(author, drive, source)
        assert caught.value.code == "reviewer_probe_failed"
        assert caught.value.delegated_run_id == "run-1"
        evidence = caught.value.checking_evidence
    assert evidence["status"] == "capture_failed"
    assert "evidence summary volume unavailable" in evidence["error"]
    area = rc.seat_dir(drive, "task-1", "thread:plan_review:t1") / "area"
    assert (area / rc.EVIDENCE_DIRNAME / "coverage.xml").read_bytes() == ThreadGateway.coverage(1)
    assert (area / "reviewer-test.txt").read_bytes() == b"reviewer turn 1\n"
    assert not (rc.seat_dir(drive, "task-1", "op-1") / "evidence" / "summary.json").exists()
    assert len(thread_gateway.turn_requests) == 1


@pytest.mark.parametrize("source_kind", ["directory", "git"])
@pytest.mark.parametrize("replacement", ["removed", "file_to_directory", "directory_to_file"])
def test_final_capture_inventory_matches_summary_after_unsettled_retry(
    author, tmp_path, source_kind, replacement,  # noqa: F811
):
    drive = tmp_path / "drive"
    if source_kind == "directory":
        source_root = tmp_path / "plain"
        source_root.mkdir()
        (source_root / "source.txt").write_bytes(b"immutable subject\n")
        source = rc.freeze_checking_source(source_root, data_root=drive)
    else:
        source = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    area = Path(rc.prepare_seat_area(drive, "task", "op", source))
    declared = area / rc.EVIDENCE_DIRNAME
    declared.mkdir()
    output = declared / "result"
    if replacement == "directory_to_file":
        output.mkdir()
        (output / "obsolete.log").write_bytes(b"earlier bytes\n")
    else:
        output.write_bytes(b"earlier bytes\n")
    (declared / "coverage.xml").write_bytes(b"<coverage>earlier</coverage>\r\n")
    first = rc.capture_seat_evidence(drive, "task", "op", release=False)
    assert first["status"] == "retained" and not first["refresh_safe"]
    assert area.exists()

    if output.is_dir():
        shutil.rmtree(output)
    else:
        output.unlink()
    if replacement == "file_to_directory":
        output.mkdir()
        (output / "final.log").write_bytes(b"settled bytes\r\n\x00")
    elif replacement == "directory_to_file":
        output.write_bytes(b"settled bytes\r\n\x00")
    (declared / "coverage.xml").write_bytes(b"<coverage>settled</coverage>\r\n")
    expected = {
        path.relative_to(area).as_posix(): path.read_bytes()
        for path in declared.rglob("*") if path.is_file()
    }

    # The synchronous test writer has stopped. Its final retry must describe
    # exactly the files retained on disk, including prior output removals.
    final = rc.capture_seat_evidence(drive, "task", "op", release=True)
    assert final["status"] == "retained", final
    assert final["refresh_safe"] and not area.exists()
    evidence = Path(final["evidence_dir"])
    assert json.loads((evidence / "summary.json").read_text()) == final
    actual = {
        path.relative_to(evidence / "files").as_posix(): path.read_bytes()
        for path in (evidence / "files").rglob("*") if path.is_file()
    }
    assert actual == expected
    rows = {row["path"]: row for row in final["named_evidence"]}
    assert set(rows) == set(actual)
    for path, payload in actual.items():
        assert rows[path]["size"] == len(payload)
        assert rows[path]["sha256"] == hashlib.sha256(payload).hexdigest()


def test_missing_thread_copy_cannot_skip_uncaptured_previous_turn(author, tmp_path):  # noqa: F811
    drive = tmp_path / "drive"
    source = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    area = Path(rc.thread_seat_area(drive, "task", "seat", source, operation_id="op-1"))
    (area / "uncollected.log").write_bytes(b"must survive recovery\n")
    saved = area.with_name("saved-op-1")
    area.rename(saved)
    marker = (area.parent / "checking.json").read_bytes()

    with pytest.raises(rc.CheckingAreaUnavailable, match="capture|quiescent"):
        rc.thread_seat_area(drive, "task", "seat", source, operation_id="op-2")

    assert not area.exists()
    assert (area.parent / "checking.json").read_bytes() == marker
    assert (saved / "uncollected.log").read_bytes() == b"must survive recovery\n"
    assert not (rc.seat_dir(drive, "task", "op-2") / "checking.json").exists()


def test_missing_thread_copy_can_restore_same_turn_frozen_source(author, tmp_path):  # noqa: F811
    drive = tmp_path / "drive"
    source = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    area = Path(rc.thread_seat_area(drive, "task", "seat", source, operation_id="op-1"))
    rc.bind_checking_thread(drive, "task", "seat", "sticky-thread")
    area.rename(area.with_name("saved-op-1"))

    restored = rc.thread_seat_area(drive, "task", "seat", source, operation_id="op-1")

    assert restored == str(area)
    assert (area / "f.txt").read_bytes() == b"A staged\n"
    assert json.loads((area.parent / "checking.json").read_text())["thread_id"] == "sticky-thread"


def test_capture_retry_keeps_old_receipt_bytes_and_rebind_keeps_latest_capture(author, tmp_path):  # noqa: F811
    drive = tmp_path / "drive"
    source = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    area = Path(rc.thread_seat_area(drive, "task", "seat", source, operation_id="op-1"))
    rc.bind_checking_thread(drive, "task", "seat", "sticky-thread")
    named = area / rc.EVIDENCE_DIRNAME / "coverage.xml"
    named.parent.mkdir()
    named.write_bytes(b"<coverage>earlier</coverage>\r\n")
    first = rc.capture_seat_evidence(drive, "task", "op-1")
    named.write_bytes(b"<coverage>settled</coverage>\r\n")
    final = rc.capture_seat_evidence(drive, "task", "op-1", release=True)
    assert first["evidence_dir"] != final["evidence_dir"]
    assert (Path(first["evidence_dir"]) / "files" / rc.EVIDENCE_DIRNAME / "coverage.xml").read_bytes() == b"<coverage>earlier</coverage>\r\n"
    rc.bind_checking_thread(drive, "task", "seat", "sticky-thread")
    assert rc.capture_seat_evidence(drive, "task", "op-1") == final
    area.rename(area.with_name("saved-op-1"))
    rc.thread_seat_area(drive, "task", "seat", source, operation_id="op-1")
    assert rc.capture_seat_evidence(drive, "task", "op-1") == final


def run_one_shot(author, drive, source, monkeypatch, edit):  # noqa: F811
    """The real one-shot session consumer; the engine transport edits the bound copy."""
    original_start = FakeGateway.start_run

    def start(self, request, **kwargs):
        edit(Path(request["execution"]["workspaceRoot"]))
        return original_start(self, request, **kwargs)

    monkeypatch.setattr(FakeGateway, "start_run", start)
    return run_delegated_review_session(
        prompt="review the frozen subject", root=str(author), custody_drive=drive,
        invocation=SessionInvocation(
            task_id="task-1", surface="multi_model_review", slot_id="t1", timeout_sec=60.0,
            operation_id="op-1", session_route=review_session_route(), retry_state={},
            access="full", checking_source=source))


def _unlock(path):
    if path.exists():
        path.chmod(0o755)


def _incomplete(receipt, path):
    """Explicitly incomplete and naming ``path``: never a complete receipt beside skipped bytes."""
    assert receipt["status"] in {"partial", "capture_failed"}, f"{path} was skipped behind a complete capture: {receipt}"
    assert path in json.dumps([receipt.get("gaps"), receipt.get("error")]), receipt
    assert not receipt["refresh_safe"]


@_LOCKABLE
def test_unlistable_named_output_keeps_one_shot_copy_without_complete_receipt(
    author, tmp_path, writable_engine, monkeypatch,  # noqa: F811
):
    drive = tmp_path / "drive"
    source = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    area = rc.seat_dir(drive, "task-1", "op-1") / "area"
    deep = area / rc.EVIDENCE_DIRNAME / "deep"

    def edit(copy):
        deep.mkdir(parents=True)
        (copy / rc.EVIDENCE_DIRNAME / "report.log").write_bytes(b"listed output\n")
        (deep / "result.log").write_bytes(b"output inside a folder the host cannot list\n")
        deep.chmod(0)

    try:
        receipt = run_one_shot(author, drive, source, monkeypatch, edit)["checking_evidence"]
        _incomplete(receipt, f"{rc.EVIDENCE_DIRNAME}/deep")
        assert (area / rc.EVIDENCE_DIRNAME / "report.log").read_bytes() == b"listed output\n"
        assert (area / "f.txt").is_file(), "an incomplete capture must not partially release the copy"
    finally:
        _unlock(deep)
    assert (deep / "result.log").read_bytes() == b"output inside a folder the host cannot list\n"


def test_transient_named_output_listing_failure_never_loses_bytes(
    author, tmp_path, writable_engine, monkeypatch,  # noqa: F811
):
    drive = tmp_path / "drive"
    source = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    area = rc.seat_dir(drive, "task-1", "op-1") / "area"
    deep = area / rc.EVIDENCE_DIRNAME / "deep"
    real_scandir, failed = os.scandir, []

    def flaky(path="."):
        # One listing of this folder fails as an exhausted descriptor table or a
        # flaky volume would; deletion and every other reader still list it.
        if not failed and isinstance(path, (str, os.PathLike)) and os.fspath(path) == str(deep):
            failed.append(os.fspath(path))
            raise OSError(errno.EIO, "fixture transient listing failure", os.fspath(path))
        return real_scandir(path)

    def edit(copy):
        deep.mkdir(parents=True)
        (deep / "result.log").write_bytes(b"only copy of this output\n")
        monkeypatch.setattr(os, "scandir", flaky)

    receipt = run_one_shot(author, drive, source, monkeypatch, edit)["checking_evidence"]
    assert failed == [str(deep)]
    saved = Path(receipt["evidence_dir"]) / "files" / rc.EVIDENCE_DIRNAME / "deep" / "result.log"
    survivors = [path.read_bytes() for path in (saved, deep / "result.log") if path.is_file()]
    assert survivors == [b"only copy of this output\n"], f"named output vanished behind {receipt}"
    _incomplete(receipt, f"{rc.EVIDENCE_DIRNAME}/deep")


@_LOCKABLE
def test_unreadable_named_output_is_disclosed_and_keeps_the_copy(
    author, tmp_path, writable_engine, monkeypatch,  # noqa: F811
):
    drive = tmp_path / "drive"
    source = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    area = rc.seat_dir(drive, "task-1", "op-1") / "area"
    declared = area / rc.EVIDENCE_DIRNAME

    def edit(copy):
        declared.mkdir()
        for name in ("a.log", "b.log", "c.log"):
            (declared / name).write_bytes(f"{name}\n".encode())
        (declared / "b.log").chmod(0)

    try:
        receipt = run_one_shot(author, drive, source, monkeypatch, edit)["checking_evidence"]
        _incomplete(receipt, f"{rc.EVIDENCE_DIRNAME}/b.log")
        assert sorted(path.name for path in declared.iterdir()) == ["a.log", "b.log", "c.log"]
    finally:
        if (declared / "b.log").exists():
            (declared / "b.log").chmod(0o644)
    assert (declared / "b.log").read_bytes() == b"b.log\n"


@_LOCKABLE
def test_unlistable_reviewer_folder_keeps_git_copy_without_complete_receipt(
    author, tmp_path, writable_engine, monkeypatch,  # noqa: F811
):
    drive = tmp_path / "drive"
    source = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    area = rc.seat_dir(drive, "task-1", "op-1") / "area"
    locked = area / "scratch" / "locked"

    def edit(copy):
        # Git reports an unlistable folder only as a localized stderr warning and exits 0.
        locked.mkdir(parents=True)
        (copy / "scratch" / "listed.txt").write_bytes(b"listed\n")
        (locked / "out.txt").write_bytes(b"inside an unlistable folder\n")
        locked.chmod(0)

    try:
        receipt = run_one_shot(author, drive, source, monkeypatch, edit)["checking_evidence"]
        _incomplete(receipt, "scratch/locked")
        assert (area / "scratch" / "listed.txt").is_file()
    finally:
        _unlock(locked)
    assert (locked / "out.txt").read_bytes() == b"inside an unlistable folder\n"


@_LOCKABLE
def test_unlistable_named_output_blocks_thread_refresh_before_touching_the_copy(
    author, tmp_path, thread_gateway,  # noqa: F811
):
    drive = tmp_path / "drive"
    source = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    area = rc.seat_dir(drive, "task-1", "thread:plan_review:t1") / "area"
    deep = area / rc.EVIDENCE_DIRNAME / "deep"

    def lock_output(copy):
        deep.mkdir(parents=True)
        (deep / "result.log").write_bytes(b"turn-one output in an unlistable folder\n")
        deep.chmod(0)

    thread_gateway.before_turn = lock_output
    try:
        first = _run(author, drive, source)
        evidence = first["checking_evidence"]
        _incomplete(evidence, f"{rc.EVIDENCE_DIRNAME}/deep")
        with pytest.raises(ReviewRouteUnavailable) as blocked:
            _run(author, drive, source, operation="op-2", thread_id=first["thread_id"])
        assert blocked.value.code == "review_checking_copy_unavailable"
        assert len(thread_gateway.turn_requests) == 1
        assert (area / "reviewer-test.txt").read_bytes() == b"reviewer turn 1\n"
        assert (area / rc.EVIDENCE_DIRNAME / "coverage.xml").read_bytes() == ThreadGateway.coverage(1)
    finally:
        _unlock(deep)
    assert (deep / "result.log").read_bytes() == b"turn-one output in an unlistable folder\n"


@pytest.mark.skipif(os.name == "nt", reason="symlink privileges vary on Windows")
def test_partial_generation_stays_readable_after_a_later_complete_release(author, tmp_path):  # noqa: F811
    drive = tmp_path / "drive"
    source = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    area = Path(rc.prepare_seat_area(drive, "task", "op", source))
    linked = tmp_path / "linked-output"
    linked.mkdir()
    (linked / "elsewhere.log").write_bytes(b"never retained through a link\n")
    (area / rc.EVIDENCE_DIRNAME).symlink_to(linked, target_is_directory=True)
    (area / "probe_test.py").write_bytes(b"assert True\n")
    first = rc.capture_seat_evidence(drive, "task", "op", release=True)
    assert first["status"] == "partial" and area.is_dir()
    earlier = {path.name: path.read_bytes() for path in Path(first["evidence_dir"]).iterdir() if path.is_file()}
    assert set(earlier) == {"delta.patch", "summary.json"}

    (area / rc.EVIDENCE_DIRNAME).unlink()
    (area / rc.EVIDENCE_DIRNAME).mkdir()
    (area / rc.EVIDENCE_DIRNAME / "final.log").write_bytes(b"settled\n")
    final = rc.capture_seat_evidence(drive, "task", "op", release=True)
    assert final["status"] == "retained" and not area.exists()
    assert final["evidence_dir"] != first["evidence_dir"]
    assert {path.name: path.read_bytes() for path in Path(first["evidence_dir"]).iterdir()
            if path.is_file()} == earlier, "an earlier partial generation is evidence, not scratch"
    assert rc.capture_seat_evidence(drive, "task", "op") == final
