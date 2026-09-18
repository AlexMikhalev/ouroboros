"""Current author plans retain exact source and independent critic authority."""
import copy
import json

import pytest

from ouroboros.task_results import (closed_plan_review_wave, load_plan_review_state,
    plan_review_gate_projection, record_plan_review_wave)
from ouroboros.tools import plan_review as pr
from ouroboros.tools.plan_review_artifacts import current_author_plan, authority_wave
from tests.test_plan_review_engine import harness, _call, _finding, DECK_SPEC  # noqa: F401


@pytest.mark.parametrize("enforcement", ["advisory", "blocking"])
@pytest.mark.parametrize("action", ["finish", "stop"])
def test_last_critic_correction_retains_current_source(harness, monkeypatch, enforcement, action):  # noqa: F811
    h = harness
    h.state["enforcement"] = enforcement
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", enforcement)
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "1")
    ctx = h.make_ctx()
    findings = json.dumps([_finding("bad-budget", "blocking", breaks="claim_1")])
    transport = h.install({slot: findings for slot in ("s1", "s2", "s3")})
    _call(ctx)
    before = load_plan_review_state(h.drive, ctx.task_id)
    critic = copy.deepcopy(before["waves"][0])
    spec = {**DECK_SPEC, "acceptance_claims": ["the corrected claim"]}
    result = _call(ctx, spec, plan="Corrected complete plan.", review_disposition={
        "review_fingerprint": critic["request_fingerprint"], "items": [], "author_action": action,
        "author_disposition": {"disposition": "partial", "rationale": "Corrected the budget and saved the exact plan."}})
    assert "Current author plan saved" in result
    after = load_plan_review_state(h.drive, ctx.task_id)
    assert len(transport.calls) == 1 and after["cycles_paid"] == before["cycles_paid"] == 1
    assert authority_wave(h.drive, ctx.task_id, after["waves"][0]) == critic
    assert closed_plan_review_wave(after) is None
    authored = current_author_plan(h.drive, ctx.task_id, after)
    assert authored["spec"]["acceptance_claims"][0]["claim"] == "the corrected claim"
    assert authored["plan_prose"] == "Corrected complete plan."
    gate = plan_review_gate_projection(after, enforcement)
    assert gate["closed"] is False
    assert gate["allow"] is True  # at cap: terminalization, never a clean verdict
    assert gate["status"] == ("author_stopped" if action == "stop" else "advisory_open" if enforcement == "advisory" else "cycles_exhausted")
    if action == "stop" or enforcement == "blocking":
        from ouroboros.outcomes import derive_loop_outcome
        from ouroboros.project_dialogue import outcome_phase
        outcome = derive_loop_outcome("Saved current plan; no implementation.", {}, {"force_plan_decision": gate})
        assert outcome_phase({"status": "completed", "outcome_axes": outcome["outcome_axes"]}, {}) == "error"
    from ouroboros.review_evidence_sections import _accept_effective_claims
    claims, source, _ = _accept_effective_claims(ctx, {}, h.drive, ctx.task_id)
    assert (source == "author_plan") is (enforcement == "advisory" and action == "finish")
    if source == "author_plan":
        assert claims[0]["claim"] == "the corrected claim"
        from ouroboros.review_evidence import build_task_acceptance_evidence
        packet = build_task_acceptance_evidence(ctx, llm_trace={}, drive_root=h.drive, task_id=ctx.task_id, budget_chars=1000000)
        assert packet["acceptance_claims_source"] == "author_plan"
        assert packet["task_contract"]["acceptance_claims"][0]["claim"] == "the corrected claim"
    # A late old-wave update preserves the independently selected current source.
    record_plan_review_wave(h.drive, ctx.task_id, critic)
    assert load_plan_review_state(h.drive, ctx.task_id)["current_attempt"] == after["current_attempt"]


def test_explicit_blocking_stop_before_cap_releases_only_finalization(harness):  # noqa: F811
    h = harness
    ctx = h.make_ctx()
    h.install({"s1": None, "s2": None, "s3": None})
    _call(ctx)
    before = load_plan_review_state(h.drive, ctx.task_id)
    assert not plan_review_gate_projection(before, "blocking")["allow"]
    text = pr._handle_plan_task(ctx, review_disposition={
        "review_fingerprint": before["current_attempt"]["fingerprint"], "items": [], "author_action": "stop",
        "author_disposition": {"disposition": "deferred", "rationale": "Review is unavailable; retain this plan for a later task."}})
    assert "No implementation approval" in text
    after = load_plan_review_state(h.drive, ctx.task_id)
    assert plan_review_gate_projection(after, "blocking")["status"] == "author_stopped"
    assert closed_plan_review_wave(after) is None


def test_advisory_author_can_select_current_plan_after_no_dispatch_outcome(harness, monkeypatch):  # noqa: F811
    h = harness
    h.state.update(enforcement="advisory", slots=[])
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    ctx = h.make_ctx()
    unavailable = _call(ctx)
    assert "No review models configured" in unavailable
    before = load_plan_review_state(h.drive, ctx.task_id)
    assert before["cycles_paid"] == 0 and not before["waves"]
    result = _call(ctx, {**DECK_SPEC, "acceptance_claims": ["current claim"]},
        review_disposition={"review_fingerprint": before["current_attempt"]["fingerprint"], "items": [],
            "author_action": "finish", "author_disposition": {"disposition": "accepted", "rationale": "The unavailable reviewer was disclosed; proceed with the current plan."}})
    assert "Advisory author finish permits proceeding" in result
    after = load_plan_review_state(h.drive, ctx.task_id)
    assert after["cycles_paid"] == 0 and not after["waves"]
    assert current_author_plan(h.drive, ctx.task_id, after)["spec"]["acceptance_claims"][0]["claim"] == "current claim"


def test_full_plan_needs_explicit_action_even_with_a_prior_wave(harness, monkeypatch):  # noqa: F811
    h = harness
    h.state["enforcement"] = "advisory"
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    ctx = h.make_ctx()
    transport = h.install({slot: json.dumps([_finding("budget", "blocking", breaks="claim_1")])
                           for slot in ("s1", "s2", "s3")})
    _call(ctx)
    before = load_plan_review_state(h.drive, ctx.task_id)
    result = _call(ctx, plan="Changed plan without a finish action.", review_disposition={
        "review_fingerprint": before["waves"][-1]["request_fingerprint"], "items": [],
        "author_disposition": {"disposition": "partial", "rationale": "This is a stance, not a finish choice."}})
    assert "PLAN_REVIEW_DISPOSITION_MIXED_ENVELOPE" in result
    assert load_plan_review_state(h.drive, ctx.task_id) == before
    assert len(transport.calls) == 1
