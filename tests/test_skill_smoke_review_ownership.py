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
import time
from types import SimpleNamespace

import pytest

import tests.test_skill_smoke_official as smoke
from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request
from tests.conftest import _THREAD_LEAK_GRACE_SEC
from tests.test_skill_smoke_official import owned_review_workers, review_env  # noqa: F401 -- fixtures

_HELD_SEC = _THREAD_LEAK_GRACE_SEC + 1.0  # outlasts the thread-leak check's grace


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


def test_settling_waits_out_a_late_worker_but_names_a_settled_attempt_left_behind(tmp_path):
    from ouroboros import review_custody

    before = set(smoke.active_review_attempts())
    release = threading.Event()
    _late_review(tmp_path, release, "smoke-settle-late")
    try:
        assert [row["late"] for key, row in smoke.active_review_attempts().items() if key not in before] == [True]
        threading.Timer(0.5, release.set).start()
        assert smoke.settle_review_workers(before) == ""  # the by-design late worker is waited out
    finally:
        release.set()  # a failed assertion above must not leave the worker held for its bound

    # An attempt whose caller took a settled answer must not outlive the review: no long wait for it.
    lingering = review_custody.ActiveReviewAttempt(key="smoke-lingering", operation_id="op-lingering")
    with review_custody._ACTIVE_LOCK:
        review_custody._ACTIVE[lingering.key] = lingering
    try:
        started = time.monotonic()
        problem = smoke.settle_review_workers(before)
        assert "none of them late" in problem and "op-lingering" in problem, problem
        assert time.monotonic() - started < 10, "a settled attempt got the late worker's long wait"
    finally:
        with review_custody._ACTIVE_LOCK:
            review_custody._ACTIVE.pop(lingering.key, None)


def test_a_leak_beside_a_late_worker_is_named_after_the_late_worker_retires(tmp_path):
    from ouroboros import review_custody

    before = set(smoke.active_review_attempts())
    release = threading.Event()
    actor = _late_review(tmp_path, release, "smoke-settle-mixed")
    lingering = review_custody.ActiveReviewAttempt(key="smoke-mixed-lingering", operation_id="op-mixed-lingering")
    with review_custody._ACTIVE_LOCK:
        review_custody._ACTIVE[lingering.key] = lingering
    try:
        threading.Timer(_HELD_SEC, release.set).start()  # past the leak's grace: the late worker outlives it
        started = time.monotonic()
        problem = smoke.settle_review_workers(before)
        assert "none of them late" in problem and "op-mixed-lingering" in problem, problem
        # The fixture restores the environment right after this report: never under a running late worker.
        assert actor["operation_id"] not in smoke.wait_for_review_workers(before, 0, poll_sec=0).values()
        assert time.monotonic() - started < 10, "the leak got the late worker's long wait"
    finally:
        release.set()
        with review_custody._ACTIVE_LOCK:
            review_custody._ACTIVE.pop(lingering.key, None)


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


def _wave(*records):
    return [{"raw_actor_records": list(records)}]


def test_a_provider_outage_is_told_apart_from_a_review_that_answered(monkeypatch):
    transport = {"status": "error", "transport_status": "provider_transport_error"}
    refused = {**transport, "http_status": 429, "failure_code": "429", "tokens_in": 0, "tokens_out": 0}
    unavailable = {**transport, "http_status": "503"}
    # The model answered with calls the native executor could not use: labelled a transport error, no HTTP status.
    malformed = {**transport, "failure_code": "native_round_without_progress"}
    rejected = {**transport, "http_status": 400}  # the request itself was refused: ours to fix, never waited out
    unparseable = {"status": "error", "transport_status": "ok", "raw_text": "not a verdict"}
    answered = {"status": "ok", "transport_status": "ok"}
    for history, outage in ((_wave(refused), True), (_wave(refused, unavailable), True),
                            (_wave(refused, answered), False), (_wave(malformed), False),
                            (_wave(refused, malformed), False), (_wave(rejected), False),
                            (_wave({**refused, "http_status": True}), False),
                            (_wave(unparseable), False), (_wave(), False), ([], False)):
        monkeypatch.setattr(smoke, "load_history", lambda *_a, history=history, **_k: history)
        assert smoke._provider_never_answered("skill") is outage, history


def test_every_wave_the_retries_can_send_is_within_the_review_cycle_cap(review_env):  # noqa: F811 -- fixture
    """A refused wave is a paid cycle too, so under the shipped cap of two the outage waits' waves were
    refused locally before they reached the provider (proving run 39251921588)."""
    from ouroboros.review_cycles import review_max_cycles
    from ouroboros.settings_defaults import SETTINGS_DEFAULTS

    assert int(SETTINGS_DEFAULTS["OUROBOROS_REVIEW_MAX_CYCLES"]) < smoke._OUTAGE_RETRIES + 2
    assert review_max_cycles() == smoke._REVIEW_DISPATCHES == smoke._OUTAGE_RETRIES + 2


def test_an_outage_is_waited_out_but_an_answered_failure_keeps_its_one_retry(monkeypatch):
    sleeps, flows = [], []
    monkeypatch.setattr(smoke.time, "sleep", sleeps.append)
    monkeypatch.setattr(smoke, "_smoke_retry_refusal", lambda *_a: "")
    monkeypatch.setattr(smoke, "_review_diag", lambda *_a: "")

    def run(results, outage):
        sleeps.clear()
        flows.clear()
        monkeypatch.setattr(smoke, "_run_review_flow", lambda *_a: flows.append(1) or results[len(flows) - 1])
        monkeypatch.setattr(smoke, "_provider_never_answered", lambda _name: outage(len(flows)))
        return smoke._review_with_one_settled_retry("skill", "skill")

    pending, clean = ("pending", "quorum failure", "not_required", {}), ("clean", "", "not_required", {})
    # Two outages, then the provider answers: waited out twice, no settled retry spent.
    status, *_rest, note = run([pending, pending, clean], lambda calls: calls <= 2)
    assert status == "clean" and len(flows) == 3 and sleeps == [smoke._OUTAGE_BACKOFF_SEC] * 2, (flows, sleeps)
    assert "2 provider outage" in note
    # A model that answered but failed: exactly one settled retry, never the outage wait.
    status, *_rest, note = run([pending, pending, clean], lambda _calls: False)
    assert status == "pending" and len(flows) == 2 and sleeps == [] and "one settled retry" in note
    # A route that never answers: bounded outage waits, then the one settled retry, then red.
    status, *_rest, note = run([pending] * 9, lambda _calls: True)
    assert status == "pending" and len(sleeps) == smoke._OUTAGE_RETRIES and len(flows) == smoke._OUTAGE_RETRIES + 2
