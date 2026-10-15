"""A current author's plan keeps criticism visible without inheriting its approval."""
import json


from ouroboros.owner_hurry import force_plan_decision
from ouroboros.task_results import closed_plan_review_wave, load_plan_review_state, plan_review_gate_projection
from tests.test_plan_review_engine import DECK_SPEC, _call, _finding, harness as _harness

harness = _harness


def test_unreadable_plan_source_does_not_invent_a_critic(harness, monkeypatch):
    """A legacy plan-review state that cannot be read is an engaged gate with no objections of
    its own: it holds as ``unreadable`` and invents neither a critic nor a verdict."""
    ctx = harness.make_ctx(force_plan=True)
    monkeypatch.setattr("ouroboros.task_results.load_plan_review_state", lambda *_a: (_ for _ in ()).throw(ValueError("source gap")))
    decision = force_plan_decision(ctx, {}, enforcement="blocking")
    assert decision["allow"] is False and decision["status"] == "unreadable" and decision["unreadable"] is True
    assert decision["open_objections"] == [] and decision["holding"] == []
    monkeypatch.setattr("ouroboros.task_results.load_plan_review_state", lambda *_a: {})
    readable = force_plan_decision(ctx, {}, enforcement="blocking")
    assert readable["status"] == "force_plan_unmet" and readable["unreadable"] is False


def test_an_author_finish_at_a_spent_cap_releases_on_its_own_wave_too():
    """tools/plan_review._apply_author_subject stamps cycles_exhausted on the ATTEMPT, not
    on the wave, so reading the wave alone held a task whose author may only finish
    honestly and cannot buy another cycle."""
    state = {"schema_version": 2,
             "current_attempt": {"fingerprint": "F1", "status": "cycles_exhausted",
                                 "reason": "author_current_plan"},
             "waves": [{"request_fingerprint": "F1", "aggregate": "REVISE_PLAN", "closed": False}]}
    gate = plan_review_gate_projection(state, "blocking")
    assert gate["status"] == "cycles_exhausted" and gate["allow"] is True
    assert gate["review_capacity_reason"] == "review_cycles_exhausted"


def test_a_spent_cap_does_not_release_while_a_paid_slot_can_still_settle():
    """_apply_author_subject is the one cycles_exhausted writer with no in-flight guard, so
    reading the attempt status alone would release finalization while a paid reviewer could
    still close the wave — trading an honest hold for a premature blocked terminal."""
    state = {"schema_version": 2,
             "current_attempt": {"fingerprint": "F1", "status": "cycles_exhausted",
                                 "reason": "author_current_plan"},
             "waves": [{"request_fingerprint": "F1", "aggregate": "REVISE_PLAN",
                        "closed": False, "custody_pending": True}]}
    gate = plan_review_gate_projection(state, "blocking")
    assert gate["status"] == "open" and gate["allow"] is False
    assert gate["custody_pending"] is True


def test_author_selection_after_a_green_critic_publishes_the_critics_real_pair(harness, monkeypatch):
    """A revised plan selected after a GREEN critic wave gets NO invented verdict: the tool
    result carries the critic wave's real (GREEN, closed) pair labelled historical, the text
    says the earlier plan was GREEN and this plan has none of its own, the durable gate
    projection stays open with `historical_critic`, and the call succeeds (the validator
    used to raise on GREEN + closed=False AFTER the plan was persisted and narrated).
    Re-selecting the reviewed GREEN plan itself keeps its own pair, unlabelled; a
    REVISE_PLAN critic keeps today's open pair."""
    from ouroboros.tools import plan_review_legacy as pr
    from ouroboros.tools.plan_review_runtime import publish_plan_review_projection

    published = []

    def capture(ctx, review, text):
        published.append(dict(review))
        return publish_plan_review_projection(ctx, review, text)

    monkeypatch.setattr(pr, "_publish_plan_review_projection", capture)
    h = harness
    h.state["enforcement"] = "advisory"
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    clean = "[]\nNO_FINDINGS"
    h.install({s: clean for s in ("s1", "s2", "s3")})
    ctx = h.make_ctx()
    _call(ctx)
    critic_fp = load_plan_review_state(h.drive, ctx.task_id)["current_attempt"]["fingerprint"]
    ctx._active_builtin_tool_result = None  # the registry sidecar seam: the typed meta lands here
    result = _call(ctx, {**DECK_SPEC, "acceptance_claims": ["Corrected claim"]}, plan="Corrected plan.",
        review_disposition={"review_fingerprint": critic_fp, "items": [], "author_action": "finish",
                            "author_disposition": {"disposition": "partial", "rationale": "Considered."}})
    assert "Current author plan saved" in result and "ERROR" not in result
    assert f"Earlier plan {critic_fp} was GREEN; this revised plan has no verdict of its own." in result
    assert published[-1]["aggregate_signal"] == "GREEN" and published[-1]["closed"] is True
    assert published[-1]["historical_critic"] is True and published[-1]["author_action"] == "finish"
    meta = ctx._active_builtin_tool_result.meta
    assert meta == {"plan_review_outcome": "GREEN", "plan_review_closed": True,
                    "plan_review_historical_critic": True}
    gate = plan_review_gate_projection(load_plan_review_state(h.drive, ctx.task_id), "advisory")
    assert gate["historical_critic"] is True and gate["outcome"] == "GREEN"
    assert gate["closed"] is False and gate["status"] == "advisory_open" and gate["allow"] is True
    assert closed_plan_review_wave(load_plan_review_state(h.drive, ctx.task_id)) is None
    # Quiet side 1: selecting the reviewed GREEN plan itself is its own verdict, unlabelled.
    own = h.make_ctx(task_id="task-own")
    own._active_builtin_tool_result = None
    _call(own)
    own_fp = load_plan_review_state(h.drive, "task-own")["current_attempt"]["fingerprint"]
    same = _call(own, review_disposition={"review_fingerprint": own_fp, "items": [], "author_action": "finish",
                                          "author_disposition": {"disposition": "accepted", "rationale": "As reviewed."}})
    assert "Current author plan saved" in same and "no verdict of its own" not in same
    assert own._active_builtin_tool_result.meta == {"plan_review_outcome": "GREEN", "plan_review_closed": True}
    assert published[-1]["historical_critic"] is False
    # Quiet side 2: a REVISE_PLAN critic keeps its open pair for the revised plan.
    h.install({s: json.dumps([_finding("first", "blocking", breaks="claim_1")]) for s in ("s1", "s2", "s3")})
    revised = h.make_ctx(task_id="task-revise")
    _call(revised)
    revise_fp = load_plan_review_state(h.drive, "task-revise")["current_attempt"]["fingerprint"]
    _call(revised, {**DECK_SPEC, "acceptance_claims": ["Corrected claim"]}, plan="Corrected plan.",
          review_disposition={"review_fingerprint": revise_fp, "items": [], "author_action": "finish",
                              "author_disposition": {"disposition": "partial", "rationale": "Considered."}})
    assert (published[-1]["aggregate_signal"], published[-1]["closed"]) == ("REVISE_PLAN", False)
    assert published[-1]["historical_critic"] is True
