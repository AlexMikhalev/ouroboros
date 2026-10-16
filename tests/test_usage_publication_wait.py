"""Only observed publication probes/waits spend the publication budget, not other contention."""
import asyncio
from types import SimpleNamespace

import pytest

from ouroboros import _usage_wait, config, model_wait, queue_observation, store
from ouroboros.llm_attempt import PhysicalDispatchInterrupted
from ouroboros.usage_ledger import ControlAuthorityUnavailable
from tests.test_admission_contention import database_reader, publish, root as root

pytestmark = pytest.mark.serial


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    owner = SimpleNamespace(task={}, control_reason=lambda: "")
    monkeypatch.setattr(_usage_wait, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(model_wait, "current_model_wait", lambda: owner)
    monkeypatch.setattr(model_wait, "dispatch_deadline_remaining_sec", lambda: None)
    monkeypatch.setattr(_usage_wait, "_check", lambda: None)
    monkeypatch.setattr(_usage_wait, "_hold", lambda *_args: None)
    monkeypatch.setattr(queue_observation, "DISPATCH_PUBLICATION_WAIT_SEC", 2.0)
    monkeypatch.setattr(queue_observation, "DISPATCH_PUBLICATION_POLL_SEC", 0.5)
    return now, owner


def _sleep(monkeypatch, callback):
    monkeypatch.setattr(queue_observation, "_sleep", callback)

    async def sleeping(seconds):
        callback(seconds)

    monkeypatch.setattr(_usage_wait.asyncio, "sleep", sleeping)


def _retry(mode, callback):
    if mode == "async":
        return asyncio.run(_usage_wait.retry_presend_async(callback, check=False))
    return _usage_wait.retry_presend(callback, check=False)


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_real_store_contention_between_publication_gaps_does_not_spend_their_budget(root, monkeypatch, clock, mode):
    now, _owner = clock
    publish(root)
    (root / "state/queue.alive").unlink()
    probes, waits, busy = [], [], []
    with database_reader(root, exclusive=True) as release:
        def probe():
            probes.append(len(probes) + 1)
            if len(probes) == 2:
                now[0] += 10.0  # A slow acquisition failed for storage, not missing publication.
                try:
                    with store.open_store(root, write=False, wait_sec=0):
                        pytest.fail("the independent SQLite writer was not exclusive")
                except store.StoreBusy as error:
                    assert error.reason == "contention"
                    busy.append(error)
                    raise
            now[0] += 0.2
            return queue_observation.dispatch_probe(root)

        def sleep(seconds):
            waits.append(seconds)
            now[0] += seconds
            if len(waits) == 2:
                release()
            elif len(waits) == 3:
                publish(root, generation="recovered")

        _sleep(monkeypatch, sleep)
        assert _retry(mode, probe)["generation"] == "recovered"
    assert probes == [1, 2, 3, 4] and len(busy) == 1
    assert waits == [0.5, _usage_wait.ACQUISITION_SLICE_SEC, 0.5]


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_intervening_contention_does_not_renew_spent_publication_budget(monkeypatch, clock, mode):
    now, _owner = clock
    calls, sleeps = [], []

    def probe():
        calls.append(len(calls) + 1)
        if len(calls) == 2:
            now[0] += 10
            raise store.StoreBusy("busy", reason="contention")
        now[0] += 0.6
        raise queue_observation.DispatchPublicationPending("same uncertified publication")

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    _sleep(monkeypatch, sleep)
    with pytest.raises(ControlAuthorityUnavailable) as caught:
        _retry(mode, probe)
    assert caught.value.cause == "dispatch_publication_unconfirmed"
    assert calls == [1, 2, 3, 4]
    assert sleeps == pytest.approx([0.5, _usage_wait.ACQUISITION_SLICE_SEC, 0.3])


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("slow", ["probe", "sleep"])
def test_real_elapsed_publication_work_keeps_the_existing_bound(monkeypatch, clock, mode, slow):
    now, _owner = clock
    calls, sleeps = [], []

    def probe():
        calls.append(True)
        now[0] += 1.1 if slow == "probe" else 0.1
        raise queue_observation.DispatchPublicationPending("still uncertified")

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += 2.1 if slow == "sleep" else seconds

    _sleep(monkeypatch, sleep)
    with pytest.raises(ControlAuthorityUnavailable, match="still uncertified"):
        _retry(mode, probe)
    assert len(calls) == 2 and sleeps == [0.5], "actual probe/sleep elapsed must count, not only requested delays"


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("bound", ["interactive", "unowned"])
def test_other_real_elapsed_bounds_still_apply_during_storage_contention(monkeypatch, clock, mode, bound):
    now, owner = clock
    if bound == "interactive":
        owner.task["_is_direct_chat"] = True
        monkeypatch.setattr(config, "get_task_idle_timeout_sec", lambda: 2.0)
    else:
        monkeypatch.setattr(model_wait, "current_model_wait", lambda: None)
        monkeypatch.setattr(_usage_wait, "USAGE_LOCK_TIMEOUT_SEC", 2.0)
    error = store.StoreBusy("same storage contention", reason="contention")

    def probe():
        now[0] += 3
        raise error

    _sleep(monkeypatch, lambda _: pytest.fail("the existing elapsed bound must end this wait"))
    if bound == "interactive":
        with pytest.raises(PhysicalDispatchInterrupted, match="accounting_wait_expired"):
            _retry(mode, probe)
    else:
        with pytest.raises(store.StoreBusy) as caught:
            _retry(mode, probe)
        assert caught.value is error


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("control", ["stop", "deadline"])
def test_controls_win_between_publication_and_busy_retries(monkeypatch, clock, mode, control):
    now, _owner = clock
    calls, sleeps = [], []

    def check():
        if len(sleeps) >= 2:
            raise PhysicalDispatchInterrupted(control)

    def probe():
        calls.append(True)
        if len(calls) == 1:
            raise queue_observation.DispatchPublicationPending("publication in flight")
        raise store.StoreBusy("storage busy", reason="contention")

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(_usage_wait, "_check", check)
    _sleep(monkeypatch, sleep)
    with pytest.raises(PhysicalDispatchInterrupted, match=control):
        _retry(mode, probe)
    assert len(calls) == 2, "control must prevent another unsent callback invocation"
