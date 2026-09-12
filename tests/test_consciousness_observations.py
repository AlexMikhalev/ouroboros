"""Production-shaped durability tests for background observations."""

from __future__ import annotations

import concurrent.futures
import json
import multiprocessing
import pathlib
import queue
from unittest.mock import MagicMock, patch

import pytest


def _make(tmp_path):
    from ouroboros.consciousness import BackgroundConsciousness

    drive = tmp_path / "drive"
    repo = tmp_path / "repo"
    (drive / "logs").mkdir(parents=True, exist_ok=True)
    repo.mkdir(exist_ok=True)
    with patch.object(BackgroundConsciousness, "_build_registry", return_value=MagicMock()):
        return BackgroundConsciousness(
            drive_root=drive,
            repo_dir=repo,
            event_queue=queue.Queue(),
            owner_chat_id_fn=lambda: None,
        ), drive


def _enqueue_from_process(drive_str, result_queue, start_event):
    """Exercise the durable writer seam from an independent process."""
    from ouroboros.consciousness import BackgroundConsciousness

    drive = pathlib.Path(drive_str)
    repo = drive.parent / "repo-process"
    repo.mkdir(exist_ok=True)
    with patch.object(BackgroundConsciousness, "_build_registry", return_value=MagicMock()):
        instance = BackgroundConsciousness(
            drive_root=drive,
            repo_dir=repo,
            event_queue=queue.Queue(),
            owner_chat_id_fn=lambda: None,
        )
    start_event.wait(10)
    result_queue.put(instance.inject_observation(
        "process-payload",
        observation_id="process-stable-id",
        source="process",
        kind="trace",
        ref={"worker": "process"},
    ))


def test_observations_are_append_only_and_deduplicated_over_100_rows(tmp_path):
    bc, drive = _make(tmp_path)
    for index in range(125):
        assert bc.inject_observation(
            f"payload-{index}",
            observation_id=f"obs-{index}",
            source="test",
            kind="trace",
            ref={"path": "logs/events.jsonl", "line": index + 1},
        )
    assert not bc.inject_observation("replacement", observation_id="obs-7", source="other")
    pending = bc._snapshot_pending_observations()
    assert len(pending) == 125
    assert pending[7]["payload"] == "payload-7"
    store = drive / "state" / "consciousness_observations.jsonl"
    rows = [json.loads(line) for line in store.read_text().splitlines()]
    assert len(rows) == 125
    assert all(row["id"].startswith("obs-") for row in rows)
    assert all(set(("id", "source", "kind", "time", "payload", "ref")) <= row.keys() for row in rows)


def test_concurrent_enqueue_same_id_has_one_durable_row(tmp_path):
    bc, drive = _make(tmp_path)

    def enqueue(_):
        return bc.inject_observation("same", observation_id="same-id", source="thread")

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(enqueue, range(80)))
    assert sum(results) == 1
    rows = [json.loads(line) for line in (drive / "state" / "consciousness_observations.jsonl").read_text().splitlines()]
    assert [row["id"] for row in rows] == ["same-id"]


def test_cross_instance_enqueue_same_id_is_atomically_deduplicated(tmp_path, monkeypatch):
    import os
    import threading
    from ouroboros import consciousness, platform_layer

    first, drive = _make(tmp_path)
    second, _ = _make(tmp_path)
    trace = []
    original_acquire = consciousness.acquire_exclusive_file_lock
    original_release = consciousness.release_exclusive_file_lock

    def record(event, path, fd):
        facts = {"event": event, "thread": threading.get_ident(), "path": str(path), "fd": fd}
        for name, target in (("fd_identity", fd), ("path_identity", path)):
            if target is None:
                facts[name] = None
                continue
            try:
                stat = os.fstat(target) if isinstance(target, int) else target.stat()
                facts[name] = (stat.st_dev, stat.st_ino, stat.st_size)
            except (OSError, TypeError) as exc:
                facts[name] = type(exc).__name__
        facts["kernel_tier"] = platform_layer._KERNEL_LOCK_TIER.get(os.path.realpath(str(path.parent)))
        trace.append(facts)

    def acquire(path, **kwargs):
        fd = original_acquire(path, **kwargs)
        record("acquired", path, fd)
        return fd

    def release(path, fd):
        try:
            record("releasing", path, fd)
        finally:
            original_release(path, fd)

    monkeypatch.setattr(consciousness, "acquire_exclusive_file_lock", acquire)
    monkeypatch.setattr(consciousness, "release_exclusive_file_lock", release)

    def enqueue(instance):
        return instance.inject_observation(
            "same", observation_id="cross-instance-id", source="instance"
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enqueue, (first, second)))
    # Keep native failure facts: an unknown read and two different lock names
    # can both resemble a lost race, but require different repairs.
    diagnostics = {"results": results, "locks": trace, "states": [
        {"rows": list((instance._observation_state_cache or {}).get("rows", {})),
         "gaps": (instance._observation_state_cache or {}).get("gap_reasons", ["not read"])}
        for instance in (first, second)
    ]}
    assert sum(results) == 1, diagnostics
    rows = [
        json.loads(line)
        for line in (drive / "state" / "consciousness_observations.jsonl").read_text().splitlines()
    ]
    assert [row["id"] for row in rows] == ["cross-instance-id"]


def test_age_stale_lock_is_never_evicted_from_a_live_observation_writer(tmp_path, monkeypatch):
    """A LIVE holder of the inbox lock must survive an aged lock file.

    Age-only staleness let a contender unlink a live writer's lock; both then
    read one still-empty inbox and both claimed the same stable ID -- the
    Windows full-test symptom `results=[True, True]` / `assert 2 == 1`.
    """
    import os
    import threading
    import time

    from ouroboros import platform_layer
    from ouroboros.utils import jsonl_append_lock_path

    owner, drive = _make(tmp_path)
    contender, _ = _make(tmp_path)
    store = drive / "state" / "consciousness_observations.jsonl"
    lock_path = jsonl_append_lock_path(store)
    # Pin the NAME tier: there the eviction has no kernel backstop, so the
    # staleness contract alone decides whether a live holder keeps its lock.
    monkeypatch.setattr(platform_layer, "kernel_file_locks_enforced", lambda _path: False)

    holding = threading.Event()
    read_state = type(owner)._read_observation_state

    def stall_with_a_backdated_lock(instance, **kwargs):
        state = read_state(instance, **kwargs)
        if instance is owner and not holding.is_set():
            aged = time.time() - 600.0  # far past this seam's stale_sec=10.0
            os.utime(lock_path, (aged, aged))
            holding.set()
            time.sleep(1.0)  # a slow owner, still inside its transaction
        return state

    monkeypatch.setattr(type(owner), "_read_observation_state", stall_with_a_backdated_lock)

    def enqueue(instance):
        if instance is contender:
            assert holding.wait(10), "owner never entered its critical section"
        return instance.inject_observation(
            "same", observation_id="live-holder-id", source="instance"
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enqueue, (owner, contender)))

    assert sum(results) == 1, results
    rows = [json.loads(line) for line in store.read_text().splitlines()]
    assert [row["id"] for row in rows] == ["live-holder-id"]


@pytest.mark.parametrize("damage", ["unreadable", "malformed", "invalid_utf8"])
def test_enqueue_does_not_claim_a_new_id_from_an_incomplete_inbox(tmp_path, monkeypatch, damage):
    first, drive = _make(tmp_path)
    assert first.inject_observation("original", observation_id="stable")
    store = drive / "state" / "consciousness_observations.jsonl"
    original = store.read_bytes()
    second, _ = _make(tmp_path)
    with monkeypatch.context() as patch:
        if damage == "unreadable":
            open_path = pathlib.Path.open
            def refused(path, *args, **kwargs):
                if path == store and args and args[0] == "rb":
                    raise PermissionError("controlled inbox read refusal")
                return open_path(path, *args, **kwargs)
            patch.setattr(pathlib.Path, "open", refused)
        else:
            store.write_bytes(b"{broken\n" if damage == "malformed" else b"\xff\n")
        before = store.read_bytes() if damage != "unreadable" else original
        assert second.inject_observation("replacement", observation_id="stable") is False
        assert second.status_snapshot()["observation_source_complete"] is False
    assert store.read_bytes() == before
    store.write_bytes(original)
    assert second.inject_observation("replacement", observation_id="stable") is False
    assert second.inject_observation("new payload", observation_id="new") is True
    assert [row["payload"] for row in second._snapshot_pending_observations()] == ["original", "new payload"]


def test_new_observations_survive_an_older_source_gap(tmp_path):
    instance, drive = _make(tmp_path)
    store = drive / "state" / "consciousness_observations.jsonl"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(b"{broken\n")
    assert instance.inject_observation("fresh owner observation") is True
    assert instance.status_snapshot()["observation_source_complete"] is False
    pending = instance._snapshot_pending_observations()
    assert [row["payload"] for row in pending] == ["fresh owner observation"]
    assert instance._ack_observations(pending) is False
    assert store.read_bytes().startswith(b"{broken\n")


def test_cross_process_enqueue_same_id_is_atomically_deduplicated(tmp_path):
    try:
        context = multiprocessing.get_context("fork")
    except ValueError:
        pytest.skip("fork process context is unavailable on this platform")
    _, drive = _make(tmp_path)
    start_event = context.Event()
    result_queue = context.Queue()
    workers = [
        context.Process(
            target=_enqueue_from_process,
            args=(str(drive), result_queue, start_event),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    start_event.set()
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0
    assert sorted(result_queue.get(timeout=2) for _ in workers) == [False, True]
    rows = [
        json.loads(line)
        for line in (drive / "state" / "consciousness_observations.jsonl").read_text().splitlines()
    ]
    assert [row["id"] for row in rows] == ["process-stable-id"]


def test_context_snapshot_is_bounded_and_status_has_no_payload(tmp_path):
    bc, _ = _make(tmp_path)
    for index in range(15):
        bc.inject_observation("x" * 10_000, observation_id=f"obs-{index}", source="source-a")
    snapshot = bc._snapshot_pending_observations()
    rendered = bc._render_observations(snapshot)
    assert len(rendered) < 20_000
    assert "total=15" in rendered
    assert "read_file(root='runtime_data', path='state/consciousness_observations.jsonl')" in rendered
    assert "omitted=5" in rendered
    status = bc.status_snapshot()
    assert status["pending_observation_count"] == 15
    assert status["oldest_observation_at"]
    assert "payload" not in json.dumps(status)


def test_status_uses_cached_projection_after_initial_rebuild(tmp_path, monkeypatch):
    bc, _ = _make(tmp_path)
    bc.inject_observation("one", observation_id="one")
    assert bc.status_snapshot()["pending_observation_count"] == 1
    calls = {"read": 0}
    original = pathlib.Path.open

    def count_open(path, *args, **kwargs):
        if str(path).endswith("consciousness_observations.jsonl"):
            calls["read"] += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", count_open)
    assert bc.status_snapshot()["pending_observation_count"] == 1
    assert bc.status_snapshot()["pending_observation_count"] == 1
    assert calls["read"] == 0


def test_status_snapshot_never_materializes_pending_payloads(tmp_path, monkeypatch):
    bc, _ = _make(tmp_path)
    bc.inject_observation("payload", observation_id="status-id")

    def fail_if_snapshot_called():
        raise AssertionError("status_snapshot must use cached counters")

    monkeypatch.setattr(bc, "_snapshot_pending_observations", fail_if_snapshot_called)
    first = bc.status_snapshot()
    second = bc.status_snapshot()
    assert first["pending_observation_count"] == 1
    assert second["oldest_observation_at"] == first["oldest_observation_at"]


def test_malformed_store_row_is_disclosed_and_blocks_ack(tmp_path):
    bc, drive = _make(tmp_path)
    bc.inject_observation("known", observation_id="known")
    store = drive / "state" / "consciousness_observations.jsonl"
    with store.open("a", encoding="utf-8") as handle:
        handle.write("{broken\n")
    restarted, _ = _make(tmp_path)
    status = restarted.status_snapshot()
    assert status["observation_source_complete"] is False
    assert status["observation_gap_count"] == 1
    assert restarted._ack_observations(restarted._snapshot_pending_observations()) is False
    assert [row["id"] for row in restarted._snapshot_pending_observations()] == ["known"]


def test_unknown_and_malformed_rows_create_gaps_and_block_ack(tmp_path):
    bc, drive = _make(tmp_path)
    store = drive / "state" / "consciousness_observations.jsonl"
    store.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "op": "enqueue",
            "id": "valid",
            "source": "test",
            "kind": "trace",
            "time": "2026-08-22T00:00:00Z",
            "payload": "kept",
            "ref": None,
        },
        {"op": "future-op", "id": "unknown", "payload": "ignored"},
        {"op": "ack"},
        {"op": "ack", "id": 42},
        {
            "op": "enqueue",
            "id": "missing-ref",
            "source": "test",
            "kind": "trace",
            "time": "2026-08-22T00:00:01Z",
            "payload": "ignored",
        },
    ]
    store.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    restarted, _ = _make(tmp_path)
    status = restarted.status_snapshot()
    assert status["observation_source_complete"] is False
    assert status["observation_gap_count"] == 4
    assert [row["id"] for row in restarted._snapshot_pending_observations()] == ["valid"]
    assert restarted._ack_observations(restarted._snapshot_pending_observations()) is False


def test_ghost_ack_is_a_gap_and_does_not_preack_later_enqueue(tmp_path):
    _, drive = _make(tmp_path)
    store = drive / "state" / "consciousness_observations.jsonl"
    store.parent.mkdir(parents=True, exist_ok=True)
    ghost_ack = {"op": "ack", "id": "future-id", "time": "2026-08-22T00:00:00Z"}
    later_enqueue = {
        "op": "enqueue",
        "id": "future-id",
        "source": "test",
        "kind": "trace",
        "time": "2026-08-22T00:00:01Z",
        "payload": "must remain pending",
        "ref": None,
    }
    store.write_text(
        json.dumps(ghost_ack) + "\n" + json.dumps(later_enqueue) + "\n",
        encoding="utf-8",
    )
    restarted, _ = _make(tmp_path)
    status = restarted.status_snapshot()
    assert status["observation_source_complete"] is False
    assert status["observation_gap_count"] == 1
    assert [row["id"] for row in restarted._snapshot_pending_observations()] == ["future-id"]
    assert restarted._ack_observations(restarted._snapshot_pending_observations()) is False


def test_success_acks_only_cycle_snapshot_and_later_rows_stay_pending(tmp_path, monkeypatch):
    bc, _ = _make(tmp_path)
    bc.inject_observation("first", observation_id="first")
    snapshot = bc._snapshot_pending_observations()
    bc.inject_observation("second", observation_id="second")
    assert bc._ack_observations(snapshot)
    assert [row["id"] for row in bc._snapshot_pending_observations()] == ["second"]


def test_successful_cognition_acks_after_thought_receipt(tmp_path, monkeypatch):
    bc, _ = _make(tmp_path)
    bc.inject_observation("settle me", observation_id="settled")
    monkeypatch.setattr(bc, "_build_context", lambda **_: "context")
    monkeypatch.setattr(bc, "_tool_schemas", lambda: [])
    monkeypatch.setattr(bc, "_check_budget", lambda: True)
    monkeypatch.setattr(
        "ouroboros.llm_observability.chat_observed",
        lambda *args, **kwargs: ({"content": "done"}, {"cost": None}),
    )
    assert bc._think_scoped() is True
    assert bc._snapshot_pending_observations() == []


def test_cancelled_cycle_keeps_snapshot_pending(tmp_path, monkeypatch):
    bc, _ = _make(tmp_path)
    bc.inject_observation("retry me", observation_id="cancelled")
    bc._stop_event.set()
    monkeypatch.setattr(bc, "_build_context", lambda **_: "context")
    monkeypatch.setattr(bc, "_tool_schemas", lambda: [])
    assert bc._think_scoped() is False
    assert [row["id"] for row in bc._snapshot_pending_observations()] == ["cancelled"]


@pytest.mark.parametrize("failure", [RuntimeError("provider"), OverflowError("context")])
def test_failed_cycle_keeps_observations_pending(tmp_path, monkeypatch, failure):
    bc, _ = _make(tmp_path)
    bc.inject_observation("must replay", observation_id="replay")
    if isinstance(failure, OverflowError):
        monkeypatch.setattr(bc, "_build_context", lambda *args, **kwargs: (_ for _ in ()).throw(failure))
    else:
        monkeypatch.setattr(bc, "_build_context", lambda *args, **kwargs: "context")
        monkeypatch.setattr("ouroboros.llm_observability.chat_observed", lambda *args, **kwargs: (_ for _ in ()).throw(failure))
    assert bc._think_scoped() is False
    assert [row["id"] for row in bc._snapshot_pending_observations()] == ["replay"]


def test_tool_receipt_write_failure_forbids_cycle_ack(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from ouroboros import consciousness

    bc, _ = _make(tmp_path)
    bc.inject_observation("must settle", observation_id="tool-gap")
    bc._build_context = lambda **_: "context"
    bc._tool_schemas = lambda: [{
        "type": "function",
        "function": {"name": "read_file", "parameters": {"type": "object"}},
    }]
    bc._llm = SimpleNamespace(_resolve_remote_target=lambda _model: {
        "provider": "openai", "resolved_model": "test-model",
    })
    bc._registry.get_timeout.return_value = 1
    from ouroboros.tools.tool_result import ToolResult
    bc._registry.execute_result.return_value = ToolResult(status="ok", code="OK", text="tool-result")
    bc._registry._ctx = SimpleNamespace(pending_events=[])
    responses = [
        ({"content": "", "tool_calls": [{
            "id": "tool-call",
            "function": {"name": "read_file", "arguments": "{}"},
        }]}, {"cost": 0.0}),
        ({"content": "settled"}, {"cost": 0.0}),
    ]
    monkeypatch.setattr(
        "ouroboros.llm_observability.chat_observed",
        lambda *args, **kwargs: responses.pop(0),
    )
    monkeypatch.setattr(consciousness, "get_consciousness_model", lambda: "openai/test-model")
    monkeypatch.setattr(consciousness, "resolve_effort", lambda _slot: "medium")
    monkeypatch.setattr(consciousness, "append_jsonl", lambda path, row: path.name != "tools.jsonl")

    assert bc._think_scoped() is False
    assert [row["id"] for row in bc._snapshot_pending_observations()] == ["tool-gap"]


def test_budget_refusal_after_response_forbids_cycle_ack(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from ouroboros import consciousness

    bc, drive = _make(tmp_path)
    bc.inject_observation("budget retry", observation_id="budget-gap")
    bc._build_context = lambda **_: "context"
    bc._tool_schemas = lambda: []
    bc._llm = SimpleNamespace(_resolve_remote_target=lambda _model: {
        "provider": "openai", "resolved_model": "test-model",
    })
    seen = {"chat": 0}

    def fake_chat(*args, **kwargs):
        seen["chat"] += 1
        return {"content": "would settle"}, {"cost": 0.0}

    monkeypatch.setattr("ouroboros.llm_observability.chat_observed", fake_chat)
    monkeypatch.setattr(consciousness, "get_consciousness_model", lambda: "openai/test-model")
    monkeypatch.setattr(consciousness, "resolve_effort", lambda _slot: "medium")
    monkeypatch.setattr(bc, "_check_budget", lambda: False)

    assert bc._think_scoped() is False
    assert seen["chat"] == 1
    assert [row["id"] for row in bc._snapshot_pending_observations()] == ["budget-gap"]
    events = [
        json.loads(line)
        for line in (drive / "logs" / "events.jsonl").read_text().splitlines()
    ]
    assert any(row.get("type") == "bg_budget_exceeded_mid_cycle" for row in events)


def test_budget_receipt_write_failure_latches_settlement_gap(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from ouroboros import consciousness

    bc, _ = _make(tmp_path)
    bc.inject_observation("budget receipt retry", observation_id="budget-receipt-gap")
    bc._build_context = lambda **_: "context"
    bc._tool_schemas = lambda: []
    bc._llm = SimpleNamespace(_resolve_remote_target=lambda _model: {
        "provider": "openai", "resolved_model": "test-model",
    })
    monkeypatch.setattr(
        "ouroboros.llm_observability.chat_observed",
        lambda *args, **kwargs: ({"content": "would settle"}, {"cost": 0.0}),
    )
    monkeypatch.setattr(consciousness, "get_consciousness_model", lambda: "openai/test-model")
    monkeypatch.setattr(consciousness, "resolve_effort", lambda _slot: "medium")
    monkeypatch.setattr(bc, "_check_budget", lambda: False)
    monkeypatch.setattr(
        consciousness,
        "append_jsonl",
        lambda _path, row: row.get("type") != "bg_budget_exceeded_mid_cycle",
    )

    assert bc._think_scoped() is False
    assert bc._cycle_settlement_failed is True
    assert [row["id"] for row in bc._snapshot_pending_observations()] == ["budget-receipt-gap"]


def test_restart_replays_unacknowledged_observation_by_id(tmp_path):
    bc, _ = _make(tmp_path)
    bc.inject_observation("survive", observation_id="restart-id")
    restarted, _ = _make(tmp_path)
    assert [row["id"] for row in restarted._snapshot_pending_observations()] == ["restart-id"]
