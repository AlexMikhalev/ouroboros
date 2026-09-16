"""Authority levels of a consciousness wake-up and how they follow its work (P3).

Observe / Act / Full (owner decision В10', default Act) are carried as
``metadata.consciousness_autonomy`` and derived at task build into the
contract's ``disabled_tools`` and the per-task ``runtime_mode_cap`` (В21=A);
for a consciousness-origin task the disabled list binds at DISPATCH ONLY so the
wake's tool schemas and prompt prefix are byte-identical to an owner turn's
(В31=B, the I3 comparison below). The origin (label, ledger category, level)
is inherited by everything the wake starts; a wake speaks as a task through
``steer_task`` (ISSUER, PLAN 5.2a); ``/evolve off`` is sticky against the agent
tool (В12); a Full-level campaign stays inside the consciousness tree.
"""

from __future__ import annotations

import json
import pathlib
import types

import pytest

from ouroboros import consciousness_authority as ca
from ouroboros.tools.registry import ToolContext, ToolRegistry

WAKE_META = {
    "initiator": "consciousness", "usage_category": "consciousness",
    "wake_reason": "heartbeat", "consciousness_autonomy": "act", "model_role": "consciousness",
}


def _wake_task(level="act", **extra):
    task = {"id": "wake-1", "type": "task", "text": "wake", "_is_direct_chat": True, "chat_id": 1,
            "metadata": {**WAKE_META, "consciousness_autonomy": level, **extra}}
    return ca.apply_consciousness_authority(task)


def _registry(tmp_path, metadata=None, *, task_id="turn-1"):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "README.md").write_text("ok\n", encoding="utf-8")
    drive = tmp_path / "drive"
    drive.mkdir(exist_ok=True)
    reg = ToolRegistry(repo_dir=repo, drive_root=drive)
    reg.set_context(ToolContext(
        repo_dir=repo, drive_root=drive, task_id=task_id, is_direct_chat=True,
        task_metadata=dict(metadata or {}),
    ))
    return reg


# --- the level tables -------------------------------------------------------------


def test_levels_and_their_two_consequences():
    assert ca.LEVELS == ("observe", "act", "full")
    assert ca.disabled_tools_for("full") == []
    assert ca.disabled_tools_for("act") == list(ca.ACT_DISABLED)
    observe = ca.disabled_tools_for("observe")
    assert set(ca.ACT_DISABLED) <= set(observe)
    assert {"promote_chat_to_task", "schedule_subagent", "write_file", "run_command",
            "browser_action", "initiate_presence", "submit_skill_to_hub"} <= set(observe)
    # The nanny of a running campaign is never withheld, at any level.
    assert "steer_task" not in observe and "steer_task" not in ca.disabled_tools_for("act")
    # Observe is an EXCEPTION list: reading and talking stay available by default.
    for name in ("read_file", "web_search", "browse_page", "send_user_message", "escalate",
                 "knowledge_write", "update_scratchpad", "switch_model", "enable_tools"):
        assert name not in observe
    assert ca.runtime_mode_cap_for("act") == "light" == ca.runtime_mode_cap_for("observe")
    assert ca.runtime_mode_cap_for("full") == ""


def test_unknown_level_falls_back_to_the_owner_setting(monkeypatch):
    monkeypatch.setenv("OUROBOROS_CONSCIOUSNESS_AUTONOMY", "observe")
    assert ca.normalize_level("bogus") == "observe"
    assert ca.normalize_level("") == "observe"
    assert ca.normalize_level("FULL") == "full"


def test_observe_table_covers_every_registry_entry_marked_mutates_worktree(tmp_path):
    """The registry marker is the second source of the same fact; the table cannot drift."""
    reg = _registry(tmp_path)
    marked = {e.name for e in reg._entries.values() if e.mutates_worktree and not e.alias_for}
    assert marked, "the catalog carries mutates_worktree entries"
    missing = marked - set(ca.OBSERVE_DISABLED)
    assert not missing, f"mutates_worktree entries missing from OBSERVE_WORLD_MUTATION_TOOLS: {sorted(missing)}"
    unknown = set(ca.OBSERVE_DISABLED) - {e.name for e in reg._entries.values()}
    # Skill/project tools are registered lazily (skills, journal); the built-in names must exist.
    assert unknown <= {"toggle_skill", "skill_owner_action", "journal_write", "workpad_write",
                       "configure_presence", "initiate_presence", "delegate_start"}, sorted(unknown)


# --- derivation at task build ---------------------------------------------------


def test_apply_consciousness_authority_derives_both_consequences_once():
    task = _wake_task("act")
    assert task["metadata"]["disabled_tools"] == list(ca.ACT_DISABLED)
    assert task["metadata"]["runtime_mode_cap"] == "light"
    full = _wake_task("full")
    assert full["metadata"]["disabled_tools"] == [] and full["metadata"]["runtime_mode_cap"] == ""
    # An explicit producer list stands; an owner turn is untouched.
    explicit = _wake_task("act", disabled_tools=["web_search"])
    assert explicit["metadata"]["disabled_tools"] == ["web_search"]
    owner = ca.apply_consciousness_authority({"id": "o", "metadata": {"client_message_id": "cm"}})
    assert "disabled_tools" not in owner["metadata"] and "runtime_mode_cap" not in owner["metadata"]


def test_contract_carries_the_derived_list_and_origin_helpers():
    from ouroboros.contracts.task_contract import attach_task_contract

    task = attach_task_contract(_wake_task("observe"))
    assert task["task_contract"]["disabled_tools"] == ca.disabled_tools_for("observe")
    assert "toggle_evolution" in ca.task_disabled_tools(task)
    origin = ca.consciousness_origin_metadata(task["metadata"])
    assert origin == {"initiator": "consciousness", "usage_category": "consciousness_task",
                      "consciousness_autonomy": "observe"}
    assert ca.consciousness_origin_metadata({"client_message_id": "cm"}) == {}
    assert ca.is_consciousness_origin(origin) and not ca.is_consciousness_origin(None)


@pytest.mark.parametrize(("install", "cap", "expected"), [
    ("cyber_pro", "light", "light"), ("pro", "light", "light"), ("advanced", "light", "light"),
    ("light", "light", "light"), ("light", "advanced", "light"), ("cyber_pro", "", "cyber_pro"),
    ("pro", "bogus", "pro"),
])
def test_effective_runtime_mode_is_the_stricter_of_install_and_cap(install, cap, expected):
    assert ca.effective_runtime_mode(install, {"runtime_mode_cap": cap}) == expected
    assert ca.effective_runtime_mode(install, None) == install


# --- dispatch-only enforcement (В31=B) -------------------------------------------


def test_consciousness_contract_keeps_the_full_schema_set_and_refuses_at_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    main = _registry(tmp_path, {"client_message_id": "cm-1"})
    wake = _registry(tmp_path, _wake_task("act")["metadata"])
    # Same schemas, same advertised names, same initial envelope, same omission manifest.
    assert wake.schemas() == main.schemas()
    assert wake.available_tools() == main.available_tools()
    assert wake.initial_tool_names() == main.initial_tool_names()
    assert wake.capability_omissions() == main.capability_omissions()
    assert not any(item.get("reason") == "disabled_by_contract" for item in wake.capability_omissions())
    assert "toggle_evolution" in wake.available_tools()
    assert wake.get_schema_by_name("toggle_evolution") is not None
    assert wake.policy_hidden_reason("toggle_evolution") is None
    # The dispatcher is the mechanism: the withheld name is refused with the typed block.
    result = wake.execute("toggle_evolution", {"enabled": True, "objective": "x"})
    assert "RESOURCE_CONSTRAINT_BLOCKED" in result and "toggle_evolution" in result
    for name, args in (("request_restart", {}), ("set_tool_timeout", {"seconds": 30}),
                       ("toggle_consciousness", {"action": "stop"})):
        assert "RESOURCE_CONSTRAINT_BLOCKED" in wake.execute(name, args), name


def test_an_ordinary_contract_still_hides_its_disabled_tools(tmp_path):
    """The dispatch-only case is the consciousness special case, not a general change."""
    reg = _registry(tmp_path, {"disabled_tools": ["toggle_evolution"]})
    assert "toggle_evolution" not in reg.available_tools()
    assert all(s["function"]["name"] != "toggle_evolution" for s in reg.schemas())
    assert reg.get_schema_by_name("toggle_evolution") is None
    assert reg.policy_hidden_reason("toggle_evolution") == "disabled by this task's contract (disabled_tools)"
    assert any(item.get("reason") == "disabled_by_contract" for item in reg.capability_omissions())


def test_i3_serialized_request_prefix_matches_an_owner_turn(tmp_path, monkeypatch):
    """The provider request's cached prefix — the tool schema array and the two
    cached system blocks up to the dynamic boundary (context_fit) — is byte-identical
    for an owner turn and for a wake at Act and at Observe built from one snapshot."""
    from ouroboros.context import build_llm_messages
    from tests.test_cache_optimization import _make_env_and_memory

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    env, memory = _make_env_and_memory(tmp_path)
    owner = {"id": "t-owner", "type": "task", "text": "hi", "_is_direct_chat": True, "chat_id": 1,
             "metadata": {"client_message_id": "cm-1"}}
    owner_msgs, _ = build_llm_messages(env=env, memory=memory, task=owner)
    owner_prefix = [json.dumps(owner_msgs[0]["content"][i], sort_keys=True) for i in (0, 1)]
    owner_reg = _registry(tmp_path, owner["metadata"], task_id="t-owner")
    owner_tools = json.dumps(owner_reg.schemas(), sort_keys=True)
    for level in ("act", "observe"):
        task = _wake_task(level)
        task.update(id="t-owner", text="hi")  # the same turn: the wake differs only in its metadata
        msgs, _ = build_llm_messages(env=env, memory=memory, task=task)
        prefix = [json.dumps(msgs[0]["content"][i], sort_keys=True) for i in (0, 1)]
        assert prefix == owner_prefix, level
        assert "cache_control" not in msgs[0]["content"][2]
        reg = _registry(tmp_path, task["metadata"], task_id="t-owner")
        assert json.dumps(reg.schemas(), sort_keys=True) == owner_tools, level
        assert reg.capability_omissions() == owner_reg.capability_omissions(), level


# --- the per-task mode cap (В21=A): level x install mode --------------------------


_BLOCKED_CALLS = (
    ("write_file", {"path": "README.md", "content": "changed\n"}),
    ("commit_reviewed", {"commit_message": "test"}),
    ("run_command", {"cmd": "touch x.py"}),
    ("start_service", {"cmd": ["sleep", "5"], "name": "svc"}),
)


@pytest.mark.parametrize("mode", ["light", "advanced", "pro", "cyber_pro"])
@pytest.mark.parametrize("level", ["act", "observe"])
@pytest.mark.parametrize(("tool_name", "args"), _BLOCKED_CALLS)
def test_act_and_observe_cannot_touch_the_repo_in_any_install_mode(tmp_path, monkeypatch, mode, level, tool_name, args):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", mode)
    reg = _registry(tmp_path, _wake_task(level)["metadata"])
    result = reg.execute(tool_name, dict(args))
    expected = "RESOURCE_CONSTRAINT_BLOCKED" if level == "observe" and tool_name != "commit_reviewed" else "LIGHT_MODE_BLOCKED"
    assert expected in result, (mode, level, tool_name, result[:300])
    assert not (tmp_path / "repo" / "x.py").exists()
    assert (tmp_path / "repo" / "README.md").read_text(encoding="utf-8") == "ok\n"


@pytest.mark.parametrize("mode", ["advanced", "pro", "cyber_pro"])
def test_full_follows_the_install_mode(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", mode)
    reg = _registry(tmp_path, _wake_task("full")["metadata"])
    assert "LIGHT_MODE_BLOCKED" not in reg.execute("write_file", {"path": "scratch.txt", "content": "changed\n"})
    assert (tmp_path / "repo" / "scratch.txt").read_text(encoding="utf-8") == "changed\n"
    assert "LIGHT_MODE_BLOCKED" not in reg.execute("run_command", {"cmd": "touch x.py"})


def test_full_in_a_light_install_is_still_light(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path, _wake_task("full")["metadata"])
    assert "LIGHT_MODE_BLOCKED" in reg.execute("write_file", {"path": "README.md", "content": "x"})


def test_act_keeps_the_light_positive_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")
    reg = _registry(tmp_path, _wake_task("act")["metadata"])
    for root in ("task_drive", "artifact_store"):
        result = reg.execute("write_file", {"root": root, "path": "notes.txt", "content": "kept\n"})
        assert "BLOCKED" not in result, (root, result[:300])
    assert "BLOCKED" not in reg.execute("read_file", {"path": "README.md"})


# --- the wake through the real lane --------------------------------------------


def test_the_lane_attaches_the_level_to_the_wake_contract(monkeypatch, tmp_path):
    import queue
    import threading

    from ouroboros import agent as agent_module
    from supervisor import workers
    from tests.test_consciousness_wake_lane import _lane, _wait_for

    _lane(monkeypatch, tmp_path, event_q=queue.Queue())
    seen: list = []
    done = threading.Event()

    class Actor:
        def handle_task(self, task):
            seen.append(task)
            done.set()
            return []

    monkeypatch.setattr(agent_module, "make_agent", lambda **kw: Actor())
    receipt = workers.handle_wake_direct(1, "wake", {**WAKE_META, "consciousness_autonomy": "act"})
    assert receipt["admitted"] is True
    assert done.wait(10) and _wait_for(lambda: bool(seen))
    task = seen[0]
    assert task["task_contract"]["disabled_tools"] == list(ca.ACT_DISABLED)
    assert task["metadata"]["runtime_mode_cap"] == "light"


# --- ISSUER: a wake speaks as a task -------------------------------------------


def test_routing_issuer_treats_a_wake_as_a_task_but_keeps_real_owner_relays(tmp_path):
    from ouroboros.tools.control_routing import ISSUER_OWNER_TURN, ISSUER_TASK, _routing_issuer

    wake = types.SimpleNamespace(task_id="wake-1", is_direct_chat=True, last_owner_delivery=None,
                                 task_metadata=dict(_wake_task("act")["metadata"]))
    assert _routing_issuer(wake) == {"kind": ISSUER_TASK, "task_id": "wake-1", "root_task_id": "wake-1"}
    owner = types.SimpleNamespace(task_id="turn-1", is_direct_chat=True, last_owner_delivery=None,
                                  task_metadata={"client_message_id": "cm-1"})
    assert _routing_issuer(owner) == {"kind": ISSUER_OWNER_TURN}
    # The two other triggers stay: a consciousness root relaying a REAL owner message.
    relaying = types.SimpleNamespace(task_id="c-root", is_direct_chat=False,
                                     last_owner_delivery={"client_message_id": "cm-9", "text": "go"},
                                     task_metadata={"initiator": "consciousness"})
    assert _routing_issuer(relaying) == {"kind": ISSUER_OWNER_TURN}
    stamped = types.SimpleNamespace(task_id="c-root", is_direct_chat=True, last_owner_delivery=None,
                                    task_metadata={"initiator": "consciousness", "client_message_id": "cm-2"})
    assert _routing_issuer(stamped) == {"kind": ISSUER_OWNER_TURN}


def test_steer_from_a_wake_is_written_as_an_independent_task_message(tmp_path, monkeypatch):
    from ouroboros.tools import control_routing

    sent: list = []
    monkeypatch.setattr(control_routing, "_send_task_message",
                        lambda ctx, issuer, target, msg, chat_id: sent.append((issuer, target, msg)) or "WRITTEN")
    ctx = types.SimpleNamespace(
        pending_events=[], event_queue=None, current_chat_id=1, drive_root=tmp_path,
        task_id="wake-1", is_direct_chat=True, last_owner_delivery=None,
        task_metadata=dict(_wake_task("act")["metadata"]),
    )
    assert control_routing._steer_task(ctx, task_id="r-1", message="please also check X") == "WRITTEN"
    assert sent == [({"kind": "task", "task_id": "wake-1", "root_task_id": "wake-1"}, "r-1", "please also check X")]
    assert ctx.pending_events == []


# --- origin inheritance: promote / followup / subagent -------------------------


@pytest.fixture
def _promote_root(tmp_path, monkeypatch):
    """The real promote admission path (tool -> supervisor handler -> worker_promotion)."""
    import ouroboros.config as cfg
    import supervisor.message_bus as mb
    import supervisor.queue as queue_mod
    from supervisor import workers

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue_mod, "DRIVE_ROOT", str(tmp_path))
    monkeypatch.setattr(queue_mod, "ACCEPTANCE_FENCES", {})
    monkeypatch.setattr(mb, "get_bridge", lambda: types.SimpleNamespace(broadcast=lambda payload: None))
    monkeypatch.setattr(workers, "_announce_created_project", lambda *a, **kw: None)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_promote_from_a_wake_mints_a_consciousness_root_through_the_real_admission(_promote_root):
    from ouroboros.tools.control_routing import _promote_chat_to_task
    from ouroboros.utils import append_jsonl
    from supervisor.events_project_routing import _handle_promote_chat_to_task

    tmp_path = _promote_root
    enqueued: list = []
    captured: dict = {}
    supervisor = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path, RUNNING={}, PENDING=[], WORKERS={0: types.SimpleNamespace()},
        bridge=types.SimpleNamespace(send_routing_ack=lambda *a, **k: None, broadcast=lambda *a, **k: None),
        enqueue_task=lambda task: enqueued.append(task) or dict(task),
        persist_queue_snapshot=lambda **_k: True, load_state=lambda: {"owner_chat_id": 1},
        append_jsonl=append_jsonl,
    )
    ctx = types.SimpleNamespace(
        pending_events=[], current_chat_id=1, drive_root=tmp_path, budget_drive_root=str(tmp_path),
        task_id="wake-1", is_direct_chat=True, last_owner_delivery=None, project_id="",
        task_metadata=dict(_wake_task("act")["metadata"]), task_contract={},
        event_queue=types.SimpleNamespace(
            put_nowait=lambda event: (captured.update(event), _handle_promote_chat_to_task(event, supervisor))),
    )
    out = _promote_chat_to_task(ctx, "audit the logs", workspace="none", predecessor_task_id="")
    assert out.startswith("OK: task"), out
    # The event carries the origin by value and nothing the wake asked for is stripped.
    assert captured["initiator"] == "consciousness" and captured["consciousness_autonomy"] == "act"
    assert captured["usage_category"] == "consciousness_task" and captured["workspace"] == "none"
    assert "presence" not in captured
    [root] = enqueued
    assert root["actor_id"] == "consciousness" and root["delegation_role"] == "root"
    assert root["metadata"]["initiator"] == "consciousness"
    assert root["metadata"]["usage_category"] == "consciousness_task"
    assert root["task_contract"]["disabled_tools"] == list(ca.ACT_DISABLED)
    assert root["metadata"]["runtime_mode_cap"] == "light" and "_presence_origin" not in root


def test_promoted_root_is_stamped_and_its_contract_derives_the_level(tmp_path, monkeypatch):
    import supervisor.workers as workers

    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    enqueued: list = []

    def enqueue(task):
        enqueued.append(task)
        return dict(task)

    ctx = types.SimpleNamespace(enqueue_task=enqueue, persist_queue_snapshot=lambda **_k: True,
                                load_state=lambda: {"owner_chat_id": 1})
    evt = {"type": "promote_chat_to_task", "task_id": "c0000001", "objective": "audit the logs",
           "chat_id": 1, "workspace": "none", "initiator": "consciousness",
           "usage_category": "consciousness_task", "consciousness_autonomy": "act"}
    assert workers.promote_chat_to_task(evt, ctx)["status"] == "scheduled"
    task = enqueued[0]
    assert task["actor_id"] == "consciousness" and task["delegation_role"] == "root"
    assert task["metadata"]["initiator"] == "consciousness"
    assert task["metadata"]["usage_category"] == "consciousness_task"
    assert task["metadata"]["consciousness_autonomy"] == "act"
    assert task["task_contract"]["disabled_tools"] == list(ca.ACT_DISABLED)
    assert task["metadata"]["runtime_mode_cap"] == "light"
    assert task["source"] == "promote_chat_to_task" and "_presence_origin" not in task


def test_followup_template_inherits_the_origin_and_admission_derives_the_level(tmp_path, monkeypatch):
    from supervisor import queue
    from tests.test_schedule_followup import _ctx, _followup

    ctx = _ctx(tmp_path)
    ctx.task_metadata.update(_wake_task("act")["metadata"])
    assert _followup(ctx).startswith("FOLLOWUP_SCHEDULED")
    record = queue.list_scheduled_tasks(tmp_path / "data")["tasks"][0]
    meta = record["task"]["metadata"]
    assert meta["initiator"] == "consciousness" and meta["usage_category"] == "consciousness_task"
    assert meta["consciousness_autonomy"] == "act" and "task_contract" not in record["task"]
    monkeypatch.setattr(queue, "load_state", lambda: {"owner_chat_id": 1})
    task = queue._task_from_schedule(record)
    assert task["delegation_role"] == "root" and task["metadata"]["initiator"] == "consciousness"
    assert task["task_contract"]["disabled_tools"] == list(ca.ACT_DISABLED)
    assert task["metadata"]["runtime_mode_cap"] == "light"


def test_owner_followup_template_carries_no_origin(tmp_path):
    from supervisor import queue
    from tests.test_schedule_followup import _ctx, _followup

    assert _followup(_ctx(tmp_path)).startswith("FOLLOWUP_SCHEDULED")
    meta = queue.list_scheduled_tasks(tmp_path / "data")["tasks"][0]["task"]["metadata"]
    assert "initiator" not in meta and "consciousness_autonomy" not in meta


def test_subagent_payload_lands_the_origin_on_the_child_metadata():
    from supervisor.task_dispatch import build_scheduled_task_payload

    fields = {"tid": "kid1", "chat_id": 1, "text": "x", "desc": "x", "role": "researcher",
              "root_task_id": "wake-1", "delegation_role": "subagent", "actor_id": "subagent:researcher",
              "origin_metadata": ca.consciousness_origin_metadata(_wake_task("act")["metadata"])}
    task = build_scheduled_task_payload(fields)
    assert task["metadata"]["initiator"] == "consciousness"
    assert task["metadata"]["usage_category"] == "consciousness_task"
    assert task["metadata"]["consciousness_autonomy"] == "act"
    plain = build_scheduled_task_payload({**fields, "origin_metadata": {}})
    assert "initiator" not in plain["metadata"]


def test_schedule_subagent_event_names_the_origin():
    """The tool stamps ``origin_metadata`` on the schedule event beside the envelope."""
    source = pathlib.Path("ouroboros/tools/control_scheduling.py").read_text(encoding="utf-8")
    assert '"origin_metadata": consciousness_origin_metadata(metadata),' in source
    handler = pathlib.Path("supervisor/events_schedule_task.py").read_text(encoding="utf-8")
    assert '"origin_metadata": evt.get("origin_metadata"),' in handler


# --- evolution: eligibility, sticky owner stop, campaign provenance ------------


def test_post_task_promotion_is_refused_when_toggle_evolution_is_withheld():
    from ouroboros.post_task_evolution import _eligible

    assert _eligible({"type": "task"}) is True
    assert _eligible({"type": "task", "task_contract": {"disabled_tools": ["toggle_evolution"]}}) is False
    assert _eligible({"type": "task", "metadata": {"disabled_tools": ["toggle_evolution"]}}) is False
    from ouroboros.contracts.task_contract import attach_task_contract

    assert _eligible(attach_task_contract(_wake_task("act"))) is False
    assert _eligible(attach_task_contract(_wake_task("full"))) is True


def test_globalized_promotion_view_keeps_the_contract(tmp_path, monkeypatch):
    from ouroboros import agent_task_pipeline as pipeline
    from ouroboros.contracts.task_contract import attach_task_contract

    seen: list = []
    monkeypatch.setattr(pipeline, "_update_improvement_backlog", lambda env, entry: None)
    monkeypatch.setattr("ouroboros.post_task_evolution.maybe_promote",
                        lambda env, task, entry, llm: seen.append(task))
    task = attach_task_contract({**_wake_task("act"), "project_id": "lab"})
    pipeline._run_global_backlog_promotion_only(
        types.SimpleNamespace(drive_root=tmp_path), task,
        {"backlog_candidates": [{"summary": "tidy the logs"}]}, None,
    )
    assert seen and seen[0]["task_contract"]["disabled_tools"] == list(ca.ACT_DISABLED)


def test_request_file_and_pending_apply_carry_the_origin(tmp_path, monkeypatch):
    from ouroboros import post_task_evolution as pte

    task = _wake_task("full")
    pte._write_request(tmp_path, {"objective": "improve X", "requires_plan_review": False}, task)
    req = json.loads((tmp_path / pte._REQUEST_REL).read_text(encoding="utf-8"))
    assert req["initiator"] == "consciousness" and req["consciousness_autonomy"] == "full"
    assert req["usage_category"] == "consciousness_task"
    calls: list = []
    monkeypatch.setattr("ouroboros.config.get_post_task_evolution_enabled", lambda: True)
    monkeypatch.setattr("supervisor.evolution_lifecycle.evolution_block_reason", lambda: "")
    monkeypatch.setattr("supervisor.evolution_lifecycle.start_evolution_campaign",
                        lambda objective, source="", **kw: calls.append((objective, source, kw)) or {"id": "c1"})
    monkeypatch.setattr("supervisor.state.load_state", lambda: {"owner_chat_id": 7})

    def _update_state(mutator):
        live: dict = {}
        mutator(live)
        return live

    monkeypatch.setattr("supervisor.state.update_state", _update_state)
    monkeypatch.setattr("ouroboros.config.get_post_task_evolution_budget_usd", lambda: 0.0)
    assert pte.apply_pending_request(tmp_path) is True
    assert calls == [("improve X", "post_task", {"origin": {
        "initiator": "consciousness", "usage_category": "consciousness_task", "consciousness_autonomy": "full"}})]


def _toggle_ctx(state, sent):
    return types.SimpleNamespace(load_state=state.load_state,
                                 send_with_budget=lambda cid, text, **kw: sent.append(text))


def test_agent_tool_enable_is_refused_while_the_owner_stop_stands(tmp_path, monkeypatch):
    """В12: /evolve off is sticky against toggle_evolution — the typed refusal, no campaign."""
    import supervisor.state as state
    from supervisor import events as events_mod
    from supervisor import evolution_lifecycle as el

    state.init(tmp_path)
    state.update_state(lambda live: live.update(owner_chat_id=7, evolution_owner_stopped=True))
    started: list = []
    monkeypatch.setattr(el, "evolution_block_reason", lambda: "")
    monkeypatch.setattr(el, "start_evolution_campaign",
                        lambda objective, source="", **kw: started.append(source) or {"status": "active"})
    sent: list = []
    events_mod._handle_toggle_evolution({"enabled": True, "objective": "x"}, _toggle_ctx(state, sent))
    assert started == []
    assert bool(state.load_state().get("evolution_owner_stopped")) is True
    assert not state.load_state().get("evolution_mode_enabled")
    assert sent and "stayed OFF" in sent[0] and "sticky" in sent[0]


def test_agent_tool_enable_without_an_owner_stop_starts_a_campaign_with_the_origin(tmp_path, monkeypatch):
    import supervisor.state as state
    from supervisor import events as events_mod
    from supervisor import evolution_lifecycle as el

    state.init(tmp_path)
    state.update_state(lambda live: live.update(owner_chat_id=7, evolution_owner_stopped=False))
    started: list = []
    monkeypatch.setattr(el, "evolution_block_reason", lambda: "")
    monkeypatch.setattr(el, "start_evolution_campaign",
                        lambda objective, source="", **kw: started.append((source, kw)) or {"status": "active"})
    sent: list = []
    evt = {"enabled": True, "objective": "x", "initiator": "consciousness",
           "usage_category": "consciousness_task", "consciousness_autonomy": "full"}
    events_mod._handle_toggle_evolution(evt, _toggle_ctx(state, sent))
    assert started == [("agent_tool", {"origin": {
        "initiator": "consciousness", "usage_category": "consciousness_task", "consciousness_autonomy": "full"}})]
    live = state.load_state()
    assert live["evolution_mode_enabled"] is True and live["evolution_owner_stopped"] is False


def test_toggle_tool_stamps_the_turn_origin_on_its_event(monkeypatch):
    from ouroboros.tools.control_runtime import _toggle_evolution

    monkeypatch.setattr("supervisor.evolution_lifecycle.evolution_block_reason", lambda: "")
    ctx = types.SimpleNamespace(pending_events=[], task_metadata=dict(_wake_task("full")["metadata"]))
    assert _toggle_evolution(ctx, True, "improve X").startswith("OK")
    evt = ctx.pending_events[0]
    assert evt["type"] == "toggle_evolution" and evt["initiator"] == "consciousness"
    assert evt["consciousness_autonomy"] == "full"
    owner = types.SimpleNamespace(pending_events=[], task_metadata={"client_message_id": "cm"})
    _toggle_evolution(owner, True, "improve X")
    assert "initiator" not in owner.pending_events[0]


def test_campaign_keeps_the_origin_and_its_cycle_tasks_inherit_it(tmp_path, monkeypatch):
    from supervisor import evolution_lifecycle, queue, state

    state.init(tmp_path)
    queue.init(tmp_path)
    pending: list = []
    queue.init_queue_refs(pending, {}, {"value": 0})
    monkeypatch.setattr(state, "TOTAL_BUDGET_LIMIT", 0.0)
    origin = {"initiator": "consciousness", "usage_category": "consciousness_task", "consciousness_autonomy": "full"}
    campaign = evolution_lifecycle.start_evolution_campaign("Improve", source="agent_tool", origin=origin)
    assert campaign["initiator"] == "consciousness" and campaign["consciousness_autonomy"] == "full"
    # A resume keeps the recorded origin (the owner's later resume does not erase it).
    campaign["status"] = "paused"
    assert evolution_lifecycle._write_evolution_campaign(campaign) is True
    resumed = evolution_lifecycle.start_evolution_campaign("", source="owner_chat")
    assert resumed["initiator"] == "consciousness"
    state.update_state(lambda live: live.update(owner_chat_id=1, evolution_mode_enabled=True,
                                                evolution_owner_stopped=False))
    monkeypatch.setattr(evolution_lifecycle, "evolution_block_reason", lambda: "")
    monkeypatch.setattr(queue, "send_with_budget", lambda *a, **k: None)
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": None)
    monkeypatch.setattr("ouroboros.consciousness_allowance.allowance_window",
                        lambda root, now=None: {"status": "available", "limit_usd": 20.0, "accounted_usd": 0.0,
                                                "remaining_usd": 20.0, "unknown_unmetered": 0, "resets_at": ""})
    queue.enqueue_evolution_task_if_needed()
    assert len(pending) == 1
    task = pending[0]
    assert task["type"] == "evolution" and task["metadata"]["initiator"] == "consciousness"
    assert task["metadata"]["usage_category"] == "consciousness_task"
    assert task["task_contract"]["disabled_tools"] == [] and task["metadata"]["runtime_mode_cap"] == ""


def test_owner_campaign_carries_no_origin(tmp_path):
    from supervisor import evolution_lifecycle, queue, state

    state.init(tmp_path)
    queue.init(tmp_path)
    campaign = evolution_lifecycle.start_evolution_campaign("Improve", source="owner_chat")
    assert "initiator" not in campaign
    assert ca.consciousness_origin_metadata(campaign) == {}
