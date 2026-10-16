"""An ``after`` read answers only when its rows bring the window up to date (CLIENT_CONTRACT §5).

New rows carry their own tasks' facts. What changes without a new row — a settled task's question
closing, its late phase settling, a shown skill operation ending — or rows a read cannot take
cannot reach the window through them, so the read refuses (409 ``history_delta_reset``) and the
window reads its newest page again. A running task's document moves with its work and its rows
bring the facts: that never refuses.
"""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

from ouroboros.gateway import history, history_paging
from ouroboros.gateway.history import make_chat_history_endpoint
from tests._store_shared import seed_result

TS = "2026-10-16T10:00:{:02d}Z"


def _append(root, name, rows, *, raw=""):
    path = root / "logs" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(raw + "".join(json.dumps(row) + "\n" for row in rows))


def _read(root, cursor=None, chat=1):
    params = {"chat_id": str(chat), **({"cursor": cursor} if cursor else {})}
    response = asyncio.run(make_chat_history_endpoint(root)(SimpleNamespace(query_params=params)))
    return response.status_code, json.loads(response.body)


def _task(root, task_id, status, index, **facts):
    seed_result(root, task_id, {"task_id": task_id, "status": status, "chat_id": 1, "ts": TS.format(index), **facts})


def _answered(root, task_id, index):
    _append(root, "chat", [{"ts": TS.format(index), "direction": "out", "chat_id": 1, "task_id": task_id,
                            "text": f"answer {task_id}"}])
    _append(root, "progress", [{"ts": TS.format(index), "content": f"step {task_id}", "task_id": task_id,
                                "chat_id": 1}])


def _cursor(token):
    return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


def test_a_shown_task_that_changes_after_it_settled_takes_the_newest_page_again(tmp_path):
    _task(tmp_path, "done", "completed", 0)
    _task(tmp_path, "busy", "running", 1)
    _task(tmp_path, "late", "completed", 2, root_phase_checkpoint={"post_task_synthesis": "running"})
    for index, task_id in enumerate(("done", "busy", "late")):
        _answered(tmp_path, task_id, index)
    status, page = _read(tmp_path)
    assert status == 200 and set(_cursor(page["after_cursor"])["tasks"]) == {"done", "busy", "late"}
    status, quiet = _read(tmp_path, page["after_cursor"])
    assert status == 200 and quiet["messages"] == []

    # Running and finalizing documents move with their work: the delta goes on.
    _task(tmp_path, "busy", "running", 1, title="still working")
    _task(tmp_path, "late", "completed", 2, root_phase_checkpoint={"post_task_synthesis": "running"}, title="x")
    status, going = _read(tmp_path, quiet["after_cursor"])
    assert status == 200, going

    # A settled task changed with no row to say so (its question closed): the newest page answers.
    _task(tmp_path, "done", "completed", 0, owner_quiz={"q1": {"quiz_id": "q1", "state": "closed"}})
    status, refused = _read(tmp_path, going["after_cursor"])
    assert status == 409 and refused["reason_code"] == "history_delta_reset"
    status, fresh = _read(tmp_path)
    assert status == 200 and _read(tmp_path, fresh["after_cursor"])[0] == 200

    # The late phase settling is a change the window cannot see either.
    _task(tmp_path, "late", "completed", 2, root_phase_checkpoint={"post_task_synthesis": "completed"})
    status, refused = _read(tmp_path, fresh["after_cursor"])
    assert status == 409 and refused["reason_code"] == "history_delta_reset"


def test_an_after_read_that_cannot_read_a_stored_row_refuses(tmp_path):
    _append(tmp_path, "chat", [{"ts": TS.format(0), "direction": "in", "chat_id": 1, "text": "hello"}])
    _, page = _read(tmp_path)
    # A line its writer has not finished is read next time: the delta answers without it.
    _append(tmp_path, "chat", [], raw=json.dumps({"ts": TS.format(1), "direction": "in", "chat_id": 1, "text": "half"}))
    status, partial = _read(tmp_path, page["after_cursor"])
    assert status == 200 and partial["messages"] == []
    _append(tmp_path, "chat", [], raw="\n")
    status, finished = _read(tmp_path, partial["after_cursor"])
    assert status == 200 and [row["text"] for row in finished["messages"]] == ["half"]
    # A stored line nobody can read would be stepped over: the delta refuses instead.
    _append(tmp_path, "chat", [{"ts": TS.format(2), "direction": "in", "chat_id": 1, "text": "after it"}],
            raw="{not json\n")
    status, refused = _read(tmp_path, finished["after_cursor"])
    assert status == 409 and refused["reason_code"] == "history_delta_reset"


def test_a_shown_skill_operation_that_ended_takes_the_newest_page_again(tmp_path, monkeypatch):
    operation = {"text": "Skill review: `x` — running", "role": "system", "system_type": "skill_lifecycle",
                 "ts": TS.format(5), "is_progress": True, "task_id": "", "chat_id": 1, "lifecycle_virtual": True}
    running = {"row": operation}
    monkeypatch.setattr(history, "_active_lifecycle_row", lambda _filter: running["row"])
    _append(tmp_path, "chat", [{"ts": TS.format(0), "direction": "in", "chat_id": 1, "text": "hello"}])
    _, page = _read(tmp_path)
    assert _cursor(page["after_cursor"])["lifecycle"] is True
    status, going = _read(tmp_path, page["after_cursor"])
    assert status == 200  # still running: the delta shows it again
    running["row"] = None
    status, refused = _read(tmp_path, going["after_cursor"])
    assert status == 409 and refused["reason_code"] == "history_delta_reset"
    status, fresh = _read(tmp_path)
    assert status == 200 and _cursor(fresh["after_cursor"])["lifecycle"] is False
    assert _read(tmp_path, fresh["after_cursor"])[0] == 200


def test_a_late_arrival_in_a_delta_is_out_of_order_against_the_window(tmp_path):
    from ouroboros.projects_registry import create_project

    chat = create_project(tmp_path, "late", name="Late")["chat_id"]
    _append(tmp_path, "chat", [{"ts": TS.format(5), "direction": "out", "chat_id": chat, "text": "shown answer"}])
    _, page = _read(tmp_path, chat=chat)
    _append(tmp_path, "chat", [{"ts": TS.format(1), "direction": "out", "chat_id": chat, "text": "late delivery"}])
    status, late = _read(tmp_path, page["after_cursor"], chat=chat)
    assert status == 200 and late["window"]["latest_message"]["out_of_order"] is True
    _append(tmp_path, "chat", [{"ts": TS.format(9), "direction": "out", "chat_id": chat, "text": "new answer"}])
    status, fresh = _read(tmp_path, late["after_cursor"], chat=chat)
    assert status == 200 and fresh["window"]["latest_message"]["out_of_order"] is False


def test_a_window_too_large_for_its_cursor_reads_its_newest_page(tmp_path, monkeypatch):
    _task(tmp_path, "done", "completed", 0)
    _answered(tmp_path, "done", 0)
    status, page = _read(tmp_path)
    assert status == 200 and page["after_cursor"]
    monkeypatch.setattr(history_paging, "_CURSOR_CHARS", len(page["after_cursor"]) - 1)
    status, page_without = _read(tmp_path)
    assert status == 200 and "after_cursor" not in page_without
    status, refused = _read(tmp_path, page["after_cursor"])
    assert status == 400 and refused["reason_code"] == "history_cursor_invalid"  # longer than the bound
