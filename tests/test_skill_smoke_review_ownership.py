"""Keyless checks of the skill-smoke live review lane's own machinery.

``tests/test_skill_smoke_official.py::test_review_grants_and_enable`` runs a paid
live review only in its CI leg, and its verdict stays live by owner decision. Two
of its properties are machinery, not model behaviour, so they are pinned here with
test doubles and no provider: the review runs under the product's own slot window
and the test's outer budget covers that window, and the test owns review custody's
by-design late worker (``owned_review_workers``) instead of tripping the session's
thread-leak check.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import tests.test_skill_smoke_official as smoke
from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request
from tests.test_skill_smoke_official import owned_review_workers, review_env  # noqa: F401 -- fixtures

_HELD_SEC = 3.0  # longer than the thread-leak check's 2 s grace (tests/conftest.py)


@pytest.fixture()
def stale_review_overrides(monkeypatch):
    """A runner environment that still carries the retired test-side windows."""
    monkeypatch.setenv("OUROBOROS_REVIEW_MODEL_TIMEOUT_SEC", "300")
    monkeypatch.setenv("OUROBOROS_LLM_TRANSPORT_READ_TIMEOUT_SEC", "120")


def test_the_live_review_runs_in_the_product_window_and_its_budget_covers_it(
        stale_review_overrides, review_env):  # noqa: F811 -- pytest fixture injection
    from ouroboros.review_custody import _logical_timeout

    # As the skill review builds its api slot (no timeout fields) for a context with no task deadline.
    slot = ReviewSlot(slot_id="skill-smoke-reviewer", model=smoke._REVIEW_MODEL)
    window = _logical_timeout(slot, ReviewRequest(surface="skill_review", goal="review"), {})
    assert window == smoke._REVIEW_WINDOW_SEC  # the shipped transport bound: review_env left no override
    marker = next(mark for mark in smoke.test_review_grants_and_enable.pytestmark if mark.name == "timeout")
    # Two waves at the window, a late worker's drain to the same bound, and setup on top.
    assert marker.args == (smoke._REVIEW_TEST_TIMEOUT_SEC,) and smoke._REVIEW_TEST_TIMEOUT_SEC > 3 * window


def _late_review(tmp_path, release: threading.Event, task_id: str):
    """One review whose slot window ends while its worker still holds the paid call."""

    class HeldLLM:
        def chat(self, **_kwargs):
            assert release.wait(30), "the test never released the held review call"
            return {"content": '{"verdict":"PASS","findings":[],"summary":"late"}'}, {}

    ctx = SimpleNamespace(task_id=task_id, task_attempt=1, task_metadata={}, event_queue=None, pending_events=[])
    request = ReviewRequest(surface="scope", goal="review diff", task_id=task_id, task_attempt=1)
    slot = ReviewSlot(slot_id="slot_a", model="same/model", timeout_sec=0.05, transport_timeout_sec=30)
    result = run_review_request(request, slots=[slot], drive_root=tmp_path, llm=HeldLLM(), usage_ctx=ctx)
    actor = result.actors[0]
    assert (actor["operation_state"], actor["late_result_pending"]) == ("in_flight", True), actor
    return actor


def test_the_drain_reports_a_held_late_worker_and_returns_once_it_retires(tmp_path):
    release = threading.Event()
    before = set(smoke.active_review_attempts())
    actor = _late_review(tmp_path, release, "smoke-drain-direct")
    try:
        still = smoke.wait_for_review_workers(before, 0.3, poll_sec=0.05)
        assert list(still.values()) == [actor["operation_id"]]  # a worker that has not retired is named
    finally:
        release.set()
    assert smoke.wait_for_review_workers(before, 10, poll_sec=0.05) == {}


def test_owned_review_workers_holds_teardown_until_the_late_worker_retires(
        tmp_path, owned_review_workers):  # noqa: F811 -- pytest fixture injection
    """Without the fixture's wait this item fails the session's thread-leak check:
    the late worker outlives the test body by more than that check's grace."""
    release = threading.Event()
    before = set(smoke.active_review_attempts())
    actor = _late_review(tmp_path, release, "smoke-drain-fixture")
    threading.Timer(_HELD_SEC, release.set).start()
    # The body ends while the worker still holds the call: only the fixture's teardown waits for it.
    assert actor["operation_id"] in smoke.wait_for_review_workers(before, 0, poll_sec=0).values()
