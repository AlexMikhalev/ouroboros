"""One room rule (owner decision A4): the bus write seam, replay membership and cognition
agree on the room a task-addressed row belongs to, and a warm frame costs no store read.

Two-sided: every case fails on d375d9fd0 (the bus never re-addressed a row that reached
it without lineage, and replay membership read only the explicit-roots map) and asserts
the still-working case beside it (a Main direct task with a project_id stays Main, a
pinned or origin-addressed row keeps its producer's address).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ouroboros import projects_registry as pr
from ouroboros.project_dialogue import ROOM_FIXED_ROW_TYPES, room_membership
from ouroboros.task_results import write_task_result

pytestmark = pytest.mark.serial


def _bus(monkeypatch, root):
    from supervisor import message_bus

    sent = []
    monkeypatch.setattr(message_bus, "DATA_DIR", root)
    monkeypatch.setattr(message_bus, "load_state", lambda: {"owner_id": 1, "owner_chat_id": 1})
    monkeypatch.setattr(message_bus, "get_bridge", lambda: SimpleNamespace(send_message=lambda *a, **k: sent.append((a, k))))
    return message_bus, sent


def _progress_rows(root):
    return [json.loads(line) for line in (root / "logs/progress.jsonl").read_text().splitlines()]


def _world(root):
    """A converted root with a cancelled child, a root bound to A with a child bound to B, a
    room-born root, and a Main direct task that carries a project_id."""
    a, b, native = (pr.create_project(root, pid, name=pid) for pid in ("room-a", "room-b", "room-native"))
    write_task_result(root, "root-1", "running", root_task_id="root-1", chat_id=1)
    pr.bind_task_to_project(root, "root-1", a["id"], origin={"absent": "system"})
    write_task_result(root, "kid-1", "cancelled", delegation_role="subagent", parent_task_id="root-1",
                      root_task_id="root-1", chat_id=1)
    write_task_result(root, "mid-1", "running", delegation_role="subagent", parent_task_id="root-1",
                      root_task_id="root-1", chat_id=1)
    pr.bind_task_to_project(root, "mid-1", b["id"], origin={"absent": "system"})
    write_task_result(root, "grand-1", "running", delegation_role="subagent", parent_task_id="mid-1",
                      root_task_id="root-1", chat_id=1)
    write_task_result(root, "born-1", "running", root_task_id="born-1", chat_id=native["chat_id"],
                      project_id=native["id"])
    write_task_result(root, "direct-1", "running", root_task_id="direct-1", chat_id=1, _is_direct_chat=True,
                      project_id=native["id"])
    return {"a": a["chat_id"], "b": b["chat_id"], "native": native["chat_id"]}


def _membership(root, chat):
    projects = pr.reserved_project_chat_ids(root)
    return room_membership(chat if chat in projects else 1, projects, [], pr.RoomResolver(root))


@pytest.mark.parametrize("task_id, room", [
    ("kid-1", "a"),        # a child of a converted root that carries only its id
    ("grand-1", "b"),      # root bound to A, its parent bound to B: the nearest conversion wins
    ("born-1", "native"),  # born in a Project, bound to nothing
])
def test_the_bus_and_replay_put_a_project_tasks_bare_row_in_its_room(tmp_path, monkeypatch, task_id, room):
    rooms = _world(tmp_path)
    bus, sent = _bus(monkeypatch, tmp_path)
    bus.send_with_budget(1, "Stopped by Ouroboros", is_progress=True, task_id=task_id, role="system",
                         system_type="subagent_terminal_notice")
    row = _progress_rows(tmp_path)[-1]
    assert row["chat_id"] == rooms[room] == sent[-1][0][0], "the durable row and the live frame share the room"
    if task_id != "born-1":
        assert (row["delegation_role"], row["parent_task_id"], row["root_task_id"]) == (
            "subagent", {"kid-1": "root-1", "grand-1": "mid-1"}[task_id], "root-1"), "lineage rides the row"
    # Replay: the row as it was recorded before the seam (chat 1, task id only) lives in the same room.
    legacy = {"type": "send_message", "task_id": task_id, "chat_id": 1}
    assert _membership(tmp_path, rooms[room])(1, legacy) is True
    assert _membership(tmp_path, 1)(1, legacy) is False, "Main does not keep another room's task"
    assert all(_membership(tmp_path, rooms[other])(1, legacy) is False for other in rooms if other != room)


def test_live_routing_and_replay_agree_for_a_deep_chain_with_two_conversions(tmp_path):
    rooms = _world(tmp_path)
    assert pr.project_chat_for_task_tree(tmp_path, "grand-1") == rooms["b"]
    assert pr.project_chat_for_task_tree(tmp_path, "grand-1", "mid-1", "root-1") == rooms["b"], "a supplied root never shortcuts the parent chain"
    assert pr.project_chat_for_task_tree(tmp_path, "kid-1", "root-1", "root-1") == rooms["a"]
    row = {"task_id": "grand-1", "parent_task_id": "mid-1", "root_task_id": "root-1"}
    assert _membership(tmp_path, rooms["b"])(1, row) and not _membership(tmp_path, rooms["a"])(1, row)


def test_a_main_direct_task_with_a_project_id_stays_main(tmp_path, monkeypatch):
    rooms = _world(tmp_path)
    bus, sent = _bus(monkeypatch, tmp_path)
    for meta in ({"_is_direct_chat": True}, {}):  # the flagged turn and its unflagged finalization frame
        bus.send_with_budget(1, "Thinking", is_progress=True, task_id="direct-1", role="system",
                             system_type="host_progress", progress_meta=meta)
        assert _progress_rows(tmp_path)[-1]["chat_id"] == 1 == sent[-1][0][0]
    assert pr.project_chat_for_task_tree(tmp_path, "direct-1") == 0, "project_id is a copy, never the room"
    assert _membership(tmp_path, 1)(1, {"task_id": "direct-1"}) is True
    assert _membership(tmp_path, rooms["native"])(1, {"task_id": "direct-1"}) is False


@pytest.mark.parametrize("system_type", sorted(ROOM_FIXED_ROW_TYPES))
def test_a_fixed_row_type_keeps_its_producers_address_and_borrows_no_lineage(tmp_path, monkeypatch, system_type):
    rooms = _world(tmp_path)
    bus, sent = _bus(monkeypatch, tmp_path)
    bus.send_with_budget(rooms["native"], "Not delivered", task_id="kid-1", role="system", system_type=system_type)
    stored = [json.loads(line) for line in (tmp_path / "logs/chat.jsonl").read_text().splitlines()][-1]
    assert stored["chat_id"] == rooms["native"] == sent[-1][0][0], "the producer's address stands"
    assert not {"delegation_role", "parent_task_id", "root_task_id"} & set(stored), "the target is not the speaker"


def _count_store_work(monkeypatch):
    from ouroboros import store, task_status, task_store

    counts = {"open_store": 0, "load_effective_task_result": 0}
    real_open, real_load = store.open_store, task_status.load_effective_task_result

    def _open(*a, **k):
        counts["open_store"] += 1
        return real_open(*a, **k)

    def _load(*a, **k):
        counts["load_effective_task_result"] += 1
        return real_load(*a, **k)

    monkeypatch.setattr(store, "open_store", _open)
    monkeypatch.setattr(task_store, "open_store", _open)
    monkeypatch.setattr(pr, "open_store", _open)
    monkeypatch.setattr(task_status, "load_effective_task_result", _load)
    return counts


@pytest.mark.parametrize("through_handler", [False, True], ids=["send_with_budget", "event_queue_handler"])
def test_a_warm_frame_opens_no_store_and_a_cold_task_id_opens_at_most_once(tmp_path, monkeypatch, through_handler):
    from supervisor.events_chat_delivery import _handle_send_message

    rooms = _world(tmp_path)
    bus, sent = _bus(monkeypatch, tmp_path)
    counts = _count_store_work(monkeypatch)
    ctx = SimpleNamespace(DRIVE_ROOT=tmp_path, RUNNING={}, send_with_budget=bus.send_with_budget)

    def frame(task_id, meta, n=10):
        for i in range(n):
            if through_handler:
                _handle_send_message({"type": "send_message", "chat_id": 1, "text": f"frame {i}", "is_progress": True,
                                      "role": "system", "system_type": "host_progress", "task_id": task_id,
                                      "progress_meta": dict(meta)}, ctx)
            else:
                bus.send_with_budget(1, f"frame {i}", is_progress=True, task_id=task_id, progress_meta=dict(meta),
                                     role="system", system_type="host_progress")

    lineage = {"delegation_role": "subagent", "parent_task_id": "root-1", "root_task_id": "root-1"}
    frame("kid-1", lineage)
    assert counts == {"open_store": 0, "load_effective_task_result": 0}, "a frame carrying its lineage opens nothing"
    frame("grand-1", {})
    opened = counts["open_store"]
    assert 0 < opened <= 2, "one indexed read per unseen id on the chain (grand-1, mid-1 is bound)"
    frame("grand-1", {})
    assert counts["open_store"] == opened, "warm frames open nothing"
    assert counts["load_effective_task_result"] == 0, "the seam never parses a result body"
    assert {a[0][0] for a in sent} == {rooms["a"], rooms["b"]}


def test_a_conversion_invalidates_room_answers_and_a_store_commit_does_not(tmp_path):
    rooms = _world(tmp_path)
    assert pr.project_chat_for_task_tree(tmp_path, "kid-1") == rooms["a"]
    write_task_result(tmp_path, "kid-1", "cancelled", progress_note="a later commit")
    assert pr.RoomResolver(tmp_path).answers.get("kid-1") == rooms["a"], "a commit keeps the warm answer"
    c = pr.create_project(tmp_path, "room-c", name="room-c")
    pr.bind_task_to_project(tmp_path, "kid-1", c["id"], origin={"absent": "system"})
    assert pr.project_chat_for_task_tree(tmp_path, "kid-1") == c["chat_id"], "the nearest conversion wins at once"


def test_an_unadmitted_id_is_answered_but_a_miss_is_never_remembered(tmp_path):
    rooms = _world(tmp_path)
    assert pr.project_chat_for_task_tree(tmp_path, "later-1") == 0 and pr.task_lineage(tmp_path, "later-1") == {}
    assert pr.project_chat_for_task_tree(tmp_path, "later-2", "root-1", "root-1") == rooms["a"], "supplied lineage routes an unseen id"
    for task_id in ("later-1", "later-2"):
        write_task_result(tmp_path, task_id, "scheduled", delegation_role="subagent", parent_task_id="root-1",
                          root_task_id="root-1", chat_id=1)
        assert pr.project_chat_for_task_tree(tmp_path, task_id) == rooms["a"], "admission creates the facts"
