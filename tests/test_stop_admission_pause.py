"""Every process-ending stop pauses admission before it reads the queue; an abandoned one reopens it.

The lane-level behaviour (a committed launch reaches its worker while the stop waits, a waiting one
writes nothing, the bound) lives in tests/test_launch_outside_q.py; this module pins each door.
"""
from __future__ import annotations

import inspect

import pytest

from supervisor import queue
from supervisor import workspace_preparation as prep


@pytest.fixture
def generation(monkeypatch):
    monkeypatch.setattr(queue, "RETURNS_NAMED", {})
    prep._reset_for_tests()
    current = prep.start_generation(stop=lambda: False)
    yield current
    prep._reset_for_tests()


def test_pause_closes_the_generation_and_resume_reopens_it(generation):
    assert not generation.closed()
    assert prep.pause_admission(1.0) is True and generation.closed()
    prep.resume_admission()
    assert not generation.closed()


def test_an_abandoned_restart_reopens_the_queue_and_the_admission_lane(generation):
    assert prep.pause_admission(1.0) and generation.closed()
    queue.name_queue_returns([], "planned_restart")
    queue.reopen_queue_returns("planned_restart")
    assert not generation.closed() and not queue.RETURNS_NAMED


def test_planned_restart_and_quit_teardown_pause_admission_first(monkeypatch):
    import server
    from ouroboros import server_restart

    calls = []
    monkeypatch.setattr(prep, "pause_admission", lambda timeout=None: calls.append("pause") or True)
    assert server_restart._begin_task_teardown(True) == server_restart._shutdown_task_cleanup_args(True)
    assert server_restart._begin_task_teardown(False) == server_restart._shutdown_task_cleanup_args(False)
    assert calls == ["pause", "pause"]
    # Both server doors begin their teardown through it, before they read the queue or retire the pool.
    assert "_begin_task_teardown(restart_requested=True)" in inspect.getsource(server._perform_supervisor_restart)
    assert inspect.getsource(server).count("_begin_task_teardown(restart_requested)") == 1


@pytest.mark.parametrize("raises", [False, True])
def test_the_update_stops_its_writers_with_admission_paused(monkeypatch, raises):
    from ouroboros.gateway import control

    events = []
    monkeypatch.setattr(prep, "pause_admission", lambda timeout=None: events.append("pause") or True)
    monkeypatch.setattr(prep, "resume_admission", lambda: events.append("resume"))

    def stop(reason):
        events.append(("stop", reason, "paused" if events[-1] == "pause" else "open"))
        if raises:
            raise RuntimeError("writer stop failed")
        return ["active:writer"]

    monkeypatch.setattr(control, "_stop_repo_writers", stop)
    if raises:
        with pytest.raises(RuntimeError):
            control._quiesce_repo_writers("smart")
    else:
        assert control._quiesce_repo_writers("smart") == ["active:writer"]
    assert events == ["pause", ("stop", "smart", "paused"), "resume"]
