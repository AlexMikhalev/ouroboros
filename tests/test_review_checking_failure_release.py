"""A failed reviewer run's checking copy is released only after its engine scope settled.

Owner choice (quiz 0b6b8e44, 2026-10-14): a failed, cancelled or interrupted
run keeps its copy while the stop of its processes is unconfirmed. The exact
run detail's ``summary.processScope`` with ``settled: true`` confirms it; then a
complete current capture releases a one-shot copy or lets the thread's next turn
refresh its bound copy, as normal success does. Every other scope keeps the
copy. The real host consumers run here: the session consumer, attach-only
collection, custody, materialization and capture. Only the engine transport is
synthetic; no native harness, daemon, credentials or provider is used, so the
engine's own projection of the scope is not qualified by this module.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from ouroboros import claudexor_daemon
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
    _terminal_detail,
)
from tests.test_review_attach_evidence import (  # noqa: F401
    COVERAGE,
    REVIEWER_EDIT,
    RUN,
    _assert_receipt,
    _collect,
    checking_run,
)
from tests.test_review_checking import _author_state, _git, author  # noqa: F401
from tests.test_review_checking_scope import ABSENT
from tests.test_review_operation_collection import PASS, _observe, _started, forbid_effects  # noqa: F401
from tests.test_review_session_rights import writable_engine  # noqa: F401
from tests.test_review_thread_checking import ThreadGateway, _assert_coverage, _run, fake_route  # noqa: F401

# The engine's compact scope projection (counts, not pids). macOS reaps settle
# without a provable census, so ``completeness`` there is ``not_provable``.
SETTLED = {"settled": True, "coverage": "marker+capture", "completeness": "complete",
           "census": "ok", "survivors": 0, "unreadable": 0}
SETTLED_MACOS = {**SETTLED, "completeness": "not_provable"}
UNSETTLED = {**SETTLED, "settled": False, "survivors": 1}
# The engine's answer where no census exists (Windows): not a settlement.
NO_CENSUS = {"settled": False, "coverage": "tree_only", "completeness": "not_provable",
             "census": "unsupported", "survivors": 0, "unreadable": 0}
FAILURES = ("failed", "cancelled", "interrupted")
UNKNOWN = {
    "absent": ABSENT, "null": None, "unsettled": UNSETTLED, "no-census": NO_CENSUS,
    "settled-string": {**SETTLED, "settled": "true"}, "settled-one": {**SETTLED, "settled": 1},
    "no-settled-flag": {key: value for key, value in SETTLED.items() if key != "settled"},
    "string": "settled", "list": [SETTLED],
}


def _detail(state, scope=ABSENT, **summary):
    summary["state"] = state
    if scope is not ABSENT:
        summary["processScope"] = copy.deepcopy(scope)
    return {"summary": summary}


def _generations(home):
    """Every evidence generation's file bytes and mtimes, keyed by generation and path."""
    return {(generation.name, path.relative_to(generation).as_posix()): (path.read_bytes(), path.stat().st_mtime_ns)
            for generation in sorted(home.glob("evidence*")) for path in generation.rglob("*") if path.is_file()}


@pytest.mark.parametrize("state", FAILURES)
@pytest.mark.parametrize("scope", [SETTLED, SETTLED_MACOS, {"settled": True}, {**NO_CENSUS, "settled": True}],
                         ids=["linux", "macos", "minimal", "future-windows-settled"])
def test_settled_scope_permits_failed_release(state, scope):
    assert rc.checking_copy_release(_detail(state, scope)) is True
    restarted = _detail(state, scope, failure={"code": "host_restart"})
    assert rc.checking_copy_release(restarted) is True


@pytest.mark.parametrize("state", FAILURES)
@pytest.mark.parametrize("scope", list(UNKNOWN.values()), ids=list(UNKNOWN))
def test_unknown_scope_keeps_failed_copy(state, scope):
    assert rc.checking_copy_release(_detail(state, scope)) is False


@pytest.mark.parametrize("detail", [
    _detail("running", SETTLED), _detail("queued", SETTLED), _detail("", SETTLED), _detail(None, SETTLED),
    _detail(["failed"], SETTLED), _detail({"failed": True}, SETTLED), _detail("FAILED", SETTLED),
    {"summary": None, "processScope": SETTLED}, {"summary": ["failed", SETTLED]}, {},
    {"state": "failed", "processScope": SETTLED},
    {"summary": {"state": "running"}, "runFacts": {"state": "failed", "processScope": SETTLED}},
], ids=["running", "queued", "empty-state", "null-state", "list-state", "mapping-state", "case",
        "null-summary", "list-summary", "empty", "top-level", "run-facts"])
def test_nonterminal_or_malformed_detail_never_releases(detail):
    assert rc.checking_copy_release(detail) is False


def test_success_without_a_scope_key_still_releases():
    """The minimum shared with the success-path owner; its scope semantics are not pinned here."""
    assert rc.checking_copy_release(_detail("succeeded")) is True


# ---- attach-only collection (no POST, no preparation) ----

def _attached(state, scope, text=PASS):
    return {**_detail(state, scope), "primaryOutput": {"text": text, "truncated": False}}


@pytest.mark.parametrize("state", FAILURES)
def test_attach_collection_releases_settled_failure_and_keeps_the_error(
        checking_run, monkeypatch, forbid_effects, state):  # noqa: F811
    root, subject, area, run = checking_run
    calls = _observe(monkeypatch, _attached(state, SETTLED_MACOS))
    _started(root, run_id=RUN)
    actor = _collect(root, run).actors[0]
    _assert_receipt(actor, area, released=True)
    assert actor["status"] == "error" and state in actor["error"] and RUN in actor["error"]
    assert not actor.get("semantic_verdict") and not actor.get("parsed")
    assert actor["recovery_binding"] == run["actors"][0]["recovery_binding"]
    assert calls == [("handshake",), ("get_run", RUN), ("close",)]
    assert (subject / "check.py").read_bytes() == b"# original candidate\n"


@pytest.mark.parametrize("scope", list(UNKNOWN.values()), ids=list(UNKNOWN))
def test_attach_collection_keeps_unknown_failure(checking_run, monkeypatch, forbid_effects, scope):  # noqa: F811
    root, _subject, area, run = checking_run
    _observe(monkeypatch, _attached("failed", scope))
    _started(root, run_id=RUN)
    actor = _collect(root, run).actors[0]
    _assert_receipt(actor, area, released=False)
    assert (area / "check.py").read_bytes() == REVIEWER_EDIT
    assert actor["status"] == "error"


def test_late_bytes_after_unknown_failure_are_recaptured_before_release(
        checking_run, monkeypatch, forbid_effects):  # noqa: F811
    root, _subject, area, run = checking_run
    detail = _attached("failed", None)
    calls = _observe(monkeypatch, detail)
    _started(root, run_id=RUN)
    home = area.parent
    first = _collect(root, run).actors[0]["usage"]["checking_evidence"]
    assert first["retention_reason"] == "writer_quiescence_unverified" and area.is_dir()
    earlier = _generations(home)

    # A writer that outlived the snapshot adds bytes only the copy holds.
    late = area / rc.EVIDENCE_DIRNAME / "late.log"
    late.write_bytes(b"written after the first snapshot\x00\n")
    (area / "check.py").write_bytes(REVIEWER_EDIT + b"late edit\n")
    real_delta = rc._directory_delta

    def forbidden(*_args, **_kwargs):
        pytest.fail("a repeated unknown observation recomputed the capture")

    monkeypatch.setattr(rc, "_directory_delta", forbidden)
    again = _collect(root, run).actors[0]["usage"]["checking_evidence"]
    assert again == first and _generations(home) == earlier and late.is_file()

    detail["summary"]["processScope"] = copy.deepcopy(SETTLED_MACOS)
    monkeypatch.setattr(rc, "_directory_delta", real_delta)
    actor = _collect(root, run).actors[0]
    final = actor["usage"]["checking_evidence"]
    assert final["status"] == "retained" and final["refresh_safe"] is True
    assert final["evidence_dir"] != first["evidence_dir"] and not area.exists()
    saved = Path(final["evidence_dir"]) / "files"
    assert (saved / rc.EVIDENCE_DIRNAME / "late.log").read_bytes() == b"written after the first snapshot\x00\n"
    assert (saved / "check.py").read_bytes() == REVIEWER_EDIT + b"late edit\n"
    assert {key: value for key, value in _generations(home).items() if key in earlier} == earlier
    marker = json.loads((home / "checking.json").read_text())
    assert str(rc._capture_directory(home, marker)) == final["evidence_dir"]
    assert actor["status"] == "error" and "failed" in actor["error"]
    monkeypatch.setattr(rc, "_directory_delta", forbidden)
    assert _collect(root, run).actors[0]["usage"]["checking_evidence"] == final
    assert calls == [("handshake",), ("get_run", RUN), ("close",)] * 4


@pytest.mark.parametrize("gap", ["fifo", "evidence-file", "summary-write"])
def test_settled_failure_with_incomplete_capture_keeps_copy_and_every_generation(
        checking_run, monkeypatch, forbid_effects, gap):  # noqa: F811
    root, _subject, area, run = checking_run
    detail = _attached("failed", None)
    _observe(monkeypatch, detail)
    _started(root, run_id=RUN)
    home = area.parent
    _collect(root, run)
    earlier = _generations(home)
    declared = area / rc.EVIDENCE_DIRNAME
    original_write = rc.atomic_write_json
    if gap == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO output requires POSIX")
        os.mkfifo(declared / "pipe")
    elif gap == "evidence-file":
        (declared / "coverage.xml").unlink()
        declared.rmdir()
        declared.write_bytes(b"not a folder\n")
    else:
        def fail_summary(path, value, *args, **kwargs):
            if Path(path).name == "summary.json":
                raise OSError("evidence summary volume unavailable")
            return original_write(path, value, *args, **kwargs)

        monkeypatch.setattr(rc, "atomic_write_json", fail_summary)
    detail["summary"]["processScope"] = copy.deepcopy(SETTLED)
    receipt = _collect(root, run).actors[0]["usage"]["checking_evidence"]
    assert receipt["status"] == ("capture_failed" if gap == "summary-write" else "partial")
    assert not receipt.get("refresh_safe") and area.is_dir()
    assert (area / "check.py").read_bytes() == REVIEWER_EDIT
    assert {key: value for key, value in _generations(home).items() if key in earlier} == earlier


# ---- the one-shot session consumer ----

def _one_shot(author, drive, source, **fields):  # noqa: F811
    invocation = dict(task_id="task-1", surface="multi_model_review", slot_id="t1", timeout_sec=60.0,
                      operation_id="op-1", session_route=review_session_route(), retry_state={},
                      access="full", checking_source=source)
    invocation.update(fields)
    return run_delegated_review_session(prompt="review the frozen subject", root=str(author),
                                        custody_drive=drive, invocation=SessionInvocation(**invocation))


def _failed_detail(scope):
    detail = _terminal_detail("", state="failed")
    detail["summary"]["failure"] = {"code": "reviewer_probe_failed", "safeMessage": "The reviewer probe failed"}
    if scope is not ABSENT:
        detail["summary"]["processScope"] = copy.deepcopy(scope)
    return detail


@pytest.mark.parametrize("scope", [SETTLED_MACOS, ABSENT, None, NO_CENSUS], ids=["settled", "absent", "null", "no-census"])
def test_one_shot_failure_keeps_error_and_releases_only_when_settled(
        author, tmp_path, writable_engine, monkeypatch, scope):  # noqa: F811
    drive = tmp_path / "drive"
    source = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    before = _author_state(author)
    original_start = FakeGateway.start_run

    def start(self, request, **kwargs):
        copy_root = Path(request["execution"]["workspaceRoot"])
        (copy_root / rc.EVIDENCE_DIRNAME).mkdir()
        (copy_root / rc.EVIDENCE_DIRNAME / "coverage.xml").write_bytes(ThreadGateway.coverage(1))
        return original_start(self, request, **kwargs)

    monkeypatch.setattr(FakeGateway, "start_run", start)
    FakeGateway.detail = _failed_detail(scope)
    with pytest.raises(ClaudexorUnavailable) as caught:
        _one_shot(author, drive, source)
    assert caught.value.code == "reviewer_probe_failed" and caught.value.delegated_run_id == "run-1"
    evidence = caught.value.checking_evidence
    _assert_coverage(evidence, ThreadGateway.coverage(1))
    area = rc.seat_dir(drive, "task-1", "op-1") / "area"
    settled = scope == SETTLED_MACOS
    assert area.exists() is not settled
    assert evidence["refresh_safe"] is settled
    assert evidence.get("retention_reason") == (None if settled else "writer_quiescence_unverified")
    assert sum(len(gateway.start_requests) for gateway in FakeGateway.instances) == 1
    assert _author_state(author) == before


@pytest.mark.parametrize("scope", [SETTLED_MACOS, None], ids=["settled", "null"])
def test_one_shot_rejoin_after_release_returns_the_stored_receipt_without_a_new_copy(
        author, tmp_path, writable_engine, monkeypatch, scope):  # noqa: F811
    drive = tmp_path / "drive"
    source = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    FakeGateway.detail = _failed_detail(SETTLED_MACOS)
    checkpoint = {}
    with pytest.raises(ClaudexorUnavailable) as caught:
        _one_shot(author, drive, source,
                  pending_invocation_checkpoint=lambda token: checkpoint.update(pending_invocation_id=token))
    released = caught.value.checking_evidence
    area = rc.seat_dir(drive, "task-1", "op-1") / "area"
    assert released["refresh_safe"] and not area.exists()
    stored = _generations(area.parent)

    def forbidden(*_args, **_kwargs):
        pytest.fail("an exact rejoin prepared or recaptured a checking copy")

    for name in ("prepare_seat_area", "thread_seat_area", "_materialize_git", "_git_delta"):
        monkeypatch.setattr(rc, name, forbidden)
    FakeGateway.detail = _failed_detail(scope)
    with pytest.raises(ClaudexorUnavailable) as rejoined:
        _one_shot(author, drive, None, retry_state=dict(checkpoint), reconcile_only=True)
    assert rejoined.value.code == "reviewer_probe_failed" and rejoined.value.delegated_run_id == "run-1"
    assert rejoined.value.checking_evidence == released
    assert not area.exists() and _generations(area.parent) == stored
    assert sum(len(gateway.start_requests) for gateway in FakeGateway.instances) == 1


# ---- the sticky thread ----

class ScopedThreadGateway(ThreadGateway):
    """The thread transport whose exact run detail also projects the engine's scope."""

    def __init__(self):
        super().__init__()
        self.process_scope = ABSENT

    def get_run(self, run_id, **kwargs):
        detail = super().get_run(run_id, **kwargs)
        if self.process_scope is not ABSENT:
            detail["summary"]["processScope"] = copy.deepcopy(self.process_scope)
        else:
            assert "processScope" not in detail["summary"], "the transport must really omit the key"
        return detail


@pytest.fixture
def scoped(fake_route, monkeypatch):  # noqa: F811
    FakeGateway.catalog_entry["accessProfilesSupported"] = ["readonly", "workspace_write", "full"]
    gateway = ScopedThreadGateway()
    monkeypatch.setattr(claudexor_daemon, "ensure_owned_gateway", lambda: gateway)
    return gateway


def _fail_first_turn(author, drive, gateway, state, scope, code="reviewer_probe_failed", **fields):  # noqa: F811
    source = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    gateway.terminal_state, gateway.failure_code, gateway.process_scope = state, code, scope
    with pytest.raises(ClaudexorUnavailable) as caught:
        _run(author, drive, source, **fields)
    assert caught.value.code == code and caught.value.delegated_run_id == "run-1"
    _assert_coverage(caught.value.checking_evidence, ThreadGateway.coverage(1))
    return caught.value


def _next_subject(author, gateway, area):  # noqa: F811
    """The author stages a new subject; the next turn must see it in the same bound root."""
    (author / "f.txt").write_bytes(b"B staged\n")
    _git(author, "add", "f.txt")
    gateway.terminal_state, gateway.process_scope = "succeeded", ABSENT
    gateway.expected_subject = b"B staged\n"
    seen = []

    def refreshed(current):
        seen.append(current)
        assert current == area and not (current / "reviewer-test.txt").exists()
        assert not (current / rc.EVIDENCE_DIRNAME / "turn-1.log").exists()

    gateway.before_turn = refreshed
    return seen


@pytest.mark.parametrize("state,code", [
    ("failed", "reviewer_probe_failed"), ("cancelled", "reviewer_probe_failed"),
    ("interrupted", "reviewer_probe_failed"), ("interrupted", "host_restart"),
])
def test_settled_failed_turn_lets_next_turn_refresh_the_same_thread_copy(author, tmp_path, scoped, state, code):  # noqa: F811
    drive = tmp_path / "drive"
    error = _fail_first_turn(author, drive, scoped, state, SETTLED_MACOS, code=code)
    evidence = error.checking_evidence
    area = rc.seat_dir(drive, "task-1", "thread:plan_review:t1") / "area"
    assert evidence["refresh_safe"] is True and "retention_reason" not in evidence
    assert area.is_dir(), "a thread copy stays at its bound path between turns"
    assert len(scoped.turn_requests) == 1, "a failed turn is not retried"
    first_generation = _generations(rc.seat_dir(drive, "task-1", "op-1"))

    seen = _next_subject(author, scoped, area)
    source_b = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    second = _run(author, drive, source_b, operation="op-2", thread_id="thread-1")
    assert seen == [area]
    assert second["thread_id"] == "thread-1" and second["turn_id"] == "turn-2"
    assert second["thread_receipt"]["continuity"] == {"kind": "native_resume"}
    _assert_coverage(second["checking_evidence"], ThreadGateway.coverage(2))
    assert second["checking_evidence"]["refresh_safe"] is True
    assert len(scoped.thread_requests) == 1 and not scoped.start_requests
    create = scoped.thread_requests[0]
    assert create["workspaceRoot"] == str(area) and create["access"] == "full"
    assert [row[0] for row in scoped.turn_requests] == ["thread-1", "thread-1"]
    assert _generations(rc.seat_dir(drive, "task-1", "op-1")) == first_generation


@pytest.mark.parametrize("state", FAILURES)
@pytest.mark.parametrize("scope", [ABSENT, None, UNSETTLED, NO_CENSUS, {**SETTLED, "settled": "true"}],
                         ids=["absent", "null", "unsettled", "no-census", "settled-string"])
def test_unknown_failed_turn_keeps_thread_copy_and_refuses_next_turn(author, tmp_path, scoped, state, scope):  # noqa: F811
    drive = tmp_path / "drive"
    evidence = _fail_first_turn(author, drive, scoped, state, scope).checking_evidence
    assert evidence["refresh_safe"] is False
    assert evidence["retention_reason"] == "writer_quiescence_unverified"
    area = rc.seat_dir(drive, "task-1", "thread:plan_review:t1") / "area"
    source = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    paid = []
    with pytest.raises(ReviewRouteUnavailable) as blocked:
        _run(author, drive, source, operation="op-2", thread_id="thread-1", dispatch_stamp=lambda: paid.append(1))
    assert blocked.value.code == "review_checking_copy_unavailable"
    assert not paid and len(scoped.turn_requests) == 1
    assert (area / "reviewer-test.txt").read_bytes() == b"reviewer turn 1\n"
    assert (area / rc.EVIDENCE_DIRNAME / "coverage.xml").read_bytes() == ThreadGateway.coverage(1)


def test_timeout_cancel_with_settled_scope_keeps_timeout_and_opens_next_turn(
        author, tmp_path, scoped, monkeypatch):  # noqa: F811
    drive = tmp_path / "drive"
    source = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    scoped.process_scope = SETTLED_MACOS
    original_get, original_cancel = scoped.get_run, scoped.cancel_run

    def get_run(run_id, **kwargs):
        if not scoped.cancels:
            scoped.run_gets.append(run_id)
            return {"summary": {"state": "running"}, "lastSeq": 1}
        return original_get(run_id, **kwargs)

    def cancel_run(run_id, *, reason=""):
        receipt = original_cancel(run_id, reason=reason)
        scoped.terminal_state = "cancelled"
        return receipt

    monkeypatch.setattr(scoped, "get_run", get_run)
    monkeypatch.setattr(scoped, "cancel_run", cancel_run)
    with pytest.raises(TimeoutError, match="exceeded the slot budget") as caught:
        _run(author, drive, source, timeout_sec=0)
    assert caught.value.delegated_run_id == "run-1"
    assert caught.value.checking_terminal_detail["summary"]["processScope"] == SETTLED_MACOS
    assert scoped.cancels == [("run-1", "review_slot_timeout")]
    evidence = caught.value.checking_evidence
    _assert_coverage(evidence, ThreadGateway.coverage(1))
    assert evidence["refresh_safe"] is True
    area = rc.seat_dir(drive, "task-1", "thread:plan_review:t1") / "area"
    assert area.is_dir()

    _next_subject(author, scoped, area)
    source_b = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    second = _run(author, drive, source_b, operation="op-2", thread_id="thread-1")
    assert second["turn_id"] == "turn-2" and len(scoped.thread_requests) == 1
    assert second["checking_evidence"]["refresh_safe"] is True


def test_late_thread_bytes_are_recaptured_by_the_exact_rejoin_before_refresh(author, tmp_path, scoped):  # noqa: F811
    drive = tmp_path / "drive"
    checkpoint = {}
    first = _fail_first_turn(author, drive, scoped, "failed", None,
                             pending_invocation_checkpoint=lambda token: checkpoint.update(pending_invocation_id=token))
    unknown = first.checking_evidence
    assert unknown["retention_reason"] == "writer_quiescence_unverified"
    operation_home = rc.seat_dir(drive, "task-1", "op-1")
    earlier = _generations(operation_home)
    area = rc.seat_dir(drive, "task-1", "thread:plan_review:t1") / "area"
    (area / rc.EVIDENCE_DIRNAME / "late.log").write_bytes(b"late thread bytes\r\n")
    source = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    with pytest.raises(ReviewRouteUnavailable):
        _run(author, drive, source, operation="op-2", thread_id="thread-1")

    scoped.process_scope = SETTLED_MACOS
    with pytest.raises(ClaudexorUnavailable) as rejoined:
        _run(author, drive, None, retry_state=dict(checkpoint), reconcile_only=True)
    assert rejoined.value.code == "reviewer_probe_failed" and rejoined.value.delegated_run_id == "run-1"
    final = rejoined.value.checking_evidence
    assert final["refresh_safe"] is True and final["evidence_dir"] != unknown["evidence_dir"]
    assert (Path(final["evidence_dir"]) / "files" / rc.EVIDENCE_DIRNAME / "late.log").read_bytes() == b"late thread bytes\r\n"
    assert area.is_dir() and (area / rc.EVIDENCE_DIRNAME / "late.log").is_file()
    assert {key: value for key, value in _generations(operation_home).items() if key in earlier} == earlier
    assert len(scoped.turn_requests) == 1, "the rejoin observes the started run and posts nothing"

    seen = _next_subject(author, scoped, area)
    source_b = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    assert _run(author, drive, source_b, operation="op-2", thread_id="thread-1")["turn_id"] == "turn-2"
    assert seen == [area]


@pytest.mark.parametrize("scope", [SETTLED_MACOS, None], ids=["settled", "null"])
def test_stale_rejoin_after_next_turn_never_touches_the_newer_turn(author, tmp_path, scoped, monkeypatch, scope):  # noqa: F811
    drive = tmp_path / "drive"
    checkpoint = {}
    released = _fail_first_turn(author, drive, scoped, "failed", SETTLED_MACOS,
                                pending_invocation_checkpoint=lambda token: checkpoint.update(
                                    pending_invocation_id=token)).checking_evidence
    area = rc.seat_dir(drive, "task-1", "thread:plan_review:t1") / "area"
    _next_subject(author, scoped, area)
    source_b = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    second = _run(author, drive, source_b, operation="op-2", thread_id="thread-1")
    marker = (area.parent / "checking.json").read_bytes()
    newer = {path.relative_to(area).as_posix(): path.read_bytes() for path in area.rglob("*")
             if path.is_file() and ".git" not in path.relative_to(area).parts}
    generations = _generations(rc.seat_dir(drive, "task-1", "op-1")), _generations(rc.seat_dir(drive, "task-1", "op-2"))

    def forbidden(*_args, **_kwargs):
        pytest.fail("an exact rejoin prepared or recaptured a checking copy")

    for name in ("prepare_seat_area", "thread_seat_area", "_materialize_git", "_git_delta"):
        monkeypatch.setattr(rc, name, forbidden)
    scoped.terminal_state, scoped.process_scope = "failed", scope
    with pytest.raises(ClaudexorUnavailable) as rejoined:
        _run(author, drive, None, retry_state=dict(checkpoint), reconcile_only=True)
    assert rejoined.value.delegated_run_id == "run-1"
    assert rejoined.value.checking_evidence == released
    assert len(scoped.turn_requests) == 2 and len(scoped.thread_requests) == 1 and not scoped.start_requests
    assert (area.parent / "checking.json").read_bytes() == marker
    assert {path.relative_to(area).as_posix(): path.read_bytes() for path in area.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(area).parts} == newer
    assert newer["reviewer-test.txt"] == b"reviewer turn 2\n"
    assert (_generations(rc.seat_dir(drive, "task-1", "op-1")),
            _generations(rc.seat_dir(drive, "task-1", "op-2"))) == generations
    _assert_coverage(second["checking_evidence"], ThreadGateway.coverage(2))
    assert second["checking_evidence"]["refresh_safe"] is True


@pytest.mark.parametrize("gap", ["fifo", "summary-write"])
def test_settled_failed_turn_with_incomplete_capture_refuses_next_turn(
        author, tmp_path, scoped, monkeypatch, gap):  # noqa: F811
    drive = tmp_path / "drive"
    if gap == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO output requires POSIX")
        scoped.before_turn = lambda area: (area / rc.EVIDENCE_DIRNAME).mkdir() or os.mkfifo(
            area / rc.EVIDENCE_DIRNAME / "pipe")
    else:
        original_write = rc.atomic_write_json

        def fail_summary(path, value, *args, **kwargs):
            if Path(path).name == "summary.json":
                raise OSError("evidence summary volume unavailable")
            return original_write(path, value, *args, **kwargs)

        monkeypatch.setattr(rc, "atomic_write_json", fail_summary)
    source = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    scoped.terminal_state, scoped.process_scope = "failed", SETTLED_MACOS
    with pytest.raises(ClaudexorUnavailable) as caught:
        _run(author, drive, source)
    evidence = caught.value.checking_evidence
    assert evidence["status"] == ("partial" if gap == "fifo" else "capture_failed")
    assert not evidence.get("refresh_safe")
    scoped.before_turn = None
    with pytest.raises(ReviewRouteUnavailable) as blocked:
        _run(author, drive, source, operation="op-2", thread_id="thread-1")
    assert blocked.value.code == "review_checking_copy_unavailable"
    area = rc.seat_dir(drive, "task-1", "thread:plan_review:t1") / "area"
    assert len(scoped.turn_requests) == 1
    assert (area / "reviewer-test.txt").read_bytes() == b"reviewer turn 1\n"
