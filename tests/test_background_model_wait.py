"""Real background loop and model-wait controls over a controlled model engine."""

from copy import deepcopy
import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from ouroboros import config, model_wait
from ouroboros import llm_claudexor as transport
from ouroboros.consciousness import BackgroundConsciousness
from ouroboros.gateway import task_model_wait as gateway
from tests.test_llm_claudexor import Gateway, MODEL, result, ledger


def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("controlled background state did not arrive")


@pytest.fixture
def background(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(root / "settings.json"))
    monkeypatch.setenv("TOTAL_BUDGET", "100")
    monkeypatch.setenv("OUROBOROS_MODEL_CONSCIOUSNESS", MODEL)
    monkeypatch.setattr(config, "CLAUDEXOR_MODEL_POLL_INTERVAL_SEC", 0.005)
    monkeypatch.setattr(config, "NETWORK_WAIT_BACKOFF_START_SEC", 0.005)
    monkeypatch.setattr(config, "NETWORK_WAIT_BACKOFF_MAX_SEC", 0.01)
    registry = SimpleNamespace(_ctx=SimpleNamespace(task_metadata={}),
                               get_timeout=lambda _name: 10, schemas=lambda: [])
    tools = []
    registry.execute = lambda name, args: tools.append((name, args)) or "read completed once"
    from ouroboros.tools.tool_result import ToolResult
    registry.execute_result = lambda name, args: (
        tools.append((name, args))
        or ToolResult(status="ok", code="OK", text="read completed once", meta={"source": "fixture"})
    )
    monkeypatch.setattr(BackgroundConsciousness, "_build_registry", lambda self: registry)
    events = queue.Queue()
    bc = BackgroundConsciousness(root, tmp_path / "repo", events, lambda: 1)
    monkeypatch.setattr(bc, "_build_context", lambda **kwargs: "Own background context")
    monkeypatch.setattr(bc, "_tool_schemas", lambda: [])
    engine = Gateway()
    first = result()
    first["message"] = {"role": "assistant", "content": "read first", "tool_calls": [
        {"id": "read-once", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"file"}'}}]}
    refusal = result(outcome="failed", problem={"code": "subscription_window_exhausted", "message": "controlled quota"})
    final = result()
    final["message"] = {"role": "assistant", "content": "Cycle truly finished"}
    engine.results = [first, refusal, final]
    engine.dispatch = ["response_received", "not_started", "response_received"]
    monkeypatch.setattr(transport, "ensure_owned_gateway", lambda: engine)
    ready = threading.Event()
    metadata_ready = threading.Event()
    monkeypatch.setattr(bc._llm, "claudexor_model_sources", lambda: {
        "sources": [{"id": "codex", "credentialHarness": "fixture"}]})

    def catalog(*_, **_kw):
        # Source discovery publishes a second revision before this read. Tests
        # need that stable snapshot before exercising the real decision ingress.
        metadata_ready.set()
        return {"source": "codex", "models": [{"id": "exact-model"}] if ready.is_set() else []}

    monkeypatch.setattr(bc._llm, "claudexor_model_catalog", catalog)
    bc.inject_observation("one pending observation", observation_id="one")
    outcomes, failures = [], []

    def run():
        try:
            outcomes.append(bc._think())
        except BaseException as error:
            failures.append(error)

    def start():
        bc._running = True
        bc._stop_event.clear()
        metadata_ready.clear()
        bc._thread = threading.Thread(target=run)
        bc._thread.start()
        return bc._thread

    yield SimpleNamespace(bc=bc, engine=engine, ready=ready, metadata_ready=metadata_ready, events=events, tools=tools,
                          outcomes=outcomes, failures=failures, root=root, start=start,
                          decide=lambda body: gateway._decide(root, body, get_background_model_wait=bc.live_model_wait))
    bc._stop_event.set()
    bc._wakeup_event.set()
    if bc._thread:
        bc._thread.join(5)
        assert not bc._thread.is_alive()
    bc._tool_executor.shutdown(wait=True, cancel_futures=True)


def waiting(fixture):
    if not fixture.metadata_ready.is_set():
        return None
    rows = fixture.bc.model_wait_snapshot()["model_waits"]
    return next((row for row in rows.values() if row["state"] == "waiting"), None)


def decision(row, action="retry", **fields):
    return {"request_id": "owner-choice", "decision_id": f"model_wait:bg-consciousness:{row['wait_id']}",
            "revision": row["revision"], "action": action, **fields}


@pytest.mark.parametrize("code", ["subscription_window_exhausted", "auth_required"])
def test_background_quota_holds_exact_cycle_without_task_record(background, code):
    f = background
    f.engine.results[1]["problem"]["code"] = code
    thread = f.start()
    until(lambda: waiting(f))
    owner = f.bc.live_model_wait()
    assert owner.execution_window_remaining() is None
    assert waiting(f)["worker_slot_held"] is False
    assert waiting(f)["reason"] == ("auth" if code == "auth_required" else "quota")
    assert f.tools == [("read_file", {"path": "file"})]
    assert f.outcomes == [] and len(f.bc._snapshot_pending_observations()) == 1
    f.ready.set()
    thread.join(5)
    assert f.failures == [] and f.outcomes == [True]
    assert len(f.tools) == 1 and len(f.engine.creates) == 3
    assert f.bc._snapshot_pending_observations() == []
    assert not (f.root / "task_results" / "bg-consciousness.json").exists()
    assert owner.closed and f.bc.live_model_wait() is None


def test_foreground_pause_after_quota_preserves_old_call_until_resume(background):
    f = background
    thread = f.start()
    until(lambda: waiting(f))
    f.bc.pause()
    f.ready.set()
    until(lambda: not waiting(f))
    assert thread.is_alive() and f.outcomes == []
    assert len(f.engine.creates) == 2 and len(f.tools) == 1
    f.bc.resume()
    thread.join(5)
    assert f.failures == [] and f.outcomes == [True]
    assert len(f.engine.creates) == 3 and len(f.tools) == 1


def test_stop_during_model_wait_keeps_observations_unacknowledged(background):
    f = background
    thread = f.start()
    until(lambda: waiting(f))
    owner = f.bc.live_model_wait()
    f.bc.stop()
    thread.join(5)
    assert f.failures == [] and f.outcomes == [False]
    assert f.bc._last_idle_reason == "stopped" and owner.closed
    assert len(f.engine.creates) == 2 and len(f.bc._snapshot_pending_observations()) == 1
    assert not (f.root / "task_results" / "bg-consciousness.json").exists()


def test_unknown_outcome_is_not_waitable_or_free(background):
    f = background
    f.engine.results[1] = result(outcome="unknown", problem={"code": "provider_unavailable", "message": "unknown"})
    f.engine.dispatch[1] = "unknown"
    f.start().join(5)
    assert f.failures == [] and f.outcomes == [False]
    assert len(f.engine.creates) == 2 and len(f.tools) == 1
    assert ledger(f.root)[-1]["state"] == "unresolved"
    assert len(f.bc._snapshot_pending_observations()) == 1


def test_stop_of_inflight_background_operation_cancels_same_id_once(background):
    f = background
    f.engine.pending = True
    thread = f.start()
    until(lambda: bool(f.engine.reads))
    f.bc.stop()
    thread.join(5)
    assert f.failures == [] and f.outcomes == [False]
    assert f.bc._last_idle_reason == "stopped"
    assert len(f.engine.creates) == 1 and f.engine.cancels == [("op-0", "host_cancelled")]
    assert ledger(f.root)[-1]["state"] == "unresolved"
    assert len(f.bc._snapshot_pending_observations()) == 1


def test_image_window_fallback_does_not_create_background_cycle_deadline(tmp_path, monkeypatch):
    from ouroboros.tools.vision import _vision_execution_window

    monkeypatch.setattr(config, "get_task_abs_ceiling_sec", lambda: 900)
    with model_wait.task_model_wait_scope(task={"id": "background"}, drive_root=tmp_path,
                                          event_queue=None, worker_slot_held=False, owner_control=lambda: None) as owner:
        owner.started_monotonic = 0
        assert owner.execution_window_remaining() is None
        assert _vision_execution_window() == 900
        assert owner.control_reason() is None


def test_background_owner_retains_lexical_deadlines_without_task_ceiling(tmp_path, monkeypatch):
    owner = model_wait.TaskModelWait(task={"id": "bg-consciousness"}, drive_root=tmp_path,
                                     event_queue=None, worker_slot_held=False, owner_control=lambda: None)
    owner.started_monotonic = 0
    monkeypatch.setattr(config, "get_task_abs_ceiling_sec", lambda: 0)
    assert owner.control_reason() is None and owner.execution_window_remaining() is None
    with model_wait.task_model_wait_scope(task={"id": "bg-consciousness"}, drive_root=tmp_path,
                                          event_queue=None, worker_slot_held=False, owner_control=lambda: None) as bound:
        with model_wait.execution_deadline_scope(model_wait.monotonic_now() - 1):
            assert bound.control_reason() == "execution_deadline"
        with model_wait.calendar_scope("2000-01-01T00:00:00Z"):
            assert bound.control_reason() == "deadline"


def test_owner_switch_is_same_live_map_and_old_cycle_action_is_refused(background):
    f = background
    thread = f.start()
    until(lambda: waiting(f))
    row = deepcopy(waiting(f))
    owner = f.bc.live_model_wait()
    owned_row = owner.waits[row["wait_id"]]
    response = f.decide(decision(row, "switch", model=MODEL, credential_profile_id="replacement",
                                 use_local=False, persist_role=False))
    assert response.status_code == 202
    assert owner.waits[row["wait_id"]] is owned_row
    thread.join(5)
    assert f.failures == [] and f.outcomes == [True]
    assert f.engine.uploads[-1][0]["account"] == {"mode": "pin", "profileId": "replacement"}
    assert f.decide(decision(row)).status_code == 409
    assert not (f.root / "settings.json").exists()


def test_persistent_background_switch_saves_only_consciousness(background, monkeypatch):
    f = background
    path = f.root / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", path)
    path.write_text(json.dumps({"OUROBOROS_MODEL": "main-stays", "OUROBOROS_MODEL_LIGHT": "light-stays"}))
    thread = f.start()
    until(lambda: waiting(f))
    response = f.decide(decision(deepcopy(waiting(f)), "switch", model=MODEL,
                                 credential_profile_id="replacement", use_local=False, persist_role=True))
    assert response.status_code == 202 and json.loads(response.body)["saved"] is True
    thread.join(5)
    saved = json.loads(path.read_text())
    assert saved["OUROBOROS_MODEL"] == "main-stays" and saved["OUROBOROS_MODEL_LIGHT"] == "light-stays"
    assert saved["OUROBOROS_MODEL_CONSCIOUSNESS"] == MODEL
    assert json.loads(saved["OUROBOROS_MODEL_ACCOUNTS"])["consciousness"] == "replacement"
    assert f.outcomes == [True] and not f.failures


@pytest.mark.parametrize("phase", ["before", "after"])
def test_current_background_owner_fences_settings_after_live_owner_changes(background, monkeypatch, phase):
    from ouroboros.gateway import owner_settings

    f = background
    monkeypatch.setattr(config, "SETTINGS_PATH", f.root / "settings.json")
    thread = f.start()
    until(lambda: waiting(f))
    old_owner = f.bc.live_model_wait()
    original = owner_settings._owner_update_settings

    def stale_before_write(transform, **kwargs):
        if phase == "before":
            f.bc._model_wait = None
        value = original(transform, **kwargs)
        f.bc._model_wait = None
        return value

    monkeypatch.setattr(owner_settings, "_owner_update_settings", stale_before_write)
    response = f.decide(decision(deepcopy(waiting(f)), "switch", model=MODEL,
                                 credential_profile_id="replacement", use_local=False, persist_role=True))
    assert response.status_code == 409 and json.loads(response.body)["saved"] is (phase == "after")
    assert (f.root / "settings.json").exists() is (phase == "after")
    f.bc._model_wait = old_owner
    f.bc.stop()
    thread.join(5)


def test_background_wait_forwarding_and_reload_use_existing_owner(background):
    from ouroboros.gateway.history import _assemble_history_response
    from ouroboros.utils import append_jsonl
    from supervisor.task_model_wait import handle_task_model_wait

    f = background
    thread = f.start()
    until(lambda: (waiting(f) or {}).get("revision"))
    owner = f.bc.live_model_wait()
    # Source metadata can publish a newer revision while the cycle waits.
    # Keep this snapshot current throughout forwarding and reload assertions.
    with owner.lock:
        row = deepcopy(waiting(f))
        event = {"type": "task_model_wait", "task_id": "bg-consciousness", "ts": "2026-09-07T00:00:00Z", **row}
        forwarded = []
        ctx = SimpleNamespace(RUNNING={}, DRIVE_ROOT=f.root, consciousness=f.bc,
                              append_jsonl=append_jsonl, bridge=SimpleNamespace(push_log=forwarded.append))
        handle_task_model_wait(event, ctx)
        assert len(forwarded) == 1 and forwarded[0]["chat_id"] == 1
        handle_task_model_wait({**event, "model_wait_owner_id": "previous-cycle"}, ctx)
        assert len(forwarded) == 1 and ctx.RUNNING == {}
        append_jsonl(f.root / "logs" / "progress.jsonl", {"task_id": "bg-consciousness",
                     "type": "send_message", "is_progress": True, "ts": "2026-09-07T00:00:00Z", "text": "Earlier thought"})
        payload = json.loads(_assemble_history_response(f.root, 1, 10, 10, owner.snapshot()))
        current = payload["messages"][-1]
        assert current["model_wait_live"] and current["model_wait_owner_id"] == owner.owner_id
        assert not any(row.get("task_terminal_status") for row in payload["messages"] if row.get("is_progress"))
        f.bc._owner_chat_id_fn = lambda: 0
        handle_task_model_wait(event, ctx)
        assert forwarded[-1]["chat_id"] == 0
        hidden = json.loads(_assemble_history_response(f.root, 1, 10, 10, {**owner.snapshot(), "chat_id": 0}))
        assert not any(row.get("model_wait_live") for row in hidden["messages"])
        assert any(row.get("task_terminal_status") == "done" for row in hidden["messages"] if row.get("is_progress"))
    f.bc.stop()
    thread.join(5)


def test_previous_cycle_decision_cannot_target_a_new_live_cycle(background):
    f = background
    thread = f.start()
    until(lambda: waiting(f))
    previous = deepcopy(waiting(f))
    previous_owner = f.bc.live_model_wait()
    f.ready.set()
    thread.join(5)
    f.ready.clear()
    f.engine.results.extend(deepcopy(f.engine.results))
    f.engine.dispatch.extend(list(f.engine.dispatch))
    thread = f.start()
    until(lambda: waiting(f))
    current = f.bc.live_model_wait()
    assert current is not previous_owner and current.owner_id != previous_owner.owner_id
    assert f.decide(decision(previous)).status_code == 404
    assert "pending_action" not in waiting(f)
    assert len(f.engine.creates) == 5
    f.bc.stop()
    thread.join(5)


@pytest.mark.parametrize("override,problem_context,expected", [
    (None, {}, "configured-pin"), ("", {}, ""),
    (None, {"credentialProfileId": "proved-profile"}, "proved-profile"),
])
def test_auth_wait_profile_hint_preserves_intent_without_inventing_route(tmp_path, monkeypatch, override, problem_context, expected):
    monkeypatch.setenv("OUROBOROS_MODEL_ACCOUNTS", json.dumps({"main": "configured-pin"}))
    waiter = model_wait.TaskModelWait(task={"id": "actor"}, drive_root=tmp_path,
                                      event_queue=None, worker_slot_held=False,
                                      row_mutator=lambda key, fn: model_wait.mutate_live_wait(waiter, key, fn),
                                      owner_control=lambda: "stopped")
    error = transport.ClaudexorModelError({"code": "auth_required", "context": problem_context},
                                          model_role="main", route={})
    with pytest.raises(model_wait.ModelWaitInterrupted):
        waiter.wait(None, error, {"model": MODEL, "model_role": "main", "model_account_override": override})
    row, = waiter.waits.values()
    assert row["credential_profile_id"] == expected
    assert error.route == {}  # The UI hint is not claimed as an actual provider route.


@pytest.mark.serial
@pytest.mark.parametrize("surface", ["web", "host"])
def test_real_background_owner_is_shared_by_both_decision_transports(background, surface):
    from tests.test_model_wait import _decision_clients

    f = background
    thread = f.start()
    until(lambda: waiting(f))
    row = deepcopy(waiting(f))
    owner = f.bc.live_model_wait()
    with _decision_clients(f.root, f.bc.live_model_wait) as clients:
        response = clients[surface](decision(row, "switch", model=MODEL,
            credential_profile_id="replacement", use_local=False, persist_role=False))
        assert response.status_code == 202 and response.json()["saved"] is False
        thread.join(5)
        assert f.failures == [] and f.outcomes == [True] and owner.closed
        assert f.engine.uploads[-1][0]["account"] == {"mode": "pin", "profileId": "replacement"}
        stale = clients[surface](decision(row))
        assert stale.status_code == 409 and stale.json()["reason_code"] == "task_not_live"
    assert not (f.root / "task_results" / "bg-consciousness.json").exists()
