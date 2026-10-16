"""Reply facts and addresses name persisted rows, independently of their transport/view."""
import json

import pytest

from ouroboros.chat_chain import format_address, row_address
from ouroboros.chat_references import resolve_reply
from supervisor import message_bus
from tests.test_chat_attachments import _history, _web_bridge


def test_reply_is_host_resolved_and_replay_uses_the_same_address(tmp_path, monkeypatch):
    bridge, echoes = _web_bridge(tmp_path, monkeypatch)
    original = message_bus.log_chat("out", 1, 0, "  One\n\t" + "word " * 80, ts="2026-10-01T00:00:00Z")
    address = format_address(row_address(original))
    bridge.handle_web_message("this one", client_message_id="reply", task_metadata={"reply_to": address})
    row = json.loads((tmp_path / "logs/chat.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["reply_to"]["snippet"] == ("One " + "word " * 80)[:160]
    assert row["reply_to"]["role"] == "assistant"
    assert echoes[-1]["reply_to"] == row["reply_to"]
    assert echoes[-1]["row_address"] == format_address(row_address(row))
    replay = next(r for r in _history(tmp_path) if r.get("client_message_id") == "reply")
    assert replay["row_address"] == echoes[-1]["row_address"]
    assert replay["reply_to"] == row["reply_to"]
    queued = bridge._inbox.get_nowait()
    assert queued["task_metadata"]["reply_reference"] == row["reply_to"]
    bridge.handle_web_message("this one", client_message_id="reply", task_metadata={"reply_to": address})
    assert bridge._inbox.empty()
    with pytest.raises(ValueError, match="different message"):
        bridge.handle_web_message("this one", client_message_id="reply", task_metadata={"reply_to": "task:other"})


def test_reply_survives_rotation_and_missing_reference_stays_explicit(tmp_path):
    row = {"chat_id": 1, "ts": "2026-10-01T00:00:00Z", "direction": "in", "text": "yes"}
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive/chat_20261002T000000.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    address = format_address(row_address(row))
    assert resolve_reply(tmp_path, address)["snippet"] == "yes"
    assert resolve_reply(tmp_path, "task:missing") == {"address": "task:missing", "unresolved": True}
    assert resolve_reply(tmp_path, address[:-12] + "000000000000")["unresolved"] is True
    with pytest.raises(ValueError, match="string"):
        resolve_reply(tmp_path, {"snippet": "forged"})


def test_skill_ingress_shares_reply_and_address_with_web(tmp_path, monkeypatch):
    bridge, echoes = _web_bridge(tmp_path, monkeypatch)
    row = message_bus.log_chat("out", 1, 0, "Pick this", ts="2026-10-01T00:00:00Z")
    address = format_address(row_address(row))
    accepted, rejoined = message_bus.accept_local_message(
        bridge, tmp_path, "yes", chat_id=1, user_id=1, client_message_id="skill-reply",
        source="skill:telegram", task_metadata={"reply_to": address})
    assert not rejoined and accepted["reply_to"]["address"] == address
    queued = bridge._inbox.get_nowait()
    message_bus.record_inbound_message(bridge, queued, chat_id=1, user_id=1,
        client_message_id="skill-reply", text="yes", ts=accepted["ts"])
    assert echoes[-1]["row_address"] == format_address(row_address(accepted))
    assert echoes[-1]["reply_to"] == accepted["reply_to"]
