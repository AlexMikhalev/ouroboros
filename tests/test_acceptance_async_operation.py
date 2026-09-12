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
