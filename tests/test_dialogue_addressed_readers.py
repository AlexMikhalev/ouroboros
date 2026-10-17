"""Addressed readers use SQL truth, preserve owner bytes, and disclose incomplete import."""
from contextlib import contextmanager

import pytest

from ouroboros import dialogue_import as di, dialogue_store as ds, notice_receipts, owner_words
from ouroboros.gateway.routing_decision import _origin_message, handle_routing_decision
from ouroboros.project_dialogue import (
    append_chat_annotation, build_owner_message_ref, latest_chat_annotations, resolve_owner_message_source,
    resolve_legacy_continuation_narrative,
)
from tests._dialogue import seed


def owner(client_id="owner-one", *, chat_id=1, **extra):
    return {"ts": "2026-10-17T00:00:01+00:00", "direction": "in", "chat_id": chat_id,
            "client_message_id": client_id, "text": "  Слова владельца\n\tцеликом  ", **extra}


@contextmanager
def metered(monkeypatch):
    """Measure actual SQL instructions, including short-lived composite transactions."""
    counts = {"statements": 0, "steps": 0}
    transaction = ds.transaction

    @contextmanager
    def traced(*args, **kwargs):
        with transaction(*args, **kwargs) as conn:
            conn.set_trace_callback(lambda _: counts.__setitem__("statements", counts["statements"] + 1))
            conn.set_progress_handler(lambda: counts.__setitem__("steps", counts["steps"] + 1) or 0, 1)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)
                conn.set_progress_handler(None, 0)

    with monkeypatch.context() as patched:
        patched.setattr(ds, "transaction", traced)
        yield counts


@pytest.mark.parametrize("reverse", [False, True])
def test_probes_page_overlapping_keys_without_repeats_or_foreign_rows(tmp_path, reverse):
    receipts = []
    for index in range(9):
        receipts.append(ds.append(tmp_path, "chat", owner(f"own-{index}", task_id="selected")))
        ds.append(tmp_path, "chat", owner(f"foreign-{index}", chat_id=22))
    upper = ds.stream_end(tmp_path, "chat")
    ds.append(tmp_path, "chat", owner("later", task_id="selected"))
    entries = list(ds.iter_probes(tmp_path, "chat", [("chat_id", 1), ("task_id", "selected")],
                                 lower=receipts[1].seq, upper=upper, batch=2, newest_first=reverse))
    expected = [row.seq for row in receipts[1:]]
    assert [row.seq for row in entries] == (expected[::-1] if reverse else expected)
    assert list(ds.iter_probes(tmp_path, "chat", [], batch=2)) == []
    with pytest.raises(ValueError, match="positive"):
        list(ds.iter_probes(tmp_path, "chat", [("chat_id", 1)], batch=0))


def test_import_marker_only_intersects_unimported_range(tmp_path):
    seed(tmp_path, archives={"chat_20261016T000000.jsonl": [owner("older")]}, live={"chat": [owner()]})
    ds.ensure(tmp_path)
    [segment] = di.pending_segments(tmp_path)
    live = ds.rows_by_client_id(tmp_path, "owner-one")[0]
    assert ds.range_coverage(tmp_path, "chat", upper=live.seq) == {
        "imported": False, "pending": 1, "mismatch": 0}
    assert ds.range_coverage(tmp_path, "chat", lower=live.seq)["imported"]
    assert ds.range_coverage(tmp_path, "chat", upper=0)["imported"]
    with pytest.raises(ds.StoreMigrating, match="still importing"):
        ds.require_imported(tmp_path, "chat", upper=live.seq)
    ds.require_imported(tmp_path, "chat", lower=live.seq)
    di.import_segment(tmp_path, "chat", segment["name"])
    assert ds.range_coverage(tmp_path, "chat")["imported"]
    ds.require_imported(tmp_path, "chat")


def test_origin_and_notice_import_never_claim_absence_while_import_is_pending(tmp_path):
    seed(tmp_path, archives={"chat_20261016T000000.jsonl": [owner()]})
    ds.ensure(tmp_path)
    append_chat_annotation(tmp_path, "owner-one", action="route_decision", status="needs_manual_target",
                           routing_token="route-one", options=[{"action": "steer_task", "task_id": "target"}])
    status, payload = handle_routing_decision(tmp_path, request_id="click-one",
                                            decision_id="routing:owner-one:route-one", option_index=0)
    assert status == 503 and payload["error"] == "history_importing"
    assert "still importing" in payload["reason"]
    with pytest.raises(ds.StoreMigrating):
        notice_receipts.import_upgrade_receipts(tmp_path)
    [segment] = di.pending_segments(tmp_path)
    di.import_segment(tmp_path, "chat", segment["name"])
    assert _origin_message(tmp_path, "owner-one") == owner()
    assert _origin_message(tmp_path, "absent") == {}
    assert notice_receipts.import_upgrade_receipts(tmp_path) == {}


@pytest.mark.parametrize("reader", ["owner", "routing", "words", "notices"])
def test_named_reader_has_equal_sql_work_with_one_or_thirty_foreign_rooms(tmp_path, monkeypatch, reader):
    row = owner()
    ref = build_owner_message_ref(chat_id=1, client_message_id=row["client_message_id"], ts=row["ts"], text=row["text"])
    counts = []
    for rooms in (1, 30):
        root = tmp_path / str(rooms)
        ds.append(root, "chat", row)
        ds.append(root, "chat", {"chat_id": 1, "direction": "system", "type": "legacy_memory_notice", "ts": row["ts"]})
        for room in range(rooms):
            for number in range(10):
                ds.append(root, "chat", owner(f"foreign-{room}-{number}", chat_id=room + 10))
        reads = {
            "owner": lambda: resolve_owner_message_source(root, ref),
            "routing": lambda: _origin_message(root, "owner-one"),
            "words": lambda: owner_words._inbound_rows(root, {"owner-one": "steer_task"}),
            "notices": lambda: notice_receipts.import_upgrade_receipts(root),
        }
        with metered(monkeypatch) as measured:
            result = reads[reader]()
        counts.append(dict(measured))
        expected = {"1:legacy_memory_notice": {"chat_id": 1, "type": "legacy_memory_notice",
                                             "ts": row["ts"], "recorded": True}}
        assert result == (expected if reader == "notices" else [row] if reader == "words" else row)
        assert not (root / "logs" / "chat.jsonl").exists()
    assert counts[0] == counts[1] and counts[0]["steps"] > 0


def test_annotation_compaction_uses_stored_ids_and_keeps_pending_import(tmp_path, monkeypatch):
    from ouroboros import project_dialogue as pd

    seed(tmp_path, archives={"chat_20261016T000000.jsonl": [owner()]})
    monkeypatch.setattr(pd, "_COMPACT_AT_BYTES", 1)
    append_chat_annotation(tmp_path, "owner-one", action="steer_task", status="delivered")
    append_chat_annotation(tmp_path, "absent", action="steer_task", status="delivered")
    assert set(latest_chat_annotations(tmp_path)) == {"owner-one", "absent"}
    [segment] = di.pending_segments(tmp_path)
    di.import_segment(tmp_path, "chat", segment["name"])
    append_chat_annotation(tmp_path, "owner-one", action="steer_task", status="delivered")
    assert set(latest_chat_annotations(tmp_path)) == {"owner-one"}


def test_continuation_narrative_is_addressed_to_its_task_and_checks_result_identity(tmp_path):
    result_ref = {"kind": "task_result", "task_id": "selected", "reader": "get_task_result"}
    summary = {"task_id": "selected", "chat_id": 1, "type": "task_summary",
               "summary_kind": "authored_root_summary", "summary_id": "task-narrative:selected",
               "result_ref": result_ref, "source_coverage": {"task_result": result_ref}, "text": "Kept answer"}
    assert resolve_legacy_continuation_narrative(tmp_path, "selected", result_ref) is None
    receipt = ds.append(tmp_path, "chat", summary)
    ds.append(tmp_path, "chat", {**summary, "task_id": "foreign", "text": "Another task"})
    ds.append(tmp_path, "chat", {**summary, "result_ref": {**result_ref, "task_id": "foreign"}, "text": "Wrong ref"})
    actual = resolve_legacy_continuation_narrative(tmp_path, "selected", result_ref)
    assert actual["text"] == "Kept answer" and actual["source"]["history_id"] == f"chat:{receipt.seq}"
    assert actual["source"]["path"] == str(ds.path(tmp_path))
    assert resolve_legacy_continuation_narrative(tmp_path, "foreign", result_ref) is None
