"""Cold collection retains a terminal reviewer's real checking-copy evidence."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from ouroboros import delegate_custody as custody, review_checking as checking
from ouroboros.loop_acceptance_review import acceptance_run_pending
from ouroboros.review_dispatch import collect_task_acceptance_run, review_operation_binding
from ouroboros.review_records import ReviewRequest, ReviewSlot
from tests.test_review_operation_collection import (
    PASS, TASK, _ctx, _observe, _session_run, _started, forbid_effects,  # noqa: F401
)

OPERATION = "op-session"
INVOCATION = f"inv-{OPERATION}"
RUN = "run-paid"
COVERAGE = b'<?xml version="1.0"?><coverage lines-covered="3"/>\n'
REVIEWER_EDIT = b"# reviewer reproduction\nassert 2 + 2 == 4\n"


@pytest.fixture
def checking_run(tmp_path):
    """A dispatched full-access seat, with its real retained subject and copy."""
    root, subject = tmp_path / "data", tmp_path / "subject"
    subject.mkdir()
    (subject / "check.py").write_bytes(b"# original candidate\n")
    source = checking.freeze_checking_source(subject, data_root=root)
    area = Path(checking.prepare_seat_area(root, TASK, OPERATION, source))
    (area / "check.py").write_bytes(REVIEWER_EDIT)
    (area / checking.EVIDENCE_DIRNAME).mkdir()
    (area / checking.EVIDENCE_DIRNAME / "coverage.xml").write_bytes(COVERAGE)
    run = _session_run()
    run["request"]["checking_source"] = source
    run["slot_roster"][0]["session_access"] = "full"
    binding = review_operation_binding(
        ReviewRequest(**run["request"]), ReviewSlot(**run["slot_roster"][0]), OPERATION)
    run["actors"][0].update(
        pending_invocation_id=INVOCATION,
        recovery_binding={**binding, "pending_invocation_id": INVOCATION})
    return root, subject, area, run


def _terminal(state="succeeded"):
    return {"summary": {"state": state}, "primaryOutput": {"text": PASS, "truncated": False}}


def _collect(root, run):
    return collect_task_acceptance_run(run, drive_root=root, usage_ctx=_ctx(root))


def _assert_receipt(actor, area, *, released=True):
    receipt = actor["usage"]["checking_evidence"]
    assert receipt["status"] == "retained" and receipt["gaps"] == []
    if released:
        assert not area.exists(), "normal succeeded summary permits release after complete capture"
        assert receipt["refresh_safe"] is True
        assert "retention_reason" not in receipt
    else:
        assert area.exists(), "failed or interrupted terminal lacks the normal-success boundary"
        assert receipt["area"] == str(area)
        assert receipt["refresh_safe"] is False
        assert receipt["retention_reason"] == "writer_quiescence_unverified"
    evidence = Path(receipt["evidence_dir"])
    assert (evidence / "files" / "check.py").read_bytes() == REVIEWER_EDIT
    assert (evidence / "files" / checking.EVIDENCE_DIRNAME / "coverage.xml").read_bytes() == COVERAGE
    assert receipt["named_evidence"] == [{
        "path": f"{checking.EVIDENCE_DIRNAME}/coverage.xml", "size": len(COVERAGE),
        "sha256": hashlib.sha256(COVERAGE).hexdigest(),
    }]
    assert "check.py" in receipt["changed"]
    assert json.loads((evidence / "summary.json").read_text()) == receipt
    assert actor["operation_id"] == OPERATION
    assert actor["usage"]["invocation_id"] == INVOCATION
    assert not actor["usage"].get("pending_invocation_id"), "a terminal token must not create a new live wait"
    assert actor["recovery_binding"]["pending_invocation_id"] == INVOCATION
    assert actor["operation_state"] == "late_settled"
    return receipt


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled", "interrupted"])
def test_attach_only_terminal_collection_captures_edits_and_named_evidence(
        checking_run, monkeypatch, forbid_effects, state):  # noqa: F811
    root, subject, area, run = checking_run
    calls = _observe(monkeypatch, _terminal(state))
    _started(root, run_id=RUN)
    result = _collect(root, run)
    actor = result.actors[0]
    _assert_receipt(actor, area, released=state == "succeeded")
    assert actor["recovery_binding"] == run["actors"][0]["recovery_binding"]
    assert result.collection["facts"] == {"s": "collected"}
    assert not acceptance_run_pending(result)
    assert (subject / "check.py").read_bytes() == b"# original candidate\n"
    assert not (subject / checking.EVIDENCE_DIRNAME).exists()
    assert calls == [("handshake",), ("get_run", RUN), ("close",)]
    if state == "succeeded":
        assert actor["parsed"]["verdict"] == "PASS"
    else:
        assert actor["status"] == "error" and state in actor["error"]
        assert not actor.get("semantic_verdict")


def test_repeat_attach_collection_reuses_the_retained_receipt_without_recreating_copy(
        checking_run, monkeypatch, forbid_effects):  # noqa: F811
    root, _subject, area, run = checking_run
    calls = _observe(monkeypatch, _terminal())
    _started(root, run_id=RUN)
    first = _collect(root, run)
    receipt = _assert_receipt(first.actors[0], area)
    summary = Path(receipt["evidence_dir"], "summary.json")
    summary_bytes, summary_mtime = summary.read_bytes(), summary.stat().st_mtime_ns

    def forbidden(*_args, **_kwargs):
        pytest.fail("repeat collection recomputed or rematerialized the terminal copy")

    monkeypatch.setattr(checking, "_directory_delta", forbidden)
    monkeypatch.setattr(checking, "prepare_seat_area", forbidden)
    again = _collect(root, run)  # an old pending checkpoint observes the same paid run
    assert _assert_receipt(again.actors[0], area) == receipt
    assert summary.read_bytes() == summary_bytes and summary.stat().st_mtime_ns == summary_mtime
    assert calls == [("handshake",), ("get_run", RUN), ("close",)] * 2
    settled = _collect(root, {**dataclasses.asdict(first), "authority": "host_root"})
    assert settled.collection["facts"] == {"s": "settled"}
    assert settled.actors[0]["usage"]["checking_evidence"] == receipt
    assert calls == [("handshake",), ("get_run", RUN), ("close",)] * 2


@pytest.mark.parametrize("gap", ["budget", "capture_error"])
def test_attach_collection_exposes_capture_gap_and_keeps_recoverable_area(
        checking_run, monkeypatch, forbid_effects, gap):  # noqa: F811
    root, _subject, area, run = checking_run
    _observe(monkeypatch, _terminal())
    _started(root, run_id=RUN)
    if gap == "budget":
        monkeypatch.setattr(checking, "EVIDENCE_RETAIN_BYTES", 1)
    else:
        def unavailable(*_args, **_kwargs):
            raise OSError("fixture evidence read failed")
        monkeypatch.setattr(checking, "_directory_delta", unavailable)
    result = _collect(root, run)
    actor = result.actors[0]
    receipt = actor["usage"]["checking_evidence"]
    assert receipt["status"] == ("partial" if gap == "budget" else "capture_failed")
    assert receipt["area"] == str(area) and area.is_dir()
    assert (area / "check.py").read_bytes() == REVIEWER_EDIT
    assert (area / checking.EVIDENCE_DIRNAME / "coverage.xml").read_bytes() == COVERAGE
    assert actor["parsed"]["verdict"] == "PASS", "evidence capture does not erase the paid answer"
    assert actor["recovery_binding"] == run["actors"][0]["recovery_binding"]
    if gap == "budget":
        assert {row["reason"] for row in receipt["gaps"]} == {"evidence_budget_exceeded"}
    else:
        assert "fixture evidence read failed" in receipt["error"]


@pytest.mark.parametrize("state", ["running", "daemon_unavailable", "unbound"])
def test_unfinished_or_unavailable_invocation_never_captures_or_releases(
        checking_run, monkeypatch, forbid_effects, state):  # noqa: F811
    root, _subject, area, run = checking_run
    # An answer can precede adapter completion; only the final daemon summary
    # supplies the normal-success capture/release boundary.
    calls = _observe(monkeypatch, {"summary": {"state": "running"},
                                   "primaryOutput": {"text": PASS, "truncated": False}})
    _started(root, run_id="" if state == "unbound" else RUN)

    def forbidden(*_args, **_kwargs):
        pytest.fail("nonterminal or unavailable invocation reached evidence capture")

    monkeypatch.setattr(checking, "capture_seat_evidence", forbidden)
    if state == "daemon_unavailable":
        def unavailable():
            raise ConnectionError("fixture daemon unavailable")
        monkeypatch.setattr("ouroboros.claudexor_daemon.read_owned_gateway", unavailable)
    result = _collect(root, run)
    assert result.collection["facts"] == {"s": "unavailable" if state == "unbound" else "deferred"}
    assert result.actors[0]["late_result_pending"]
    assert result.actors[0]["recovery_binding"] == run["actors"][0]["recovery_binding"]
    assert area.exists() and (area / "check.py").read_bytes() == REVIEWER_EDIT
    assert not (area.parent / "evidence").exists()
    if state != "running":
        assert calls == []


@pytest.mark.parametrize("foreign", ["task_id", "root_task_id", "operation_id", "slot_id", "surface"])
def test_foreign_invocation_binding_cannot_collect_or_release_checking_copy(
        checking_run, monkeypatch, forbid_effects, foreign):  # noqa: F811
    root, _subject, area, run = checking_run
    calls = _observe(monkeypatch, _terminal())
    requested = {
        "invocation_id": INVOCATION, "task_id": TASK, "root_task_id": TASK,
        "operation_id": OPERATION, "slot_id": "s", "surface": "task_acceptance",
        "request": {"prompt": "review", "harnesses": ["codex"]},
    }
    requested[foreign] = "foreign-value"
    custody.emit(root, custody.START_REQUESTED, requested)
    custody.emit(root, custody.STARTED, {"run_id": RUN, "invocation_id": INVOCATION, "task_id": TASK})

    def forbidden(*_args, **_kwargs):
        pytest.fail("foreign invocation reached evidence capture")

    monkeypatch.setattr(checking, "capture_seat_evidence", forbidden)
    before = copy.deepcopy(run["actors"][0]["recovery_binding"])
    result = _collect(root, run)
    assert result.collection["facts"] == {"s": "unavailable"}
    assert result.actors[0]["operation_state"] == "custody_lost"
    assert result.actors[0]["late_result_pending"]
    assert result.actors[0]["recovery_binding"] == before
    assert calls == [] and area.exists()
    assert not (area.parent / "evidence").exists()


def test_capture_exception_does_not_hide_the_paid_verdict_or_release_the_copy(
        checking_run, monkeypatch, forbid_effects):  # noqa: F811
    root, _subject, area, run = checking_run
    _observe(monkeypatch, _terminal())
    _started(root, run_id=RUN)
    captures = []

    def failed_capture(data, task, operation, *, release):
        captures.append((data, task, operation, release))
        raise OSError("capture unavailable")

    monkeypatch.setattr(checking, "capture_seat_evidence", failed_capture)
    result = _collect(root, run)
    actor = result.actors[0]
    assert captures == [(root, TASK, OPERATION, True)]
    assert actor["parsed"]["verdict"] == "PASS"
    assert result.collection["facts"] == {"s": "collected"}
    assert actor["usage"]["checking_evidence"]["status"] == "capture_failed"
    assert "capture unavailable" in actor["usage"]["checking_evidence"]["error"]
    assert area.exists() and (area / checking.EVIDENCE_DIRNAME / "coverage.xml").read_bytes() == COVERAGE
