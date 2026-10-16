"""Queued roots are explicit mail targets, never implicit room readers."""
from types import SimpleNamespace

import pytest

from ouroboros.room_reader import is_reading
from ouroboros.server_routing_context import _addressable_root_tasks


@pytest.mark.parametrize("fields,running,expected", [
    ({}, True, True), ({}, False, False), ({"_owner_hold": True}, True, False),
    ({"_budget_pause": {"status": "paused"}}, True, False),
    ({"_is_direct_chat": True}, True, True),
    ({"owner_wait": {"state": "waiting", "kind": "quiz"}}, True, True),
])
def test_structural_reader_state(tmp_path, fields, running, expected):
    assert is_reading(tmp_path, "work", {"id": "work", **fields}, running=running, fences={}) is expected


def test_restored_direct_root_stays_addressable_but_queued_root_does_not_read(tmp_path):
    task = {"id": "restored", "chat_id": 1, "drive_root": str(tmp_path), "_is_direct_chat": True}
    ctx = SimpleNamespace(DRIVE_ROOT=tmp_path, RUNNING={}, PENDING=[task], BUDGET_ROOT_FENCES={})
    rows = _addressable_root_tasks(ctx, 1)
    assert rows[0]["task_id"] == "restored" and rows[0]["room_reader"] is False
    ctx.RUNNING, ctx.PENDING = {"restored": {"task": task}}, []
    assert _addressable_root_tasks(ctx, 1)[0]["room_reader"] is True


@pytest.mark.parametrize("hold_at_admission", [False, True])
def test_room_intake_rechecks_the_current_task_under_the_queue_lock(tmp_path, monkeypatch, hold_at_admission):
    from ouroboros.projects_registry import create_project
    from ouroboros.server_owner_routing import _route_project_chat_to_running_task
    from ouroboros.owner_mailbox import drain_owner_entries
    from supervisor import queue as q
    from tests.test_project_routing_v664 import _ctx
    room = create_project(tmp_path, "reader-race")
    task = {"id": "reader", "chat_id": room["chat_id"], "drive_root": str(tmp_path)}
    ctx = _ctx(tmp_path, running={"reader": {"task": task}})
    class AdmissionLock:
        def acquire(self):
            ctx.RUNNING["reader"] = {"task": {**task, "_owner_hold": hold_at_admission}}
        def release(self):
            pass
    monkeypatch.setattr(q, "_queue_lock", AdmissionLock())
    result = _route_project_chat_to_running_task(ctx, room["chat_id"], "followup", "reader-race")
    assert result == ("" if hold_at_admission else "reader")
    assert [e["text"] for e in drain_owner_entries(tmp_path, "reader")] == ([] if hold_at_admission else ["followup"])
