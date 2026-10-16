"""Upgrade/replay reaches the existing sender, preserving content and event identity."""
import asyncio
import copy
from collections import deque
from types import SimpleNamespace

import pytest

from ouroboros import terminal_projection
from ouroboros.task_results import load_task_result, write_task_result
from ouroboros.utils import append_jsonl
from supervisor import events_chat_delivery as delivery, message_bus
from tests.test_completion_notices import _record
from tests.test_project_completion_mirror import _enqueue_row
from tests.test_telegram_subagent_cards import _setup, _Rec


@pytest.mark.parametrize("notify", ["off", "on"])
@pytest.mark.parametrize("salvage", [False, True])
@pytest.mark.parametrize("lane", ["canonical", "outbox"])
def test_legacy_pending_event_through_sender(notify, salvage, lane, tmp_path, monkeypatch):
    plugin, api = _setup(tmp_path / "telegram", monkeypatch, TELEGRAM_NOTIFY_TASKS=notify)
    handle = plugin._make_outbound(api)
    root = tmp_path / "host"
    record = _record("receipt")
    record.update(terminal_origin="host_salvage" if salvage else "model_final",
                  result="Preserved intermediate output (not a final answer): useful draft" if salvage else "Final answer")
    event = _enqueue_row(root, monkeypatch, record)
    # This additive field is the only difference from the actual pre-upgrade producer.
    event["progress_meta"].pop("completion_notice")
    original = copy.deepcopy(event)
    ready = {"token": "publication-1", "main_delivery": {"event": event, "attempts": 0}}
    fields = {key: value for key, value in record.items() if key not in {"task_id", "status"}}
    fields.update(canonical_terminal_projection_ready=ready)
    write_task_result(root, "root-project", "completed", **fields)
    if lane == "canonical":
        event = terminal_projection.retain_main_delivery(root, "root-project", event)
    # Outbox replay goes directly to the same sender, bypassing the producer.
    frames = []
    bridge = message_bus.LocalChatBridge({})
    bridge._broadcast_fn = frames.append
    monkeypatch.setattr(message_bus, "DATA_DIR", root)
    monkeypatch.setattr(message_bus, "get_bridge", lambda: bridge)
    monkeypatch.setattr(message_bus, "load_state", lambda: {"owner_id": 7})
    monkeypatch.setattr(message_bus, "_advance_project_visible_revision", lambda _chat: None)
    monkeypatch.setattr(message_bus, "publish_event", lambda _topic, row: asyncio.run(handle(row)))
    monkeypatch.setattr(delivery, "_DELIVERED_MESSAGE_IDS", deque(maxlen=256))
    monkeypatch.setattr(delivery, "_handoff_delivered_review", lambda *_a, **_kw: None)
    ctx = SimpleNamespace(DRIVE_ROOT=root, RUNNING={}, append_jsonl=append_jsonl,
                          send_with_budget=message_bus.send_with_budget)
    delivery._handle_send_message(event, ctx)
    assert frames[-1]["content"] == original["text"]
    assert frames[-1]["target_label"] == original["progress_meta"]["target_label"]
    assert frames[-1]["delivery_id"] == original["delivery_id"]
    sent = "\n".join(row[1] for row in _Rec.sent)
    assert ("useful draft" in sent) is salvage
    if notify == "off":
        assert len(_Rec.sent) == int(salvage)
        assert "done with warnings" not in sent.lower()
    else:
        assert len(_Rec.sent) == 1 and original["progress_meta"]["target_label"] in sent
        assert "earlier version" not in sent
    stored = load_task_result(root, "root-project", strict=True)
    assert stored["canonical_terminal_projection_ready"]["main_delivery"]["event"] == original
    delivery._handle_send_message(event, ctx)
    assert len(_Rec.sent) == (1 if notify == "on" or salvage else 0)


def test_unknown_legacy_source_is_not_assumed_status_only(tmp_path, monkeypatch):
    plugin, api = _setup(tmp_path, monkeypatch, TELEGRAM_NOTIFY_TASKS="off")
    event = {"text": "Kept content whose result source is unavailable.", "role": "system",
             "system_type": "project_completion_summary", "task_id": "old"}
    assert terminal_projection.completion_event_view(event, {}) is event
    asyncio.run(plugin._make_outbound(api)(event))
    assert _Rec.sent[0][1] == event["text"]
