"""Evidence completion failures and retries through real checking consumers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from ouroboros import review_checking as rc
from ouroboros.gateways.claudexor import ClaudexorUnavailable
from tests.test_review_checking import author  # noqa: F401
from tests.test_review_thread_checking import (  # noqa: F401
    ThreadGateway,
    _run,
    fake_route,
    thread_gateway,
)


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
