"""Canonical delegation requests stay replayable outside the event row."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from ouroboros import delegate_custody as custody
from ouroboros import observability
from ouroboros.delegate_pending import request_body
from tests._delegated_transport_shared import (
    _LiveRunStub, _nanny_ctx,
    _owned_gateway_uses_each_test_transport,  # noqa: F401 -- autouse fixture
)
from tests._review_session_route_shared import fake_route as _fake_route

fake_route = _fake_route


def _start_rows(root):
    return [row for row in custody._iter_rows(custody.event_log_path(root))
            if row["type"] == custody.START_REQUESTED]


def _digest(body):
    # The engine digests parsed JSON with stable keys, not wire whitespace.
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("surface", ["delegate", "review"])
@pytest.mark.parametrize("failure", [None, "blob", "event"])
def test_canonical_body_and_event_precede_dispatch(tmp_path, monkeypatch, request, surface, failure):
    from ouroboros.gateways import claudexor as gateway
    from ouroboros.review_execution import ReviewRouteUnavailable
    from ouroboros.tools import delegate
    from tests._review_session_route_shared import _run_session_directly

    prompt = "Inspect these exact bytes: Привет\n" + "x" * 50_000
    order, bodies = [], []
    real_write, real_emit = observability.write_blob, custody.emit

    def write(root, body, **kwargs):
        if isinstance(body, dict) and body.get("prompt") == prompt:
            assert order == []
            if failure == "blob":
                raise OSError("fixture refuses body storage")
            bodies.append(copy.deepcopy(body))
            ref = real_write(root, body, **kwargs)
            order.append("blob")
            return ref
        return real_write(root, body, **kwargs)

    def emit(root, kind, payload):
        if kind == custody.START_REQUESTED:
            assert order == ["blob"]
            assert "request" not in payload
            assert request_body(root, payload) == bodies[0]
            if failure == "event":
                return False
            order.append("event")
        return real_emit(root, kind, payload)

    if surface == "review":
        stub_type = request.getfixturevalue("fake_route")
    else:
        stub = _LiveRunStub()
        stub_type = type(stub)
        monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=weak-model:low")
        monkeypatch.setattr(gateway, "ClaudexorGateway", lambda *a, **k: stub)
    original_start = stub_type.start_run

    def dispatch(self, body, *, idempotency_key=""):
        assert order == ["blob", "event"]
        stored = custody.invocation_record(tmp_path, idempotency_key)
        assert stored["request"] == body == bodies[0]
        assert _digest(stored["request"]) == _digest(body)
        order.append("dispatch")
        return original_start(self, body, idempotency_key=idempotency_key)

    monkeypatch.setattr(observability, "write_blob", write)
    monkeypatch.setattr(custody, "emit", emit)
    monkeypatch.setattr(stub_type, "start_run", dispatch)
    if surface == "review":
        if failure:
            with pytest.raises(ReviewRouteUnavailable) as caught:
                _run_session_directly(tmp_path, prompt=prompt)
            assert caught.value.code == "start_request_row_unwritable"
        else:
            _run_session_directly(tmp_path, prompt=prompt)
    else:
        result = json.loads(delegate._delegate_start(_nanny_ctx(tmp_path), prompt).text)
        assert result["status"] == ("refused" if failure else "started")
        if failure:
            assert result["reason"] == "start_request_row_unwritable"
    if failure:
        assert "dispatch" not in order
        assert _start_rows(tmp_path) == []
    else:
        row = _start_rows(tmp_path)[0]
        assert row["prompt_chars"] == len(prompt)
        assert len(json.dumps(row).encode("utf-8")) < 2_000
        assert order == ["blob", "event", "dispatch"]


def test_raw_review_envelope_and_thread_projection_round_trip(tmp_path):
    from ouroboros.review_thread_continuity import start_review_thread_turn

    body = {"prompt": "Привет\n" + "x" * 80_000,
            "instructions": "Authorization: Bearer EXAMPLE-NOT-A-REAL-CREDENTIAL",
            "authPreference": "subscription", "mode": "ask", "access": "readonly",
            "scope": {"kind": "project", "root": str(tmp_path)},
            "harnesses": ["codex"], "primaryHarness": "codex", "maxSeconds": 300,
            "credentialProfileId": None, "_use_thread": True, "_thread_id": "thread-1",
            "execution": {"delegated": False, "isolation": "in_place"},
            "outputSchema": {"type": "object", "properties": {"passed": {"type": "boolean"}}}}
    original = copy.deepcopy(body)
    assert custody.record_start_requested(tmp_path, invocation_id="review", task_id="task", request=body)
    row = _start_rows(tmp_path)[0]
    assert row["request_ref"]["kind"] == "json"
    restored = custody.invocation_record(tmp_path, "review")["request"]
    assert restored == body == original
    assert _digest(restored) == _digest(body)
    projections = []

    class Gateway:
        def start_thread_turn(self, thread_id, request, *, idempotency_key):
            projections.append((thread_id, request, idempotency_key))
            return {"runId": "run"}

    for envelope in (body, restored):
        start_review_thread_turn(Gateway(), envelope["_thread_id"], envelope, idempotency_key="review")
    assert projections[0] == projections[1]
    assert not {"_thread_id", "_use_thread", "scope", "execution"} & projections[0][1].keys()


def test_legacy_inline_wins_even_beside_an_unreadable_ref(tmp_path, monkeypatch):
    inline = {"prompt": "legacy request"}
    assert custody.emit(tmp_path, custody.START_REQUESTED, {
        "invocation_id": "legacy", "request": inline, "request_ref": {"path": "missing"}})
    def refuse_read(*args, **kwargs):
        raise AssertionError("legacy inline must not read a blob")
    monkeypatch.setattr(observability, "read_blob_ref", refuse_read)
    assert custody.invocation_record(tmp_path, "legacy")["request"] == inline
    assert custody.pending_invocations(tmp_path)[0]["request"] == inline


@pytest.mark.parametrize("damage", ["missing", "invalid_size", "corrupt"])
def test_unreadable_ref_keeps_request_unknown_and_retry_refuses(tmp_path, damage):
    from ouroboros.tools.delegate_integration import _validated_invocation

    assert custody.record_start_requested(tmp_path, invocation_id="lost", task_id="task",
                                          request={"prompt": "exact original"})
    row = _start_rows(tmp_path)[0]
    if damage == "missing":
        Path(row["request_ref"]["path"]).unlink()
    elif damage == "corrupt":
        Path(row["request_ref"]["path"]).write_bytes(b"not gzip")
    else:
        row["request_ref"]["size"] = "invalid"
        custody.event_log_path(tmp_path).write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert custody.invocation_record(tmp_path, "lost")["request"] is None
    assert custody.pending_invocations(tmp_path) == []
    record, refusal = _validated_invocation(tmp_path, "lost", "task", "exact original")
    assert record is None
    assert json.loads(refusal.text)["reason"] == "invocation_request_unrecorded"


def test_pending_scan_only_reads_blobs_for_surviving_invocations(tmp_path, monkeypatch):
    for identity in ("started", "refused", "pending"):
        assert custody.record_start_requested(tmp_path, invocation_id=identity,
                                              request={"prompt": identity})
    assert custody.emit(tmp_path, custody.STARTED, {"invocation_id": "started", "run_id": "run"})
    assert custody.emit(tmp_path, custody.START_FAILED, {"invocation_id": "refused", "definite": True})
    reads, original = [], observability.read_blob_ref
    def read(*args, **kwargs):
        body = original(*args, **kwargs)
        reads.append(body["prompt"])
        return body
    monkeypatch.setattr(observability, "read_blob_ref", read)
    pending = custody.pending_invocations(tmp_path)
    assert [row["invocation_id"] for row in pending] == ["pending"]
    assert reads == ["pending"]
    assert "request_ref" not in pending[0]


@pytest.mark.parametrize("different", [False, True])
def test_task_event_omits_only_an_equal_contract_mirror_without_mutation(tmp_path, different):
    from ouroboros.contracts.task_contract import attach_task_contract
    from ouroboros.utils import sanitize_task_for_event

    task = attach_task_contract({"id": "task", "text": "Inspect the files", "metadata": {"source": "web"}})
    if different:
        task["metadata"]["task_contract"] = {"other": "contract"}
    original = copy.deepcopy(task)
    projected = sanitize_task_for_event(task, tmp_path / "logs")
    assert task == original
    assert projected["task_contract"] == original["task_contract"]
    assert projected["metadata"]["source"] == "web"
    assert ("task_contract" in projected["metadata"]) is different
    if different:
        assert projected["metadata"]["task_contract"] == original["metadata"]["task_contract"]
