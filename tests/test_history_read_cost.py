"""A room's read costs what its own rows cost (issue #1771).

Under load every call that gives up the interpreter lock (a SQLite connection or statement, a
``stat``, an ``open``) waits milliseconds to get it back, so a page's latency is the number of
such calls. A Project page read through the room index makes the same calls however many other
rooms' archives the chain holds.
"""
from __future__ import annotations

import collections
import os
import pathlib
import sqlite3

from ouroboros import room_index
from tests._store_shared import seed_result
from tests.test_chat_history_paging import isolated_runtime, request, row, write  # noqa: F401


def _room(root, foreign):
    from ouroboros.projects_registry import create_project

    chat = create_project(root, "costly", name="Costly")["chat_id"]
    write(root / "archive" / "chat_20260901T000000.jsonl", [row(index, chat_id=chat) for index in range(20)])
    for index in range(foreign):
        write(root / "archive" / f"chat_20260902T{index:06d}.jsonl", [row(item, text="foreign") for item in range(40)])
        write(root / "archive" / f"progress_20260902T{index:06d}.jsonl",
              [row(item, "progress", task_id=f"other-{index}") for item in range(40)])
    write(root / "logs" / "chat.jsonl", [row(99, chat_id=chat)])
    write(root / "logs" / "progress.jsonl", [row(98, "progress", chat_id=chat, task_id="room-task")])
    seed_result(root, "room-task", {"task_id": "room-task", "status": "completed", "chat_id": chat})
    room_index.catch_up(root)
    room_index.backfill(root)
    return chat


def _counted(monkeypatch):
    counts = collections.Counter()
    connect, stat, path_stat, path_open = sqlite3.connect, os.stat, pathlib.Path.stat, pathlib.Path.open

    def counted_connect(*args, **kwargs):
        counts["connect"] += 1
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(lambda _statement: counts.update(["statement"]))
        return connection

    def tally(name, real):
        def call(*args, **kwargs):
            counts[name] += 1
            return real(*args, **kwargs)
        return call

    monkeypatch.setattr(sqlite3, "connect", counted_connect)
    monkeypatch.setattr(os, "stat", tally("stat", stat))
    monkeypatch.setattr(pathlib.Path, "stat", tally("stat", path_stat))
    monkeypatch.setattr(pathlib.Path, "open", tally("open", path_open))
    return counts


def _cost(root, chat, monkeypatch):
    passive = getattr(room_index.room_rows, "passive", room_index.room_rows)
    monkeypatch.setattr(room_index, "room_rows", passive)  # the index is current, as the keeper keeps it
    status, warm = request(root, chat_id=str(chat))  # the process caches a first read fills
    assert status == 200 and len([m for m in warm["messages"] if not m.get("is_progress")]) == 21
    with monkeypatch.context() as patch:
        counts = _counted(patch)
        status, page = request(root, chat_id=str(chat))
    assert status == 200 and page["messages"] == warm["messages"]
    return counts


def test_a_project_page_costs_the_same_however_many_other_rooms_archives_exist(tmp_path, monkeypatch):
    few, many = tmp_path / "few", tmp_path / "many"
    small = _cost(few, _room(few, 3), monkeypatch)
    large = _cost(many, _room(many, 30), monkeypatch)
    for kind in ("connect", "statement", "open"):
        assert large[kind] == small[kind], (kind, small, large)
    assert large["stat"] <= small["stat"] + 2, (small, large)  # the newest archive is stat'ed afresh
    # A store read for the room's tasks, one for their facts, the index: a few statements each.
    assert small["connect"] <= 4 and small["statement"] <= 24 and small["open"] <= 20 and small["stat"] <= 32, small
