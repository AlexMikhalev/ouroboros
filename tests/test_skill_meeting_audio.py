"""The bundled meeting_audio skill: manifest, tools, control channel, backends and call peer.

No test touches a real audio device, permission prompt or meeting: every platform
backend runs against fakes of its subprocesses (pactl, pw-*), pyobjc modules or
PyAudioWPatch, and the call peer talks to a local fake of the Host Service door
that keeps the same rules as the real one (hello first, frames at the declared
rate and current epoch, flush, bye, close when the call ends).
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import importlib
import json
import pathlib
import queue
import shutil
import socket
import subprocess
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

import capture_common  # noqa: E402
import capture_linux  # noqa: E402
import capture_macos  # noqa: E402
import capture_windows  # noqa: E402
import meeting_apps  # noqa: E402
import meeting_capture  # noqa: E402
import meeting_control  # noqa: E402
import meeting_peer  # noqa: E402
import probe_capture  # noqa: E402
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


def test_pcm_conversions_scale_clip_and_average():
    from array import array

    floats = array("f", [0.0, 0.5, -0.5, 1.5, -1.0, 0.25]).tobytes()
    assert array("h", capture_common.float32_mono(floats)).tolist() == [0, 16384, -16384, 32767, -32767, 8192]
    planar = array("f", [1.0, 0.0, 0.0, 1.0]).tobytes()  # two channels, two frames each
    assert array("h", capture_common.float32_mono(planar, 2, planar=True)).tolist() == [16384, 16384]
    stereo = array("h", [100, 300, -50, -150]).tobytes()
    assert array("h", capture_common.int16_mono(stereo, 2)).tolist() == [200, -100]


# ---- Linux backend -------------------------------------------------------------------------

class FakePactl:
    """A stateful fake of pactl/pw-dump/pw-link for one desktop session."""

    def __init__(self, *, pipewire=False, graph=None, sink_inputs="", sinks="1\talsa_output.pci\tmodule\n",
                 modules=None):
        self.pipewire = pipewire
        self.graph = graph or []
        self.sink_inputs = sink_inputs
        self.sinks = sinks
        self.sources = ""
        self.modules = dict(modules or {})
        self.next_id = 536870913
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        assert kwargs["env"]["LC_ALL"] == "C"
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")  # noqa: E731
        if argv[0] == "pw-dump":
            return ok(json.dumps(self.graph)) if self.pipewire else subprocess.CompletedProcess(argv, 1, "", "no pw")
        if argv[0] == "pw-link":
            return ok()
        if argv[:2] == ["pactl", "info"]:
            return ok("Server Name: pulseaudio\n")
        if argv[:3] == ["pactl", "list", "sink-inputs"]:
            return ok(self.sink_inputs)
        if argv[:4] == ["pactl", "list", "sinks", "short"]:
            return ok(self.sinks)
        if argv[:4] == ["pactl", "list", "sources", "short"]:
            return ok(self.sources)
        if argv[:4] == ["pactl", "list", "modules", "short"]:
            return ok("".join(f"{mid}\t{name}\t{args}\t\n" for mid, (name, args) in self.modules.items()))
        if argv[:2] == ["pactl", "load-module"]:
            module_id, self.next_id = self.next_id, self.next_id + 1
            self.modules[module_id] = (argv[2], " ".join(argv[3:]))
            return ok(f"{module_id}\n")
        if argv[:2] == ["pactl", "unload-module"]:
            return ok() if self.modules.pop(int(argv[2]), None) else subprocess.CompletedProcess(argv, 1, "", "no")
        raise AssertionError(f"unexpected command {argv}")


class FakeProc:
    def __init__(self, argv, stdout_bytes=b"", pid=4242):
        self.argv = argv
        self.pid = pid
        self.stdout = __import__("io").BytesIO(stdout_bytes)
        self.stderr = __import__("io").BytesIO(b"")
        self.stdin = __import__("io").BytesIO()
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def linux_backend(tmp_path, fake, tools, procs, stdout=b""):
    def popen(argv, **kwargs):
        proc = FakeProc(argv, stdout)
        procs.append(proc)
        return proc

    return capture_linux.LinuxBackend(tmp_path, which=lambda name: f"/usr/bin/{name}" if name in tools else None,
                                      run=fake.run, popen=popen, env={"PATH": "/usr/bin"})


def test_linux_names_the_missing_tools(tmp_path):
    backend = linux_backend(tmp_path, FakePactl(), set(), [])
    with pytest.raises(capture_common.CaptureFailed) as failed:
        backend.running_apps()
    assert failed.value.code == "MEETING_AUDIO_TOOLS_MISSING"
    assert "pw-record" in failed.value.message and "parec" in failed.value.message


PW_GRAPH = [
    {"id": 50, "type": "PipeWire:Interface:Node", "info": {"props": {
        "media.class": "Stream/Output/Audio", "application.name": "ZOOM VoiceEngine",
        "application.process.binary": "zoom", "application.process.id": 900, "node.name": "ZOOM VoiceEngine"}}},
    {"id": 51, "type": "PipeWire:Interface:Port", "info": {"direction": "output", "props": {"node.id": 50}}},
    {"id": 52, "type": "PipeWire:Interface:Port", "info": {"direction": "output", "props": {"node.id": 50}}},
    {"id": 60, "type": "PipeWire:Interface:Node", "info": {"props": {
        "media.class": "Stream/Output/Audio", "application.name": "Firefox", "application.process.binary": "firefox"}}},
    {"id": 70, "type": "PipeWire:Interface:Node", "info": {"props": {
        "media.class": "Stream/Input/Audio", "node.name": capture_linux.NODE_NAME, "application.process.id": 4242}}},
    {"id": 71, "type": "PipeWire:Interface:Port", "info": {"direction": "input", "props": {"node.id": 70}}},
]


def test_linux_pipewire_links_only_the_meeting_apps_streams(tmp_path):
    fake, procs = FakePactl(pipewire=True, graph=PW_GRAPH), []
    pcm = b"\x01\x00" * 960 * 2
    backend = linux_backend(tmp_path, fake, {"pw-record", "pw-dump", "pw-link"}, procs, stdout=pcm)
    apps = backend.running_apps()
    assert [(app.name, app.ident, app.handle) for app in apps] == [
        ("ZOOM VoiceEngine", "zoom", "50"), ("Firefox", "firefox", "60")]
    selection = meeting_apps.choose(apps)
    assert selection.label == "Zoom"
    capture = backend.open_capture(selection)
    received = []
    capture.start(lambda chunk, stream=0: received.append((chunk, stream)), lambda reason: None)
    try:
        assert procs[0].argv[:12] == ["pw-record", "--raw", "--target", "0", "--rate", "48000",
                                      "--channels", "1", "--format", "s16", "--latency", "20ms"]
        assert json.loads(procs[0].argv[13])["node.name"] == capture_linux.NODE_NAME
        assert [call for call in fake.calls if call[0] == "pw-link"] == [["pw-link", "51", "71"],
                                                                         ["pw-link", "52", "71"]]
        assert until(lambda: len(received) == 2)
        assert received[0] == (pcm[:1920], 0)
        assert capture.describe()["source"] == "application streams"
        assert capture.disclosures() == []
    finally:
        capture.stop()


SINK_INPUTS = """Sink Input #42
\tDriver: protocol-native.c
\tSink: 1
\tSample Specification: s16le 2ch 48000Hz
\tProperties:
\t\tmedia.name = "Playback"
\t\tapplication.name = "ZOOM VoiceEngine"
\t\tapplication.process.id = "900"
\t\tapplication.process.binary = "zoom"
Sink Input #43
\tSink: 1
\tProperties:
\t\tapplication.name = "Ouroboros voice"
"""


def test_linux_pulse_records_the_sink_monitor_and_discloses_it(tmp_path):
    fake, procs = FakePactl(sink_inputs=SINK_INPUTS), []
    backend = linux_backend(tmp_path, fake, {"parec", "pactl"}, procs)
    apps = backend.running_apps()
    assert [(app.name, app.handle) for app in apps] == [("ZOOM VoiceEngine", "42")]
    capture = backend.open_capture(meeting_apps.choose(apps, "zoom"))
    capture.start(lambda chunk, stream=0: None, lambda reason: None)
    try:
        assert procs[0].argv == ["parec", "--raw", "--device=alsa_output.pci.monitor", "--format=s16le",
                                 "--rate=48000", "--channels=1", "--latency-msec=20",
                                 "--client-name=Ouroboros meeting capture"]
        assert capture.describe()["includes_own_voice"] is True
        [note] = capture.disclosures()
        assert "everything playing on alsa_output.pci" in note and "own whispered hints" in note
    finally:
        capture.stop()


def test_virtual_microphone_unloads_exactly_the_modules_it_loaded(tmp_path):
    fake, procs = FakePactl(modules={1: ("module-device-restore", "")}), []
    backend = linux_backend(tmp_path, fake, {"pactl", "pacat"}, procs)
    voice = backend.open_voice(48000)
    voice.start()
    record = json.loads((tmp_path / capture_linux.MODULES_FILE).read_text(encoding="utf-8"))
    loaded = [entry["id"] for entry in record["modules"]]
    assert [entry["name"] for entry in record["modules"]] == ["module-null-sink", "module-remap-source"]
    assert fake.modules[loaded[1]] == ("module-remap-source", "master=ouroboros_voice.monitor "
                                       "source_name=ouroboros_mic source_properties=device.description=Ouroboros")
    assert procs[0].argv[:4] == ["pacat", "--playback", "--raw", "--device=ouroboros_voice"]
    assert "--rate=48000" in procs[0].argv
    assert voice.close() == list(reversed(loaded))
    assert set(fake.modules) == {1}
    assert not (tmp_path / capture_linux.MODULES_FILE).exists()


def test_crash_leftover_modules_are_unloaded_only_when_still_ours(tmp_path):
    null_args = "sink_name=ouroboros_voice sink_properties=device.description=Ouroboros-voice"
    (tmp_path / capture_linux.MODULES_FILE).write_text(json.dumps({"modules": [
        {"id": 25, "name": "module-null-sink", "argument": null_args},
        {"id": 26, "name": "module-remap-source", "argument": "master=ouroboros_voice.monitor source_name=x"},
    ], "pid": 1}), encoding="utf-8")
    # The server restarted since: id 26 now belongs to somebody else's module.
    fake = FakePactl(modules={25: ("module-null-sink", null_args), 26: ("module-loopback", "latency_msec=1")})
    backend = linux_backend(tmp_path, fake, {"pactl"}, [])
    assert backend.cleanup_leftovers() == [25]
    assert set(fake.modules) == {26}
    assert not (tmp_path / capture_linux.MODULES_FILE).exists()
    assert [call for call in fake.calls if call[:2] == ["pactl", "unload-module"]] == [
        ["pactl", "unload-module", "25"]]


def test_virtual_microphone_leaves_a_same_named_device_alone(tmp_path):
    fake = FakePactl(sinks="5\touroboros_voice\tmodule-null-sink.c\n")
    backend = linux_backend(tmp_path, fake, {"pactl", "pacat"}, [])
    with pytest.raises(capture_linux.VoiceFailed) as failed:
        backend.open_voice(48000).start()
    assert failed.value.code == "MEETING_VOICE_NAME_TAKEN"
    assert not any(call[:2] in (["pactl", "load-module"], ["pactl", "unload-module"]) for call in fake.calls)


def test_voice_drops_audio_of_older_epochs_on_flush(tmp_path):
    voice = capture_linux.LinuxVoice(linux_backend(tmp_path, FakePactl(), {"pacat"}, []), 24000)
    voice.play(b"\x01\x00" * 4, 24000, 1)
    voice.play(b"\x02\x00" * 4, 24000, 1)
    voice.flush(2)
    assert list(voice._queue) == [] and voice.stats["dropped_bytes"] == 16
    voice.play(b"\x03\x00" * 4, 24000, 1)  # a frame of the old epoch still in flight
    voice.play(b"\x04\x00" * 4, 24000, 2)
    voice.play(b"\x05\x00" * 4, 16000, 2)  # wrong rate is never played
    assert [item[0] for item in voice._queue] == [2]


# ---- macOS backend (fake pyobjc) --------------------------------------------------------------

class FakeNSObject:
    def __init_subclass__(cls, protocols=None, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.protocols = protocols

    @classmethod
    def alloc(cls):
        return cls.__new__(cls)

    def init(self):
        return self


class FakeConfig(FakeNSObject):
    def __init__(self):
        self.calls = {}

    def init(self):
        self.calls = {}
        return self

    def __getattr__(self, name):
        if name.startswith("set") and name.endswith("_"):
            return lambda value: self.calls.__setitem__(name, value)
        raise AttributeError(name)


class FakeFilter(FakeNSObject):
    def initWithDisplay_includingApplications_exceptingWindows_(self, display, apps, windows):
        self.display, self.apps, self.windows = display, list(apps), list(windows)
        return self


class FakeStream(FakeNSObject):
    created = []

    def initWithFilter_configuration_delegate_(self, content_filter, config, delegate):
        self.filter, self.config, self.delegate, self.outputs, self.started = content_filter, config, delegate, [], False
        FakeStream.created.append(self)
        return self

    def addStreamOutput_type_sampleHandlerQueue_error_(self, output, kind, queue_, error):
        self.outputs.append((output, kind, queue_))
        return True, None

    def startCaptureWithCompletionHandler_(self, completion):
        self.started = True
        completion(None)

    def stopCaptureWithCompletionHandler_(self, completion):
        self.started = False
        completion(None)


class FakeSCApp:
    def __init__(self, bundle, pid):
        self._bundle, self._pid = bundle, pid

    def bundleIdentifier(self):
        return self._bundle

    def processID(self):
        return self._pid


class FakeContentSource:
    content = None
    error = None
    calls = 0

    @classmethod
    def getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(cls, a, b, completion):
        cls.calls += 1
        completion(cls.content, cls.error)


class FakeRunningApp:
    def __init__(self, name, bundle, pid, policy=0):
        self._v = (name, bundle, pid, policy)

    def localizedName(self):
        return self._v[0]

    def bundleIdentifier(self):
        return self._v[1]

    def processIdentifier(self):
        return self._v[2]

    def activationPolicy(self):
        return self._v[3]


class FakeAccess:
    def __init__(self, granted):
        self.granted_value, self.requests = granted, 0

    def granted(self):
        return self.granted_value

    def request(self):
        self.requests += 1
        return False


def fake_frameworks(running):
    FakeStream.created = []
    FakeContentSource.calls = 0
    FakeContentSource.content = SimpleNamespace(
        applications=lambda: [FakeSCApp("us.zoom.xos", 10), FakeSCApp("com.google.chrome", 11)],
        displays=lambda: ["display-1"])
    FakeContentSource.error = None
    capture_macos._HANDLER_CLASS.clear()
    workspace = SimpleNamespace(runningApplications=lambda: running)
    core_media = SimpleNamespace(
        CMTimeMake=lambda value, scale: (value, scale),
        CMSampleBufferGetDataBuffer=lambda buffer: buffer["block"],
        CMBlockBufferGetDataLength=lambda block: len(block),
        CMBlockBufferCopyDataBytes=lambda block, offset, length, out: (0, block[offset:offset + length]),
        CMSampleBufferGetFormatDescription=lambda buffer: "description",
        CMAudioFormatDescriptionGetStreamBasicDescription=lambda description: SimpleNamespace(
            mSampleRate=48000.0, mFormatFlags=capture_macos.K_AUDIO_FORMAT_FLAG_IS_FLOAT | (1 << 5),
            mChannelsPerFrame=1))
    return SimpleNamespace(
        objc=SimpleNamespace(protocolNamed=lambda name: f"protocol:{name}"),
        AppKit=SimpleNamespace(NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace)),
        Foundation=SimpleNamespace(NSObject=FakeNSObject),
        CoreMedia=core_media,
        SCK=SimpleNamespace(SCShareableContent=FakeContentSource, SCContentFilter=FakeFilter,
                            SCStreamConfiguration=FakeConfig, SCStream=FakeStream))


MAC_RUNNING = [FakeRunningApp("zoom.us", "us.zoom.xos", 10), FakeRunningApp("Google Chrome", "com.google.chrome", 11),
               FakeRunningApp("Dock helper", "com.apple.dock.extra", 12, policy=2)]


def test_macos_lists_regular_apps_and_requires_macos_13(tmp_path):
    backend = capture_macos.MacBackend(tmp_path, frameworks=fake_frameworks(MAC_RUNNING),
                                       access=FakeAccess(True), macos_version="14.5")
    assert [app.ident for app in backend.running_apps()] == ["us.zoom.xos", "com.google.chrome"]
    old = capture_macos.MacBackend(tmp_path, frameworks=fake_frameworks(MAC_RUNNING), access=FakeAccess(True),
                                   macos_version="12.7")
    with pytest.raises(capture_common.CaptureFailed) as failed:
        old.permission()
    assert failed.value.code == "MEETING_PLATFORM_UNSUPPORTED"


def test_macos_missing_consent_shows_the_prompt_and_lists_nothing(tmp_path):
    access = FakeAccess(False)
    backend = capture_macos.MacBackend(tmp_path, frameworks=fake_frameworks(MAC_RUNNING), access=access,
                                       macos_version="15.1")
    companion = meeting_capture.MeetingCompanion(tmp_path, "http://127.0.0.1:1", "token", backend)
    reply = companion.listen_start("call-1", "")
    assert reply["code"] == "MEETING_PERMISSION_PENDING" and "Screen & System Audio Recording" in reply["message"]
    assert access.requests == 1 and FakeContentSource.calls == 0


def test_macos_capture_includes_only_the_meeting_app_and_converts_audio(tmp_path):
    from array import array

    fw = fake_frameworks(MAC_RUNNING)
    backend = capture_macos.MacBackend(tmp_path, frameworks=fw, access=FakeAccess(True), macos_version="14.5")
    capture = backend.open_capture(meeting_apps.choose(backend.running_apps()))
    received, errors = [], []
    capture.start(lambda pcm, stream=0: received.append((pcm, stream)), errors.append)
    [stream] = FakeStream.created
    assert [app.bundleIdentifier() for app in stream.filter.apps] == ["us.zoom.xos"]
    assert stream.filter.display == "display-1" and stream.filter.windows == []
    assert stream.config.calls["setCapturesAudio_"] is True
    assert stream.config.calls["setExcludesCurrentProcessAudio_"] is True
    assert (stream.config.calls["setSampleRate_"], stream.config.calls["setChannelCount_"]) == (48000, 1)
    assert [kind for _, kind, _ in stream.outputs] == [capture_macos.SC_STREAM_OUTPUT_AUDIO,
                                                       capture_macos.SC_STREAM_OUTPUT_SCREEN]
    assert stream.delegate.protocols == ["protocol:SCStreamOutput", "protocol:SCStreamDelegate"]
    samples = array("f", [0.0, 0.5, -1.0]).tobytes()
    stream.delegate.stream_didOutputSampleBuffer_ofType_(stream, {"block": samples},
                                                         capture_macos.SC_STREAM_OUTPUT_AUDIO)
    stream.delegate.stream_didOutputSampleBuffer_ofType_(stream, {"block": samples},
                                                         capture_macos.SC_STREAM_OUTPUT_SCREEN)
    assert [array("h", pcm).tolist() for pcm, _ in received] == [[0, 16384, -32767]]
    assert errors == []
    capture.stop()
    assert stream.started is False and stream.delegate.sink is None


def test_macos_reports_an_app_screencapturekit_does_not_offer(tmp_path):
    fw = fake_frameworks(MAC_RUNNING)
    backend = capture_macos.MacBackend(tmp_path, frameworks=fw, access=FakeAccess(True), macos_version="14.5")
    capture = backend.open_capture(meeting_apps.Selection((RunningApp("Teams", "com.microsoft.teams2", 99),),
                                                         "Microsoft Teams", False))
    with pytest.raises(capture_common.CaptureFailed) as failed:
        capture.start(lambda *a: None, lambda reason: None)
    assert failed.value.code == "MEETING_APP_NOT_CAPTURABLE" and FakeStream.created == []


def test_capture_attribution_probe_classifies_without_prompting(tmp_path, monkeypatch):
    paths = {300: "/Applications/Ouroboros.app/Contents/Resources/python-standalone/bin/python3",
             200: "/Applications/Ouroboros.app/Contents/Resources/python-standalone/bin/python3",
             100: "/Applications/Ouroboros.app/Contents/MacOS/Ouroboros", 1: "/sbin/launchd"}
    parents = {300: 200, 200: 100, 100: 1}
    record = probe_capture.attribution(300, path_of=paths.get, parent_of=lambda pid: parents.get(pid, 0),
                                       responsible_of=lambda pid: 100, granted=lambda: False)
    assert [row["pid"] for row in record["chain"]] == [300, 200, 100, 1]
    assert record["responsible"] == {"pid": 100, "path": paths[100], "app_bundle": "/Applications/Ouroboros.app",
                                     "kind": "app_bundle"}
    assert record["preflight_granted"] is False
    assert probe_capture.classify("/usr/local/bin/python3.12") == "python"
    monkeypatch.setattr(probe_capture, "attribution", lambda: dict(record))
    monkeypatch.setattr(probe_capture.sys, "platform", "darwin")
    assert probe_capture.main(["--state-dir", str(tmp_path)]) == 0
    saved = json.loads((tmp_path / probe_capture.RECORD_FILE).read_text(encoding="utf-8"))
    assert saved["real_capture"] == "NOT_RUN"


# ---- Windows backend (fake PyAudioWPatch and process loopback) -------------------------------------

def test_process_loopback_structures_match_the_windows_abi():
    params, variant = capture_windows.activation_params(1234)
    assert ctypes.sizeof(capture_windows.AUDIOCLIENT_ACTIVATION_PARAMS) == 12
    assert ctypes.sizeof(capture_windows.WAVEFORMATEX) == 18
    assert ctypes.sizeof(capture_windows.PROPVARIANT) == (24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)
    assert (variant.vt, variant.blob.cbSize) == (capture_windows.VT_BLOB, 12)
    assert params.ProcessLoopbackParams.TargetProcessId == 1234
    assert params.ProcessLoopbackParams.ProcessLoopbackMode == 0  # include the target tree only
    assert bytes(capture_windows.IID_IAUDIO_CLIENT).hex() == "4cadb91cfadb324cb178c2f568a703b2"
    fmt = capture_windows.capture_format()
    assert (fmt.nSamplesPerSec, fmt.nChannels, fmt.wBitsPerSample, fmt.nBlockAlign) == (48000, 2, 16, 4)


def test_windows_lists_one_root_per_program_tree():
    rows = [(1000, 1, "chrome.exe"), (1001, 1000, "chrome.exe"), (1002, 1000, "chrome.exe"),
            (2000, 50, "Zoom.exe"), (4, 0, "System"), (3000, 2000, "zWebview2Agent.exe")]
    roots = capture_windows.root_processes(rows)
    assert [(app.ident, app.pid) for app in roots] == [("chrome.exe", 1000), ("zoom.exe", 2000),
                                                       ("zwebview2agent.exe", 3000)]
    assert meeting_apps.choose(roots).label == "Zoom"


class FakeLoopStream:
    def __init__(self, pid, stream, failure=""):
        self.pid, self.stream, self._failure, self.failure = pid, stream, failure, ""
        self.started = self.stopped = False

    def prepare(self):
        self.failure = self._failure

    def start(self, on_pcm, on_error):
        self.started = True

    def stop(self):
        self.stopped = True


class FakePyAudio:
    paInt16, paContinue = 8, 0
    instances = []

    class PyAudio:
        def __init__(self):
            self.opened, self.terminated = [], False
            FakePyAudio.instances.append(self)

        def get_default_wasapi_loopback(self):
            return {"index": 7, "name": "Speakers [Loopback]", "defaultSampleRate": 44100.0, "maxInputChannels": 2}

        def open(self, **kwargs):
            self.opened.append(kwargs)
            return SimpleNamespace(start_stream=lambda: None, stop_stream=lambda: None, close=lambda: None)

        def terminate(self):
            self.terminated = True


def windows_backend(tmp_path, *, build=22631, failure=""):
    FakePyAudio.instances = []
    return capture_windows.WindowsBackend(
        tmp_path, processes=lambda: [(2000, 50, "Zoom.exe"), (2001, 2000, "Zoom.exe")],
        pyaudio_loader=lambda: FakePyAudio, build=build,
        stream_factory=lambda pid, stream: FakeLoopStream(pid, stream, failure))


def test_windows_prefers_process_loopback_of_the_meeting_tree(tmp_path):
    backend = windows_backend(tmp_path)
    capture = backend.open_capture(meeting_apps.choose(backend.running_apps()))
    assert isinstance(capture, capture_windows.ProcessLoopbackCapture)
    assert [(stream.pid, stream.stream) for stream in capture.streams] == [(2000, 0)]
    assert capture.describe()["includes_own_voice"] is False and capture.disclosures() == []
    assert not (tmp_path / capture_windows.ATTEMPT_FILE).exists()
    capture.start(lambda *a: None, lambda reason: None)
    capture.stop()
    assert capture.streams[0].started and capture.streams[0].stopped
    assert FakePyAudio.instances == []


def test_windows_falls_back_to_the_device_loopback_and_says_it_hears_the_whisper(tmp_path):
    from array import array

    backend = windows_backend(tmp_path, failure="activation refused (HRESULT 0x80070057)")
    capture = backend.open_capture(meeting_apps.choose(backend.running_apps()))
    assert isinstance(capture, capture_windows.EndpointLoopbackCapture)
    facts = capture.describe()
    assert facts["includes_own_voice"] is True and "0x80070057" in facts["fallback_reason"]
    assert capture.rate == 44100
    [note] = capture.disclosures()
    assert "Speakers [Loopback]" in note and "own whispered hints" in note
    received = []
    capture.start(lambda pcm, stream=0: received.append(pcm), lambda reason: None)
    [opened] = FakePyAudio.instances[0].opened
    assert (opened["format"], opened["channels"], opened["rate"], opened["input_device_index"],
            opened["start"]) == (8, 2, 44100, 7, False)
    assert opened["stream_callback"](array("h", [10, 30, -10, -30]).tobytes(), 2, None, 0) == (None, 0)
    assert array("h", received[0]).tolist() == [20, -20]
    capture.stop()
    assert FakePyAudio.instances[0].terminated


def test_windows_crash_during_activation_turns_process_loopback_off(tmp_path):
    (tmp_path / capture_windows.ATTEMPT_FILE).write_text("attempt")
    backend = windows_backend(tmp_path)
    usable, reason = backend.process_loopback_state()
    assert usable is False and "ended the companion" in reason
    assert isinstance(backend.open_capture(meeting_apps.choose(backend.running_apps())),
                      capture_windows.EndpointLoopbackCapture)
    old = windows_backend(tmp_path / "old", build=19045)
    assert old.process_loopback_state()[0] is False


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
