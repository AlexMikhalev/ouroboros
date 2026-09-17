"""Readable recent history survives an unavailable archive without false cursors."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ouroboros.gateway import history


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    from supervisor import queue, state

    for module, names in (
        (state, ("DRIVE_ROOT", "STATE_PATH", "STATE_LAST_GOOD_PATH", "STATE_LOCK_PATH")),
        (queue, ("DRIVE_ROOT", "QUEUE_SNAPSHOT_PATH")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, getattr(module, name))
    state.init(tmp_path)
    queue.init(tmp_path)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(history, "_active_lifecycle_row", lambda _filter: None)
    assert state.DRIVE_ROOT == queue.DRIVE_ROOT == tmp_path


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def row(index, **extra):
    return {"ts": "2026-09-12T00:00:00Z", "chat_id": 1,
            "direction": "in", "text": f"human-{index}", **extra}


def request(root, **params):
    response = asyncio.run(history.make_chat_history_endpoint(root)(SimpleNamespace(query_params=params)))
    return response.status_code, json.loads(response.body)


def deny_archive(monkeypatch, root, failure):
    if failure == "directory":
        original = os.scandir

        def scandir(path):
            if Path(path) == root / "archive":
                raise PermissionError("archive unavailable")
            return original(path)

        monkeypatch.setattr(os, "scandir", scandir)
    else:
        original = Path.open

        def open_path(path, *args, **kwargs):
            if path == root / "archive" / "chat_20260901T000000.jsonl":
                raise PermissionError("old archive segment unavailable")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", open_path)


@pytest.mark.parametrize("failure", ["directory", "oldest_segment"])
def test_unavailable_archive_keeps_readable_recent_and_recovers_exact_chain(tmp_path, monkeypatch, failure):
    write(tmp_path / "archive" / "chat_20260901T000000.jsonl", [row("archive")])
    write(tmp_path / "logs" / "chat.jsonl", [row(index) for index in range(180)])
    write(tmp_path / "logs" / "progress.jsonl", [
        {"ts": "2026-09-12T00:00:00Z", "chat_id": 1,
         "task_id": "working", "content": f"progress-{index}"} for index in range(3)
    ])
    with monkeypatch.context() as denied:
        deny_archive(denied, tmp_path, failure)
        status, partial = request(tmp_path)
    assert status == 200
    assert [message["text"] for message in partial["messages"] if not message["is_progress"]] == [
        f"human-{index}" for index in range(30, 180)
    ]
    assert [message["text"] for message in partial["messages"] if message["is_progress"]] == [
        f"progress-{index}" for index in range(3)
    ]
    assert partial["reason_code"] == "history_source_unavailable"
    assert partial["window"]["complete"] is False
    assert "chat_source_unavailable" in partial["window"]["truncated_by"]
    assert partial["has_more"] is True
    assert partial["next_cursor"] is partial["page_cursor"] is None

    # Recovery starts the first trustworthy physical snapshot. The inaccessible
    # prefix never produces offsets which later point into the wrong segment.
    status, recovered = request(tmp_path)
    assert status == 200 and recovered["page_cursor"] and recovered["next_cursor"]
    assert "reason_code" not in recovered
    status, older = request(tmp_path, cursor=recovered["next_cursor"])
    assert status == 200 and older["has_more"] is False
    messages = recovered["messages"] + older["messages"]
    assert len(messages) == len({message["history_id"] for message in messages}) == 184
    assert {message["text"] for message in messages} >= {"human-archive", "human-0", "human-179"}


@pytest.mark.parametrize("handle", ["next_cursor", "page_cursor"])
def test_cursor_bound_unavailable_source_keeps_the_exact_retry(tmp_path, monkeypatch, handle):
    write(tmp_path / "logs" / "chat.jsonl", [row(index) for index in range(200)])
    first_status, first = request(tmp_path)
    assert first_status == 200
    cursor = first[handle]
    with monkeypatch.context() as denied:
        deny_archive(denied, tmp_path, "directory")
        status, failed = request(tmp_path, cursor=cursor)
    assert status == 503 and failed["messages"] == []
    assert failed["reason_code"] == "history_source_unavailable"
    assert failed["window"]["complete"] is False
    assert failed["has_more"] is True and failed["next_cursor"] == cursor
    status, retry = request(tmp_path, cursor=failed["next_cursor"])
    assert status == 200
    if handle == "page_cursor":
        assert retry["messages"] == first["messages"]
    else:
        assert [message["text"] for message in retry["messages"]] == [f"human-{index}" for index in range(50)]


def test_partial_recent_uses_current_project_membership_and_legacy_limit(tmp_path, monkeypatch):
    from ouroboros.projects_registry import bind_task_to_project, create_project

    project = create_project(tmp_path, "room", name="Room")
    bind_task_to_project(tmp_path, "bound", project["id"], origin={"absent": "system"})
    write(tmp_path / "logs" / "chat.jsonl", [
        row("bound", task_id="bound"), row("direct", chat_id=project["chat_id"]),
        row("main"), row("hidden", chat_id=0), row("a2a", chat_id=-100),
    ])
    with monkeypatch.context() as denied:
        deny_archive(denied, tmp_path, "directory")
        main_status, main = request(tmp_path, limit="1")
        project_status, project_history = request(tmp_path, chat_id=str(project["chat_id"]), limit="2")
    assert main_status == project_status == 200
    assert [message["text"] for message in main["messages"]] == ["human-main"]
    assert [message["text"] for message in project_history["messages"]] == ["human-bound", "human-direct"]
    assert main["reason_code"] == project_history["reason_code"] == "history_source_unavailable"
    assert main["page_cursor"] is project_history["page_cursor"] is None
