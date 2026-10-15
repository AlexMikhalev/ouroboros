"""The bundled meeting_audio skill: manifest, tools, control channel, call peer and companion.

The call peer talks to a local fake of the Host Service door that keeps the real
door's rules (hello first, frames at the declared rate and current epoch, flush,
bye, close when the call ends); the companion runs with a fake capture backend.
The platform backends have their own suite, test_skill_meeting_audio_backends.py.
No test touches a real audio device, permission prompt or meeting.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import pathlib
import queue
import shutil
import socket
import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "meeting_audio"
SCRIPTS = SKILL_DIR / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import meeting_apps  # noqa: E402
import meeting_capture  # noqa: E402
import meeting_control  # noqa: E402
import meeting_peer  # noqa: E402
from meeting_apps import RunningApp  # noqa: E402

CALL_META = {"live_call": {"call_id": "call-1", "chat_id": 7, "engine": "openai", "model": "m"}}


def until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def load_plugin():
    package = types.ModuleType("meeting_audio_under_test")
    package.__path__ = [str(SKILL_DIR)]
    sys.modules["meeting_audio_under_test"] = package
    return importlib.import_module("meeting_audio_under_test.plugin")


class FakeAPI:
    def __init__(self, state_dir: pathlib.Path):
        self.state_dir = state_dir
        self.tools = {}
        self.companions = []

    def get_state_dir(self) -> str:
        return str(self.state_dir)

    def register_tool(self, name, handler, **metadata):
        self.tools[name] = {"handler": handler, **metadata}

    def register_companion_process(self, name):
        self.companions.append(name)


# ---- manifest, preflight, plugin -------------------------------------------------------

def test_manifest_declares_tools_companion_and_isolated_platform_dependencies():
    from ouroboros.contracts.skill_manifest import VALID_SKILL_PERMISSIONS, parse_skill_manifest_text
    from ouroboros.skill_dependencies import _manifest_declared_raw, normalize_declared_dependency_specs

    manifest = parse_skill_manifest_text((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))
    assert (manifest.name, manifest.type, manifest.entry, manifest.runtime) == (
        "meeting_audio", "extension", "plugin.py", "python3")
    assert set(manifest.permissions) == {"tool", "call_peer", "companion_process", "subprocess"}
    assert manifest.plugin_api["version"] == "2.0"
    [companion] = manifest.companion_processes
    assert companion["name"] == "meeting_capture"
    assert companion["command"] == ["python3", "scripts/meeting_capture.py"]
    # call_peer becomes a known permission when the Host Service door lands; until then it is
    # the only soft warning this manifest may produce.
    warnings = manifest.validate()
    if "call_peer" in VALID_SKILL_PERMISSIONS:
        assert warnings == []
    else:
        assert warnings and all("call_peer" in warning for warning in warnings)
    auto, manual, dependency_warnings = normalize_declared_dependency_specs(_manifest_declared_raw(manifest))
    assert manual == [] and dependency_warnings == []
    by_platform = {tuple(spec["platforms"]): spec for spec in auto}
    assert by_platform[("darwin",)]["package"] == "pyobjc-framework-ScreenCaptureKit==12.2.2"
    assert by_platform[("win32",)]["package"] == "PyAudioWPatch==0.2.12.9"
    assert all(spec["check"]["argv"][0] == "python3" for spec in auto)


def test_skill_preflight_is_clean(tmp_path, monkeypatch):
    from ouroboros.tools.registry import ToolContext
    from ouroboros.tools.skill_preflight import _handle_skill_preflight

    skills_root = tmp_path / "skills"
    shutil.copytree(SKILL_DIR, skills_root / "meeting_audio",
                    ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    (tmp_path / "repo").mkdir()
    (tmp_path / "drive").mkdir()
    result = json.loads(_handle_skill_preflight(
        ToolContext(repo_dir=tmp_path / "repo", drive_root=tmp_path / "drive"), skill="meeting_audio"))
    assert result["ok"] is True, result
    assert result["files_failed"] == 0
    assert {row["path"] for row in result["files"]} >= {"plugin.py", "scripts/meeting_capture.py"}


def test_plugin_registers_five_ctx_tools_and_the_companion(tmp_path):
    from ouroboros.extension_process_runner import _handler_wants_ctx

    api = FakeAPI(tmp_path)
    load_plugin().register(api)
    assert set(api.tools) == {"meeting_listen_start", "meeting_listen_stop", "meeting_speak_start",
                              "meeting_speak_stop", "meeting_status"}
    assert all(_handler_wants_ctx(tool["handler"]) for tool in api.tools.values())
    assert api.companions == ["meeting_capture"]


def test_listen_start_outside_a_call_refuses_in_plain_words(tmp_path, monkeypatch):
    plugin = load_plugin()
    monkeypatch.setattr(plugin, "request", lambda *a, **k: pytest.fail("no companion call outside a call"))
    api = FakeAPI(tmp_path)
    plugin.register(api)
    reply = json.loads(api.tools["meeting_listen_start"]["handler"](SimpleNamespace(task_metadata={}), app="zoom"))
    assert reply["ok"] is False and reply["code"] == "MEETING_NO_CALL"
    assert "press Call" in reply["message"]
    reply = json.loads(api.tools["meeting_listen_start"]["handler"](SimpleNamespace(task_metadata=None)))
    assert reply["code"] == "MEETING_NO_CALL"


@pytest.mark.parametrize("platform_name,word", [("darwin", "macOS"), ("win32", "Windows")])
def test_speaking_is_refused_on_darwin_and_win32(tmp_path, monkeypatch, platform_name, word):
    plugin = load_plugin()
    monkeypatch.setattr(plugin, "_platform", lambda: platform_name)
    monkeypatch.setattr(plugin, "request", lambda *a, **k: pytest.fail("speaking must not reach the companion"))
    api = FakeAPI(tmp_path)
    plugin.register(api)
    for name in ("meeting_speak_start", "meeting_speak_stop"):
        reply = json.loads(api.tools[name]["handler"](SimpleNamespace(task_metadata=CALL_META)))
        assert reply["ok"] is False and reply["code"] == "MEETING_SPEAK_UNSUPPORTED"
        assert word in reply["message"] and "audio driver" in reply["message"]
        assert "browser participant" in reply["message"]


def test_speaking_on_linux_reaches_the_companion_with_the_call(tmp_path, monkeypatch):
    plugin = load_plugin()
    monkeypatch.setattr(plugin, "_platform", lambda: "linux")
    seen = []
    monkeypatch.setattr(plugin, "request", lambda state, op, args, **k: seen.append((op, args)) or {"ok": True})
    api = FakeAPI(tmp_path)
    plugin.register(api)
    assert json.loads(api.tools["meeting_speak_start"]["handler"](SimpleNamespace(task_metadata=CALL_META)))["ok"]
    assert seen == [("speak_start", {"call_id": "call-1"})]


# ---- control channel ------------------------------------------------------------------

@pytest.mark.serial
def test_tools_reach_the_companion_over_the_control_channel(tmp_path):
    seen = []

    def handler(op, args):
        seen.append((op, args))
        return {"ok": True, "state": "listening", "message": "listening to Zoom"}

    server = meeting_control.ControlServer(tmp_path, handler)
    server.start()
    try:
        endpoint = json.loads((tmp_path / meeting_control.CONTROL_FILE).read_text(encoding="utf-8"))
        assert endpoint["port"] == server.port and len(endpoint["token"]) >= 32
        if sys.platform != "win32":
            assert (tmp_path / meeting_control.CONTROL_FILE).stat().st_mode & 0o077 == 0
        api = FakeAPI(tmp_path)
        load_plugin().register(api)
        reply = json.loads(api.tools["meeting_listen_start"]["handler"](
            SimpleNamespace(task_metadata=CALL_META), app=" zoom "))
        assert reply["message"] == "listening to Zoom"
        status = json.loads(api.tools["meeting_status"]["handler"](SimpleNamespace(task_metadata={})))
        assert status["in_call"] is False
        assert seen == [("listen_start", {"call_id": "call-1", "app": "zoom"}), ("status", {})]
    finally:
        server.stop()
    assert not (tmp_path / meeting_control.CONTROL_FILE).exists()


@pytest.mark.serial
def test_control_channel_refuses_a_wrong_token_and_reports_a_missing_companion(tmp_path):
    server = meeting_control.ControlServer(tmp_path, lambda op, args: pytest.fail("unauthenticated request"))
    server.start()
    try:
        endpoint = json.loads((tmp_path / meeting_control.CONTROL_FILE).read_text(encoding="utf-8"))
        (tmp_path / meeting_control.CONTROL_FILE).write_text(json.dumps({**endpoint, "token": "forged"}))
        reply = meeting_control.request(tmp_path, "status")
        assert reply == {"ok": False, "code": "MEETING_CONTROL_REFUSED", "message": "not authorized"}
    finally:
        server.stop()
    (tmp_path / meeting_control.CONTROL_FILE).unlink(missing_ok=True)
    with pytest.raises(meeting_control.ControlUnavailable, match="not running"):
        meeting_control.request(tmp_path, "status")
    api = FakeAPI(tmp_path)
    load_plugin().register(api)
    reply = json.loads(api.tools["meeting_listen_stop"]["handler"](SimpleNamespace(task_metadata={})))
    assert reply["code"] == "MEETING_COMPANION_UNAVAILABLE" and "not running" in reply["message"]


# ---- wire mirror and application choice ---------------------------------------------------

def test_companion_wire_mirrors_the_frozen_call_protocol():
    from ouroboros import call_protocol as cp

    assert meeting_peer.HEADER.format == cp._HEADER.format and meeting_peer.HEADER.size == cp.HEADER_SIZE
    assert (meeting_peer.MAGIC, meeting_peer.PROTOCOL_VERSION) == (cp._MAGIC, cp.PROTOCOL_VERSION)
    assert (meeting_peer.FRAME_AUDIO_IN, meeting_peer.FRAME_AUDIO_OUT) == (cp.FRAME_AUDIO_IN, cp.FRAME_AUDIO_OUT)
    assert (meeting_peer.MAX_FRAME_PAYLOAD, meeting_peer.MAX_CONTROL_BYTES) == (
        cp.MAX_FRAME_PAYLOAD, cp.MAX_CONTROL_BYTES)
    assert meeting_peer.PEER_MESSAGE_TYPES == cp.PEER_MESSAGE_TYPES
    assert meeting_peer.SERVER_MESSAGE_TYPES == cp.SERVER_MESSAGE_TYPES
    assert (meeting_peer.PEER_MEETING, meeting_peer.STATE_CLOSED) == (cp.PEER_MEETING, cp.STATE_CLOSED)
    pcm = b"\x01\x00\xff\x7f"
    decoded = cp.decode_frame(meeting_peer.encode_frame(cp.FRAME_AUDIO_IN, 70000, 5, 48000, 2, pcm))
    assert decoded == cp.AudioFrame(cp.FRAME_AUDIO_IN, 70000 & 0xFFFF, 5, 48000, 2, pcm)
    mirrored = meeting_peer.decode_frame(cp.encode_frame(cp.AudioFrame(cp.FRAME_AUDIO_OUT, 3, 9, 24000, 0, pcm)))
    assert mirrored == {"kind": 2, "epoch": 3, "seq": 9, "rate": 24000, "stream": 0, "pcm": pcm}


RUNNING = [
    RunningApp("zoom.us", "us.zoom.xos", 10),
    RunningApp("Google Chrome", "com.google.chrome", 11),
    RunningApp("Slack", "com.tinyspeck.slackmacgap", 12),
    RunningApp("Microsoft Edge", "com.microsoft.edgemac", 13),
    RunningApp("Notes", "com.apple.Notes", 14),
]


def test_default_choice_is_running_meeting_apps_never_a_browser():
    selection = meeting_apps.choose(RUNNING)
    assert [app.pid for app in selection.apps] == [10, 12]
    assert selection.label == "Zoom and Slack" and selection.browser is False
    assert meeting_apps.same_browser_note(selection) == ""
    with pytest.raises(meeting_apps.TargetRefused) as refused:
        meeting_apps.choose([app for app in RUNNING if app.pid in (11, 13, 14)])
    assert refused.value.code == "MEETING_APP_NOT_FOUND"
    assert refused.value.candidates == ["Google Chrome", "Microsoft Edge"]
    assert "name it with app" in refused.value.message


def test_named_app_matches_exactly_then_by_part_and_refuses_ambiguity():
    assert meeting_apps.choose(RUNNING, "ZOOM").label == "Zoom"
    chrome = meeting_apps.choose(RUNNING, "chrome")
    assert chrome.browser is True and [app.pid for app in chrome.apps] == [11]
    assert "Ouroboros page is open in Google Chrome" in meeting_apps.same_browser_note(chrome)
    with pytest.raises(meeting_apps.TargetRefused) as refused:
        meeting_apps.choose(RUNNING, "com.")
    assert refused.value.code == "MEETING_APP_AMBIGUOUS"
    with pytest.raises(meeting_apps.TargetRefused) as missing:
        meeting_apps.choose(RUNNING, "webex")
    assert missing.value.code == "MEETING_APP_NOT_FOUND" and "Zoom" in missing.value.candidates


def test_candidate_lists_name_what_they_leave_out():
    names = [f"App {index}" for index in range(meeting_apps.MAX_LISTED_CANDIDATES + 3)]
    assert meeting_apps.listed(names).endswith("and 3 more")


# ---- the call peer against a fake Host Service door ------------------------------------------------

class FakeDoor:
    """The Host Service call door's rules, on a real local socket."""

    token = "skill-token"

    def __init__(self, calls=("call-1",), epoch=7):
        self.calls = set(calls)
        self.epoch = epoch
        self.opened, self.hellos, self.frames, self.controls = [], [], [], []
        self.outbox: "queue.Queue" = queue.Queue()
        self.url = ""
        self.sockets = 0

    def send(self, message) -> None:
        self.outbox.put(("bytes", message) if isinstance(message, bytes) else ("text", json.dumps(message)))

    def end_call(self, reason="finished") -> None:
        self.outbox.put(("close", reason))

    def app(self):
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route, WebSocketRoute

        door = self

        async def open_call(request):
            if request.headers.get("x-skill-token") != door.token:
                return JSONResponse({"ok": False, "code": "call_peer_forbidden", "error": "forbidden"}, 403)
            body = await request.json()
            door.opened.append(body)
            if body.get("call_id") not in door.calls:
                return JSONResponse({"ok": False, "code": "call_not_found", "error": "call_not_found"}, 404)
            return JSONResponse({"call_id": body["call_id"], "peer_path": f"/call/{body['call_id']}/peer"})

        async def peer(websocket):
            if websocket.headers.get("x-skill-token") != door.token:
                await websocket.close(code=1008)
                return
            await websocket.accept()
            door.sockets += 1
            hello = json.loads(await websocket.receive_text())
            door.hellos.append(hello)
            await websocket.send_text(json.dumps({"type": "state", "state": "live", "reason": ""}))
            await websocket.send_text(json.dumps({"type": "flush", "epoch": door.epoch}))

            async def pump_out():
                while True:
                    await asyncio.sleep(0.01)
                    while not door.outbox.empty():
                        kind, payload = door.outbox.get_nowait()
                        if kind == "text":
                            message = json.loads(payload)
                            if message.get("type") == "flush":
                                door.epoch = message["epoch"]
                            await websocket.send_text(payload)
                        elif kind == "bytes":
                            await websocket.send_bytes(payload)
                        else:
                            await websocket.send_text(json.dumps({"type": "state", "state": "closed",
                                                                  "reason": payload}))
                            await websocket.close(code=1000)
                            return

            sender = asyncio.create_task(pump_out())
            try:
                while True:
                    event = await websocket.receive()
                    if event["type"] == "websocket.disconnect":
                        return
                    if event.get("bytes") is not None:
                        frame = meeting_peer.decode_frame(event["bytes"])
                        assert frame["rate"] == hello["rate"], "frames must carry the hello rate"
                        door.frames.append(frame)
                    elif event.get("text") is not None:
                        message = json.loads(event["text"])
                        door.controls.append(message)
                        if message.get("type") == "bye":
                            await websocket.close(code=1000)
                            return
            except RuntimeError:
                return  # the call was ended from the sender side
            finally:
                sender.cancel()

        return Starlette(routes=[Route("/call/open", open_call, methods=["POST"]),
                                 WebSocketRoute("/call/{call_id}/peer", peer)])


@contextlib.contextmanager
def serve_fake_door():
    """Run a FakeDoor on a real loopback socket for the duration of the block."""
    import uvicorn

    door = FakeDoor()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(door.app(), log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    assert until(lambda: server.started, 10.0)
    door.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    try:
        yield door
    finally:
        server.should_exit = True
        thread.join(10.0)
        sock.close()


@pytest.fixture
def fake_door():
    with serve_fake_door() as door:
        yield door


def out_frame(epoch: int, pcm: bytes, rate: int = 48000) -> bytes:
    return meeting_peer.encode_frame(meeting_peer.FRAME_AUDIO_OUT, epoch, 0, rate, 0, pcm)


@pytest.mark.serial
def test_peer_says_hello_sends_true_rate_follows_flush_and_says_bye(fake_door):
    heard, flushes, closed = [], [], []
    peer = meeting_peer.DoorPeer(fake_door.url, FakeDoor.token, "call-1", rate=48000, label="Zoom (meeting audio)",
                                 speaker_grant=True, on_audio_out=lambda *a: heard.append(a),
                                 on_flush=flushes.append, on_closed=lambda *a: closed.append(a))
    peer.open()
    assert fake_door.opened == [{"role": "meeting", "call_id": "call-1", "mic": True, "speaker": True}]
    peer.attach(speaker=True)
    assert fake_door.hellos == [{"type": "hello", "protocol": 1, "role": "meeting", "mic": True, "speaker": True,
                                 "rate": 48000, "label": "Zoom (meeting audio)"}]
    assert peer.epoch == 7 and flushes == [7]
    assert peer.send_audio(b"\x00\x01" * 960)
    assert until(lambda: len(fake_door.frames) == 1)
    assert {key: fake_door.frames[0][key] for key in ("kind", "epoch", "seq", "rate", "stream")} == {
        "kind": 1, "epoch": 7, "seq": 0, "rate": 48000, "stream": 0}
    fake_door.send({"type": "flush", "epoch": 8})
    assert until(lambda: flushes == [7, 8])
    fake_door.send(out_frame(7, b"\x01\x00" * 4))  # stale: played before the interruption
    fake_door.send(out_frame(8, b"\x02\x00" * 4))
    assert until(lambda: len(heard) == 1)
    assert heard == [(b"\x02\x00" * 4, 48000, 8)] and peer.stats["audio_out_stale"] == 1
    peer.send_audio(b"\x00\x02" * 960, stream=1)
    assert until(lambda: len(fake_door.frames) == 2)
    assert (fake_door.frames[1]["epoch"], fake_door.frames[1]["seq"], fake_door.frames[1]["stream"]) == (8, 1, 1)
    peer.send_context("This meeting audio is everything playing on Speakers.")
    peer.detach("stopped")
    assert until(lambda: fake_door.controls[-1:] == [{"type": "bye", "reason": "stopped"}])
    assert fake_door.controls[0] == {"type": "context", "text": "This meeting audio is everything playing on "
                                     "Speakers.", "source": "meeting_audio"}
    assert closed == [] or closed[0][0] != peer.attachment  # an intended bye is not "the call ended"


@pytest.mark.serial
def test_peer_reports_the_host_ending_the_call(fake_door):
    closed = []
    peer = meeting_peer.DoorPeer(fake_door.url, FakeDoor.token, "call-1", rate=16000, label="x",
                                 speaker_grant=False, on_closed=lambda attachment, reason: closed.append(
                                     (attachment, reason)))
    peer.open()
    peer.attach(speaker=False)
    fake_door.end_call("finished")
    assert until(lambda: closed)
    assert closed == [(peer.attachment, "finished")]
    assert peer.send_audio(b"\x00\x00") is False


@pytest.mark.serial
def test_peer_refusals_are_typed(fake_door):
    gone = meeting_peer.DoorPeer(fake_door.url, FakeDoor.token, "call-unknown", rate=16000, label="x",
                                 speaker_grant=False)
    with pytest.raises(meeting_peer.PeerRefused) as refused:
        gone.open()
    assert refused.value.code == "call_not_found"
    forged = meeting_peer.DoorPeer(fake_door.url, "not-the-token", "call-1", rate=16000, label="x",
                                   speaker_grant=False)
    with pytest.raises(meeting_peer.PeerRefused) as forbidden:
        forged.open()
    assert forbidden.value.code == "call_peer_forbidden"
    narrow = meeting_peer.DoorPeer(fake_door.url, FakeDoor.token, "call-1", rate=16000, label="x",
                                   speaker_grant=False)
    narrow.open()
    with pytest.raises(meeting_peer.PeerRefused) as widened:
        narrow.attach(speaker=True)
    assert widened.value.code == "audio_direction_forbidden"


# ---- the companion: one session from listen to the end of the call ---------------------------------

class FakeCapture:
    rate = 16000

    def __init__(self, selection, notes=()):
        self.selection, self.notes = selection, list(notes)
        self.on_pcm = self.on_error = None
        self.stopped = False

    def describe(self):
        return {"backend": "fake", "source": "test", "device": "", "includes_own_voice": bool(self.notes)}

    def disclosures(self):
        return self.notes

    def start(self, on_pcm, on_error):
        self.on_pcm, self.on_error = on_pcm, on_error

    def stop(self):
        self.stopped = True


class FakeVoice:
    def __init__(self):
        self.played, self.flushes, self.closed, self.started = [], [], False, False

    def start(self):
        self.started = True

    def play(self, pcm, rate, epoch):
        self.played.append((pcm, rate, epoch))

    def flush(self, epoch):
        self.flushes.append(epoch)

    def close(self):
        self.closed = True
        return []


class FakeBackend:
    platform = "test"

    def __init__(self, speaking=True, notes=()):
        self.speaking_supported = speaking
        self.notes = notes
        self.captures, self.voices = [], []

    def permission(self):
        return "granted", ""

    def request_permission(self):
        raise AssertionError("granted backends never prompt")

    def running_apps(self):
        return [RunningApp("zoom.us", "us.zoom.xos", 10), RunningApp("Google Chrome", "com.google.chrome", 11)]

    def open_capture(self, selection):
        self.captures.append(FakeCapture(selection, self.notes))
        return self.captures[-1]

    def cleanup_leftovers(self):
        return []

    def open_voice(self, rate):
        self.voices.append(FakeVoice())
        return self.voices[-1]


@pytest.mark.serial
def test_companion_listens_until_the_host_ends_the_call(tmp_path, fake_door):
    backend = FakeBackend(notes=["This meeting audio is everything playing on Speakers."])
    companion = meeting_capture.MeetingCompanion(tmp_path, fake_door.url, FakeDoor.token, backend)
    reply = companion.listen_start("call-1", "")
    assert reply["ok"] is True and reply["message"] == "listening to Zoom"
    assert reply["includes_own_voice"] is True and reply["rate"] == 16000
    assert fake_door.opened[0]["speaker"] is True  # the grant is widest; hello narrows it
    assert fake_door.hellos[0]["speaker"] is False and fake_door.hellos[0]["rate"] == 16000
    assert fake_door.hellos[0]["label"] == "Zoom (meeting audio)"
    capture = backend.captures[0]
    capture.on_pcm(b"\x01\x00" * 320, 0)
    assert until(lambda: len(fake_door.frames) == 1 and fake_door.controls)
    assert fake_door.controls[0]["text"].startswith("This meeting audio is everything playing")
    assert companion.listen_start("call-1", "")["message"] == "already listening to Zoom"
    fake_door.end_call("finished")
    assert until(lambda: capture.stopped)
    status = companion.status()
    assert status["state"] == "idle" and "the call ended (finished)" in status["message"]


@pytest.mark.serial
def test_companion_speaking_mode_reattaches_and_releases_the_voice(tmp_path, fake_door):
    backend = FakeBackend()
    companion = meeting_capture.MeetingCompanion(tmp_path, fake_door.url, FakeDoor.token, backend)
    assert companion.speak_start("call-1")["code"] == "MEETING_NOT_LISTENING"
    assert companion.listen_start("call-1", "chrome")["message"] == "listening to Google Chrome"
    assert until(lambda: fake_door.controls)
    assert "Ouroboros page is open in Google Chrome" in fake_door.controls[0]["text"]
    reply = companion.speak_start("call-1")
    assert reply["state"] == "speaking" and "'Ouroboros' microphone" in reply["message"]
    assert [hello["speaker"] for hello in fake_door.hellos] == [False, True]
    voice = backend.voices[0]
    assert voice.started and voice.flushes == [7]
    fake_door.send(out_frame(7, b"\x05\x00" * 8, rate=16000))
    assert until(lambda: voice.played)
    assert voice.played == [(b"\x05\x00" * 8, 16000, 7)]
    stopped = companion.speak_stop()
    assert stopped["state"] == "listening" and voice.closed
    assert [hello["speaker"] for hello in fake_door.hellos] == [False, True, False]
    assert companion.listen_stop()["message"] == "stopped listening to Google Chrome"
    assert backend.captures[0].stopped
    assert until(lambda: fake_door.controls[-1]["type"] == "bye")


@pytest.mark.serial
def test_companion_refuses_a_closed_call_and_releases_the_capture(tmp_path, fake_door):
    backend = FakeBackend()
    companion = meeting_capture.MeetingCompanion(tmp_path, fake_door.url, FakeDoor.token, backend)
    reply = companion.listen_start("call-gone", "zoom")
    assert reply == {"ok": False, "code": "MEETING_CALL_GONE",
                     "message": "The call is no longer open, so there is nothing to join."}
    assert backend.captures[0].stopped and companion.status()["state"] == "idle"
    assert companion.listen_start("", "zoom")["code"] == "MEETING_NO_CALL"
    refused = companion.listen_start("call-1", "webex")
    assert refused["code"] == "MEETING_APP_NOT_FOUND" and "Zoom" in refused["candidates"]


@pytest.mark.serial
def test_companion_without_speaking_support_refuses_it(tmp_path, fake_door):
    companion = meeting_capture.MeetingCompanion(tmp_path, fake_door.url, FakeDoor.token, FakeBackend(speaking=False))
    assert companion.listen_start("call-1", "zoom")["ok"] is True
    assert fake_door.opened[0]["speaker"] is False
    assert companion.speak_start("call-1")["code"] == "MEETING_SPEAK_UNSUPPORTED"
    companion.shutdown()
    assert until(lambda: any(c.get("type") == "context" and "stopped" in c.get("text", "")
                             for c in fake_door.controls))
