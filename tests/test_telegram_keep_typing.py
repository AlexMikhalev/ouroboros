"""Keep-typing refresh loop for the Telegram bridge (#1766).

One ``sendChatAction`` decays on Telegram clients in ~5s while a turn runs
for minutes. These tests pin the loop contract: periodic re-sends below the
client expiry, backoff on transient failures, a permanent-rejection stop,
the lifetime cap, replacement on a re-armed ``chat.typing``, retirement on a
terminal outbound row (progress rows keep the loop), and the unload cancel.
Loop timing is patched to keep the suite fast (no test sleeps a real 3s).
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest


def _load():
    """Load plugin (which imports the lib modules) and return the typing module."""
    root = Path(__file__).resolve().parents[1] / "skills" / "telegram"
    pkg = types.ModuleType("tg_kt"); pkg.__path__ = [str(root)]; sys.modules["tg_kt"] = pkg
    spec = importlib.util.spec_from_file_location("tg_kt.plugin", root / "plugin.py")
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return sys.modules["tg_kt.lib.telegram_typing"]


def _api_module():
    return sys.modules["tg_kt.lib.telegram_api"]


@pytest.fixture(autouse=True)
def _clean_loops():
    yield
    # Every test must leave no armed loop behind (the unload contract).
    mod = None
    try:
        mod = sys.modules.get("tg_kt.lib.telegram_typing")
    except Exception:
        mod = None
    if mod is not None:
        mod.stop_all_keep_typing()
        assert mod.active_keep_typing_keys() == []


class _Rec:
    """Recording client: counts send_chat_action calls, optional failures."""
    def __init__(self, failures=None):
        self.actions = 0
        self.failures = list(failures or [])

    async def send_chat_action(self, chat_id, action="typing"):
        self.actions += 1
        if self.failures:
            raise self.failures.pop(0)


class _Api:
    def __init__(self):
        self.warnings = []

    def log(self, level, message):
        self.warnings.append((level, message))


def _run(coro):
    return asyncio.run(coro)


def test_refresh_resends_until_terminal_row(monkeypatch):
    mod = _load()
    rec = _Rec()
    sends = []
    monkeypatch.setattr(mod, "_REFRESH_SEC", 0.01)
    monkeypatch.setattr(mod, "_SEND_TIMEOUT_SEC", 0.5)

    async def scenario():
        api = _Api()
        mod.start_keep_typing(api, 7, "task-1", client_factory=lambda: rec)
        await asyncio.sleep(0.05)  # ~5 ticks at 10ms
        assert rec.actions >= 3
        assert mod.active_keep_typing_keys() == ["7:task-1"]
        # A progress row must NOT retire the loop (the turn is still working);
        # the terminal row (the call below) retires it:
        mod.stop_keep_typing(7, "task-1")
        await asyncio.sleep(0.05)
        after = rec.actions
        await asyncio.sleep(0.05)
        assert rec.actions == after  # no further re-sends after the stop
        assert mod.active_keep_typing_keys() == []
        return sends

    _run(scenario())


def test_transient_failure_backs_off_then_recovers(monkeypatch):
    telegram_api = _api_module()
    TelegramTransportError = telegram_api.TelegramTransportError
    mod = _load()
    rec = _Rec(failures=[TelegramTransportError("dead network")])
    monkeypatch.setattr(mod, "_REFRESH_SEC", 0.01)
    monkeypatch.setattr(mod, "_BACKOFF_MAX_SEC", 0.04)
    monkeypatch.setattr(mod, "_SEND_TIMEOUT_SEC", 0.5)

    async def scenario():
        api = _Api()
        mod.start_keep_typing(api, 7, "task-2", client_factory=lambda: rec)
        await asyncio.sleep(0.02)
        # The first send failed (transport): the loop backed off but is alive.
        assert mod.active_keep_typing_keys() == ["7:task-2"]
        # A later successful send resumes the base cadence; give it time to
        # fire several times and record the action count.
        base = rec.actions
        await asyncio.sleep(0.1)
        assert rec.actions > base
        mod.stop_keep_typing(7, "task-2")
        return None

    _run(scenario())


def test_permanent_rejection_stops_the_loop(monkeypatch):
    telegram_api = _api_module()
    TelegramRequestRejected = telegram_api.TelegramRequestRejected
    mod = _load()
    rejection = TelegramRequestRejected("Telegram API sendChatAction returned HTTP 403.")
    rec = _Rec(failures=[rejection])
    monkeypatch.setattr(mod, "_REFRESH_SEC", 0.01)
    monkeypatch.setattr(mod, "_SEND_TIMEOUT_SEC", 0.5)

    async def scenario():
        api = _Api()
        mod.start_keep_typing(api, 9, "task-3", client_factory=lambda: rec)
        await asyncio.sleep(0.05)
        assert rec.actions == 1  # the rejected send fired once, then the loop stopped
        assert mod.active_keep_typing_keys() == []
        assert any("stopped" in msg for _lvl, msg in api.warnings)
        return None

    _run(scenario())


def test_rearm_replaces_the_previous_loop(monkeypatch):
    mod = _load()
    rec = _Rec()
    monkeypatch.setattr(mod, "_REFRESH_SEC", 0.01)
    monkeypatch.setattr(mod, "_SEND_TIMEOUT_SEC", 0.5)

    async def scenario():
        api = _Api()
        mod.start_keep_typing(api, 7, "task-4", client_factory=lambda: rec)
        first = mod.active_keep_typing_keys()
        mod.start_keep_typing(api, 7, "task-4", client_factory=lambda: rec)
        await asyncio.sleep(0.05)
        # Still exactly one armed loop for the (chat, task) pair.
        assert mod.active_keep_typing_keys() == ["7:task-4"]
        assert first == ["7:task-4"]
        return None

    _run(scenario())


def test_plugin_typing_handler_arms_and_outbound_terminal_retires(monkeypatch, tmp_path):
    """The wiring contract: chat.typing arms the loop for the event's activity,
    a non-progress outbound row retires it, a progress row does not."""
    mod = _load()  # plugin is loaded as a side effect (tg_kt.plugin)
    plugin = sys.modules["tg_kt.plugin"]

    rec = _Rec()
    monkeypatch.setattr(plugin, "_telegram_client", lambda api: rec)
    monkeypatch.setattr(mod, "_REFRESH_SEC", 0.01)
    monkeypatch.setattr(mod, "_SEND_TIMEOUT_SEC", 0.5)

    class _Api:
        def __init__(self, tmp):
            self._tmp = tmp
        def get_state_dir(self):
            return str(self._tmp / "state" / "skills" / "telegram-bridge")
        def get_settings(self, keys):
            return {}
        def log(self, *a, **k):
            pass

    async def scenario(tmp):
        api = _Api(tmp)
        (tmp / "state" / "skills" / "telegram-bridge").mkdir(parents=True)
        typing_handler = plugin._make_typing(api)
        outbound_handler = plugin._make_outbound(api)
        # chat.typing: the immediate action fires AND the loop is armed.
        await typing_handler({"activity_id": "act-77", "chat_id": 77, "transport": {"kind": "telegram", "conversation_id": "77"}})
        assert rec.actions == 1
        await asyncio.sleep(0.04)
        assert mod.active_keep_typing_keys() == ["77:act-77"]
        assert rec.actions >= 2
        # A progress row does not retire the loop.
        await outbound_handler({"chat_id": 77, "task_id": "act-77", "text": "working…", "is_progress": True, "transport": {"kind": "telegram", "conversation_id": "77"}})
        await asyncio.sleep(0.02)
        assert mod.active_keep_typing_keys() == ["77:act-77"]
        # The terminal row retires it.
        await outbound_handler({"chat_id": 77, "task_id": "act-77", "text": "the answer", "is_progress": False, "transport": {"kind": "telegram", "conversation_id": "77"}})
        assert mod.active_keep_typing_keys() == []
        return None

    _run(scenario(tmp_path))


def test_lifetime_cap_stops_a_ghost_loop(monkeypatch):
    mod = _load()
    rec = _Rec()
    monkeypatch.setattr(mod, "_REFRESH_SEC", 0.01)
    monkeypatch.setattr(mod, "_MAX_LIFETIME_SEC", 0.03)
    monkeypatch.setattr(mod, "_SEND_TIMEOUT_SEC", 0.5)

    async def scenario():
        api = _Api()
        mod.start_keep_typing(api, 7, "task-5", client_factory=lambda: rec)
        await asyncio.sleep(0.06)
        assert mod.active_keep_typing_keys() == []  # capped: no ghost loop
        return None

    _run(scenario())


def test_unload_cancels_every_loop(monkeypatch):
    mod = _load()
    rec = _Rec()
    monkeypatch.setattr(mod, "_REFRESH_SEC", 0.01)
    monkeypatch.setattr(mod, "_SEND_TIMEOUT_SEC", 0.5)

    async def scenario():
        api = _Api()
        mod.start_keep_typing(api, 1, "task-a", client_factory=lambda: rec)
        mod.start_keep_typing(api, 2, "task-b", client_factory=lambda: rec)
        assert len(mod.active_keep_typing_keys()) == 2
        mod.stop_all_keep_typing()
        assert mod.active_keep_typing_keys() == []
        return None

    _run(scenario())
