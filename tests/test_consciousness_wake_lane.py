"""A consciousness wake-up is an ordinary Main turn (Background Consciousness redesign, P1).

The direct lane admits a turn (registered in the census before the receipt
returns) and then executes it; a wake carries its facts as plain task
metadata — the origin label, the ledger category, the model role, the
withheld tools — and ``set_next_wakeup`` clamps into the configured bounds and
persists the choice on the runtime state. The label's journey through frames,
rows and replay is pinned in ``test_consciousness_initiator_label.py``.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import threading
import time
from types import SimpleNamespace
from unittest import mock

from supervisor import workers
from supervisor.active_activity import get_direct_activity_registry

TS = "2026-09-16T12:00:00Z"
WAKE_META = {
    "initiator": "consciousness", "usage_category": "consciousness", "wake_reason": "heartbeat",
    "consciousness_autonomy": "act", "model_role": "consciousness",
}


def _lane(monkeypatch, tmp_path, *, event_q=None, sent=None):
    from supervisor import message_bus, state

    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    shared = event_q if event_q is not None else queue.Queue()
    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(workers, "REPO_DIR", tmp_path / "repo")
    monkeypatch.setattr(workers, "get_event_q", lambda: shared)
    monkeypatch.setattr(workers, "send_with_budget",
                        lambda *a, **kw: (sent if sent is not None else []).append((a, kw)))
    monkeypatch.setattr(state, "load_state", lambda: {})
    monkeypatch.setattr(state, "budget_remaining", lambda *a, **kw: 100)
    monkeypatch.setattr(message_bus, "get_bridge", lambda: SimpleNamespace(send_chat_action=lambda *a, **kw: None))
    workers.open_repo_writer_admission()


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    return predicate()


# --- the lane: synchronous admission, threaded execution -----------------------


def test_wake_is_registered_before_the_receipt_returns_and_reports_its_end(monkeypatch, tmp_path):
    from ouroboros import agent as agent_module

    events = queue.Queue()
    _lane(monkeypatch, tmp_path, event_q=events)
    entered, release = threading.Event(), threading.Event()
    finished: list = []
    actors: list = []

    class Actor:
        def handle_task(self, task):
            self.task = task
            entered.set()
            assert release.wait(10)
            return [{"type": "send_message", "task_id": task["id"], "chat_id": task["chat_id"], "text": "a thought"}]

    def make_agent(**kwargs):
        actors.append(Actor())
        return actors[-1]

    monkeypatch.setattr(agent_module, "make_agent", make_agent)
    receipt = workers.handle_wake_direct(1, "wake text", dict(WAKE_META),
                                         on_finished=lambda tid, ok: finished.append((tid, ok)))
    assert receipt["admitted"] is True and receipt["reason"] == ""
    task_id = receipt["task_id"]
    # Registered synchronously: the census lists the wake before its body runs.
    entry = get_direct_activity_registry().get(task_id)
    assert entry is not None and entry.kind == "direct_chat" and entry.chat_id == 1
    assert entry.actor is actors[0]
    assert entered.wait(10)
    task = actors[0].task
    assert task["id"] == task_id and task["type"] == "task" and task["_is_direct_chat"] is True
    assert task["text"] == "wake text" and task["chat_id"] == 1
    for absent in ("_presence_turn", "delegation_role", "project_id", "parent_task_id", "root_task_id"):
        assert absent not in task
    for key, value in WAKE_META.items():
        assert task["metadata"][key] == value
    assert isinstance(task.get("task_contract"), dict)
    assert finished == []
    release.set()
    assert _wait_for(lambda: bool(finished))
    assert finished == [(task_id, True)]
    assert get_direct_activity_registry().get(task_id) is None
    # The drained final rides the turn's origin label and lane fact by value.
    drained = events.get(timeout=5)
    assert drained["initiator"] == "consciousness" and drained["_is_direct_chat"] is True
    assert drained["chat_id"] == 1


def test_wake_refusals_are_typed_and_start_nothing(monkeypatch, tmp_path):
    from ouroboros import agent as agent_module
    from supervisor import state

    sent: list = []
    _lane(monkeypatch, tmp_path, sent=sent)
    monkeypatch.setattr(agent_module, "make_agent", lambda **kw: (_ for _ in ()).throw(AssertionError("no actor")))
    monkeypatch.setattr(state, "budget_remaining", lambda *a, **kw: 0)
    assert workers.handle_wake_direct(1, "wake", dict(WAKE_META)) == {
        "admitted": False, "task_id": "", "reason": "budget_exhausted"}

    def unavailable(*a, **kw):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(state, "budget_remaining", unavailable)
    assert workers.handle_wake_direct(1, "wake", dict(WAKE_META))["reason"] == "cost_accounting_unavailable"
    monkeypatch.setattr(state, "budget_remaining", lambda *a, **kw: 100)
    workers.close_repo_writer_admission("test-update")
    try:
        assert workers.handle_wake_direct(1, "wake", dict(WAKE_META))["reason"] == "repo_writer_gate_closed"
    finally:
        workers.open_repo_writer_admission()
    assert get_direct_activity_registry().snapshot() == []
    # A closed gate refuses the wake QUIETLY: the owner's "🔒" lock notice is for the owner's turn.
    assert not [call for call in sent if "🔒" in str(call[0][1])]
    # A refused wake writes no budget/cost notice of its own into the chat.
    assert not [call for call in sent if "Budget" in str(call[0][1]) or "accounting" in str(call[0][1])]


def test_wake_runner_failure_reports_ok_false_and_concludes_the_turn(monkeypatch, tmp_path):
    from ouroboros import agent as agent_module

    sent: list = []
    _lane(monkeypatch, tmp_path, sent=sent)
    finished: list = []

    class Actor:
        def handle_task(self, task):
            raise RuntimeError("model unreachable")

    monkeypatch.setattr(agent_module, "make_agent", lambda **kw: Actor())
    receipt = workers.handle_wake_direct(1, "wake", dict(WAKE_META),
                                         on_finished=lambda tid, ok: finished.append((tid, ok)))
    assert receipt["admitted"] is True
    assert _wait_for(lambda: bool(finished))
    assert finished == [(receipt["task_id"], False)]
    assert get_direct_activity_registry().get(receipt["task_id"]) is None
    args, kwargs = sent[-1]
    assert args[0] == 1 and "model unreachable" in args[1]
    assert kwargs["task_id"] == receipt["task_id"]
    assert kwargs["progress_meta"] == {"task_terminal_status": "failed", "initiator": "consciousness"}
    rows = [json.loads(line) for line in (tmp_path / "logs" / "supervisor.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row["type"] == "direct_chat_error" and row["task_id"] == receipt["task_id"] for row in rows)


def test_owner_turn_still_runs_admission_and_execution_as_one_call(monkeypatch, tmp_path):
    from ouroboros import agent as agent_module

    events = queue.Queue()
    _lane(monkeypatch, tmp_path, event_q=events)

    class Actor:
        def handle_task(self, task):
            self.task = task
            return [{"type": "send_message", "task_id": task["id"], "chat_id": task["chat_id"], "text": "hi"}]

    actor = Actor()
    monkeypatch.setattr(agent_module, "make_agent", lambda **kw: actor)
    workers.handle_chat_direct(1, "hello", None, task_metadata={"client_message_id": "c-1"})
    assert actor.task["text"] == "hello" and "initiator" not in actor.task.get("metadata", {})
    assert get_direct_activity_registry().snapshot() == []
    drained = events.get(timeout=5)
    assert "initiator" not in drained and drained["_is_direct_chat"] is True


def test_turn_event_queue_stamps_the_initiator_on_the_turn_events_only():
    from supervisor.log_addressing import TurnEventQueue

    inner = queue.Queue()
    turn = TurnEventQueue(inner, "t1", 7, initiator="consciousness")
    own = {"type": "tool_call_started", "task_id": "t1"}
    turn.put(own)
    assert inner.get_nowait() is own
    assert own["initiator"] == "consciousness" and own["_is_direct_chat"] is True and own["chat_id"] == 7
    other = {"type": "tool_call_started", "task_id": "t2"}
    turn.put_nowait(other)
    assert "initiator" not in other
    wrapped = {"type": "log_event", "data": {"type": "task_heartbeat", "task_id": "t1", "initiator": "kept"}}
    turn.put(wrapped)
    assert wrapped["data"]["initiator"] == "kept"
    plain = TurnEventQueue(inner, "t1", 7)
    silent = {"type": "send_message", "task_id": "t1"}
    plain.put(silent)
    assert "initiator" not in silent and silent["_is_direct_chat"] is True


# --- the wake's metadata: withheld tools, model role, ledger category ---------


def test_metadata_disabled_tools_reach_the_registry_guard_through_the_contract(tmp_path):
    from ouroboros.contracts.task_contract import attach_task_contract
    from ouroboros.tools.registry_guards import _disabled_tools
    from ouroboros.tools.tool_context import ToolContext

    task = {"id": "w", "type": "task", "chat_id": 1, "text": "wake", "_is_direct_chat": True,
            "metadata": {**WAKE_META, "disabled_tools": ["toggle_evolution", "commit_reviewed"]}}
    attach_task_contract(task)
    assert {"toggle_evolution", "commit_reviewed"} <= set(task["task_contract"]["disabled_tools"])
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path,
                      task_metadata=task["metadata"], task_contract=task["task_contract"])
    assert {"toggle_evolution", "commit_reviewed"} <= set(_disabled_tools(ctx))


def test_model_role_resolves_the_consciousness_slots_and_falls_back_to_main():
    from ouroboros.agent_dispatch import _initial_effort_for, model_role_slot_override
    from ouroboros.model_slots import task_model_binding

    meta = {"model_role": "consciousness"}
    assert task_model_binding({"metadata": meta})[0] == "consciousness"
    assert task_model_binding({"metadata": {}})[0] == "main"
    with mock.patch.dict(os.environ, {"OUROBOROS_MODEL_CONSCIOUSNESS": "", "OUROBOROS_EFFORT_CONSCIOUSNESS": "low",
                                      "OUROBOROS_EFFORT_TASK": "medium", "USE_LOCAL_CONSCIOUSNESS": ""}):
        assert model_role_slot_override(meta) is None  # an empty slot is Main
        assert _initial_effort_for({"metadata": meta}, "task") == "low"
        assert _initial_effort_for({"metadata": {}}, "task") == "medium"
    # An empty EFFORT slot is Main's effort too (owner decision 16.09, 1=A): the wake shares
    # Main's request shape; a set value is honored.
    with mock.patch.dict(os.environ, {"OUROBOROS_MODEL_CONSCIOUSNESS": "", "OUROBOROS_EFFORT_CONSCIOUSNESS": "",
                                      "OUROBOROS_EFFORT_TASK": "xhigh"}):
        assert _initial_effort_for({"metadata": meta}, "task") == "xhigh"
    # An empty model slot still honors the role's OWN local flag when the owner set it and it
    # differs from Main's (В25=B: every slot is respected; astra round 4).
    with mock.patch.dict(os.environ, {"OUROBOROS_MODEL_CONSCIOUSNESS": "", "USE_LOCAL_CONSCIOUSNESS": "true",
                                      "USE_LOCAL_MAIN": "false", "OUROBOROS_MODEL": "openai/gpt-5.6-sol"}):
        assert model_role_slot_override(meta) == ("openai/gpt-5.6-sol", True)
    with mock.patch.dict(os.environ, {"OUROBOROS_MODEL_CONSCIOUSNESS": "", "USE_LOCAL_CONSCIOUSNESS": "false",
                                      "USE_LOCAL_MAIN": "true", "OUROBOROS_MODEL": "local/model"}):
        assert model_role_slot_override(meta) == ("local/model", False)
    with mock.patch.dict(os.environ, {"OUROBOROS_MODEL_CONSCIOUSNESS": "", "USE_LOCAL_CONSCIOUSNESS": "true",
                                      "USE_LOCAL_MAIN": "true", "OUROBOROS_MODEL": "local/model"}):
        assert model_role_slot_override(meta) is None  # equal flags: Main, the same prefix
    with mock.patch.dict(os.environ, {"OUROBOROS_MODEL_CONSCIOUSNESS": "openai/gpt-5.6-sol", "USE_LOCAL_CONSCIOUSNESS": "true"}):
        assert model_role_slot_override(meta) == ("openai/gpt-5.6-sol", True)
    with mock.patch.dict(os.environ, {"OUROBOROS_MODEL_CONSCIOUSNESS": "openai/gpt-5.6-sol", "USE_LOCAL_CONSCIOUSNESS": ""}):
        assert model_role_slot_override(meta) == ("openai/gpt-5.6-sol", False)
    assert model_role_slot_override({}) is None
    assert model_role_slot_override({"model_role": "main"}) is None
    assert model_role_slot_override({"model_role": "fallback"}) is None
    assert _initial_effort_for({"reasoning_effort": "xhigh", "metadata": meta}, "task") == "xhigh"


def test_prepare_task_context_applies_the_role_slot_through_the_tool_context_seam():
    """The agent pins ``ctx.task_model_override``/``task_use_local_override`` — the
    seam the loop already reads for a subagent's model — from the role slot."""
    import inspect

    from ouroboros import agent as agent_module

    source = inspect.getsource(agent_module.OuroborosAgent._prepare_task_context)
    assert "role_slot = model_role_slot_override(task_metadata)" in source
    assert "ctx.task_model_override, ctx.task_use_local_override = role_slot" in source


def test_handle_task_ledger_category_comes_from_usage_category(monkeypatch, tmp_path):
    from ouroboros import agent as agent_module
    from ouroboros import config, model_wait, subagent_runtime, usage_accounting

    captured: list = []

    @contextlib.contextmanager
    def fake_scope(scope):
        captured.append(scope)
        yield

    monkeypatch.setattr(usage_accounting, "usage_scope", fake_scope)
    monkeypatch.setattr(model_wait, "task_model_wait_scope", lambda **kw: contextlib.nullcontext())
    monkeypatch.setattr(subagent_runtime, "apply_task_start_settings_or_disclose", lambda *a, **k: None)
    monkeypatch.setattr(config, "task_settings_scope", lambda snapshot: contextlib.nullcontext())
    monkeypatch.setattr(agent_module.OuroborosAgent, "_handle_task_scoped", lambda self, task: [])
    agent = object.__new__(agent_module.OuroborosAgent)
    agent.env = SimpleNamespace(drive_root=tmp_path)
    agent._emit_live_log = lambda *a, **k: None
    agent._event_queue = None
    agent.handle_task({"id": "w1", "type": "task", "chat_id": 1, "text": "wake", "metadata": dict(WAKE_META)})
    agent.handle_task({"id": "o1", "type": "task", "chat_id": 1, "text": "hello"})
    agent.handle_task({"id": "e1", "type": "evolution", "chat_id": 1, "text": "evolve", "metadata": {"initiator": "consciousness"}})
    assert [(scope.task_id, scope.category) for scope in captured] == [
        ("w1", "consciousness"), ("o1", "task"), ("e1", "evolution")]


# --- set_next_wakeup: clamp, persist, honest reply --------------------------------


def test_set_next_wakeup_clamps_persists_and_speaks_honestly(tmp_path, monkeypatch):
    from ouroboros.tools import control, control_runtime
    from supervisor import state

    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "locks").mkdir(parents=True)
    state.init(tmp_path)
    assert control._set_next_wakeup is control_runtime._set_next_wakeup
    ctx = SimpleNamespace(task_id="w1")
    with mock.patch.dict(os.environ, {"OUROBOROS_BG_WAKEUP_MIN": "120", "OUROBOROS_BG_WAKEUP_MAX": "600"}):
        low = control._set_next_wakeup(ctx, 30)
        assert low.startswith("OK: consciousness is off") and "120 s" in low and "clamped into 120-600 s" in low
        assert state.load_state()["consciousness_next_interval_sec"] == 120
        high = control._set_next_wakeup(ctx, 99999)
        assert "600 s" in high and "clamped" in high
        assert state.load_state()["consciousness_next_interval_sec"] == 600
        state.update_state(lambda st: st.__setitem__("bg_consciousness_enabled", True))
        spoken = control._set_next_wakeup(ctx, 300)
        assert spoken.startswith("OK: the wake-up interval is now 300 s;") and "pending keeps its time" in spoken
        assert state.load_state()["consciousness_next_interval_sec"] == 300
        assert "TOOL_ARG_ERROR" in control._set_next_wakeup(ctx, "soon")
        assert state.load_state()["consciousness_next_interval_sec"] == 300


def test_a_wake_whose_thread_cannot_start_leaves_no_registered_turn(monkeypatch, tmp_path):
    """A registered turn nobody runs would read as a live owner turn forever (opus round 3)."""
    from ouroboros import agent as agent_module

    _lane(monkeypatch, tmp_path)
    monkeypatch.setattr(agent_module, "make_agent", lambda **kw: SimpleNamespace(handle_task=lambda task: []))

    def _refuse(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", _refuse)
    receipt = workers.handle_wake_direct(1, "wake", dict(WAKE_META))
    assert receipt == {"admitted": False, "task_id": "", "reason": "admission_failed"}
    assert get_direct_activity_registry().snapshot() == []
