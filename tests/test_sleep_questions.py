"""Executor questions interrupt model-selected sleep; exposure is not an answer.

Use the real interaction producer, fitted-result ACK, warm park, cold grant and
continuation consumers. A question's existence alone never grants execution.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.test_model_sleep import _ctx, _result
from tests._budget_pause_exact_helpers import _loop_ctx

pytestmark = pytest.mark.serial  # cold consumers bind queue module state


def _run(root, run_id="run-one", task_id="sleeper", source="delegate_start"):
    from ouroboros import delegate_custody as custody, delegate_custody_current as current

    assert custody.emit(root, custody.STARTED, {
        "run_id": run_id, "invocation_id": f"invoke-{run_id}", "task_id": task_id,
        "root_task_id": task_id, "route": "route", "source": source, "access": "readonly"})
    return current.run(root, run_id)


def _question(root, run, question_id="q1", seq=2):
    from ouroboros.delegate_collection import observe_run
    from ouroboros.delegate_custody_current import run as current_run

    assert observe_run(root, run, {"summary": {"runId": run.run_id, "state": "running"}, "lastSeq": seq,
        "pendingInteractions": [{"interactionId": question_id,
                                 "questions": [{"id": "choice", "question": "Which path?"}]}]})
    return current_run(root, run.run_id)


def _source(root, question):
    from ouroboros.artifacts import read_actor_source_bytes

    return json.loads(read_actor_source_bytes(root, "sleeper", question["interaction_source"]))


def _deliver(ctx, text, monkeypatch, *, partial=False, error=False):
    from ouroboros import loop_tool_execution as loop

    monkeypatch.setattr(loop, "_deliver_tool_results", lambda *_a, **_k: [
        {"result": text, "result_partial": partial}])
    messages = []
    loop.process_tool_results([{
        "fn_name": "await_messages", "tool_call_id": "wait-call", "result": text,
        "is_error": error, "args_for_log": {}, "tool_args": {},
        "result_meta": {"tool_result_meta": {"sleep_question_notice": True}}}],
        messages, {"tool_calls": []}, lambda *_a, **_k: None, tools=SimpleNamespace(_ctx=ctx))
    return messages


def test_question_before_arm_returns_source_but_only_complete_delivery_marks_exposure(tmp_path, monkeypatch):
    from ouroboros.model_sleep import wake_reason
    from ouroboros.tools.control_task_results import _await_messages

    _result(tmp_path, "sleeper")
    _result(tmp_path, "other-branch")
    ctx = _ctx(tmp_path)
    run = _question(tmp_path, _run(tmp_path))
    raw = _await_messages(ctx, mode="warm", senders=["other-branch"])
    payload = json.loads(raw)
    assert payload["reason"] == "ready" and payload["woke_by"] == "run_question:run-one"
    assert not getattr(ctx, "_model_sleep", None) and not getattr(ctx, "_sleep_interaction_seen", {})
    assert _source(tmp_path, payload["questions"][0])["pending_interactions"][0]["interaction_id"] == "q1"
    chosen = {"senders": ["other-branch"], "any_mail": False}
    assert wake_reason(ctx, chosen) == "run_question:run-one"
    _deliver(ctx, raw, monkeypatch, partial=True)
    assert wake_reason(ctx, chosen) == "run_question:run-one"
    _deliver(ctx, raw, monkeypatch, error=True)
    assert wake_reason(ctx, chosen) == "run_question:run-one"
    messages = _deliver(ctx, raw, monkeypatch)
    assert messages[0]["role"] == "tool" and ctx._sleep_interaction_seen == {"run-one": ["q1"]}
    assert wake_reason(ctx, chosen) == ""
    assert json.loads(_await_messages(ctx, mode="warm", senders=["other-branch"]))["reason"] == "sleep_armed"
    assert run.pending_interaction_ids == ["q1"], "notification never answers"
    _question(tmp_path, run, "q2", 3)
    assert wake_reason(ctx, chosen) == "run_question:run-one"


def test_warm_question_landing_after_arm_returns_same_author_with_exact_source(tmp_path):
    from ouroboros import owner_wait
    from ouroboros.model_sleep import wake_reason
    from ouroboros.tools.control_task_results import _await_messages

    _result(tmp_path, "sleeper")
    _result(tmp_path, "other")
    ctx = _ctx(tmp_path)
    run = _run(tmp_path)
    assert json.loads(_await_messages(ctx, mode="warm", senders=["other"]))["reason"] == "sleep_armed"
    _question(tmp_path, run)
    ctx.owner_wait_callback = owner_wait.direct_owner_wait
    messages = [{"role": "user", "content": "intent"}]
    owner_wait.wait_after_tools(ctx, messages, {}, {}, 1, [], set())
    assert ctx.task_id == "sleeper" and ctx._model_sleep is None
    question = json.JSONDecoder().raw_decode(messages[-1]["content"].split("observation: ", 1)[1])[0][0]
    assert _source(tmp_path, question)["run_id"] == "run-one"
    assert wake_reason(ctx, {"any_mail": False}) == ""
    assert ctx._sleep_interaction_seen == {"run-one": ["q1"]}


def test_pooled_warm_question_uses_existing_park_resume_protocol(tmp_path):
    from ouroboros import owner_wait

    _result(tmp_path, "sleeper")
    ctx = _ctx(tmp_path)
    _question(tmp_path, _run(tmp_path))
    checkpoint = {"wait_id": "wait", "reason": "sleep", "sleep": {"any_mail": False}}
    events = []
    class Commands:
        calls = 0
        def get(self, **_kw):
            self.calls += 1
            assert self.calls <= 2
            if self.calls == 2:
                assert any(e.get("phase") == "resume" for e in events)
            return {"type": "owner_wait", "task_id": "sleeper", "task_attempt": 1,
                    "wait_id": "wait", "phase": "parked" if self.calls == 1 else "resume_granted"}
    assert owner_wait.worker_owner_wait(0, Commands(), SimpleNamespace(put=events.append), ctx,
                                        checkpoint) == "run_question:run-one"
    assert [e["phase"] for e in events] == ["park", "resume"]


@pytest.mark.parametrize("source,owner", [("review_substrate", "sleeper"), ("delegate_start", "foreign")])
def test_foreign_and_review_question_custody_does_not_belong_to_sleeper(tmp_path, source, owner):
    from ouroboros.model_sleep import wake_reason

    _result(tmp_path, "sleeper")
    run = _question(tmp_path, _run(tmp_path, task_id=owner, source=source))
    assert run.review_owned if source == "review_substrate" else run.task_id == "foreign"
    assert wake_reason(_ctx(tmp_path), {"any_mail": False}) == ""


def test_native_escalation_not_peer_text_interrupts_filtered_sleep_without_ack(tmp_path, monkeypatch):
    from ouroboros import owner_wait, task_status
    from ouroboros.model_sleep import wake_reason
    from ouroboros.owner_mailbox import write_task_message
    from ouroboros.tools.core_artifacts import _escalate

    _result(tmp_path, "sleeper")
    ctx = _ctx(tmp_path)
    chosen = {"senders": ["other"], "any_mail": False}
    assert write_task_message(tmp_path, "is this a question?", "sleeper", source_task_id="child",
                              provenance="peer_task")
    assert wake_reason(ctx, chosen) == "", "question-looking prose is not authority"
    monkeypatch.setattr(task_status, "load_effective_task_result", lambda *_a: {
        "status": "running", "task_id": "sleeper", "drive_root": str(tmp_path)})
    child = _ctx(tmp_path, "child")
    child.task_metadata = {"parent_task_id": "sleeper", "root_task_id": "sleeper",
                           "delegation_role": "subagent"}
    assert "escalated to parent" in _escalate(child, "Which path?", assumption="retain the result")
    assert wake_reason(ctx, chosen) == "mail:child"
    entries = owner_wait._wait_entries(ctx)
    escalation = next(e for e in entries if e.get("provenance") == "descendant_task")
    assert "Which path?" in escalation["text"] and not ctx._loop_mailbox_seen_ids
    ctx._loop_mailbox_seen_ids.add(escalation["msg_id"])
    assert wake_reason(ctx, chosen) == "", "exposure is owned by the ordinary mailbox drain"


@pytest.mark.parametrize("direct", [False, True])
def test_cold_unselected_question_keeps_pause_veto_then_returns_and_carries_exposure(tmp_path, monkeypatch, direct):
    from tests.test_cold_external_custody import _setup, _park
    from ouroboros import budget_pause as bp, delegate_custody_current as current, owner_pause, owner_wait
    from ouroboros.model_sleep import wake_reason
    from supervisor.sleep_wake import wake_ready_sleepers

    env = _setup(tmp_path, monkeypatch, direct=direct)
    _park(env, tmp_path, monkeypatch, direct=direct, selected={"wake_at": "2099-01-01T00:00:00+00:00"})
    _question(tmp_path, current.run(tmp_path, "run-one"))
    owner_pause.install_fence(tmp_path, "sleeper", request_id="pause")
    out = wake_ready_sleepers(env.q)
    assert out[0]["ready"]["reason"] == "run_question:run-one" and out[0]["veto"] == "owner_pause"
    assert not bp.budget_pause_row(tmp_path, "sleeper").get("grant")
    owner_pause.release_fence(tmp_path, "sleeper", reason="test")
    out = wake_ready_sleepers(env.q)
    assert out[0]["ok"]
    fresh, _ = _loop_ctx(tmp_path, "sleeper", direct=direct)
    fresh.budget_pause_resume = env.workers.PENDING[0]["_budget_pause_resume"]
    saved = bp.load_budget_pause(fresh)
    monkeypatch.setattr(owner_wait, "rebind_restored_route", lambda *_a: (None, "max"))
    messages, usage = [], {}
    bp.resume_paused_loop(SimpleNamespace(_ctx=fresh), saved, messages, {}, usage, set(), budget_remaining_usd=5)
    assert any("interaction_source" in m["content"] for m in messages)
    assert bp.budget_pause_row(tmp_path, "sleeper")["grant"]["consumed_at"]
    assert wake_reason(fresh, {"any_mail": False}) == ""
    retained = owner_wait.continuation_state(fresh, messages, {}, usage, 4, [], set())
    restored, _ = _loop_ctx(tmp_path, "sleeper")
    owner_wait.restore_continuation_state(SimpleNamespace(_ctx=restored), retained, [], {}, {}, set())
    assert wake_reason(restored, {"any_mail": False}) == ""
    _question(tmp_path, current.run(tmp_path, "run-one"), "q2", 3)
    assert wake_reason(restored, {"any_mail": False}) == "run_question:run-one"


def test_native_saved_sleep_delivers_question_on_its_existing_return_path(tmp_path, monkeypatch):
    from ouroboros import owner_wait

    _result(tmp_path, "sleeper")
    ctx = _ctx(tmp_path)
    _question(tmp_path, _run(tmp_path))
    state = owner_wait.continuation_state(ctx, [{"role": "user", "content": "intent"}], {}, {}, 3, [], set())
    ctx.owner_wait_resume = {"reason": "sleep", "sleep": {"mode": "warm"}}
    ctx.owner_wait_callback = lambda *_a: "run_question:run-one"
    monkeypatch.setattr(owner_wait, "rebind_restored_route", lambda *_a: (None, "max"))
    messages = []
    owner_wait.resume_native_loop(SimpleNamespace(_ctx=ctx), state, messages, {}, {}, set())
    assert "interaction_source" in messages[-1]["content"]
    assert ctx._sleep_interaction_seen == {"run-one": ["q1"]}


def test_delegate_wait_a_b_ack_union_and_legacy_ack_survive_restored_cognition(tmp_path):
    from ouroboros import delegate_supervision as sup, owner_wait, sleep_custody
    from ouroboros.model_sleep import wake_reason
    ctx, _ = _loop_ctx(tmp_path, "sleeper")
    for run_id in ("run-a", "run-b"):
        _question(tmp_path, _run(tmp_path, run_id))
        payload = {"run_id": run_id, "status": "waiting_on_user", "supervision_wake_id": run_id,
                   "pending_interactions": [{"interaction_id": "q1", "questions": [{"id": "choice"}]}]}
        sup._save_state(ctx, {"run_id": run_id, "pending_wake": {"wake_id": run_id,
            "interaction_ids": ["q1"], "payload": payload}})
        assert not sup.acknowledge_pending_wake(ctx, json.dumps(payload)[:20])
        assert sup.acknowledge_pending_wake(ctx, json.dumps(payload))
    assert ctx._sleep_interaction_seen == {"run-a": ["q1"], "run-b": ["q1"]}
    assert wake_reason(ctx, {"any_mail": False}) == ""
    state = owner_wait.continuation_state(ctx, [], {}, {}, 4, [], set())
    restored, _ = _loop_ctx(tmp_path, "sleeper")
    owner_wait.restore_continuation_state(SimpleNamespace(_ctx=restored), state, [], {}, {}, set())
    assert wake_reason(restored, {"any_mail": False}) == ""
    legacy, _ = _loop_ctx(tmp_path, "sleeper")
    assert sleep_custody.seen_interactions(legacy) == {}
    assert wake_reason(legacy, {"any_mail": False}).startswith("run_question:"), "legacy exposure is unknown"


def test_unavailable_question_source_does_not_suppress_later_ready_question(tmp_path, monkeypatch):
    from ouroboros import delegate_custody_current as current, model_sleep

    _question(tmp_path, _run(tmp_path))
    ctx = _ctx(tmp_path)
    original = current.run
    with monkeypatch.context() as fault:
        fault.setattr(model_sleep, "_owned_runs", lambda *_a: (_ for _ in ()).throw(OSError("read unavailable")))
        messages = []
        model_sleep.append_question_notice(ctx, "run_question:run-one", messages)
        assert "no longer readable" in messages[-1]["content"] and not getattr(ctx, "_sleep_interaction_seen", {})
    assert original(tmp_path, "run-one").pending_interaction_ids == ["q1"]
    assert model_sleep.wake_reason(ctx, {"any_mail": False}) == "run_question:run-one"


def test_implicit_custody_failure_does_not_refuse_time_only_warm_arm(tmp_path, monkeypatch):
    from ouroboros import model_sleep, store
    from ouroboros.tools.control_task_results import _await_messages

    _result(tmp_path, "sleeper")
    ctx = _ctx(tmp_path)
    budgets = []
    def broken(_ctx):
        budgets.append(store._WAIT_BUDGET.get())
        raise store.StoreBusy("contended", reason="contention")
    monkeypatch.setattr(model_sleep, "_owned_runs", broken)
    out = json.loads(_await_messages(ctx, mode="warm", wake_at="2099-01-01T00:00:00+00:00"))
    assert out["reason"] == "sleep_armed" and budgets == [0]
    assert model_sleep.wake_reason(ctx, {"wake_at": "2000-01-01T00:00:00+00:00"}) == "timeout"
    # An explicitly selected unreadable run still preserves its original error.
    with pytest.raises(store.StoreBusy):
        model_sleep.wake_reason(ctx, {"runs": ["run-one"]})
    assert all(value == 0 for value in budgets)


@pytest.mark.parametrize("woke_by", ["task:ready:completed", "mail:peer", "timeout"])
def test_concurrent_ready_condition_delivers_all_unseen_questions(tmp_path, monkeypatch, woke_by):
    from ouroboros import model_sleep, owner_wait
    from ouroboros.owner_mailbox import write_task_message
    from ouroboros.tools.control_task_results import _await_messages

    _result(tmp_path, "sleeper")
    _result(tmp_path, "ready", "completed")
    _result(tmp_path, "peer")
    ctx = _ctx(tmp_path)
    for rid in ("run-a", "run-b"):
        _question(tmp_path, _run(tmp_path, rid))
    if woke_by.startswith("task:"):
        args = {"tasks": ["ready"]}
    elif woke_by.startswith("mail:"):
        assert write_task_message(tmp_path, "ordinary", "sleeper", source_task_id="peer", provenance="peer_task")
        args = {"senders": ["peer"]}
    else:
        # Real return can have selected timeout recorded before the questions land.
        args = {"wake_at": "2000-01-01T00:00:00+00:00"}
    if woke_by != "timeout":
        raw = _await_messages(ctx, mode="warm", **args)
        assert json.loads(raw)["woke_by"] == woke_by
        assert {q["run_id"] for q in json.loads(raw)["questions"]} == {"run-a", "run-b"}
        _deliver(ctx, raw, monkeypatch)
    else:
        messages = []
        model_sleep.append_question_notice(ctx, woke_by, messages)
        assert "run-a" in messages[0]["content"] and "run-b" in messages[0]["content"]
    assert ctx._sleep_interaction_seen == {"run-a": ["q1"], "run-b": ["q1"]}
    ctx._loop_mailbox_seen_ids.update(e["msg_id"] for e in owner_wait._wait_entries(ctx))
    assert model_sleep.wake_reason(ctx, {"any_mail": False}) == ""


def test_cold_notice_marks_all_sources_it_actually_shows(tmp_path):
    from ouroboros import budget_pause, model_sleep

    ctx = _ctx(tmp_path)
    external = []
    for rid in ("run-a", "run-b"):
        run = _question(tmp_path, _run(tmp_path, rid))
        external.append({"run_id": rid, "state": "running", "pending_interaction_ids": ["q1"],
                         "interaction_source": run.interaction_source})
    row = {"reason": "sleep", "sleep": {"mode": "cold"}, "external_custody": {"state": "accepted"},
           "external_runs": {"runs": external}, "sleep_ready": {"reason": "run_question:run-a"}}
    messages = []
    budget_pause._resume_disclosure(ctx, row, {}, {"consumed_at": 100}, {}, messages)
    assert any("run-a" in m["content"] and "run-b" in m["content"] for m in messages)
    assert ctx._sleep_interaction_seen == {"run-a": ["q1"], "run-b": ["q1"]}
    assert model_sleep.wake_reason(ctx, {"any_mail": False}) == ""


def test_supervision_id_without_delivered_question_cannot_mark_sleep_exposure(tmp_path):
    from ouroboros import delegate_supervision as sup, sleep_custody

    ctx = _ctx(tmp_path)
    payload = {"run_id": "run-a", "status": "waiting_on_user", "supervision_wake_id": "wake",
               "pending_interactions": [{"interaction_id": "q1"}]}
    sup._save_state(ctx, {"run_id": "run-a", "pending_wake": {"wake_id": "wake",
        "interaction_ids": ["q1"], "payload": payload}})
    assert sup.acknowledge_pending_wake(ctx, {"supervision_wake_id": "wake"})
    assert sleep_custody.seen_interactions(ctx).get("run-a") == []
    assert sup._load_state(ctx, "")["interaction_acknowledged_ids"] == ["q1"], "old ACK is not sleep exposure"
