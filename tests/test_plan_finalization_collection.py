"""The retired engine's panel fixture (shared with ``tests/test_plan_wave_pending_truth.py``) and the
fact that an ordinary task opens no panel. The old gate-side collection of a running legacy wave is
retired: the live gate reads a legacy wave through ``plan_record.import_legacy_v2`` only (its landed
blocking findings hold, its unanswered reviewers are disclosed as one pending ask) and writes nothing."""
from __future__ import annotations


import pytest

from ouroboros.context_health import build_health_invariants
from ouroboros.owner_hurry import force_plan_decision
from tests.test_health_invariants_ownership import _env
from tests.test_plan_review_engine import CLEAN
from tests.test_plan_review_engine import harness as _engine_harness
from tests.test_plan_review_event_route import _HeldExecutor, _mailbox_entries, _wait_until

harness = _engine_harness


class _AnswerExecutor(_HeldExecutor):
    def __init__(self, answer):
        super().__init__()
        self.answer = answer

    def execute(self):
        from ouroboros.review_execution import ReviewAttemptResult

        attempt = super().execute()
        return ReviewAttemptResult(message={"content": self.answer}, usage=attempt.usage,
                                   raw_text=self.answer)


@pytest.fixture
def panel(monkeypatch):
    executors = {f"s{i}": _AnswerExecutor(CLEAN) for i in range(1, 4)}
    monkeypatch.setattr("ouroboros.review_substrate._review_route_executor",
                        lambda assignment, **_kw: executors[assignment.slot.slot_id])
    yield executors
    for executor in executors.values():
        executor.release.set()
    # Custody callbacks, not just the fake transport, must have finished.
    from ouroboros import review_custody

    assert _wait_until(lambda: not review_custody._RELEASED_WAVES)


def _sent(panel):
    return sum(executor.execute_calls for executor in panel.values())


def _settle(panel, ctx):
    for executor in panel.values():
        executor.release.set()
    assert _wait_until(lambda: len(_mailbox_entries(ctx.drive_root, ctx.task_id)) == 1)


def test_ordinary_task_and_unattributed_health_do_not_open_a_panel(harness, panel):
    ctx = harness.make_ctx()
    assert force_plan_decision(ctx, {}, enforcement="blocking")["required"] is False
    assert "PLAN REVIEW WAVE OPEN" not in build_health_invariants(_env(harness.drive))
    assert _sent(panel) == 0
