"""Informed author completion is not a request to buy the same criticism again."""
import copy
import dataclasses
import json

import pytest

from ouroboros import loop, review_substrate
from tests.test_acceptance_async_loop import ANSWER, call, full_loop  # noqa: F401


def _feedback(f, monkeypatch, kind="partial_pass"):
    """Real coordinator/loop, deterministic critic transport; no approval shortcut."""
    f.release.set()
    f.ctx.owner_wait_callback = None
    f.reviewer_verdict = {"fail": "FAIL", "degraded": "DEGRADED"}.get(kind, "PASS")
    factory = review_substrate._review_route_executor

    def executor(assignment, **kw):
        value = factory(assignment, **kw)
        execute = value.execute

        def response():
            result = execute()
            body = json.loads(result.raw_text)
            if kind == "partial_pass":
                body["criteria_used"][0]["status"] = "partial"
            elif kind == "missing_pass":
                body["criteria_used"] = []
            text = json.dumps(body)
            return dataclasses.replace(result, raw_text=text, message={"content": text})

        value.execute = response
        return value

    monkeypatch.setattr(review_substrate, "_review_route_executor", executor)


def _nominate(identifier="nominate"):
    return {"content": "", "tool_calls": [call("task_acceptance_review", {"claim": ANSWER}, identifier)]}


def _finish(door, *, rationale=None):
    args = {"action": "finish", "answer": ANSWER} if door == "finish_task" else {
        "claim": ANSWER, "goal": "Prepare report",
        **({"author_action": "finish"} if door == "author_action" else {"agent_disposition": "partial"}),
    }
    if rationale is not None:
        args["rationale"] = rationale
    return {"content": "", "tool_calls": [call(
        "finish_task" if door == "finish_task" else "task_acceptance_review", args, "finish",
    )]}


def _host_runs(trace):
    return [row for row in trace.get("review_runs", []) if row.get("authority") == "host_root"]


def _assert_exposed(f):
    run = _host_runs(f.ctx._execution_trace)[-1]
    # The fixture's transport observer stamps exposure when this scripted
    # response returns. Assert the exact settled feedback in this request now.
    assert any(source.get("settled") and source.get("binding_hash") == run["binding_hash"]
               for row in f.model_inputs[-1] for source in row.get("review_feedback", []))
    return copy.deepcopy(run)


@pytest.mark.parametrize("kind", ["partial_pass", "missing_pass", "fail", "degraded"])
@pytest.mark.parametrize("door", ["finish_task", "author_action", "agent_disposition"])
@pytest.mark.parametrize("renominate", [False, True])
def test_informed_finish_needs_no_rationale_or_second_panel(full_loop, monkeypatch, kind, door, renominate):  # noqa: F811
    f = full_loop
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    _feedback(f, monkeypatch, kind)
    original = []

    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return _nominate(), 0.0
        assert f.model_step < 7, f.progress
        if f.model_step == 2:
            original.append(_assert_exposed(f))
            assert f.ctx.owner_message_admission_agent._accepting_owner_messages
            if renominate:
                return _nominate("bare-repeat"), 0.0
        return _finish(door), 0.0

    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, _usage, trace = f.run()
    assert result == ANSWER and f.model_step == (3 if renominate else 2), (f.progress, trace.get("acceptance_decision"), trace.get("completion_refusals"), f.model_inputs[-1][-4:])
    assert len(f.review_sends) == 1
    hosts = _host_runs(trace)
    assert len(hosts) == 1
    assert hosts[0]["actors"] == original[0]["actors"]
    assert hosts[0]["aggregate_signal"] == original[0]["aggregate_signal"]
    assert hosts[0]["binding_hash"] == original[0]["binding_hash"]
    decision = trace["acceptance_decision"]
    assert decision["reason"] == "author_finish", decision
    assert decision["status"] == "finalized_unaccepted"
    assert decision["author_disposition"]["reviewer_signal"] == original[0]["aggregate_signal"]
    assert not decision.get("agent_rationale")
    assert decision["author_disposition"]["rationale"] == "Explicit completion selection."
    assert not f.ctx.owner_message_admission_agent._accepting_owner_messages


def test_bare_repeat_cannot_shadow_later_finish_with_rationale(full_loop, monkeypatch):  # noqa: F811
    f = full_loop
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    _feedback(f, monkeypatch)

    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step <= 2:
            return _nominate(str(f.model_step)), 0.0
        assert f.model_step == 3
        return _finish("finish_task", rationale="Read the partial evidence; disclose the gap."), 0.0

    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, _usage, trace = f.run()
    assert result == ANSWER and len(f.review_sends) == 1
    assert trace["acceptance_decision"]["reason"] == "author_finish"


@pytest.mark.parametrize("door", ["finish_task", "author_action", "agent_disposition"])
def test_blocking_does_not_acquire_advisory_author_authority(full_loop, monkeypatch, door):  # noqa: F811
    f = full_loop
    _feedback(f, monkeypatch)

    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return _nominate(), 0.0
        assert f.model_step <= 3
        if f.model_step == 2:
            _assert_exposed(f)
        return _finish(door), 0.0

    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    _result, _usage, trace = f.run()
    assert len(f.review_sends) == 1
    assert trace["acceptance_decision"]["reason"] == "identical_acceptance_refused"
    assert not trace["acceptance_decision"].get("author_disposition")
    assert _host_runs(trace)[0]["actors"][0]["parsed"]["criteria_used"][0]["status"] == "partial"


@pytest.mark.parametrize("door", ["finish_task", "author_action"])
def test_stop_still_requires_reason(full_loop, monkeypatch, door):  # noqa: F811
    f = full_loop
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    _feedback(f, monkeypatch)

    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return _nominate(), 0.0
        assert f.model_step <= 3
        if f.model_step == 2:
            args = {"action": "stop", "answer": ANSWER} if door == "finish_task" else {
                "author_action": "stop", "claim": ANSWER, "goal": "Report"}
            return {"content": "", "tool_calls": [call(
                "finish_task" if door == "finish_task" else "task_acceptance_review", args, "invalid-stop")]}, 0.0
        assert "requires" in str(messages) and "rationale" in str(messages)
        return _finish("finish_task"), 0.0

    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    _result, _usage, trace = f.run()
    invalid = next(row for row in trace["tool_calls"] if row["tool_call_id"] == "invalid-stop")
    assert invalid["is_error"] and len(f.review_sends) == 1
    assert trace["acceptance_decision"]["reason"] == "author_finish"


@pytest.mark.parametrize("change", ["sibling_effect", "owner_input"])
def test_informed_finish_cannot_skip_new_unobserved_work(full_loop, monkeypatch, change):  # noqa: F811
    f = full_loop
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    _feedback(f, monkeypatch)

    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return _nominate(), 0.0
        assert f.model_step <= 3
        if f.model_step == 2:
            _assert_exposed(f)
            response = _finish("finish_task")
            if change == "sibling_effect":
                response["tool_calls"].append(call("write_file", {
                    "root": "task_drive", "path": "late.txt", "content": "New unobserved evidence"}, "late-effect"))
            else:
                f.ctx._owner_directives.append({"source": "owner", "content": "New requirement"})
            return response, 0.0
        # Stop here rather than manufacturing an informed finish for changed material.
        return {"content": "", "tool_calls": [call("finish_task", {
            "action": "stop", "answer": ANSWER, "rationale": "New material still needs consideration.",
            "acceptance_subject": {"owner_source_sha256": f.ctx._acceptance_observation["owner_source_sha256"]}}, "stop")]}, 0.0

    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    _result, _usage, trace = f.run()
    assert f.model_step == 3 and len(f.review_sends) == 1
    expected = "unobserved_tool_results" if change == "sibling_effect" else "owner_input_changed"
    assert trace["completion_refusals"][0]["reason"].startswith(expected)
    assert trace["acceptance_decision"]["reason"] == "author_stop"


@pytest.mark.parametrize("door", ["finish_task", "author_action", "agent_disposition"])
@pytest.mark.parametrize("rationale", [None, "The clean approval is enough."])
def test_clean_approval_is_not_downgraded_to_author_finish(full_loop, monkeypatch, door, rationale):  # noqa: F811
    f = full_loop
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    _feedback(f, monkeypatch, "clean")

    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return _nominate(), 0.0
        assert f.model_step == 2
        _assert_exposed(f)
        return _finish(door, rationale=rationale), 0.0

    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, _usage, trace = f.run()
    assert result == ANSWER and len(f.review_sends) == 1
    assert trace["acceptance_decision"]["reason"] == "clean_pass"
    assert trace["acceptance_decision"]["status"] == "accepted"


def test_settled_nomination_without_completion_keeps_author_working(full_loop, monkeypatch):  # noqa: F811
    f = full_loop
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    _feedback(f, monkeypatch)

    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return _nominate(), 0.0
        assert f.model_step <= 3
        if f.model_step == 2:
            _assert_exposed(f)
            return {"content": "", "tool_calls": [call("send_user_message", {
                "text": "Considering the partial criterion, not selecting completion yet."}, "interim")]}, 0.0
        assert f.ctx.owner_message_admission_agent._accepting_owner_messages
        assert f.ctx._execution_trace["acceptance_decision"]["reason"] != "author_finish"
        return _finish("finish_task"), 0.0

    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    _result, _usage, trace = f.run()
    assert f.model_step == 3 and len(f.review_sends) == 1
    assert trace["acceptance_decision"]["reason"] == "author_finish"


@pytest.mark.parametrize("state", ["pending", "settled_but_unread"])
def test_predeclared_finish_waits_for_actual_settled_exposure(full_loop, monkeypatch, state):  # noqa: F811
    f = full_loop
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    _feedback(f, monkeypatch)
    f.release.clear()
    f.ctx.owner_wait_callback = f.park

    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return _nominate(), 0.0
        assert f.model_step <= 3
        if f.model_step == 2:
            assert f.entered.wait(5) and not f.release.is_set()
            assert not any(source.get("settled") for row in messages for source in row.get("review_feedback", []))
            if state == "settled_but_unread":
                f.release.set()
                with f.condition:
                    assert f.condition.wait_for(lambda: f.settled_count == 1, timeout=10)
        else:
            _assert_exposed(f)
        return _finish("finish_task"), 0.0

    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, _usage, trace = f.run()
    assert result == ANSWER and f.model_step == 3
    assert len(f.review_sends) == 1
    assert trace["acceptance_decision"]["reason"] == "author_finish"


def test_changed_criteria_do_not_reuse_old_feedback_as_authority(full_loop, monkeypatch):  # noqa: F811
    f = full_loop
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    _feedback(f, monkeypatch)

    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return _nominate(), 0.0
        assert f.model_step <= 3
        response = _finish("finish_task")
        if f.model_step == 2:
            _assert_exposed(f)
            args = json.loads(response["tool_calls"][0]["function"]["arguments"])
            args["acceptance_subject"] = {
                "owner_source_sha256": f.ctx._acceptance_observation["owner_source_sha256"],
                "effective_criteria": "Report with budget and a new independently checked timeline.",
            }
            response["tool_calls"][0]["function"]["arguments"] = json.dumps(args)
        else:
            _assert_exposed(f)
        return response, 0.0

    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, _usage, trace = f.run()
    hosts = _host_runs(trace)
    assert result == ANSWER and len(f.review_sends) == 2
    assert len(hosts) == 2 and hosts[0]["superseded_by_revision"]
    assert hosts[0]["subject_hash"] != hosts[1]["subject_hash"]
    assert trace["acceptance_decision"]["reviewer_binding_hash"] == hosts[1]["binding_hash"]
