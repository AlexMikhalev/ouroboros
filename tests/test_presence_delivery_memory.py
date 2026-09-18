"""Every memory reader retains where a reported message went and what is known."""
from __future__ import annotations

import pytest

from ouroboros.consolidator import _format_entries_for_block
from ouroboros.memory import Memory
from ouroboros.utils import append_jsonl


def _row(state):
    return {
        "ts": "2026-01-02T03:04:05Z", "direction": "out", "chat_id": 73,
        "text": "The exact message", "source": "presence:chat-provider",
        "transport": {"provider": "chat-provider", "account_id": "account-1",
                      "conversation_id": "direct-7", "thread_id": "thread-2",
                      "delivery": {"state": state, "delivery_id": "request-3", "part_id": "0"}},
    }


@pytest.mark.parametrize("state,label", [
    ("delivered", "delivery=delivered"),
    ("authored", "delivery=authored (delivery unconfirmed)"),
    ("accepted", "delivery=accepted (provider acceptance only)"),
])
def test_outgoing_destination_and_state_survive_all_memory_views(tmp_path, state, label):
    row = _row(state)
    memory = Memory(tmp_path)
    append_jsonl(tmp_path / "logs/chat.jsonl", row)
    for view in (memory.summarize_chat([row]), memory.chat_history(count=10), _format_entries_for_block([row])):
        assert "The exact message" in view
        assert "provider=chat-provider" in view
        assert "account=account-1" in view
        assert "conversation=direct-7" in view
        assert "thread=thread-2" in view
        assert label in view


@pytest.mark.parametrize("state", ["failed", "uncertain"])
def test_failed_send_is_a_system_fact_not_confirmed_speech(state):
    row = {**_row(state), "direction": "system", "type": "presence_delivery"}
    for view in (Memory._format_chat_line(row, compact=True), _format_entries_for_block([row])):
        assert "delivery=" + state in view
        assert "conversation=direct-7" in view
        assert "delivery=delivered" not in view
    assert Memory._format_chat_line(row, compact=True).startswith("📋")
    assert "[system]" in _format_entries_for_block([row])


def test_ordinary_owner_reply_format_is_unchanged():
    row = {"direction": "out", "ts": "2026-01-02T03:04:05Z", "text": "Hello", "source": "web", "transport": {}}
    assert Memory._format_chat_line(row, compact=True) == "→ 03:04 Hello"
    assert Memory._format_chat_line(row, compact=False) == "→ [2026-01-02T03:04] Hello"
    assert _format_entries_for_block([row]) == "[2026-01-02 03:04] -> Ouroboros: Hello"


def test_attachment_and_mail_receipt_facts_do_not_disappear_from_memory(tmp_path):
    row = {**_row("accepted"), "type": "presence_delivery", "text": ""}
    row["transport"]["message"] = {
        "attachments": [{"filename": "report.pdf", "mime_type": "application/pdf"}],
        "recipients": ["reader@example.org"], "subject": "Requested report",
        "provider_message_id": "provider-42",
    }
    memory = Memory(tmp_path)
    append_jsonl(tmp_path / "logs/chat.jsonl", row)
    for view in (memory.summarize_chat([row]), memory.chat_history(count=10), _format_entries_for_block([row])):
        assert "Delivery details:" in view and "report.pdf" in view
        assert "reader@example.org" in view and "Requested report" in view
        assert "provider acceptance only" in view
    assert "report.pdf" in memory.chat_history(count=10, search="report.pdf")
    legacy = {**row, "type": ""}
    assert "Delivery details:" not in Memory._format_chat_line(legacy, compact=True)
