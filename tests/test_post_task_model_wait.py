"""Post-task live custody without rerunning answered work or completed stages."""

from copy import deepcopy
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from ouroboros import agent_task_pipeline as pipeline, config, model_wait
from ouroboros import llm_claudexor as transport
from ouroboros.post_task_checkpoint import post_task_model_wait, post_task_model_waits
from ouroboros.task_results import load_task_result, write_task_result
from tests.test_llm_claudexor import Gateway, MODEL, result, ledger


def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("controlled post-task state did not arrive")


@pytest.fixture
def phase(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("TOTAL_BUDGET", "100")
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(root / "settings.json"))
    monkeypatch.setattr(config, "CLAUDEXOR_MODEL_POLL_INTERVAL_SEC", 0.005)
    monkeypatch.setattr(config, "NETWORK_WAIT_BACKOFF_START_SEC", 0.005)
    monkeypatch.setattr(config, "NETWORK_WAIT_BACKOFF_MAX_SEC", 0.01)
    task = {"id": "post-owner", "root_task_id": "post-owner", "chat_id": 1, "type": "task", "text": "Already answered", "drive_root": str(root)}
    write_task_result(root, task["id"], "completed", result="Already answered",
                      root_phase_checkpoint={"post_task_synthesis": "pending_once"})
    env = SimpleNamespace(drive_root=root, repo_dir=tmp_path, drive_path=lambda rel: root / rel)
    events, done, ready = queue.Queue(), threading.Event(), threading.Event()
    engine = Gateway([result(outcome="failed", problem={"code": "subscription_window_exhausted", "message": "quota"}), result()],
                     ["not_started", "response_received"])
    monkeypatch.setattr(transport, "ensure_owned_gateway", lambda: engine)
    monkeypatch.setattr(transport, "model_sources", lambda: {"sources": [{"id": "codex", "credentialHarness": "fixture"}]})
    monkeypatch.setattr(transport, "model_catalog", lambda _source, account=None, *, requested_model=None: {
        "source": "codex", "credentialProfileId": account or "account-a",
        "models": [{"id": "exact-model"}] if ready.is_set() else []})
    stages = []
    monkeypatch.setattr(pipeline, "_run_chat_consolidation", lambda *a: stages.append("chat"))
    monkeypatch.setattr(pipeline, "_run_scratchpad_consolidation", lambda *a: stages.append("scratch"))
    monkeypatch.setattr(pipeline, "_run_task_summary", lambda *a, **k: stages.append("summary"))
    monkeypatch.setattr(pipeline, "_update_improvement_backlog", lambda *a: stages.append("backlog"))
    monkeypatch.setattr(pipeline, "_apply_reflection_memory_actions", lambda *a, **k: None)
    monkeypatch.setattr("ouroboros.post_task_evolution.maybe_promote", lambda *a: None)

    def reflect(_env, llm, *args, **kwargs):
        stages.append("reflection")
        llm.chat([{"role": "user", "content": "reflect once after completed prior stages"}], MODEL, model_role="light")
        return {"reflection": "settled"}

    monkeypatch.setattr(pipeline, "_run_reflection", reflect)
    original = pipeline._set_root_post_task_checkpoint

    def checkpoint(*args, **kwargs):
        saved = original(*args, **kwargs)
        if args[2] in {"completed", "degraded"}:
            done.set()
        return saved

    monkeypatch.setattr(pipeline, "_set_root_post_task_checkpoint", checkpoint)
    yield SimpleNamespace(root=root, env=env, task=task, events=events, engine=engine,
                          ready=ready, done=done, stages=stages)
    task["_skip_post_task_synthesis"] = True
    ready.set()
    done.wait(5)
    until(lambda: not post_task_model_waits(root))


def launch(f):
    return pipeline._run_post_task_processing_async(f.env, f.task, {"rounds": 3}, {}, {}, f.root / "logs", event_queue=f.events)


def active(f):
    owner = post_task_model_wait(f.root, f.task["id"])
    return owner if owner and any(row["state"] == "waiting" and row.get("credential_harness") for row in owner.waits.values()) else None


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_history_keeps_answered_post_phase_open_but_never_revives_cancelled(tmp_path, status):
    from ouroboros.gateway.history import _annotate_terminal_task_truth

    result = {"status": status, "root_phase_checkpoint": {"post_task_synthesis": "running"}}
    messages = [{"task_id": "root", "role": "system", "system_type": "task_model_wait"}]
    _annotate_terminal_task_truth(messages, tmp_path, {"root": result})
    assert (messages[0].get("task_phase") == "finalizing") == (status != "cancelled")
    assert messages[0].get("task_terminal_status") == ("cancelled" if status == "cancelled" else None)
    assert result["status"] == status


@pytest.mark.parametrize("main_status", ["completed", "failed"])
def test_detached_parent_returns_and_post_wait_keeps_override_and_prior_stages(phase, main_status):
    f = phase
    if main_status == "failed":
        f.task.update(id="post-failed", root_task_id="post-failed")
        pipeline._store_task_result(f.env, f.task, "Main execution failed", {}, {},
            loop_outcome={"outcome_axes": {"execution": {"status": "failed"}}})
    assert pipeline._is_root_post_task(f.task)
    initial = load_task_result(f.root, f.task["id"])
    assert initial["status"] == main_status
    assert initial["root_phase_checkpoint"]["post_task_synthesis"] == "pending_once"
    with model_wait.task_model_wait_scope(task=f.task, drive_root=f.root, event_queue=f.events,
                                          worker_slot_held=False) as parent:
        parent.overrides["light"] = {"model": MODEL, "use_local": False, "model_account_override": "original-choice"}
        assert launch(f) is None
    assert parent.closed
    until(lambda: active(f))
    owner = active(f)
    assert owner is not parent and not owner.closed and owner.worker_slot_held is False
    assert f.stages == ["chat", "scratch", "summary", "reflection"] and not f.done.is_set()
    assert load_task_result(f.root, f.task["id"])["root_phase_checkpoint"]["post_task_synthesis"] == "running"
    assert f.engine.uploads[0][0]["account"] == {"mode": "pin", "profileId": "original-choice"}
    f.ready.set()
    assert f.done.wait(5)
    until(lambda: post_task_model_wait(f.root, f.task["id"]) is None)
    assert f.stages == ["chat", "scratch", "summary", "reflection", "backlog"]
    assert len(f.engine.creates) == 2 and f.engine.uploads[0][0]["messages"] == f.engine.uploads[1][0]["messages"]
    until(lambda: owner.closed)
    assert load_task_result(f.root, f.task["id"])["status"] == main_status


def test_detached_decision_mailbox_and_activity_remain_live_after_task_done(phase):
    from ouroboros.gateway import task_model_wait as gateway
    from ouroboros.gateway.state import _chat_activities_snapshot_safe
    from supervisor.terminal_delivery import cleanup_settled_owner_mailbox
    from ouroboros.owner_mailbox import write_owner_message, _mailbox_path
    from supervisor.task_model_wait import handle_task_model_wait
    from ouroboros.utils import append_jsonl

    f = phase
    launch(f)
    until(lambda: active(f))
    owner = active(f)
    row = deepcopy(next(row for row in owner.waits.values() if row["state"] == "waiting"))
    action = {"request_id": "post-switch", "decision_id": f"model_wait:{f.task['id']}:{row['wait_id']}",
              "revision": row["revision"], "action": "switch", "model": MODEL,
              "credential_profile_id": "replacement", "use_local": False, "persist_role": False}
    write_owner_message(f.root, "retained owner message", f.task["id"], msg_id="keep")
    cleanup_settled_owner_mailbox(f.root, f.task["id"], f.task)
    assert _mailbox_path(f.root, f.task["id"]).exists()
    activities = [row for row in _chat_activities_snapshot_safe(f.root) if row["activity_id"] == f.task["id"]]
    assert len(activities) == 1 and activities[0]["phase"] == "finalizing" and activities[0]["model_waits"]
    forwarded = []
    ctx = SimpleNamespace(RUNNING={}, DRIVE_ROOT=f.root, append_jsonl=append_jsonl,
                          bridge=SimpleNamespace(push_log=forwarded.append))
    handle_task_model_wait({"type": "task_model_wait", "task_id": f.task["id"], **row}, ctx)
    assert len(forwarded) == 1 and forwarded[0]["chat_id"] == 1
    response = gateway._decide(f.root, action)
    assert response.status_code == 202
    assert f.done.wait(5)
    until(lambda: not _mailbox_path(f.root, f.task["id"]).exists())
    assert f.engine.uploads[-1][0]["account"] == {"mode": "pin", "profileId": "replacement"}
    assert gateway._decide(f.root, action).status_code == 409
    handle_task_model_wait({"type": "task_model_wait", "task_id": f.task["id"], **row}, ctx)
    assert len(forwarded) == 1  # An ended post owner cannot be resurrected.


@pytest.mark.parametrize("unknown", [False, True])
def test_stop_or_unknown_never_marks_post_work_completed(phase, unknown):
    f = phase
    if unknown:
        f.engine.results[0]["outcome"] = "unknown"
        f.engine.dispatch[0] = "unknown"
    launch(f)
    if not unknown:
        until(lambda: active(f))
        f.task["_skip_post_task_synthesis"] = True
    assert f.done.wait(5)
    assert load_task_result(f.root, f.task["id"])["root_phase_checkpoint"]["post_task_synthesis"] == "degraded"
    assert len(f.engine.creates) == 1 and "backlog" not in f.stages
    assert ledger(f.root)[-1]["state"] == ("unresolved" if unknown else "released")


def test_pooled_post_work_holds_return_but_delivers_answer_early_once(phase, monkeypatch):
    from ouroboros.utils import in_worker_process

    f = phase
    monkeypatch.setenv("OUROBOROS_IN_WORKER", "1")
    assert in_worker_process()
    pending = [{"type": "send_message", "task_id": f.task["id"], "chat_id": 1, "text": "Already answered"},
               {"type": "task_done", "task_id": f.task["id"], "status": "completed"}]
    returned = threading.Event()

    def run():
        with model_wait.task_model_wait_scope(task=f.task, drive_root=f.root, event_queue=f.events,
                                              worker_slot_held=True):
            pipeline._dispatch_root_post_task(f.env, f.task, "Already answered", f.events, pending,
                {"rounds": 3}, {}, {}, f.root / "logs", budget_drive_root="", split_drive=False,
                project_scoped=False, project_task=False, parent_env=None, parent_task=None)
        returned.set()

    thread = threading.Thread(target=run)
    thread.start()
    until(lambda: len(f.engine.creates) == 1)
    live = list(f.events.queue)
    answers = [row for row in live if row["type"] == "send_message"]
    assert len(answers) == 1 and answers[0]["text"] == "Already answered"
    assert not returned.is_set() and not any(row["type"] == "task_done" for row in live)
    assert answers[0]["delivery_id"] == pending[0]["delivery_id"]
    assert post_task_model_wait(f.root, f.task["id"]) is None  # no cross-process owner registry
    f.ready.set()
    thread.join(5)
    assert returned.is_set() and not thread.is_alive() and len(f.engine.creates) == 2


@pytest.mark.parametrize("stage", ["chat", "scratch", "reflection", "backlog"])
def test_outer_paid_stage_cannot_swallow_typed_unknown(tmp_path, monkeypatch, stage):
    from ouroboros import post_task_synthesis as synthesis, consolidator, reflection, improvement_backlog

    error = transport.ClaudexorModelError({"code": "unknown", "message": "paid outcome unknown"}, unknown=True)
    def fail(*args, **kwargs):
        raise error
    env = SimpleNamespace(drive_root=tmp_path, drive_path=lambda rel: tmp_path / rel)
    memory = SimpleNamespace(load_identity=lambda: "identity")
    monkeypatch.setattr(consolidator, "should_consolidate", lambda *a: True)
    monkeypatch.setattr(consolidator, "should_consolidate_scratchpad", lambda *a: True)
    monkeypatch.setattr(consolidator, "consolidate", fail)
    monkeypatch.setattr(consolidator, "consolidate_scratchpad", fail)
    monkeypatch.setattr(reflection, "should_generate_reflection", lambda *a, **k: True)
    monkeypatch.setattr(reflection, "generate_reflection", fail)
    monkeypatch.setattr(improvement_backlog, "append_backlog_items", lambda *a: 1)
    monkeypatch.setattr(improvement_backlog, "groom_backlog", fail)
    calls = {"chat": lambda: synthesis._run_chat_consolidation(env, memory, None, {"id": "t"}, tmp_path / "logs"),
             "scratch": lambda: synthesis._run_scratchpad_consolidation(env, memory, None),
             "reflection": lambda: synthesis._run_reflection(env, None, {"id": "t"}, {}, {}, {}),
             "backlog": lambda: synthesis._update_improvement_backlog(env, {"backlog_candidates": [{}]})}
    with pytest.raises(transport.ClaudexorModelError) as raised:
        calls[stage]()
    assert raised.value is error


def test_promotion_outer_wrapper_propagates_typed_control_but_keeps_ordinary_fallback(tmp_path, monkeypatch):
    from ouroboros import post_task_evolution as promotion

    monkeypatch.setattr(config, "get_post_task_evolution_enabled", lambda: True)
    monkeypatch.setattr(config, "get_runtime_mode", lambda: "advanced")
    monkeypatch.setattr(config, "get_post_task_evolution_cadence", lambda: "llm")
    monkeypatch.setattr(promotion, "_eligible", lambda *_: True)
    monkeypatch.setattr(promotion, "_is_canonical_run", lambda *_: True)
    def fail(*args, **kwargs):
        raise model_wait.ModelWaitInterrupted("owner_stopped", role="main")
    monkeypatch.setattr(promotion, "_decide_promotion", fail)
    env = SimpleNamespace(drive_root=tmp_path)
    with pytest.raises(model_wait.ModelWaitInterrupted):
        promotion.maybe_promote(env, {"id": "task"}, None)
    monkeypatch.setattr(promotion, "_decide_promotion", lambda *a, **k: 1 / 0)
    assert promotion.maybe_promote(env, {"id": "task"}, None) is None


def _controlled_worker(input_queue, output_queue, data_root, repo_root, resume):
    """Run the actual worker dequeue loop with controlled post-task cognition."""
    from contextlib import ExitStack
    from pathlib import Path
    from unittest.mock import patch
    from ouroboros.usage_accounting import UsageScope, usage_scope
    from supervisor.worker_process import worker_main

    root = Path(data_root)
    env = SimpleNamespace(drive_root=root, repo_dir=Path(repo_root), drive_path=lambda rel: root / rel)
    engine = Gateway([result(outcome="failed", problem={"code": "subscription_window_exhausted", "message": "fixture"}), result()],
                     ["not_started", "response_received"])
    def reflect(_env, client, *args, **kwargs):
        output_queue.put({"type": "fixture_reflection_started"})
        client.chat([{"role": "user", "content": "one reflection"}], MODEL, model_role="light")
        return None
    class Agent:
        def handle_task(self, task):
            output_queue.put({"type": "fixture_task_started", "task_id": task["id"]})
            if task["id"] == "second":
                return []
            write_task_result(root, task["id"], "completed", result="answer",
                              root_phase_checkpoint={"post_task_synthesis": "pending_once"})
            pending = [{"type": "send_message", "task_id": task["id"], "chat_id": 1, "text": "answer"},
                       {"type": "task_done", "task_id": task["id"], "status": "completed"}]
            with usage_scope(UsageScope(drive_root=root, task_id=task["id"], root_task_id=task["id"])), model_wait.task_model_wait_scope(
                    task=task, drive_root=root, event_queue=output_queue, worker_slot_held=True):
                pipeline._dispatch_root_post_task(env, task, "answer", output_queue, pending, {"rounds": 3}, {}, {}, root / "logs",
                    budget_drive_root="", split_drive=False, project_scoped=False, project_task=False, parent_env=None, parent_task=None)
            output_queue.put({"type": "fixture_generation_count", "count": len(engine.creates)})
            return pending
    with ExitStack() as stack:
        replacements = {
            "ouroboros.agent.make_agent": lambda **kwargs: Agent(),
            "ouroboros.extension_loader.reload_all": lambda *args, **kwargs: None,
            "supervisor.worker_process._prepare_worker_task_runtime": lambda: None,
            "supervisor.worker_process._adopt_published_extensions": lambda *_: None,
            "ouroboros.llm_claudexor.ensure_owned_gateway": lambda: engine,
            "ouroboros.llm_claudexor.model_sources": lambda: {"sources": [{"id": "codex", "credentialHarness": "fixture"}]},
            "ouroboros.llm_claudexor.model_catalog": lambda source, account=None, **kwargs: {
                "source": source, "credentialProfileId": account or "account-a", "models": [{"id": "exact-model"}] if resume.is_set() else []},
            "ouroboros.agent_task_pipeline._run_chat_consolidation": lambda *_: None,
            "ouroboros.agent_task_pipeline._run_scratchpad_consolidation": lambda *_: None,
            "ouroboros.agent_task_pipeline._run_task_summary": lambda *args, **kwargs: None,
            "ouroboros.agent_task_pipeline._run_reflection": reflect,
            "ouroboros.agent_task_pipeline._update_improvement_backlog": lambda *_: None,
            "ouroboros.agent_task_pipeline._apply_reflection_memory_actions": lambda *args, **kwargs: None,
            "ouroboros.post_task_evolution.maybe_promote": lambda *_: None,
        }
        for name, value in replacements.items():
            stack.enter_context(patch(name, value))
        config.CLAUDEXOR_MODEL_POLL_INTERVAL_SEC = 0.01
        config.NETWORK_WAIT_BACKOFF_START_SEC = 0.01
        config.NETWORK_WAIT_BACKOFF_MAX_SEC = 0.01
        worker_main(0, input_queue, output_queue, repo_root, data_root)


@pytest.mark.serial
def test_real_pooled_process_does_not_dequeue_next_task_during_post_wait(tmp_path, monkeypatch):
    import multiprocessing
    from pathlib import Path

    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("TOTAL_BUDGET", "100")
    monkeypatch.setenv("OUROBOROS_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(root / "settings.json"))
    context = multiprocessing.get_context("spawn")
    incoming, outgoing, resume = context.Queue(), context.Queue(), context.Event()
    proc = context.Process(target=_controlled_worker, args=(incoming, outgoing, str(root), str(Path(__file__).resolve().parents[1]), resume))
    events = []
    def receive_until(predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            events.append(outgoing.get(timeout=max(0.01, deadline - time.monotonic())))
            if predicate(events[-1]):
                return
        raise AssertionError("worker fixture did not reach requested state")
    proc.start()
    try:
        incoming.put({"id": "first", "type": "task", "chat_id": 1, "drive_root": str(root)})
        incoming.put({"id": "second", "type": "task", "chat_id": 1, "drive_root": str(root)})
        receive_until(lambda event: event.get("type") == "task_model_wait" and event.get("state") == "waiting")
        assert events[-1]["worker_slot_held"] is True
        assert any(event.get("type") == "send_message" for event in events)
        assert not any(event.get("type") == "task_done" or event.get("task_id") == "second" for event in events)
        assert load_task_result(root, "first")["root_phase_checkpoint"]["post_task_synthesis"] == "running"
        resume.set()
        receive_until(lambda event: event.get("type") == "fixture_task_started" and event.get("task_id") == "second")
        assert sum(event.get("type") == "fixture_reflection_started" for event in events) == 1
        assert [event["count"] for event in events if event.get("type") == "fixture_generation_count"] == [2]
        assert any(event.get("type") == "task_done" and event.get("task_id") == "first" for event in events)
        answers = [event for event in events if event.get("type") == "send_message"]
        assert len(answers) == 2 and len({event["delivery_id"] for event in answers}) == 1
        incoming.put(None)
        proc.join(10)
        assert proc.exitcode == 0
    finally:
        resume.set()
        if proc.is_alive():
            incoming.put(None)
            proc.join(5)
        if proc.is_alive():
            proc.terminate()
            proc.join(5)
        incoming.close()
        outgoing.close()
        incoming.join_thread()
        outgoing.join_thread()


@pytest.mark.serial
@pytest.mark.parametrize("first_settled", ["attachments", "post_work"])
def test_mailbox_survives_until_both_attachment_and_post_work_custody_settle(tmp_path, first_settled):
    from ouroboros.owner_mailbox import _mailbox_path, write_owner_message
    from supervisor.terminal_delivery import cleanup_settled_owner_mailbox

    task = {"id": "retained-input", "drive_root": str(tmp_path)}
    pending = [{"kind": "task_attachment", "source_task_id": task["id"]}]
    post = "running"
    write_owner_message(tmp_path, "Accepted owner input", task["id"], msg_id="accepted-owner")
    path = _mailbox_path(tmp_path, task["id"])
    original = path.read_bytes()
    for step in range(3):
        write_task_result(tmp_path, task["id"], "completed",
                          child_ref_promotion={"pending_refs": pending},
                          root_phase_checkpoint={"post_task_synthesis": post})
        cleanup_settled_owner_mailbox(tmp_path, task["id"], task)
        if step < 2:
            assert path.read_bytes() == original
        else:
            assert not path.exists()
        if step == 0 and first_settled == "attachments":
            pending = []
        elif step == 0:
            post = "completed"
        else:
            pending, post = [], "completed"
