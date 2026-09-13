"""Complete Main loop with the real reviewer coordinator and controlled transport."""
from __future__ import annotations

import copy
import json
import queue
import threading
from types import SimpleNamespace

import pytest

from ouroboros import loop, review_substrate
from ouroboros.review_records import ReviewSlot
from ouroboros.tools.registry import ToolRegistry
from tests.test_loop_acceptance_gate import _seed_acceptance_root

ANSWER = "The complete report includes the requested budget."
STATUS = "How is it going?"


def call(name, arguments, identifier):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def test_queue_inspection_failure_does_not_invent_owner_generation_change():
    def inspect_unavailable(**_kwargs):
        raise OSError("queue unavailable")

    ctx = SimpleNamespace(
        _task_acceptance_fence_generation=1,
        _task_acceptance_fence_token="fence",
        _execution_trace={},
        inspect_acceptance_fence=inspect_unavailable,
    )
    assert loop._task_acceptance_owner_generation_changed(ctx) is False
    assert ctx._execution_trace["review_decision"]["admission_inspection"]["status"] == "unknown"


@pytest.fixture
def full_loop(tmp_path, monkeypatch):
    from ouroboros import review_custody
    from ouroboros.review_execution import ReviewAttemptResult

    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "required")
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "3")
    monkeypatch.setenv("OUROBOROS_MAX_ROUNDS", "12")
    monkeypatch.setenv("OUROBOROS_SAFETY_MODE", "off")
    monkeypatch.setenv("MCP_ENABLED", "false")
    monkeypatch.setattr(loop, "_maybe_inject_finalization_nudges", lambda *_args: False)
    monkeypatch.setattr("ouroboros.tools.review_helpers.review_wave_budget_gate", lambda *_a, **_k: None)
    monkeypatch.setattr("ouroboros.review_evidence.acceptance_packet_budget_chars", lambda *_: 2_000_000)
    slot = ReviewSlot(slot_id="acceptance-one", model="fixture/reviewer", effort="high", timeout_sec=30)
    slots = [slot]
    monkeypatch.setattr(review_substrate, "triad_delivery_slots", lambda **_kw: slots)
    registry = ToolRegistry(repo_dir=tmp_path / "repo", drive_root=tmp_path / "data")
    registry._ctx.repo_dir.mkdir()
    ctx = registry._ctx
    task_id = "async-loop-root"
    _seed_acceptance_root(ctx.drive_root, task_id, ctx)
    ctx.task_contract["expected_output"] = "A complete report including its budget."
    ctx.task_attempt = 1
    ctx.current_chat_id = 1
    ctx.is_direct_chat = False
    ctx.owner_message_admission_agent = SimpleNamespace(
        _owner_message_generation=0, _accepting_owner_messages=True,
        _busy=True, _current_task_id=task_id,
    )
    ctx.owner_message_admission_lock = threading.RLock()
    incoming, events = queue.Queue(), queue.Queue()
    fixture = SimpleNamespace(tools=registry, ctx=ctx, incoming=incoming, events=events,
                              model_inputs=[], review_requests=[], review_snapshots=[], review_sends=[],
                              entered=threading.Event(), release=threading.Event(), settled=threading.Event(),
                              waits=[], progress=[], model_step=0, condition=threading.Condition(),
                              settled_count=0, reviewer_verdict="PASS", slots=slots)
    original_settle = review_custody._settle_review_attempt
    def settle(*a, **kw):
        try:
            return original_settle(*a, **kw)
        finally:
            fixture.settled.set()
            with fixture.condition:
                fixture.settled_count += 1
                fixture.condition.notify_all()
    monkeypatch.setattr(review_custody, "_settle_review_attempt", settle)

    class HeldExecutor:
        def __init__(self, assignment):
            self.assignment = assignment
        def restore_custody(self, _state):
            return None
        def set_pending_invocation_checkpoint(self, _checkpoint):
            return None
        def prompt_payload(self):
            return {"messages": []}
        def prompt_chars(self):
            return 0
        def failure_custody(self):
            return {}
        def execute(self):
            from ouroboros.review_dispatch import invoke_review_paid_stamp
            invoke_review_paid_stamp(self.assignment.dispatch_stamp)
            request = self.assignment.request
            fixture.review_sends.append(self.assignment.call_id)
            fixture.review_requests.append(copy.deepcopy(request))
            fixture.review_snapshots.append(copy.deepcopy(ctx._execution_trace))
            fixture.entered.set()
            with fixture.condition:
                fixture.condition.notify_all()
            assert fixture.release.wait(10), "fixture did not release review"
            from ouroboros.review_evidence_refs import acceptance_evidence_ref_vocabulary
            vocabulary = acceptance_evidence_ref_vocabulary(request.evidence)
            reference = next(key for key, basis in vocabulary.items() if basis in {"tool_record", "packet_section"})
            response_text = json.dumps({
                "verdict": fixture.reviewer_verdict, "summary": "Independent review", "findings": [],
                "outcome_tier": "solved" if fixture.reviewer_verdict == "PASS" else "best_effort",
                "completion_coach": "Deliver the complete answer." if fixture.reviewer_verdict == "PASS" else "Add independent verification.",
                "criteria_used": [{"criterion": "full report", "status": "supported",
                                   "evidence_refs": [reference]}],
            })
            return ReviewAttemptResult(message={"content": response_text}, raw_text=response_text,
                                       usage={"prompt_tokens": 5, "completion_tokens": 3, "physical_attempt_state": "settled"})

    monkeypatch.setattr(review_substrate, "_review_route_executor", lambda assignment, **_kw: HeldExecutor(assignment))
    def park(_ctx, checkpoint):
        fixture.waits.append(copy.deepcopy(checkpoint))
        fixture.release.set()
        with fixture.condition:
            assert fixture.condition.wait_for(lambda: fixture.settled_count >= len(fixture.review_sends), timeout=10), "review did not settle"
    ctx.owner_wait_callback = park
    fixture.park = park
    fixture.run_args = dict(
        messages=[{"role": "system", "content": "Complete the owner task."},
                  {"role": "user", "content": "Prepare the complete report including its budget."}],
        tools=registry, llm=SimpleNamespace(default_model=lambda: "fixture/main"),
        drive_logs=ctx.drive_root / "logs", emit_progress=lambda text, **kw: fixture.progress.append(text),
        incoming_messages=incoming, task_id=task_id, drive_root=ctx.drive_root, event_queue=events,
    )
    fixture.run = lambda: loop.run_llm_loop(**fixture.run_args)
    yield fixture
    fixture.release.set()
    if fixture.entered.is_set():
        assert fixture.settled.wait(10)


def keep(f):
    observation = f.ctx._acceptance_observation
    assert observation["owner_source_sha256"] in str(f.model_inputs[-1])
    return {"content": json.dumps({"delivery_control": "keep", "acceptance_subject": {
        "owner_source_sha256": observation["owner_source_sha256"],
    }})}


def test_full_loop_explicit_batch_owner_status_and_free_collection(full_loop, monkeypatch):
    f = full_loop
    def main(_llm, messages, *_args, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return {"content": "The report is ready; requesting its review.", "tool_calls": [
                call("task_acceptance_review", {"claim": ANSWER}, "nominate"),
                call("write_file", {"root": "task_drive", "path": "batch-proof.txt", "content": "last batch effect"}, "last-effect"),
            ]}, 0.0
        if f.model_step == 2:
            assert f.entered.wait(5), f.progress
            assert f.ctx._delivery_candidate.full_text == ANSWER
            f.incoming.put(STATUS)
            return {"content": "", "tool_calls": [call("read_file", {"root": "task_drive", "path": "batch-proof.txt"}, "read-proof")]}, 0.0
        if f.model_step == 3:
            assert STATUS in str(messages)
            assert not f.release.is_set()
            return {"content": "", "tool_calls": [call("send_user_message", {"text": "The report is ready; its review is still running."}, "status-answer")]}, 0.0
        assert f.model_step < 8, f.progress
        return keep(f), 0.0
    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, _usage, trace = f.run()
    assert result == ANSWER, (result, f.progress, trace.get("review_decision"))
    assert len(f.review_sends) == 1
    from ouroboros.task_results import project_task_acceptance_review_capacity
    assert project_task_acceptance_review_capacity(f.ctx, task_id=f.ctx.task_id)["claimed_cycles"] == 1
    assert trace["acceptance_decision"]["status"] == "accepted"
    first = f.review_snapshots[0]
    assert [r["tool_call_id"] for r in first["tool_calls"]] == ["nominate", "last-effect"]
    assert first["tool_calls"][-1]["status"] == "ok"
    assert f.review_requests[0].subject == ANSWER
    assert "last batch effect" in json.dumps(f.review_requests[0].evidence)
    sends = list(f.events.queue)
    replies = [x for x in sends if x.get("type") == "send_message" and x.get("system_type") == "proactive_message"]
    assert len(replies) == 1 and "still running" in replies[0]["text"]
    assert all(x["subject"] == ANSWER for x in [r["request"] for r in trace["review_runs"] if r.get("authority") == "host_root"])
    assert f.waits and f.waits[0]["reason"] == "review" and not f.waits[0]["quiz_id"]
    assert f.ctx.owner_message_admission_agent._accepting_owner_messages is False



def test_new_criterion_same_answer_gets_one_new_panel_and_keeps_prior_request(full_loop, monkeypatch):
    f = full_loop
    criterion = "Show the budget as an explicit section."
    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return {"content": "", "tool_calls": [call("task_acceptance_review", {"claim": ANSWER}, "initial-review")]}, 0.0
        if f.model_step == 2:
            assert f.entered.wait(5)
            f.incoming.put(criterion)
            return {"content": "", "tool_calls": [call("send_user_message", {"text": "The full draft already includes the budget."}, "progress")]}, 0.0
        if f.model_step == 3:
            assert criterion in str(messages)
            observed = f.ctx._acceptance_observation
            return {"content": "", "tool_calls": [call("task_acceptance_review", {
                "claim": ANSWER, "acceptance_subject": {
                    "owner_source_sha256": observed["owner_source_sha256"],
                    "effective_criteria": "Complete report with the budget in its own explicit section.",
                },
            }, "new-subject-review")]}, 0.0
        assert f.model_step < 8, f.progress
        return keep(f), 0.0
    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, _usage, trace = f.run()
    assert result == ANSWER
    assert len(f.review_sends) == len(set(f.review_sends)) == 2
    requests = f.review_requests
    assert requests[0].subject == requests[1].subject == ANSWER
    assert requests[0].retry_key != requests[1].retry_key
    assert criterion not in json.dumps(requests[0].evidence)
    assert criterion in json.dumps(requests[1].evidence)
    host = [r for r in trace["review_runs"] if r.get("authority") == "host_root"]
    assert len(host) == 2 and host[0]["superseded_by_revision"]
    assert host[0]["candidate_hash"] == host[1]["candidate_hash"]
    assert host[0]["subject_hash"] != host[1]["subject_hash"]
    from ouroboros.task_results import project_task_acceptance_review_capacity
    assert project_task_acceptance_review_capacity(f.ctx, task_id=f.ctx.task_id)["claimed_cycles"] == 2


@pytest.mark.parametrize("pending_first", [False, True])
@pytest.mark.parametrize("changed_requirement", [False, True])
def test_explicit_renomination_replaces_the_held_answer_without_control_repair(
    full_loop, monkeypatch, pending_first, changed_requirement,
):
    f = full_loop
    revised = "The revised complete report includes the corrected budget of 200."
    criterion = "Use the corrected budget of 200 in the report."
    if not pending_first:
        f.release.set()
        f.ctx.owner_wait_callback = None
    original_executor = review_substrate._review_route_executor
    notified = False

    def executor(assignment, **kw):
        nonlocal notified
        if changed_requirement and not notified:
            notified = True
            f.incoming.put(criterion)
        return original_executor(assignment, **kw)

    monkeypatch.setattr(review_substrate, "_review_route_executor", executor)

    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step <= 2:
            args = {"claim": ANSWER if f.model_step == 1 else revised}
            if f.model_step == 2:
                assert f.entered.wait(5)
                if changed_requirement:
                    assert criterion in str(messages)
                    args["acceptance_subject"] = {
                        "owner_source_sha256": f.ctx._acceptance_observation["owner_source_sha256"],
                        "effective_criteria": "Complete report with the corrected budget of 200.",
                    }
            return {"content": "", "tool_calls": [call("task_acceptance_review", args, f"nominate-{f.model_step}")]}, 0.0
        assert f.model_step < 8
        return keep(f), 0.0

    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, _usage, trace = f.run()
    assert result == revised
    assert [request.subject for request in f.review_requests] == [ANSWER, revised]
    assert len(f.review_sends) == len(set(f.review_sends)) == 2
    host = [run for run in trace["review_runs"] if run.get("authority") == "host_root"]
    assert len(host) == 2 and host[0]["superseded_by_revision"]
    assert host[0]["request"]["subject"] == ANSWER and host[1]["request"]["subject"] == revised
    assert "DELIVERY_CONTROL_REPAIR" not in str(f.model_inputs)
    assert trace["acceptance_decision"]["status"] == "accepted"


def test_explicit_ready_panel_does_not_seal_before_main_finishes(full_loop, monkeypatch):
    f = full_loop
    f.release.set()
    # This supported standalone variant waits synchronously, guaranteeing the
    # real panel has settled during explicit nomination rather than after it.
    f.ctx.owner_wait_callback = None
    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return {"content": "", "tool_calls": [call("task_acceptance_review", {"claim": ANSWER}, "ready-review")]}, 0.0
        assert f.model_step == 2, f.progress
        assert f.settled.is_set()
        assert f.ctx.owner_message_admission_agent._accepting_owner_messages is True
        assert not getattr(f.ctx, "_task_acceptance_sealed_fence_token", None)
        assert f.ctx._delivery_candidate.full_text == ANSWER
        return keep(f), 0.0
    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, _usage, trace = f.run()
    assert result == ANSWER and len(f.review_sends) == 1
    assert f.ctx.owner_message_admission_agent._accepting_owner_messages is False
    assert trace["acceptance_decision"]["status"] == "accepted", (trace["acceptance_decision"], [(r.get("aggregate_signal"), r.get("actors")) for r in trace["review_runs"]])



def test_final_seal_rechecks_input_arriving_after_early_review(full_loop, monkeypatch):
    f = full_loop
    f.release.set()
    f.ctx.owner_wait_callback = None
    begins, ends = [], []
    def begin(**_kw):
        token = f"fence-{len(begins) + 1}"
        begins.append(token)
        if len(begins) == 2:
            # Final delivery has drained the mailbox, but a previously admitted
            # message arrives before the last seal. It still belongs to Main.
            f.incoming.put("One last status question before delivery.")
        return {"token": token, "owner_message_generation": 0}
    def end(**kw):
        ends.append(dict(kw))
        return {"ok": True, "status": "sealed" if kw["outcome"] == "terminal" else "released"}
    f.ctx.begin_acceptance_fence, f.ctx.end_acceptance_fence = begin, end
    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return {"content": "", "tool_calls": [call("task_acceptance_review", {"claim": ANSWER}, "early")]}, 0.0
        assert f.model_step <= 3, f.progress
        if f.model_step == 3:
            assert "One last status question" in str(messages)
            assert ends[-1]["outcome"] == "revision"
            assert f.ctx.owner_message_admission_agent._accepting_owner_messages
        return keep(f), 0.0
    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, _usage, trace = f.run()
    assert result == ANSWER and f.model_step == 3
    assert len(f.review_sends) == 1
    assert [row["outcome"] for row in ends] == ["revision", "revision", "terminal"]
    assert trace["acceptance_decision"]["status"] == "accepted"


def test_cold_loop_resume_collects_saved_roster_and_request_once(full_loop, monkeypatch):
    from ouroboros.artifacts import read_actor_source_bytes
    from ouroboros.owner_wait import set_owner_wait
    f = full_loop
    f.slots.append(ReviewSlot("acceptance-two", "fixture/second-reviewer", effort="high", timeout_sec=30))
    class PlannedPause(BaseException):
        pass
    def pause(ctx, checkpoint):
        f.waits.append(copy.deepcopy(checkpoint))
        set_owner_wait(ctx.budget_drive_root or ctx.drive_root, ctx.task_id, {**checkpoint, "state": "waiting"})
        raise PlannedPause()
    f.ctx.owner_wait_callback = pause
    def first_main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return {"content": "", "tool_calls": [call("task_acceptance_review", {"claim": ANSWER}, "nominate")]}, 0.0
        assert f.model_step == 2
        return keep(f), 0.0
    monkeypatch.setattr(loop, "call_llm_with_retry", first_main)
    with pytest.raises(PlannedPause):
        f.run()
    checkpoint = f.waits[-1]
    saved = json.loads(read_actor_source_bytes(f.ctx.drive_root, f.ctx.task_id, checkpoint["source_ref"]))
    saved_run = saved["trace"]["review_runs"][-1]
    assert saved_run["request"]["subject"] == ANSWER
    assert saved_run["slot_roster"][0]["model"] == "fixture/reviewer"
    assert checkpoint["reason"] == "review" and not checkpoint["quiz_id"]
    f.release.set()
    with f.condition:
        assert f.condition.wait_for(lambda: f.settled_count == 2, timeout=10)
    old = f.ctx
    new_tools = ToolRegistry(repo_dir=old.repo_dir, drive_root=old.drive_root)
    new = new_tools._ctx
    for key in ("task_id", "task_attempt", "task_metadata", "task_contract", "budget_drive_root", "current_chat_id"):
        setattr(new, key, copy.deepcopy(getattr(old, key)))
    new.owner_message_admission_agent = SimpleNamespace(_owner_message_generation=0, _accepting_owner_messages=True,
                                                       _busy=True, _current_task_id=old.task_id)
    new.owner_message_admission_lock = threading.RLock()
    # Cognitive route rebuilding is independent of this operation-custody test.
    # The fixture has no assembled production ContextCore or live model catalog.
    monkeypatch.setattr(loop, "_rebind_context_fit_plan", lambda *_a, **_kw: (None, "max"))
    new.context_fit_plan = None
    new.owner_wait_resume = {**checkpoint, "restart_transaction_id": "fixture-planned-restart"}
    def resumed(_ctx, handoff):
        assert handoff["wait_id"] == checkpoint["wait_id"]
        assert _ctx._delivery_candidate.full_text == ANSWER
    new.owner_wait_callback = resumed
    f.ctx, f.tools = new, new_tools
    f.run_args["tools"] = new_tools
    f.run_args["messages"] = []
    def resumed_main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        assert "planned restart" in str(messages)
        return keep(f), 0.0
    monkeypatch.setattr(loop, "call_llm_with_retry", resumed_main)
    result, _usage, trace = f.run()
    assert result == ANSWER and len(f.review_sends) == 2
    run = trace["review_runs"][-1]
    assert run["request"] == saved_run["request"]
    assert run["slot_roster"] == saved_run["slot_roster"]
    assert len(run["slot_roster"]) == 2
    assert [r["operation_id"] for r in run["actors"]] == [r["operation_id"] for r in saved_run["actors"]]
    assert trace["acceptance_decision"]["status"] == "accepted"



def test_automatic_completion_uses_the_same_retained_candidate_and_free_collect(full_loop, monkeypatch):
    f = full_loop
    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return {"content": ANSWER}, 0.0
        assert f.model_step == 2, f.progress
        return keep(f), 0.0
    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, _usage, trace = f.run()
    assert result == ANSWER and len(f.review_sends) == 1
    assert trace["acceptance_decision"]["status"] == "accepted"
    assert f.waits and f.waits[0]["reason"] == "review"



@pytest.mark.parametrize("failure", ["pending", "fail", "unavailable", "evidence_unavailable"])
def test_cyber_final_response_never_waits_for_or_obeys_critic_veto(full_loop, monkeypatch, failure):
    f = full_loop
    monkeypatch.setattr("ouroboros.config.get_runtime_mode", lambda: "cyber_pro")
    if failure == "fail":
        f.reviewer_verdict = "FAIL"
        f.release.set()
    if failure == "unavailable":
        monkeypatch.setattr(review_substrate, "triad_delivery_slots", lambda **_kw: [])
    if failure == "evidence_unavailable":
        def evidence_unavailable(*_a, **_kw):
            raise OSError("fixture evidence storage unavailable")
        monkeypatch.setattr("ouroboros.loop_acceptance_review._build_host_acceptance_evidence", evidence_unavailable)
    def forbidden_wait(*_a, **_kw):
        pytest.fail("Cyber final-response decision was parked by a review")
    f.ctx.owner_wait_callback = forbidden_wait
    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if failure == "fail" and f.model_step == 1:
            return {"content": "", "tool_calls": [call("task_acceptance_review", {"claim": ANSWER}, "explicit-critic")]}, 0.0
        if failure == "fail":
            with f.condition:
                assert f.condition.wait_for(lambda: f.settled_count == 1, timeout=10)
            assert f.model_step == 2
            return keep(f), 0.0
        assert f.model_step == 1
        return {"content": ANSWER}, 0.0
    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, _usage, trace = f.run()
    assert result == ANSWER
    assert trace["acceptance_decision"]["status"] == "finalized_unaccepted"
    assert trace["acceptance_decision"]["reason"] == "author_finish"
    assert trace["acceptance_decision"]["author_disposition"]["source"] == "author_final_response"
    assert not f.waits
    if failure == "pending":
        assert not f.release.is_set()
        assert trace["review_runs"][-1]["actors"][0]["operation_state"] in {"pending_dispatch", "in_flight"}
        assert trace["acceptance_decision"]["review_pending"]
    elif failure == "fail":
        assert trace["review_runs"][-1]["aggregate_signal"] == "FAIL"
        assert trace["review_runs"][-1]["actors"][0]["parsed"]["verdict"] == "FAIL"
        assert len(f.review_sends) == 1
    else:
        assert trace["review_runs"][-1]["aggregate_signal"] == "DEGRADED"
        assert not f.review_sends
        if failure == "evidence_unavailable":
            assert "evidence storage unavailable" in str(trace["review_runs"][-1]["degraded_reasons"])
            assert "binding_hash" not in trace["review_runs"][-1]
            assert trace["acceptance_decision"]["author_disposition"]["subject_hash"] == trace["delivery_candidate"]["subject_sha256"]


@pytest.mark.parametrize("failure", ["begin", "end", "inspect"])
def test_cyber_admission_unavailable_is_disclosed_without_review_veto(full_loop, monkeypatch, failure):
    f = full_loop
    monkeypatch.setattr("ouroboros.config.get_runtime_mode", lambda: "cyber_pro")
    f.ctx.begin_acceptance_fence = lambda **_kw: None if failure == "begin" else {"token": "unreleased-fence", "owner_message_generation": 0}
    f.ctx.end_acceptance_fence = lambda **_kw: {"ok": False, "error": "fixture release unavailable"}
    if failure == "inspect":
        def inspect_unavailable(**_kw):
            raise OSError("fixture queue inspection unavailable")
        f.ctx.inspect_acceptance_fence = inspect_unavailable
    monkeypatch.setattr(loop, "_task_acceptance_subtree_snapshot", lambda *_a: (False, [{"task_id": "child", "status": "running"}]))
    f.ctx.owner_wait_callback = lambda *_a: pytest.fail("review admission withheld Cyber final")
    def main(*_a, **_kw):
        f.model_step += 1
        assert f.model_step == 1, "Unknown admission inspection was treated as a new owner request"
        return {"content": ANSWER}, 0.0
    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, _usage, trace = f.run()
    assert result == ANSWER
    assert trace["review_decision"]["admission_fence_available"] is (failure != "begin")
    assert trace["review_decision"]["admission_released"] is (failure == "begin")
    assert trace["review_decision"]["subtree_quiescent"] is False
    assert trace["acceptance_decision"]["status"] == "finalized_unaccepted"
    if failure == "inspect":
        assert trace["review_decision"]["admission_inspection"] == {
            "status": "unknown", "reason": "queue_inspection_failed", "error_type": "OSError",
        }


def test_cyber_unread_owner_message_still_reaches_same_main(full_loop, monkeypatch):
    f = full_loop
    monkeypatch.setattr("ouroboros.config.get_runtime_mode", lambda: "cyber_pro")
    f.ctx.owner_wait_callback = lambda *_a: None  # unread inbox wakes rather than waits on a critic
    original = review_substrate._review_route_executor
    injected = False
    def executor(assignment, **kw):
        nonlocal injected
        result = original(assignment, **kw)
        if not injected:
            injected = True
            f.incoming.put(STATUS)
        return result
    monkeypatch.setattr(review_substrate, "_review_route_executor", executor)
    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return {"content": ANSWER}, 0.0
        assert f.model_step == 2 and STATUS in str(messages)
        assert f.ctx._acceptance_ack_source_sha256 != f.ctx._acceptance_observation["owner_source_sha256"]
        return keep(f), 0.0
    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, _usage, trace = f.run()
    assert result == ANSWER and f.model_step == 2
    assert len(f.review_sends) == 1
    assert trace["acceptance_decision"]["status"] == "finalized_unaccepted"
