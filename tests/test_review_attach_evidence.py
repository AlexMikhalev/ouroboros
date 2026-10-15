"""Cold collection retains a terminal reviewer's real checking-copy evidence."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
from pathlib import Path

import pytest

from ouroboros import delegate_custody as custody, review_checking as checking
from ouroboros.loop_acceptance_review import acceptance_run_pending
from ouroboros.review_dispatch import collect_task_acceptance_run, review_operation_binding
from ouroboros.review_records import ReviewRequest, ReviewSlot
from tests.test_review_checking_scope import ABSENT, NO_CENSUS, SETTLED
from tests.test_review_operation_collection import (
    PASS, TASK, _ctx, _observe, _session_run, _started, forbid_effects,  # noqa: F401
)

OPERATION = "op-session"
INVOCATION = f"inv-{OPERATION}"
RUN = "run-paid"
COVERAGE = b'<?xml version="1.0"?><coverage lines-covered="3"/>\n'
REVIEWER_EDIT = b"# reviewer reproduction\nassert 2 + 2 == 4\n"
FAIL = json.dumps({"verdict": "FAIL", "summary": "Independent review rejects the answer", "findings": [{
    "severity": "critical", "item": "intent", "evidence": "check.py", "recommendation": "repair"}]})


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


def _terminal(state="succeeded", *, scope=ABSENT, text=PASS):
    summary = {"state": state, **({} if scope is ABSENT else {"processScope": scope})}
    return {"summary": summary, "primaryOutput": {"text": text, "truncated": False}}


def _collect(root, run):
    return collect_task_acceptance_run(run, drive_root=root, usage_ctx=_ctx(root))


def _assert_receipt(actor, area, *, released=True, scope=None):
    receipt = actor["usage"]["checking_evidence"]
    assert receipt["status"] == "retained" and receipt["gaps"] == []
    if scope is None:
        assert "release_basis" not in receipt and "process_scope" not in receipt
    else:  # the weaker permission names itself beside the engine's facts, settled still false
        assert receipt["release_basis"] == checking.RELEASE_WITHOUT_CENSUS
        assert receipt["process_scope"] == scope and receipt["process_scope"]["settled"] is False
    if released:
        assert not area.exists(), "normal succeeded summary permits release after complete capture"
        assert receipt["refresh_safe"] is True
        assert "retention_reason" not in receipt
    else:
        assert area.exists(), "a terminal without the release boundary keeps its copy"
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


@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
@pytest.mark.parametrize("scope", [ABSENT, None, SETTLED], ids=["absent", "null", "settled"])
def test_unknown_process_scope_keeps_the_copy_and_collects_the_paid_verdict(
        checking_run, monkeypatch, forbid_effects, scope, verdict):  # noqa: F811
    """``null`` (the engine holds no readable scope record, e.g. a historical run first
    observed through an upgraded engine) keeps the copy; an absent key (an older engine)
    and a settled scope release it. The evidence and the paid verdict arrive either way."""
    root, subject, area, run = checking_run
    calls = _observe(monkeypatch, _terminal(scope=scope, text={"PASS": PASS, "FAIL": FAIL}[verdict]))
    _started(root, run_id=RUN)
    result = _collect(root, run)
    actor = result.actors[0]
    _assert_receipt(actor, area, released=scope is not None)
    assert actor["parsed"]["verdict"] == verdict
    assert result.collection["facts"] == {"s": "collected"} and not acceptance_run_pending(result)
    assert calls == [("handshake",), ("get_run", RUN), ("close",)], "observed once, nothing bought again"
    assert (subject / "check.py").read_bytes() == b"# original candidate\n"


@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
def test_a_census_less_success_releases_under_the_weaker_permission_and_collects_the_paid_verdict(
        checking_run, monkeypatch, forbid_effects, verdict):  # noqa: F811
    """Windows (no census): the owner's compatibility choice releases the copy after its
    complete capture; the receipt discloses the basis and the engine's facts, and the
    verdict arrives unchanged from one observation."""
    root, subject, area, run = checking_run
    calls = _observe(monkeypatch, _terminal(scope=dict(NO_CENSUS), text={"PASS": PASS, "FAIL": FAIL}[verdict]))
    _started(root, run_id=RUN)
    result = _collect(root, run)
    actor = result.actors[0]
    _assert_receipt(actor, area, released=True, scope=NO_CENSUS)
    assert actor["parsed"]["verdict"] == verdict
    assert result.collection["facts"] == {"s": "collected"} and not acceptance_run_pending(result)
    assert calls == [("handshake",), ("get_run", RUN), ("close",)], "observed once, nothing bought again"
    assert (subject / "check.py").read_bytes() == b"# original candidate\n"


@pytest.mark.parametrize("state,scope", [
    ("failed", NO_CENSUS), ("cancelled", NO_CENSUS), ("succeeded", {**NO_CENSUS, "survivors": 1}),
    ("succeeded", {**NO_CENSUS, "unreadable": 1}), ("succeeded", {**NO_CENSUS, "census": "failed"}),
    ("succeeded", None),
], ids=["failed", "cancelled", "known-survivor", "unreadable-member", "mixed-census-failed", "null"])
def test_a_census_less_permission_never_covers_failure_cancellation_survivors_or_unknowns(
        checking_run, monkeypatch, forbid_effects, state, scope):  # noqa: F811
    root, _subject, area, run = checking_run
    calls = _observe(monkeypatch, _terminal(state, scope=scope))
    _started(root, run_id=RUN)
    result = _collect(root, run)
    actor = result.actors[0]
    _assert_receipt(actor, area, released=False)
    if state == "succeeded":
        assert actor["parsed"]["verdict"] == "PASS", "the paid verdict is delivered unchanged"
    else:
        assert actor["status"] == "error" and state in actor["error"]
    assert calls == [("handshake",), ("get_run", RUN), ("close",)]


def test_a_receipt_captured_before_an_engine_upgrade_is_reused_as_stored(
        checking_run, monkeypatch, forbid_effects):  # noqa: F811
    root, _subject, area, run = checking_run
    _observe(monkeypatch, _terminal())  # an older engine: released after complete capture
    _started(root, run_id=RUN)
    receipt = _assert_receipt(_collect(root, run).actors[0], area)
    summary = Path(receipt["evidence_dir"], "summary.json")
    summary_bytes, summary_mtime = summary.read_bytes(), summary.stat().st_mtime_ns

    def forbidden(*_args, **_kwargs):
        pytest.fail("a stored receipt was recaptured or its copy rematerialized")

    monkeypatch.setattr(checking, "_directory_delta", forbidden)
    monkeypatch.setattr(checking, "prepare_seat_area", forbidden)
    calls = _observe(monkeypatch, _terminal(scope=None))  # the upgraded engine reads the same run as null
    again = _collect(root, run).actors[0]
    assert _assert_receipt(again, area) == receipt and again["parsed"]["verdict"] == "PASS"
    assert summary.read_bytes() == summary_bytes and summary.stat().st_mtime_ns == summary_mtime
    assert calls == [("handshake",), ("get_run", RUN), ("close",)]


@pytest.mark.parametrize("gap", ["special_entry", "capture_error"])
def test_attach_collection_exposes_capture_gap_and_keeps_recoverable_area(
        checking_run, monkeypatch, forbid_effects, gap):  # noqa: F811
    root, _subject, area, run = checking_run
    _observe(monkeypatch, _terminal())
    _started(root, run_id=RUN)
    if gap == "special_entry":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO output requires POSIX")
        os.mkfifo(area / "pipe")  # a reviewer output no capture can retain
    else:
        def unavailable(*_args, **_kwargs):
            raise OSError("fixture evidence read failed")
        monkeypatch.setattr(checking, "_directory_delta", unavailable)
    result = _collect(root, run)
    actor = result.actors[0]
    receipt = actor["usage"]["checking_evidence"]
    assert receipt["status"] == ("partial" if gap == "special_entry" else "capture_failed")
    assert receipt["area"] == str(area) and area.is_dir()
    assert (area / "check.py").read_bytes() == REVIEWER_EDIT
    assert (area / checking.EVIDENCE_DIRNAME / "coverage.xml").read_bytes() == COVERAGE
    assert actor["parsed"]["verdict"] == "PASS", "evidence capture does not erase the paid answer"
    assert actor["recovery_binding"] == run["actors"][0]["recovery_binding"]
    if gap == "special_entry":
        assert receipt["gaps"] == [{"path": "pipe", "reason": "unsupported_entry_type"}]
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

    def failed_capture(data, task, operation, *, release, process_scope=None):
        captures.append((data, task, operation, release, process_scope))
        raise OSError("capture unavailable")

    monkeypatch.setattr(checking, "capture_seat_evidence", failed_capture)
    result = _collect(root, run)
    actor = result.actors[0]
    assert captures == [(root, TASK, OPERATION, True, None)]
    assert actor["parsed"]["verdict"] == "PASS"
    assert result.collection["facts"] == {"s": "collected"}
    assert actor["usage"]["checking_evidence"]["status"] == "capture_failed"
    assert "capture unavailable" in actor["usage"]["checking_evidence"]["error"]
    assert area.exists() and (area / checking.EVIDENCE_DIRNAME / "coverage.xml").read_bytes() == COVERAGE
