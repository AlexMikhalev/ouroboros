"""Exact transport receipts share autobiography without resending or replay scans."""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

import ouroboros.presence_delivery as delivery
from ouroboros.gateway.host_service import create_host_service_app
from ouroboros.utils import atomic_write_json
from tests.test_host_service_api import _seed_presence_behavior, _seed_token


@pytest.fixture(autouse=True)
def isolated_supervisor(tmp_path, monkeypatch):
    import supervisor.message_bus as bus
    import supervisor.queue as queue
    import supervisor.state as state

    monkeypatch.setattr(bus, "DATA_DIR", tmp_path)
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    for name, value in {
        "DRIVE_ROOT": tmp_path, "STATE_PATH": tmp_path / "state/state.json",
        "STATE_LAST_GOOD_PATH": tmp_path / "state/state.last_good.json",
        "STATE_LOCK_PATH": tmp_path / "locks/state.lock",
    }.items():
        monkeypatch.setattr(state, name, value)


def _payload(**changes):
    return {
        "schema_version": 1, "delivery_id": "send:one", "part_id": "0",
        "state": "delivered", "provider": "slack", "account_id": "workspace-1",
        "conversation_id": "D-exact", "thread_id": "100.1", "text": "Exact café\nreply",
        "format": "markdown", "message": {"provider_message_id": "100.2"},
        "origin": {"kind": "tool"}, **changes,
    }


def _client(tmp_path, **kwargs):
    _seed_token(tmp_path, skill="transport", token="receipt-token",
                permissions=["presence"], manifest_permissions=["presence"])
    return TestClient(create_host_service_app(tmp_path, **kwargs))


def _post(client, payload):
    return client.post("/presence/delivery", json=payload, headers={"X-Skill-Token": "receipt-token"})


def _rows(tmp_path):
    path = tmp_path / "logs/chat.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


@pytest.mark.parametrize("state,direction", [
    ("delivered", "out"), ("accepted", "out"), ("failed", "system"), ("uncertain", "system"),
])
def test_http_records_exact_facts_with_nonterminal_history_type(tmp_path, state, direction):
    client = _client(tmp_path)
    payload = _payload(state=state, message={"provider_message_id": "100.2", "subject": "Café", "to": ["reader@example.test"]})
    assert _post(client, payload).json() == {"ok": True, "recorded": True, "duplicate": False}
    row, = _rows(tmp_path)
    assert row["direction"] == direction
    assert row["text"] == payload["text"] and row["format"] == payload["format"]
    assert row["type"] == "presence_delivery" and row["task_id"] == ""
    assert row["source"] == "skill:transport"
    assert row["transport"]["delivery"]["state"] == state
    for key in ("provider", "account_id", "conversation_id", "thread_id", "message", "origin"):
        assert row["transport"][key] == payload[key]
    from ouroboros.gateway.history import make_chat_history_endpoint

    request = Request({"type": "http", "method": "GET", "path": "/api/chat/history", "query_string": b"", "headers": []})
    response = asyncio.run(make_chat_history_endpoint(tmp_path)(request))
    projected = [item for item in json.loads(response.body)["messages"] if item.get("text") == payload["text"]]
    assert len(projected) == 1
    assert projected[0]["system_type"] == "presence_delivery"


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"schema_version": 2}, {"state": "queued"},
    {"part_id": 0}, {"part_id": "part-0"}, {"account_id": ""}, {"text": None},
    {"origin": {"kind": "tool", "authority": "owner"}}, {"message": []},
    {"profile": "caller-cannot-supply-authority"},
])
def test_invalid_wire_is_400_without_chat_mutation(tmp_path, change):
    response = _post(_client(tmp_path), _payload(**change))
    assert response.status_code == 400
    assert _rows(tmp_path) == []


def test_auth_permission_and_admission_refusals(tmp_path, monkeypatch):
    client = _client(tmp_path)
    assert client.post("/presence/delivery", json=_payload()).status_code == 403
    _seed_token(tmp_path, skill="ungranted", token="no-permission",
                permissions=[], manifest_permissions=["presence"])
    assert client.post("/presence/delivery", json=_payload(), headers={"X-Skill-Token": "no-permission"}).status_code == 403
    ctx = client.app.state.host_service_context
    monkeypatch.setattr(ctx.rate_limiter, "allow", lambda _key: False)
    assert _post(client, _payload()).status_code == 429
    monkeypatch.setattr(ctx.rate_limiter, "allow", lambda _key: True)
    monkeypatch.setattr(ctx, "_enter_inflight", lambda _skill: False)
    assert _post(client, _payload()).status_code == 429
    assert _rows(tmp_path) == []


def test_duplicate_conflict_physical_parts_and_distinct_skill(tmp_path):
    client = _client(tmp_path)
    assert _post(client, _payload()).status_code == 200
    assert _post(client, _payload()).json()["duplicate"] is True
    for changes in ({"text": "changed"}, {"conversation_id": "another"}, {"message": {"provider_message_id": "other"}}):
        assert _post(client, _payload(**changes)).status_code == 409
    assert _post(client, _payload(part_id="1", text="Part two")).status_code == 200
    assert _post(client, _payload(part_id="status", state="uncertain", text="Remaining part unconfirmed")).status_code == 200
    client.app.state.host_service_context.presence_deliveries.record("another-transport", _payload())
    assert len(_rows(tmp_path)) == 4


def test_large_cold_history_is_scanned_once_then_updated_incrementally(tmp_path, monkeypatch):
    path = tmp_path / "logs/chat.jsonl"
    path.parent.mkdir()
    path.write_text("".join(json.dumps({"direction": "in", "text": str(n)}) + "\n" for n in range(20_000)), encoding="utf-8")
    real_reader = delivery.jsonl_chain_handles
    scans = []

    @contextmanager
    def counted(*args, **kwargs):
        scans.append(kwargs)
        with real_reader(*args, **kwargs) as handles:
            yield handles

    monkeypatch.setattr(delivery, "jsonl_chain_handles", counted)
    recorder = delivery.PresenceDeliveryRecorder(tmp_path)
    recorder.record("transport", _payload())
    assert len(scans) == 1 and scans[0]["strict"] is True
    for n in range(100):
        payload = _payload(delivery_id=f"send:{n}")
        assert recorder.record("transport", payload)["duplicate"] is False
        assert recorder.record("transport", payload)["duplicate"] is True
    assert len(scans) == 1
    assert len(_rows(tmp_path)) == 20_101


def test_simultaneous_identical_callbacks_write_one_row(tmp_path):
    recorder = delivery.PresenceDeliveryRecorder(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: recorder.record("transport", _payload()), range(24)))
    assert sum(not result["duplicate"] for result in results) == 1
    assert len(_rows(tmp_path)) == 1


def test_rotation_and_restart_keep_receipt_identity(tmp_path):
    recorder = delivery.PresenceDeliveryRecorder(tmp_path)
    recorder.record("transport", _payload())
    archive = tmp_path / "archive"
    archive.mkdir()
    (tmp_path / "logs/chat.jsonl").rename(archive / "chat_20260918.jsonl")
    assert recorder.record("transport", _payload())["duplicate"] is True
    assert recorder.record("transport", _payload(delivery_id="later"))["duplicate"] is False
    restarted = delivery.PresenceDeliveryRecorder(tmp_path)
    assert restarted.record("transport", _payload())["duplicate"] is True
    assert restarted.record("transport", _payload(delivery_id="later"))["duplicate"] is True
    with pytest.raises(delivery.PresenceDeliveryConflict):
        restarted.record("transport", _payload(text="conflict after restart"))
    assert len(_rows(tmp_path)) == 1


@pytest.mark.parametrize("landed", [False, True])
def test_ambiguous_write_failure_invalidates_projection_before_retry(tmp_path, monkeypatch, landed):
    import supervisor.message_bus as bus

    client = _client(tmp_path)
    writer = bus.log_chat

    def fail(*args, **kwargs):
        if landed:
            writer(*args, **kwargs)
        raise OSError("simulated lost write acknowledgement")

    monkeypatch.setattr(bus, "log_chat", fail)
    assert _post(client, _payload()).status_code == 503
    assert client.app.state.host_service_context.presence_deliveries._index is None
    monkeypatch.setattr(bus, "log_chat", writer)
    retry = _post(client, _payload())
    assert retry.status_code == 200 and retry.json()["duplicate"] is landed
    assert len(_rows(tmp_path)) == 1


def test_unreadable_retained_chain_is_not_empty_history(tmp_path):
    client = _client(tmp_path)
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs/chat.jsonl").write_text("{broken\n", encoding="utf-8")
    assert _post(client, _payload()).status_code == 503
    assert (tmp_path / "logs/chat.jsonl").read_text(encoding="utf-8") == "{broken\n"


def test_required_writer_refusal_does_not_ack_or_fill_index(tmp_path, monkeypatch):
    import supervisor.message_bus as bus

    client = _client(tmp_path)
    append = bus.append_jsonl
    monkeypatch.setattr(bus, "append_jsonl", lambda *args, **kwargs: False)
    assert _post(client, _payload()).status_code == 503
    assert client.app.state.host_service_context.presence_deliveries._index is None
    assert _rows(tmp_path) == []
    monkeypatch.setattr(bus, "append_jsonl", append)
    assert _post(client, _payload()).json()["duplicate"] is False


def test_cold_chain_io_failure_remains_retryable(tmp_path, monkeypatch):
    real_reader = delivery.jsonl_chain_handles

    @contextmanager
    def unavailable(*args, **kwargs):
        raise OSError("retained generation is temporarily unreadable")
        yield  # pragma: no cover - establishes the context-manager protocol

    client = _client(tmp_path)
    monkeypatch.setattr(delivery, "jsonl_chain_handles", unavailable)
    assert _post(client, _payload()).status_code == 503
    assert _rows(tmp_path) == []
    monkeypatch.setattr(delivery, "jsonl_chain_handles", real_reader)
    assert _post(client, _payload()).json()["duplicate"] is False


@pytest.mark.parametrize("owner", ["transport", "another-transport", None])
def test_task_lineage_requires_stored_task_owned_by_reporting_transport(tmp_path, owner):
    if owner is not None:
        atomic_write_json(tmp_path / "task_results/turn-1.json", {
            "_schema_version": 1, "task_id": "turn-1", "status": "completed",
            "metadata": {"presence": {"transport_skill": owner, "binding_id": "b" * 32,
                                      "event": {"source_event_id": "in-1"}}},
        })
    payload = _payload(origin={"kind": "tool", "task_id": "turn-1", "source_event_id": "in-1"})
    assert _post(_client(tmp_path), payload).status_code == 200
    row, = _rows(tmp_path)
    assert row["task_id"] == ("turn-1" if owner == "transport" else "")
    assert row["transport"]["origin"] == payload["origin"]
    assert ("presence_provenance" in row["transport"]["delivery"]) is (owner == "transport")


def _turn_payload(binding_id):
    return {"binding_id": binding_id, "event": {
        "source_event_id": "event-1", "provider": "telegram", "account_id": "bot-1",
        "conversation_id": "room-1", "thread_id": "topic-1", "conversation_key": "ignored",
        "actor": {"platform_actor_id": "user-7"}, "conversation": {}, "message": {}, "text": "Hello",
    }}


@pytest.mark.parametrize("requested,actual", [(None, 0), (0, 0), (1, 1), (1, 0)])
def test_host_negotiates_and_echoes_actual_original_mode(tmp_path, requested, actual):
    _seed_token(tmp_path, skill="telegram-bot", token="receipt-token",
                permissions=["presence"], manifest_permissions=["presence"])
    binding = _seed_presence_behavior(tmp_path)
    captured = []

    def runner(**kwargs):
        captured.append(kwargs["event"].delivery_reporting_version)
        return SimpleNamespace(outcome="message", text="Reply", task_id="turn-1", work_ref="", delivery_reporting_version=actual)

    client = TestClient(create_host_service_app(tmp_path, presence_runner=runner))
    identity = client.get("/identity", headers={"X-Skill-Token": "receipt-token"})
    assert identity.json()["presence_delivery_version"] == 1
    payload = _turn_payload(binding)
    if requested is not None:
        payload["delivery_reporting_version"] = requested
    result = client.post("/presence/turn", json=payload, headers={"X-Skill-Token": "receipt-token"})
    assert result.status_code == 200
    assert captured == [requested or 0]
    assert result.json()["delivery_reporting_version"] == actual


@pytest.mark.parametrize("version", [True, False, "1", None, 2, -1, 1.0])
def test_host_rejects_noninteger_or_unknown_turn_mode_before_model(tmp_path, version):
    client = _client(tmp_path, presence_runner=lambda **kwargs: pytest.fail("must not execute"))
    payload = {"delivery_reporting_version": version, "event": {}, "binding_id": "b" * 32}
    response = client.post("/presence/turn", json=payload, headers={"X-Skill-Token": "receipt-token"})
    assert response.status_code == 400


@pytest.mark.parametrize("status", ["running", "completed"])
@pytest.mark.parametrize("version", [None, 0, 1])
def test_deferred_work_echoes_original_persisted_mode(tmp_path, status, version):
    _seed_token(tmp_path, skill="telegram-bot", token="receipt-token",
                permissions=["presence"], manifest_permissions=["presence"])
    binding = _seed_presence_behavior(tmp_path)
    presence = {"binding_id": binding}
    if version is not None:
        presence["delivery_reporting_version"] = version
    atomic_write_json(tmp_path / "task_results/work-1.json", {
        "_schema_version": 1, "task_id": "work-1", "status": status,
        "metadata": {"presence": presence, "presence_outcome": "message"}, "result": "Late reply",
    })
    response = TestClient(create_host_service_app(tmp_path)).get(
        "/presence/work/work-1", params={"binding_id": binding}, headers={"X-Skill-Token": "receipt-token"},
    )
    assert response.status_code == (202 if status == "running" else 200)
    assert response.json()["delivery_reporting_version"] == (version or 0)
