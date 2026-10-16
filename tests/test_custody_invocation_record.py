"""Invocation lookup must equal the independent chain oracle, including publication order."""
from __future__ import annotations

from copy import deepcopy

import pytest

from ouroboros import delegate_custody as c
from ouroboros import delegate_custody_current as current
from ouroboros import delegate_invocation as inv
from ouroboros import store


def _request(iid="inv", **facts):
    return {"type": c.START_REQUESTED, "invocation_id": iid, "task_id": "task",
            "root_task_id": "task", "surface": "task_acceptance", "operation_id": "operation",
            "slot_id": "slot", "request": {"prompt": "original"},
            "billing_group": {"billing_group_id": "original-group"}, **facts}


def _started(rid, iid="inv"):
    return {"type": c.STARTED, "invocation_id": iid, "run_id": rid, "task_id": "task"}


def _publish(root, row, number):
    event = {"event_id": f"event-{number}", "ts": f"2026-10-16T00:00:{number:02d}Z", **row}
    assert current.publish(root, event)


def _oracle_operation(root, rows):
    token = next((str(row.get("invocation_id") or "") for row in rows
                  if row["type"] == c.START_REQUESTED and row.get("operation_id") == "operation"
                  and row.get("slot_id") == "slot"), "")
    record = c.invocation_record(root, token, rows=rows) if token else None
    return {**record, "invocation_id": token} if record else None


@pytest.mark.parametrize("history", [
    [_request(), _started("healthy")],
    [_request(), {"type": c.START_FAILED, "invocation_id": "inv", "definite": True},
     _request(request={"prompt": "replacement"}, billing_group={"billing_group_id": "replacement-group"}),
     _started("b")],
    [_request(), _started("a"), _started("b")],
    [_request(), _started("z"), _started("a"), _started("z")],
], ids=["healthy", "refused-repeated-start", "last-start", "repeated-run-last-start"])
def test_live_invocation_equals_chain_at_every_prefix(tmp_path, history):
    for number, event in enumerate(history):
        _publish(tmp_path, event, number)
        rows = list(c.custody_rows(tmp_path))
        expected = c.invocation_record(tmp_path, "inv", rows=rows)
        assert c.invocation_record(tmp_path, "inv") == expected
    bound = current.run(tmp_path, expected["run_id"])
    assert bound.billing_group == history[0]["billing_group"]
    assert bound.request_event == rows[0]


@pytest.mark.parametrize("times", [
    ("2026-10-16T02:00:00Z", "2026-10-16T01:00:00Z"),
    ("2026-10-16T01:00:00Z", "2026-10-16T01:00:00Z"),
], ids=["inverted", "tied"])
def test_operation_uses_append_order_after_delayed_outbox_flush(tmp_path, monkeypatch, times):
    # Publish-time clocks tie too; event ids sort AGAINST the actual commit order.
    monkeypatch.setattr(store, "utc_now_iso", lambda: "2026-10-16T03:00:00Z")
    with monkeypatch.context() as paused:
        paused.setattr(current, "_append_durable", lambda *args: False)
        _publish(tmp_path, _request("z-first", ts=times[0], event_id="z-first"), 0)
        _publish(tmp_path, _request("a-second", ts=times[1], event_id="a-second"), 1)
    assert current.flush_outbox(tmp_path) == 2
    rows = list(c.custody_rows(tmp_path))
    assert [row["invocation_id"] for row in rows] == ["z-first", "a-second"]
    assert rows[0]["custody_ordinal"] < rows[1]["custody_ordinal"]
    assert inv.operation_record(tmp_path, "operation", "slot") == _oracle_operation(tmp_path, rows)
    # New transactions/connections must keep the same durable selection.
    _publish(tmp_path, _started("first-run", "z-first"), 2)
    _publish(tmp_path, _started("second-run", "a-second"), 3)
    rows = list(c.custody_rows(tmp_path))
    assert inv.operation_record(tmp_path, "operation", "slot") == _oracle_operation(tmp_path, rows)
    assert inv.operation_record(tmp_path, "operation", "slot")["run_id"] == "first-run"


def test_invocation_reads_return_independent_copies(tmp_path):
    _publish(tmp_path, _request(), 0)
    before = deepcopy(c.invocation_record(tmp_path, "inv"))
    returned = c.invocation_record(tmp_path, "inv")
    returned["request"]["prompt"] = "changed by reader"
    assert c.invocation_record(tmp_path, "inv") == before
