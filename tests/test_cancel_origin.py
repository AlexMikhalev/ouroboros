"""A settled cancellation retains its recorded cause through result consumers."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from ouroboros import cancel_intents
from ouroboros.gateway.history import make_chat_history_endpoint
from ouroboros.task_results import load_task_result, write_task_result
from ouroboros.tools.control_task_results import _get_task_result
from ouroboros.tools.join_ledger import _child_result_sha256
from tests._cancel_intents_shared import qenv as _qenv

qenv = _qenv


@pytest.mark.parametrize("queued", [False, True], ids=["missing-worker", "pending"])
def test_cancel_custody_keeps_origin_after_removing_intent(qenv, monkeypatch, queued):
    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_kw: None)
    task = {"id": "cancelled", "chat_id": 0}
    if queued:
        qenv.q.PENDING.append(task)
    write_task_result(qenv.drive, task["id"], "scheduled" if queued else "running")
    intent = cancel_intents.request_cancel(qenv.drive, task["id"], source="http_single")

    assert qenv.tl.cancel_task_custody(task["id"]) == qenv.tl.CANCEL_CANCELLED

    stored = load_task_result(qenv.drive, task["id"])
    assert stored["cancel_origin"] == {
        "source": "http_single", "scope": "single",
        "request_id": intent["request_id"], "requested_at": intent["requested_at"],
    }
    assert "parent_decision" not in stored
    assert cancel_intents.active_intent(qenv.drive, task["id"]) is None


def test_cascade_origin_links_the_child_to_its_cancelled_root(qenv, monkeypatch):
    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_kw: None)
    qenv.q.PENDING[:] = [
        {"id": "root", "chat_id": 0},
        {"id": "child", "chat_id": 0, "parent_task_id": "root", "root_task_id": "root"},
    ]
    for task in qenv.q.PENDING:
        write_task_result(qenv.drive, task["id"], "scheduled")
    cancel_intents.request_cancel(qenv.drive, "root", source="http_cascade", scope="cascade")

    assert qenv.tl.cancel_task_by_id("root", cascade=True)

    root = load_task_result(qenv.drive, "root")
    child = load_task_result(qenv.drive, "child")
    assert root["cancel_origin"]["source"] == "http_cascade"
    assert root["cancel_origin"]["scope"] == "cascade"
    assert child["cancel_origin"]["requested_by"] == "root"
    assert child["cancel_origin"]["reason"] == "subtree cancellation of root"
    assert child["parent_decision"] == "cancelled"


def test_history_terminal_row_retains_cancel_origin(tmp_path):
    origin = {"source": "http_single", "scope": "single", "request_id": "ci_recorded"}
    write_task_result(tmp_path, "task", "cancelled", cancel_origin=origin)
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "progress.jsonl").write_text(json.dumps({
        "ts": "2026-09-18T00:00:00Z", "task_id": "task", "content": "Working",
    }) + "\n", encoding="utf-8")
    response = asyncio.run(make_chat_history_endpoint(tmp_path)(SimpleNamespace(query_params={"limit": "10"})))
    rows = json.loads(response.body.decode("utf-8"))["messages"]
    row = next(row for row in rows if row.get("task_id") == "task")
    assert row["task_terminal_status"] == "cancelled"
    assert row["cancel_origin"] == origin


def test_cancel_origin_is_current_fact_even_on_a_conditional_result_read(tmp_path):
    stored = write_task_result(tmp_path, "task", "cancelled", result="Stopped.")
    ctx = SimpleNamespace(drive_root=tmp_path, task_metadata={})
    before = _get_task_result(ctx, "task")
    origin = {"source": "agent_tool", "reason": "Хватит", "request_origin": {"kind": "agent_task", "task_id": "actor"}}
    write_task_result(tmp_path, "task", "cancelled", cancel_origin=origin)
    marker = f"\n\n[CANCELLED_BY] {json.dumps(origin, ensure_ascii=False)}"

    assert _get_task_result(ctx, "task") == before + marker
    conditional = _get_task_result(ctx, "task", known_result_sha256=_child_result_sha256(stored))
    assert '"result_unchanged": true' in conditional
    assert conditional.endswith(marker)


def test_rebuilt_task_done_carries_durable_cancel_origin(tmp_path, monkeypatch):
    from supervisor import events

    origin = {"source": "http_single", "request_id": "ci_recorded"}
    write_task_result(tmp_path, "task", "cancelled", cancel_origin=origin)
    published = []
    monkeypatch.setattr(events, "_finish_task_done_dispatch", lambda *_a, **kw: published.append(kw["task_done_event"]))
    ctx = SimpleNamespace(DRIVE_ROOT=tmp_path, RUNNING={}, WORKERS={})

    events._handle_task_done({"task_id": "task", "status": "cancelled"}, ctx)

    assert published[0]["cancel_origin"] == origin
    rows = [json.loads(line) for line in (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert next(row for row in rows if row.get("type") == "task_done")["cancel_origin"] == origin
