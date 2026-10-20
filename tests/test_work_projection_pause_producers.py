"""The work projection of a settling owner Pause, read through its real producers.

The census (``gateway/state.py``), the members' park (``budget_pause.enter_owner_pause`` and the
supervisor's ``install_exact_budget_pause``) and the stop observation each Pause records drive
``work_projection.works_snapshot`` directly: only what still executes is said as working, and a run
whose stop nobody could confirm is said as unconfirmed, never as still working.
"""

from __future__ import annotations

from types import SimpleNamespace

from tests._budget_pause_exact_helpers import _install_queue
from tests.test_owner_pause import _live_tree, _owner_park


def _works(root):
    from ouroboros.gateway.state import _chat_activities_snapshot_safe
    from ouroboros.gateway.work_projection import works_snapshot

    raw, owners = {}, []
    _chat_activities_snapshot_safe(root, work_facts=raw, decision_owners=owners)
    return works_snapshot(root, owners, raw)["works"]


def test_a_settling_pause_says_what_still_executes_and_a_stop_nobody_confirmed(tmp_path, monkeypatch):
    from ouroboros import budget_pause, delegate_custody
    from supervisor.events_budget import install_exact_budget_pause
    from supervisor.owner_pause_control import request_owner_pause

    queue, _state, workers = _install_queue(tmp_path, monkeypatch)
    _live_tree(tmp_path, workers)  # a RUNNING root and child, and a child that never started
    delegate_custody.emit(tmp_path, delegate_custody.STARTED,
                          {"run_id": "run-1", "task_id": "child-run", "root_task_id": "root-1"})
    assert request_owner_pause("root-1", request_id="p",
                               by={"actor": "owner", "text": "", "at": "2026-10-20T00:43:00Z"})["ok"]
    [work] = _works(tmp_path)
    # The root and the running child finish their step; the child that never started is queued, not working.
    assert (work["activity"], work["transition"], work["live"]) == ("working", "pausing", True)
    assert [(tail["code"], tail["state"], tail["ids"]) for tail in work["tails"]] == [
        ("helpers_running", "live", ["child-run"]), ("delegated_running", "live", ["run-1"])]

    ctx = SimpleNamespace(DRIVE_ROOT=tmp_path, RUNNING=workers.RUNNING, PENDING=workers.PENDING,
                          WORKERS=workers.WORKERS, sort_pending=lambda: None,
                          persist_queue_snapshot=queue.persist_queue_snapshot, bridge=None)
    # The child parks while the harness gateway cannot confirm its run's stop, then the root parks: the Pause
    # still settles (that run is unresolved), and nothing executes any more.
    unconfirmed = [{"run_id": "run-1", "state": "stop_unknown", "stop_outcome": "not_issued_gateway_unavailable"}]
    for task_id, runs in (("child-run", unconfirmed), ("root-1", [])):
        saved = _owner_park(tmp_path, monkeypatch, task_id, runs)
        install_exact_budget_pause(ctx, task_id, budget_pause.exact_pause_marker(saved)["checkpoint"])
    assert not workers.RUNNING
    [work] = _works(tmp_path)
    assert (work["activity"], work["transition"], work["live"]) == ("working", "pausing", False)
    assert [(tail["code"], tail["state"], tail["ids"]) for tail in work["tails"]] == [
        ("external_runs_unconfirmed", "unknown", ["run-1"])]
    assert "stop" in {control["action"] for control in work["controls"]}, "its Stop still reaches that run"
