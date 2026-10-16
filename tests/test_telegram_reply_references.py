"""Telegram references are transport facts; the host owns their meaning and routing."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tests.test_telegram_format_parity import _load_skill

# The shared native-skill loader replaces its package entries in sys.modules.
pytestmark = pytest.mark.serial


class Api:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.logs = []

    def get_state_dir(self):
        return str(self.state_dir)

    def get_runtime_info(self):
        return {"data_dir": str(self.state_dir / "data")}

    def get_settings(self, _keys):
        return {"TELEGRAM_BOT_TOKEN": "test-token"}

    def log(self, level, message):
        self.logs.append((level, message))


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    plugin, telegram_api = _load_skill()
    api = Api(tmp_path)
    (tmp_path / "settings.json").write_text(json.dumps({"TELEGRAM_CHAT_ID": "42"}), encoding="utf-8")
    calls, replies = [], []

    class Wire:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, url, *, data, files):
            calls.append((url.rsplit("/", 1)[-1], dict(data or {})))
            if replies:
                value = replies.pop(0)
                if isinstance(value, Exception):
                    raise value
                return httpx.Response(value[0], json=value[1])
            mid = (data or {}).get("message_id") or (100 + len(calls))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": int(mid)}})

    monkeypatch.setattr(telegram_api.httpx, "AsyncClient", Wire)
    return SimpleNamespace(plugin=plugin, transport=telegram_api, api=api, calls=calls, replies=replies)


def _state(plugin):
    import sys
    return sys.modules[plugin.__package__ + ".lib.telegram_state"]


def test_every_chunk_id_is_retained_across_reload_and_nonlast_resolves(bridge):
    b = bridge
    state = _state(b.plugin)
    record = {"row_address": "row:1@2026-10-16T12:00:00Z#123456789abc", "task_id": "work-1", "kind": "message"}
    client = b.transport.TelegramClient("test-token", on_message=lambda chat, mid: state._remember_message(b.api, chat, mid, record))
    last = asyncio.run(client.send_message(42, "word " * 2000, parse_mode=""))
    assert len(b.calls) >= 3 and last == 100 + len(b.calls)
    plugin, _transport = _load_skill()  # no in-memory map survives a plugin restart
    restored = _state(plugin)
    for mid in range(101, last + 1):
        assert restored._reply_reference(Api(b.api.state_dir), 42, mid) == record["row_address"]
    assert restored._reply_reference(b.api, 43, 101) == ""
    assert restored._reply_reference(b.api, 42, 9999) == ""


def test_confirmed_chunk_survives_later_unknown_send(bridge):
    b = bridge
    seen = []
    b.replies.extend([(200, {"ok": True, "result": {"message_id": 51}}), httpx.ReadTimeout("offline")])
    client = b.transport.TelegramClient("test-token", on_message=lambda chat, mid: seen.append((chat, mid)))
    with pytest.raises(b.transport.TelegramTransportError):
        asyncio.run(client.send_message(42, "x" * 5000, parse_mode=""))
    assert seen == [(42, 51)] and len(b.calls) == 2


def test_fallback_media_keyboard_and_edit_share_confirmed_message_observer(bridge):
    b = bridge
    seen = []
    client = b.transport.TelegramClient("test-token", on_message=lambda chat, mid: seen.append((chat, mid)), reply_to_message_id=17)
    b.replies.extend([(400, {"ok": False}), (200, {"ok": True, "result": {"message_id": 61}})])

    async def run():
        await client.send_message(42, "**Hello**")
        await client.send_photo(42, "eA==")
        await client.send_document(42, b"file")
        await client.send_audio(42, b"file", "a.mp3")
        await client.send_message_with_inline_keyboard(42, "Choose", [[{"text": "A", "callback_data": "a"}]])
        await client.call("sendVideo", data={"chat_id": "42"})
        assert await client.edit_message_text(42, 61, "Updated")
        b.replies.append((400, {"ok": False, "description": "Bad Request: message is not modified"}))
        assert await client.edit_message_text(42, 61, "Updated")

    asyncio.run(run())
    assert [mid for _, mid in seen] == [61, 103, 104, 105, 106, 107, 61, 61]
    for method, data in b.calls:
        assert (data.get("reply_to_message_id") == 17) is method.startswith("send")
    assert b.calls[1][1]["text"] == "Hello"


def test_message_reference_retention_is_bounded_and_work_cards_use_task_address(bridge, monkeypatch):
    state, api = _state(bridge.plugin), bridge.api
    monkeypatch.setattr(state, "_MAX_MESSAGE_REFS", 3)
    for mid in range(1, 6):
        state._remember_message(api, 42, mid, {"row_address": f"row-{mid}", "kind": "work_card", "task_id": f"task-{mid}"})
    state._remember_message(api, 42, 3, {"row_address": "updated-row", "kind": "message"})
    assert list(state._message_refs(api)) == ["42:4", "42:5", "42:3"]
    assert state._reply_reference(api, 42, 1) == ""
    assert state._reply_reference(api, 42, 4) == "task:task-4"
    assert state._reply_reference(api, 42, 3) == "updated-row"
