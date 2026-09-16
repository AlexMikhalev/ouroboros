"""One frozen acceptance operation survives owner input and free collection."""

import dataclasses
import json
import threading
import time
from types import SimpleNamespace

import pytest

from ouroboros.loop_acceptance_review import acceptance_run_pending
from ouroboros.review_dispatch import collect_task_acceptance_run
from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request


@pytest.mark.parametrize("cold", [False, True])
def test_released_acceptance_collects_exact_producer_without_another_send(tmp_path, monkeypatch, cold):
    from ouroboros.review_custody import _ACTIVE, _ACTIVE_LOCK, _attempt_key
    from ouroboros.owner_mailbox import OwnerMailboxPeek

    entered, release, settled = threading.Event(), threading.Event(), threading.Event()
    calls = []
    original_settle = __import__("ouroboros.review_custody", fromlist=["_settle_review_attempt"])._settle_review_attempt

    def settle(*args, **kwargs):
        try:
            return original_settle(*args, **kwargs)
        finally:
            settled.set()

    monkeypatch.setattr("ouroboros.review_custody._settle_review_attempt", settle)

    class HeldModel:
        def chat(self, **kwargs):
            calls.append(kwargs)
            entered.set()
            assert release.wait(10), "fixture did not release its model"
            return {"content": json.dumps({"verdict": "FAIL", "summary": "Keep this actual criticism",
                                           "findings": []})}, {"prompt_tokens": 5, "completion_tokens": 2}

    ctx = SimpleNamespace(task_id="acceptance-root", task_attempt=1, drive_root=tmp_path,
                          budget_drive_root=tmp_path, task_metadata={}, pending_events=[], event_queue=None)
    request = ReviewRequest(surface="task_acceptance", task_id=ctx.task_id, goal="original goal",
                            subject="complete original result", evidence={"requirement": "exact original"},
                            retry_key="acceptance-subject-one", drain_deadline=time.monotonic())
    slot = ReviewSlot(slot_id="one", model="model/original", effort="high", timeout_sec=20)
    try:
        first = run_review_request(request, slots=[slot], drive_root=tmp_path, usage_ctx=ctx, llm=HeldModel())
        assert entered.wait(5)
        assert acceptance_run_pending(first)
        frozen = json.loads(json.dumps(dataclasses.asdict(first)))
        assert frozen["slot_roster"][0]["model"] == "model/original"
        assert frozen["request"]["subject"] == "complete original result"
        # Main remains independent of the frozen worker input.
        ctx.messages = [{"role": "user", "content": "How is it going?"}]
        ctx._owner_directives = [{"content": "How is it going?"}]
        still_running = collect_task_acceptance_run(frozen, drive_root=tmp_path, usage_ctx=ctx)
        assert acceptance_run_pending(still_running)
        assert len(calls) == 1
        release.set()
        assert settled.wait(5)
        assert OwnerMailboxPeek().pending(tmp_path, ctx.task_id, set(), 1)
        with _ACTIVE_LOCK:
            assert _attempt_key(request, slot) not in _ACTIVE
        if cold:
            ctx = SimpleNamespace(task_id=ctx.task_id, task_attempt=1, drive_root=tmp_path,
                                  budget_drive_root=tmp_path, task_metadata={}, pending_events=[], event_queue=None)
        result = collect_task_acceptance_run(frozen, drive_root=tmp_path, usage_ctx=ctx)
        assert not acceptance_run_pending(result)
        assert result.actors[0]["parsed"]["verdict"] == "FAIL"
        assert result.actors[0]["operation_id"] == first.actors[0]["operation_id"]
        assert result.request["subject"] == "complete original result"
        assert result.request["evidence"] == {"requirement": "exact original"}
        assert len(calls) == 1
    finally:
        release.set()
        assert settled.wait(5)


def test_missing_recorded_roster_does_not_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr("ouroboros.review_substrate.run_review_request",
                        lambda *a, **k: pytest.fail("missing source bought another review"))
    request = dataclasses.asdict(ReviewRequest(surface="task_acceptance", goal="g", retry_key="subject"))
    with pytest.raises(ValueError, match="roster is unavailable"):
        collect_task_acceptance_run({"request": request}, drive_root=tmp_path, usage_ctx=SimpleNamespace())


def test_review_park_is_not_a_question_and_preserves_operation(tmp_path):
    from ouroboros.artifacts import read_actor_source_bytes
    from ouroboros.owner_wait import wait_after_tools
    from tests.test_owner_wait import context

    ctx, captured = context(tmp_path), []
    ctx._owner_wait_requested = ""
    ctx._task_acceptance_pending = "binding-original"
    ctx.owner_wait_callback = lambda owner, checkpoint: captured.append(checkpoint)
    trace = {"review_runs": [{"binding_hash": "binding-original", "request": {"subject": "full result"}}]}
    wait_after_tools(ctx, [], trace, {}, 3, [], set(), review_binding="binding-original")
    assert len(captured) == 1
    assert captured[0]["quiz_id"] == ""
    assert captured[0]["reason"] == "review"
    source = json.loads(read_actor_source_bytes(tmp_path, ctx.task_id, captured[0]["source_ref"]))
    assert source["acceptance"]["_task_acceptance_pending"] == "binding-original"
    assert source["trace"] == trace


def test_unknown_custody_is_not_an_active_wait():
    assert not acceptance_run_pending({"actors": [{"operation_state": "custody_lost", "late_result_pending": True}]})
    assert acceptance_run_pending({"actors": [{"operation_state": "pending_dispatch"}]})


def _host_run(**overrides):
    run = {"authority": "host_root", "request": {"surface": "task_acceptance", "retry_key": "subject"},
           "slot_roster": [{"slot_id": "one", "model": "m", "route": "api_chat"}],
           "actors": [{"operation_state": "pending_dispatch"}]}
    run.update(overrides)
    return run


def test_a_settled_acceptance_run_is_never_collected_twice(tmp_path, monkeypatch):
    """``acceptance_run_pending`` is the whole idempotency guard: a settled,
    custody-lost or agent-tool run is never re-read, so no marker field exists."""
    from ouroboros import review_dispatch

    monkeypatch.setattr(review_dispatch, "collect_task_acceptance_run",
                        lambda *a, **k: pytest.fail("a run that is not pending was collected again"))
    trace = {"review_runs": [
        _host_run(actors=[{"operation_state": "settled", "parsed": {"verdict": "PASS"}}]),
        _host_run(actors=[{"operation_state": "custody_lost", "late_result_pending": True}]),
        _host_run(authority="agent_tool"),
        _host_run(slot_roster=[]),
        _host_run(request=None),
    ]}
    advanced = review_dispatch.reconcile_pending_acceptance_runs(
        trace, drive_root=tmp_path, usage_ctx=SimpleNamespace(),
    )
    assert advanced == 0


def test_an_uncollectable_pending_run_is_left_alone_and_never_raises(tmp_path, monkeypatch):
    """Fail-soft like ``plan_review_collect.collect_before_gate``: the pass
    continues, the run stays pending, and nothing dispatches."""
    from ouroboros import review_dispatch

    monkeypatch.setattr("ouroboros.review_substrate.run_review_request",
                        lambda *a, **k: pytest.fail("a stranded run bought another review"))
    for error in (ValueError("recorded acceptance roster is unavailable"),
                  KeyError("request"), OSError("custody store unavailable"), TimeoutError("slow")):
        def raising(*_a, _error=error, **_k):
            raise _error

        monkeypatch.setattr(review_dispatch, "collect_task_acceptance_run", raising)
        pending = _host_run()
        advanced = review_dispatch.reconcile_pending_acceptance_runs(
            {"review_runs": [pending]}, drive_root=tmp_path, usage_ctx=SimpleNamespace(),
        )
        assert advanced == 0 and acceptance_run_pending(pending)


def test_the_settlement_wake_names_the_free_collection_route(tmp_path):
    """The acceptance tool schema has no collect verb, so the wake must name the
    one free route back to the recorded verdicts."""
    from ouroboros.loop_acceptance_review import announce_acceptance_settlement
    from ouroboros.owner_mailbox import _mailbox_path

    announce_acceptance_settlement(
        SimpleNamespace(drive_root=tmp_path),
        SimpleNamespace(retry_key="acceptance-subject-one", task_id="root"),
        {"slots": {"one": {}}},
    )
    rows = [json.loads(line) for line in
            _mailbox_path(tmp_path, "root").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1 and rows[0]["provenance"] == "system"
    assert "acceptance-subject-one" in rows[0]["text"] and "1 released reviewer slots" in rows[0]["text"]
    assert "keep control to collect the recorded verdicts at no cost" in rows[0]["text"]
    assert "this notification is not a verdict" in rows[0]["text"]


def test_every_acceptance_wake_reoffers_a_changed_keep_contract(tmp_path, monkeypatch):
    """A replacement candidate inherits ``control_episode_seen``; the contract for
    the NEW candidate must still be shown, while identical bytes are not repeated."""
    from ouroboros.loop_acceptance_review import wait_for_acceptance_feedback
    from tests.test_delivery_forced_finalization import _forced_test_context

    loop, registry, ctx, trace = _forced_test_context(tmp_path)
    monkeypatch.setattr("ouroboros.owner_wait.wait_after_tools", lambda *_a, **_k: None)
    registry._ctx._task_acceptance_pending = "binding-one"
    blocks = lambda: str(ctx.messages).count("[DELIVERY_FINALIZATION_CONTROL]")  # noqa: E731

    first = loop._replace_delivery_candidate(registry, ctx, trace, "First complete answer.", control="candidate")
    assert first.control_episode_seen is False
    wait_for_acceptance_feedback(registry, ctx, trace, [], set())
    assert first.control_episode_seen is True and blocks() == 1
    # The same candidate renders identical bytes: a second wake adds no noise.
    wait_for_acceptance_feedback(registry, ctx, trace, [], set())
    assert blocks() == 1
    second = loop._replace_delivery_candidate(registry, ctx, trace, "Second complete answer.", control="candidate")
    assert second.control_episode_seen is True, "the inherited flag is what hid the contract"
    wait_for_acceptance_feedback(registry, ctx, trace, [], set())
    assert blocks() == 2 and second.content_sha256[:12] in str(ctx.messages)


def test_the_acceptance_wake_keeps_the_one_repair_already_spent(tmp_path, monkeypatch):
    """Scope review round 1: re-arming on every wake reset ``repair_attempted``,
    so a candidate could burn one malformed-control repair per wake instead of
    one per episode. The wake's re-offer preserves the spent repair; an ordinary
    arm (something changed) still opens a fresh episode."""
    from ouroboros.loop_acceptance_review import wait_for_acceptance_feedback
    from tests.test_delivery_forced_finalization import _forced_test_context

    loop, registry, ctx, trace = _forced_test_context(tmp_path)
    monkeypatch.setattr("ouroboros.owner_wait.wait_after_tools", lambda *_a, **_k: None)
    registry._ctx._task_acceptance_pending = "binding-one"
    candidate = loop._replace_delivery_candidate(registry, ctx, trace, "Complete answer.", control="candidate")
    wait_for_acceptance_feedback(registry, ctx, trace, [], set())
    candidate.repair_attempted = True  # the one repair was spent on a malformed control
    wait_for_acceptance_feedback(registry, ctx, trace, [], set())
    assert candidate.repair_attempted is True, "the wake re-offer must not refund the repair"
    loop._arm_delivery_control(registry, ctx, trace)
    assert candidate.repair_attempted is False, "an ordinary arm opens a new episode"


def test_the_rearmed_contract_never_rewrites_an_already_sent_row(tmp_path):
    """Issue #906: merging into a sent row discards the conversation cache. Every
    wake re-arms, so the control block must take the execution slot and append."""
    import copy as _copy

    from ouroboros.transcript_prefix import observe_send
    from tests.test_delivery_forced_finalization import _forced_test_context

    loop, registry, ctx, trace = _forced_test_context(tmp_path)
    loop._replace_delivery_candidate(registry, ctx, trace, "Complete answer.", control="candidate")
    ctx.messages.append({"role": "user", "content": "An owner follow-up that already went out."})
    observe_send(registry._ctx, ctx.messages, round_idx=1)
    sent = _copy.deepcopy(ctx.messages[-1])

    loop._arm_delivery_control(registry, ctx, trace)

    assert sent in ctx.messages, "an already-sent message was rewritten"
    control = [row for row in ctx.messages
               if "[DELIVERY_FINALIZATION_CONTROL]" in str(row.get("content") or "")]
    assert len(control) == 1 and control[0] is not ctx.messages[ctx.messages.index(sent)]
    # Only the acceptance wake's repeated re-offer is deduplicated. Every other
    # caller arms because something changed, so it always appends.
    loop._arm_delivery_control(registry, ctx, trace)
    assert str(ctx.messages).count("[DELIVERY_FINALIZATION_CONTROL]") == 2
