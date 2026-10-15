"""skills/zoom_meeting: the Zoom Meeting SDK companion, its tools and its call peer.

A fake ``zoom_meeting_sdk`` (injected through ``sys.modules``) mirrors the 0.0.27 binding
surface Hope's code uses: callback holders accept only the binding's keyword names and
the join parameters only its fields, so a misspelt SDK call fails here instead of on a
server. The Host door is a real Starlette app on a loopback port, the peer socket a real
WebSocket, and frames are checked against the core's own codec.
"""

from __future__ import annotations

import asyncio
import collections
import ctypes
import enum
import heapq
import importlib
import importlib.util
import itertools
import json
import logging
import os
import shutil
import socket
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from ouroboros import call_protocol as cp
from ouroboros.contracts.skill_manifest import VALID_SKILL_PERMISSIONS, parse_skill_manifest_text
from ouroboros.presence_bindings import PresenceBinding, PresenceEndpoint, save_presence_binding

SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "zoom_meeting"
SCRIPTS = SKILL_DIR / "scripts"
_SCRIPT_MODULES = ("companion_link", "glib_loop", "zoom_session", "meeting_peer", "zoom_bot", "probe")
CLIENT_ID = "fakeClientIdAbc"
CLIENT_SECRET = "fake-client-secret-0123456789abcdef"
PASSCODE = "pass-word-271828"
OBF = "obf-token-3141592653"
HOST_TOKEN = "host-token-5772156649"
MEETING = "85012345678"
BINDING = "b" * 32
MY_ID = 16778240


_LIVE_BOTS: list = []


@pytest.fixture(autouse=True)
def _close_bots():
    """Every companion a test built ends its call threads and cleans its SDK up."""
    yield
    while _LIVE_BOTS:
        _LIVE_BOTS.pop().close(timeout=3)


@pytest.fixture
def zoom(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    for name in _SCRIPT_MODULES:
        sys.modules.pop(name, None)
    yield types.SimpleNamespace(**{name: importlib.import_module(name) for name in _SCRIPT_MODULES})
    for name in _SCRIPT_MODULES:
        sys.modules.pop(name, None)


@pytest.fixture
def plugin():
    name = "zoom_meeting_skill_under_test"
    spec = importlib.util.spec_from_file_location(name, SKILL_DIR / "plugin.py",
                                                  submodule_search_locations=[str(SKILL_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    for key in [key for key in sys.modules if key == name or key.startswith(name + ".")]:
        sys.modules.pop(key, None)


# ---- the fake binding ---------------------------------------------------------------

_CALLBACK_KEYWORDS = {
    "AuthServiceEventCallbacks": ("onAuthenticationReturnCallback", "onLoginReturnWithReasonCallback",
                                  "onLogoutCallback", "onZoomIdentityExpiredCallback",
                                  "onZoomAuthIdentityExpiredCallback"),
    "MeetingServiceEventCallbacks": ("onMeetingStatusChangedCallback", "onMeetingStatisticsWarningNotificationCallback",
                                     "onMeetingParameterNotificationCallback", "onSuspendParticipantsActivitiesCallback",
                                     "onAICompanionActiveChangeNoticeCallback", "onMeetingTopicChangedCallback",
                                     "onMeetingFullToWatchLiveStreamCallback", "onUserNetworkStatusChangedCallback"),
    "ZoomSDKVirtualAudioMicEventCallbacks": ("onMicInitializeCallback", "onMicStartSendCallback",
                                             "onMicStopSendCallback", "onMicUninitializedCallback"),
    "MeetingRecordingCtrlEventCallbacks": ("onRecordingStatusCallback", "onCloudRecordingStatusCallback",
                                           "onRecordPrivilegeChangedCallback",
                                           "onLocalRecordingPrivilegeRequestStatusCallback",
                                           "onRequestCloudRecordingResponseCallback",
                                           "onLocalRecordingPrivilegeRequestedCallback",
                                           "onStartCloudRecordingRequestedCallback",
                                           "onCloudRecordingStorageFullCallback",
                                           "onEnableAndStartSmartRecordingRequestedCallback",
                                           "onSmartRecordingEnableActionCallbackFunc",
                                           "onTranscodingStatusChangedCallback"),
    "ZoomSDKAudioRawDataDelegateCallbacks": ("onMixedAudioRawDataReceivedCallback",
                                             "onOneWayAudioRawDataReceivedCallback",
                                             "onShareAudioRawDataReceivedCallback",
                                             "onOneWayInterpreterAudioRawDataReceivedCallback",
                                             "collectPerformanceData"),
    "MeetingChatEventCallbacks": ("onChatMsgNotificationCallback", "onChatStatusChangedNotificationCallback",
                                  "onChatMsgDeleteNotificationCallback", "onChatMessageEditNotificationCallback",
                                  "onShareMeetingChatStatusChangedCallback", "onFileSendStartCallback",
                                  "onFileReceivedCallback", "onFileTransferProgressCallback"),
    "MeetingAudioCtrlEventCallbacks": ("onUserAudioStatusChangeCallback", "onUserActiveAudioChangeCallback",
                                       "onHostRequestStartAudioCallback", "onJoin3rdPartyTelephonyAudioCallback",
                                       "onMuteOnEntryStatusChangeCallback"),
}
_JOIN_FIELDS = ("meetingNumber", "userName", "psw", "vanityID", "customer_key", "join_token", "webinarToken",
                "isVideoOff", "isAudioOff", "userZAK", "isMyVoiceInMix", "app_privilege_token", "onBehalfToken",
                "isAudioRawDataStereo", "eAudioRawdataSamplingRate")


def _libc():
    """C stdio without varargs (ctypes variadic calls are unsafe on some ABIs)."""
    libc = ctypes.CDLL(None)
    libc.puts.argtypes, libc.fflush.argtypes = [ctypes.c_char_p], [ctypes.c_void_p]
    return libc


class FakeZoom:
    """A ``zoom_meeting_sdk`` stand-in plus the levers a test pulls to play Zoom's part."""

    def __init__(self) -> None:
        m = self.m = types.ModuleType("zoom_meeting_sdk")
        fake = self
        self.calls: list = []
        self.codes = collections.defaultdict(lambda: "SDKERR_SUCCESS")
        self.topic, self.muted, self.print_on_join = "Weekly sync", True, False
        self.users = {MY_ID: "Aika", 42: "Anna", 7: "Boris"}
        self.sent: list = []
        self.cb: dict = {}
        self.on_sdkauth = None
        self.join_param: dict = {}
        self.init_param: dict = {}
        self.jwt = ""

        def _enum(name, members, export=True):
            cls = enum.IntEnum(name, [(member, index) for index, member in enumerate(members)])
            setattr(m, name, cls)
            if export:
                for member in cls:
                    setattr(m, member.name, member)

        _enum("SDKError", ("SDKERR_SUCCESS", "SDKERR_NO_IMPL", "SDKERR_WRONG_USAGE", "SDKERR_INVALID_PARAMETER",
                           "SDKERR_UNINITIALIZE", "SDKERR_UNAUTHENTICATION", "SDKERR_NO_PERMISSION"))
        _enum("AuthResult", ("AUTHRET_SUCCESS", "AUTHRET_KEYORSECRETWRONG", "AUTHRET_JWTTOKENWRONG"))
        _enum("MeetingStatus", ("MEETING_STATUS_IDLE", "MEETING_STATUS_CONNECTING", "MEETING_STATUS_WAITINGFORHOST",
                                "MEETING_STATUS_INMEETING", "MEETING_STATUS_DISCONNECTING",
                                "MEETING_STATUS_RECONNECTING", "MEETING_STATUS_FAILED", "MEETING_STATUS_ENDED",
                                "MEETING_STATUS_IN_WAITING_ROOM"))
        _enum("MeetingFailCode", ("MEETING_SUCCESS", "MEETING_FAIL_PASSWORD_ERR",
                                  "MEETING_FAIL_AUTHORIZED_USER_NOT_INMEETING"))
        _enum("MeetingEndReason", ("EndMeetingReason_None", "EndMeetingReason_KickByHost",
                                   "EndMeetingReason_EndByHost"), export=False)
        _enum("LeaveMeetingCmd", ("LEAVE_MEETING", "END_MEETING"))
        _enum("SDKUserType", ("SDK_UT_NORMALUSER", "SDK_UT_WITHOUT_LOGIN"))
        _enum("AudioRawdataSamplingRate", ("AudioRawdataSamplingRate_32K", "AudioRawdataSamplingRate_48K"))
        _enum("ZoomSDKAudioChannel", ("ZoomSDKAudioChannel_Mono", "ZoomSDKAudioChannel_Stereo"))
        _enum("RequestLocalRecordingStatus", ("RequestLocalRecording_Granted", "RequestLocalRecording_Denied",
                                              "RequestLocalRecording_Timeout"), export=False)
        _enum("AudioStatus", ("Audio_None", "Audio_Muted", "Audio_UnMuted", "Audio_Muted_ByHost",
                              "Audio_UnMuted_ByHost"), export=False)

        def code(verb):
            return m.SDKError[fake.codes[verb]]

        for cls_name, keywords in _CALLBACK_KEYWORDS.items():
            def __init__(self, _keywords=keywords, **kwargs):
                unknown = set(kwargs) - set(_keywords)
                if unknown:
                    raise TypeError(f"unexpected keyword(s) {sorted(unknown)}")
                self.kw = kwargs
            setattr(m, cls_name, type(cls_name, (), {"__init__": __init__}))

        def slotted(name, fields):
            def __init__(self):
                for field in fields:
                    setattr(self, field, None)
            return type(name, (), {"__slots__": fields, "__init__": __init__})

        m.InitParam = slotted("InitParam", ("strWebDomain", "strBrandingName", "strSupportUrl", "emLanguageID",
                                            "enableGenerateDump", "enableLogByDefault", "uiLogFileSize"))
        m.AuthContext = slotted("AuthContext", ("jwt_token",))
        params = slotted("JoinParam4WithoutLogin", _JOIN_FIELDS)

        class JoinParam:
            __slots__ = ("userType", "_param")

            def __init__(self):
                self.userType, self._param = None, params()

            @property
            def param(self):
                return self._param

        m.JoinParam = JoinParam

        def record(verb, result=None):
            fake.calls.append(verb)
            return result

        def InitSDK(param):
            fake.init_param = {f: getattr(param, f) for f in ("strWebDomain", "enableGenerateDump")}
            return record("InitSDK", code("InitSDK"))

        def CleanUPSDK():
            record("CleanUPSDK")

        class AuthService:
            def SetEvent(self, cb):
                fake.cb["auth"] = cb
                return record("AuthService.SetEvent", code("SetEvent"))

            def SDKAuth(self, context):
                fake.jwt = context.jwt_token
                result = record("SDKAuth", code("SDKAuth"))
                if fake.on_sdkauth:
                    fake.on_sdkauth()
                return result

        class User:
            def __init__(self, uid):
                self.uid = uid

            def GetUserID(self):
                return self.uid

            def GetUserName(self):
                return fake.users[self.uid]

            def IsAudioMuted(self):
                return fake.muted

        class Controllers:
            def JoinVoip(self):
                return record("JoinVoip", code("JoinVoip"))

            def UnMuteAudio(self, uid):
                return record("UnMuteAudio", code("UnMuteAudio"))

            def GetMySelfUser(self):
                return User(MY_ID)

            def GetUserByUserID(self, uid):
                return User(uid) if uid in fake.users else None

            def CanStartRawRecording(self):
                return record("CanStartRawRecording", code("CanStartRawRecording"))

            def RequestLocalRecordingPrivilege(self):
                return record("RequestLocalRecordingPrivilege", code("RequestLocalRecordingPrivilege"))

            def StartRawRecording(self):
                return record("StartRawRecording", code("StartRawRecording"))

        def controller(kind):
            ctrl = Controllers()

            def SetEvent(cb):
                fake.cb[kind] = cb
                return record(f"{kind}.SetEvent", code("SetEvent"))
            ctrl.SetEvent = SetEvent
            return ctrl

        audio_ctrl, recording_ctrl, chat_ctrl = controller("audio"), controller("recording"), controller("chat")

        class MeetingService:
            def SetEvent(self, cb):
                fake.cb["meeting"] = cb
                return record("MeetingService.SetEvent", code("SetEvent"))

            def Join(self, join):
                fake.join_param = {"userType": join.userType, **{f: getattr(join.param, f) for f in _JOIN_FIELDS}}
                if fake.print_on_join:  # like printJoinParam: C stdio, buffered when stdout is not a tty
                    _libc().puts(b"Password: " + join.param.psw.encode())
                return record("Join", code("Join"))

            def Leave(self, cmd):
                fake.leave_cmd = cmd
                return record("Leave", code("Leave"))

            def GetMeetingInfo(self):
                record("GetMeetingInfo")
                return types.SimpleNamespace(GetMeetingTopic=lambda: fake.topic)

            def GetMeetingAudioController(self):
                return audio_ctrl

            def GetMeetingParticipantsController(self):
                return audio_ctrl

            def GetMeetingRecordingController(self):
                return recording_ctrl

            def GetMeetingChatController(self):
                return chat_ctrl

        class Helper:
            def setExternalAudioSource(self, cb):
                fake.cb["mic"] = cb
                return record("setExternalAudioSource", code("setExternalAudioSource"))

            def subscribe(self, delegate, with_interpreters):
                fake.cb["delegate"] = delegate
                return record("subscribe", code("subscribe"))

            def unSubscribe(self):
                return record("unSubscribe", code("unSubscribe"))

        helper = Helper()
        settings = types.SimpleNamespace(GetAudioSettings=lambda: types.SimpleNamespace(
            EnableAutoJoinAudio=lambda flag: record("EnableAutoJoinAudio", flag)))
        m.InitSDK, m.CleanUPSDK = InitSDK, CleanUPSDK
        m.CreateAuthService = lambda: record("CreateAuthService", AuthService())
        m.CreateMeetingService = lambda: record("CreateMeetingService", MeetingService())
        m.CreateSettingService = lambda: record("CreateSettingService", settings)
        m.GetAudioRawdataHelper = lambda: record("GetAudioRawdataHelper", helper)

        class Sender:
            def send(self, data, rate, channel):
                fake.sent.append((bytes(data), rate, channel))
                return m.SDKERR_SUCCESS

        self.sender = Sender()

    # ---- Zoom's side of the conversation --------------------------------------------

    def fire(self, kind, keyword, *args):
        self.cb[kind].kw[keyword](*args)

    def auth(self, result="AUTHRET_SUCCESS"):
        self.fire("auth", "onAuthenticationReturnCallback", self.m.AuthResult[result])

    def status(self, name, result=0):
        self.fire("meeting", "onMeetingStatusChangedCallback", getattr(self.m, name), result)

    def end(self, reason):
        self.status("MEETING_STATUS_ENDED", self.m.MeetingEndReason[reason])

    def mic(self):
        self.fire("mic", "onMicInitializeCallback", self.sender)
        self.fire("mic", "onMicStartSendCallback")

    def answer(self, name):
        self.fire("recording", "onLocalRecordingPrivilegeRequestStatusCallback",
                  self.m.RequestLocalRecordingStatus[f"RequestLocalRecording_{name}"])

    def mixed(self, pcm, rate=32000):
        data = types.SimpleNamespace(GetBuffer=lambda: pcm, GetSampleRate=lambda: rate, GetChannelNum=lambda: 1)
        self.fire("delegate", "onMixedAudioRawDataReceivedCallback", data)

    def one_way(self, node_id):
        self.fire("delegate", "onOneWayAudioRawDataReceivedCallback", None, node_id)

    def chat(self, sender_id, text, to_all=True):
        info = types.SimpleNamespace(GetSenderUserId=lambda: sender_id, GetSenderDisplayName=lambda: self.users[sender_id],
                                     GetContent=lambda: text, IsChatToAll=lambda: to_all)
        self.fire("chat", "onChatMsgNotificationCallback", info, text)

    def host_mute(self):
        status = types.SimpleNamespace(GetUserId=lambda: MY_ID, GetStatus=lambda: self.m.AudioStatus.Audio_Muted_ByHost)
        self.fire("audio", "onUserAudioStatusChangeCallback", [status], "")


class ManualLoop:
    """The GLib loop's interface on virtual time: callbacks run only when pumped."""

    backend, library = "manual", "test"

    def __init__(self) -> None:
        self.now = 0.0
        self.quit_called = False
        self._lock = threading.Lock()
        self._ready: collections.deque = collections.deque()
        self._timers: list = []
        self._seq = itertools.count()

    def idle_add(self, fn):
        with self._lock:
            self._ready.append(fn)
        return 1

    def timeout_add(self, ms, fn):
        with self._lock:
            heapq.heappush(self._timers, (self.now + ms / 1000.0, next(self._seq), ms, fn))
        return 1

    def quit(self):
        self.quit_called = True

    def run_ready(self):
        ran = 0
        while True:
            with self._lock:
                if not self._ready:
                    return ran
                fn = self._ready.popleft()
            fn()
            ran += 1

    def advance(self, seconds):
        target = self.now + seconds
        while True:
            self.run_ready()
            with self._lock:
                if not self._timers or self._timers[0][0] > target:
                    break
                due, _seq, ms, fn = heapq.heappop(self._timers)
                self.now = max(self.now, due)
            if fn():
                self.timeout_add(ms, fn)
        self.now = target
        self.run_ready()

    def pump(self, until, timeout=10.0):
        deadline = time.monotonic() + timeout
        while not until():
            assert time.monotonic() < deadline, "condition not reached"
            self.advance(0.05)
            time.sleep(0.005)

    def run(self):
        while not self.quit_called:
            if self.run_ready():
                continue
            assert self._timers, "loop idle with nothing scheduled"
            self.advance(max(0.0, self._timers[0][0] - self.now))


class FakePeer:
    sent_frames = received_frames = dropped_uplink = 0

    def __init__(self, hello, callbacks):
        self.hello, self.callbacks = hello, callbacks
        self.audio, self.controls, self.closed = [], [], None

    def send_audio(self, pcm, rate):
        self.audio.append((pcm, rate))

    def send_control(self, message):
        self.controls.append(message)

    def close(self, reason):
        self.closed = reason


class FakePacer:
    def __init__(self, send, rate):
        self.rate, self.fed, self.flushes, self.sent_frames, self.flushed_bytes = rate, [], [], 0, 0

    def feed(self, pcm, epoch):
        self.fed.append((pcm, epoch))

    def flush(self, epoch):
        self.flushes.append(epoch)

    def run(self, stop):
        stop.wait(5)

    def close(self):
        pass


def make_bot(zoom, tmp_path, fake, *, door=None, refuse=None, **kwargs):
    loop = ManualLoop()
    peers, bodies = [], []

    def open_call(url, token, body):
        bodies.append(body)
        if refuse:
            raise zoom.meeting_peer.CallOpenRefused(*refuse)
        return {"call_id": "call-7", "peer_path": "/call/call-7/peer"}

    def attach(url, token, path, hello, **callbacks):
        peers.append(FakePeer(hello, callbacks))
        return peers[-1]

    options = dict(open_call=open_call, attach_peer=attach, identity_name=lambda url, token: "Aika",
                   pacer_factory=FakePacer)
    options.update(kwargs)
    bot = zoom.zoom_bot.ZoomBot(state_dir=tmp_path, loop=loop, sdk=fake.m, client_id=CLIENT_ID,
                                client_secret=CLIENT_SECRET, host_url=door or "http://127.0.0.1:9",
                                host_token=HOST_TOKEN, clock=lambda: loop.now, **options)
    _LIVE_BOTS.append(bot)
    return bot, loop, peers, bodies


def join_args(zoom, **overrides):
    args = {"meeting_number": MEETING, "passcode": PASSCODE, "display_name": "", "binding_id": BINDING,
            "account_id": zoom.companion_link.app_account_id(CLIENT_ID), "on_behalf_token": OBF,
            "requested_by": {"id": "task:t-1", "display_name": "", "requested_via": "web", "task_id": "t-1"}}
    args.update(overrides)
    return args


def drive_to_audio(fake, loop, *, raw_allowed=True):
    loop.run_ready()
    fake.auth()
    loop.run_ready()
    fake.status("MEETING_STATUS_INMEETING")
    loop.advance(2.0)
    fake.mic()
    loop.advance(3.0)
    if raw_allowed:
        loop.advance(1.5)


def open_call_via_audio(fake, loop, bot):
    fake.mixed(b"\x01\x00" * 320)
    loop.pump(lambda: bot.state == "in_call")


# ---- manifest -------------------------------------------------------------------------

def test_manifest_parses_and_preflight_is_clean(tmp_path, monkeypatch):
    from ouroboros.skill_dependencies import normalize_declared_dependency_specs
    from ouroboros.tools.registry import ToolContext
    from ouroboros.tools.skill_preflight import _handle_skill_preflight

    manifest = parse_skill_manifest_text((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))
    assert (manifest.name, manifest.type, manifest.runtime, manifest.entry, manifest.os) == (
        "zoom_meeting", "extension", "python3", "plugin.py", "linux")
    assert set(manifest.permissions) == {"tool", "presence", "call_peer", "companion_process", "net", "read_settings"}
    assert manifest.env_from_settings == ["ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET"]
    assert "presence" not in manifest.raw_extra  # a transport: the behavior is the install's own profile
    assert manifest.companion_processes == [{"name": "zoom_bot", "command": ["python3", "scripts/zoom_bot.py"],
                                             "runtime": "python3", "restart_policy": "on_failure",
                                             "max_restarts": 5}]
    assert set(manifest.model_experience) == {"what_model_sees", "token_effect"}
    expected = [] if "call_peer" in VALID_SKILL_PERMISSIONS else [
        w for w in manifest.validate() if "'call_peer'" in w]  # until the audio lane's permission lands
    assert manifest.validate() == expected
    auto, manual, warnings = normalize_declared_dependency_specs(manifest.raw_extra["install_specs"])
    assert (manual, warnings) == ([], [])
    assert [spec["package"] for spec in auto] == ["PyJWT==2.13.0", "zoom-meeting-sdk==0.0.27"]
    assert all(spec["platforms"] == ["linux-x86_64"] for spec in auto)
    assert auto[-1]["check"] == {"argv": ["python3", "-c", "import jwt, zoom_meeting_sdk"]}

    skills_root = tmp_path / "skills"
    shutil.copytree(SKILL_DIR, skills_root / "zoom_meeting", ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    (tmp_path / "repo").mkdir()
    (tmp_path / "drive").mkdir()
    ctx = ToolContext(repo_dir=tmp_path / "repo", drive_root=tmp_path / "drive")
    payload = json.loads(_handle_skill_preflight(ctx, skill="zoom_meeting"))
    assert payload["ok"] is True, payload
    assert payload["files_failed"] == 0 and payload["files_checked"] >= 7


# ---- meeting references and bindings ----------------------------------------------------

@pytest.mark.parametrize("meeting, expected", [
    ("https://us02web.zoom.us/j/85012345678?pwd=AbC123.1", (MEETING, "AbC123.1")),
    ("zoom.us/j/85012345678", (MEETING, "")),
    ("850 1234 5678", (MEETING, "")),
    ("850-1234-5678", (MEETING, "")),
    ("https://company.zoom.us/wc/join/85012345678?pwd=x9", (MEETING, "x9")),
    ("https://zoom.us/wc/85012345678/join", (MEETING, "")),
    ("https://zoom.us/s/85012345678", (MEETING, "")),
    ("https://zoom.us/w/85012345678?tk=abc", (MEETING, "")),
    ("https://zoom.com/join?confno=85012345678", (MEETING, "")),
    ("zoommtg://zoom.us/join?action=join&confno=85012345678&pwd=p1", (MEETING, "p1")),
])
def test_meeting_links_and_numbers_parse_by_url_structure(plugin, meeting, expected):
    assert plugin.parse_meeting(meeting) == expected


@pytest.mark.parametrize("meeting, fragment", [
    ("", "empty"), ("https://zoom.us/my/anton", "personal link"), ("https://example.com/j/85012345678", "not a Zoom"),
    ("https://zoom.us.evil.example/j/85012345678", "not a Zoom"), ("https://agency.zoomgov.com/j/1", "ZoomGov"),
    ("https://zoom.us/j/", "no meeting number"), ("123abc", "not a Zoom"), ("0", "not a Zoom meeting number"),
])
def test_meeting_references_that_are_not_zoom_meetings_are_refused(plugin, meeting, fragment):
    with pytest.raises(plugin.ToolRefusal) as refused:
        plugin.parse_meeting(meeting)
    assert refused.value.code == "ZOOM_MEETING_INVALID" and fragment in refused.value.message


def _binding(zoom_account, *, binding_id=BINDING, transport="zoom_meeting", account=None, conversation="*",
             behavior="meeting_persona", enabled=True):
    return PresenceBinding(binding_id=binding_id, transport_skill=transport, behavior_skill=behavior,
                           origin=PresenceEndpoint("zoom", account or zoom_account, conversation),
                           destination=PresenceEndpoint("zoom", account or zoom_account, MEETING), enabled=enabled)


def test_binding_selection_uses_the_argument_or_the_only_zoom_binding(plugin, zoom):
    account = zoom.companion_link.app_account_id(CLIENT_ID)
    one, other = _binding(account), _binding(account, binding_id="c" * 32, behavior="other_persona")
    foreign = _binding(account, binding_id="d" * 32, transport="telegram")
    pick = lambda bindings, requested="": plugin.select_binding(  # noqa: E731
        bindings, requested=requested, account_id=account, meeting_number=MEETING)
    assert pick([one, foreign]) is one
    assert pick([one, other], requested="c" * 32) is other
    assert pick([_binding(account, conversation=MEETING)]).origin.conversation_id == MEETING
    for bindings, requested, code in (
            ([foreign], "", "ZOOM_BINDING_MISSING"), ([one, other], "", "ZOOM_BINDING_REQUIRED"),
            ([one], "e" * 32, "ZOOM_BINDING_UNKNOWN"), ([_binding(account, enabled=False)], "", "ZOOM_BINDING_MISSING"),
            ([_binding(account, account="zoom-sdk-app-0000000000000000")], "", "ZOOM_BINDING_MISMATCH"),
            ([_binding(account, conversation="99999999999")], "", "ZOOM_BINDING_MISMATCH")):
        with pytest.raises(plugin.ToolRefusal) as refused:
            pick(bindings, requested)
        assert refused.value.code == code
    with pytest.raises(plugin.ToolRefusal) as refused:
        pick([foreign])
    assert account in refused.value.message and "call mode" in refused.value.message


# ---- JWT, the join sequence and refusals --------------------------------------------------

def test_sdk_jwt_carries_exactly_the_app_claims(zoom):
    import jwt

    token = zoom.zoom_session.build_sdk_jwt(CLIENT_ID, CLIENT_SECRET, now=1_800_000_000)
    assert jwt.get_unverified_header(token)["alg"] == "HS256"
    claims = jwt.decode(token, CLIENT_SECRET, algorithms=["HS256"], options={"verify_exp": False, "verify_iat": False})
    assert claims == {"appKey": CLIENT_ID, "iat": 1_800_000_000, "exp": 1_800_007_200, "tokenExp": 1_800_007_200}


def test_join_sequence_follows_hopes_order_when_raw_recording_is_allowed(zoom, tmp_path):
    import jwt

    fake = FakeZoom()
    bot, loop, _peers, _bodies = make_bot(zoom, tmp_path, fake)
    reply = bot.handle_control("join", join_args(zoom))
    assert reply["ok"] and reply["state"] == "joining" and reply["display_name"] == "Aika"
    drive_to_audio(fake, loop)
    assert fake.calls == [
        "InitSDK", "CreateAuthService", "AuthService.SetEvent", "SDKAuth", "CreateMeetingService",
        "MeetingService.SetEvent", "CreateSettingService", "EnableAutoJoinAudio", "Join", "GetMeetingInfo",
        "JoinVoip", "UnMuteAudio", "GetAudioRawdataHelper", "setExternalAudioSource", "chat.SetEvent",
        "audio.SetEvent", "recording.SetEvent", "CanStartRawRecording", "StartRawRecording", "subscribe"]
    assert fake.init_param == {"strWebDomain": "https://zoom.us", "enableGenerateDump": False}
    assert jwt.decode(fake.jwt, CLIENT_SECRET, algorithms=["HS256"], options={"verify_exp": False,
                                                                               "verify_iat": False})["appKey"] == CLIENT_ID
    param = fake.join_param
    assert param["userType"] == fake.m.SDK_UT_WITHOUT_LOGIN
    assert (param["meetingNumber"], param["userName"], param["psw"], param["onBehalfToken"]) == (
        int(MEETING), "Aika", PASSCODE, OBF)
    assert (param["isVideoOff"], param["isAudioOff"], param["isMyVoiceInMix"], param["isAudioRawDataStereo"]) == (
        True, False, False, False)
    assert param["eAudioRawdataSamplingRate"] == fake.m.AudioRawdataSamplingRate_32K
    assert bot.state == "audio_ready" and bot.snapshot()["topic"] == "Weekly sync"


def test_privilege_request_waits_for_the_host_and_starts_once_on_approval(zoom, tmp_path):
    fake = FakeZoom()
    fake.codes["CanStartRawRecording"] = "SDKERR_NO_PERMISSION"
    bot, loop, _peers, _bodies = make_bot(zoom, tmp_path, fake)
    bot.handle_control("join", join_args(zoom))
    drive_to_audio(fake, loop, raw_allowed=False)
    assert fake.calls[-2:] == ["CanStartRawRecording", "RequestLocalRecordingPrivilege"]
    assert bot.snapshot()["message"] == "in the meeting, waiting for the host to allow Aika's audio"
    loop.advance(30)
    assert "StartRawRecording" not in fake.calls
    fake.answer("Granted")
    fake.fire("recording", "onRecordPrivilegeChangedCallback", True)
    loop.advance(2)
    assert fake.calls.count("StartRawRecording") == 1 and fake.calls[-1] == "subscribe"
    assert bot.state == "audio_ready"


def test_quiet_stdout_keeps_the_bindings_join_printout_off_the_companion_stdout(zoom, tmp_path, capfd):
    fake = FakeZoom()
    fake.print_on_join = True
    bot, loop, _peers, _bodies = make_bot(zoom, tmp_path, fake)
    bot.handle_control("join", join_args(zoom))
    loop.run_ready()
    fake.auth()
    loop.run_ready()
    assert "Join" in fake.calls
    _libc().fflush(None)
    _libc().puts(b"after the join")
    _libc().fflush(None)
    out = capfd.readouterr().out
    assert "after the join" in out and PASSCODE not in out


@pytest.mark.parametrize("path", ["leave", "ended_by_host", "removed_by_host", "join_refused", "auth_refused",
                                  "privilege_denied", "privilege_timeout", "call_refused", "call_ended",
                                  "shutdown"])
def test_every_exit_path_ends_the_call_leaves_and_cleans_up_once(zoom, tmp_path, path):
    fake = FakeZoom()
    bot, loop, peers, _bodies = make_bot(
        zoom, tmp_path, fake,
        refuse=("presence_call_not_enabled", "call mode is off for this binding") if path == "call_refused" else None)
    bot.handle_control("join", join_args(zoom))
    expect_leave, expect_peer = True, path in ("leave", "ended_by_host", "removed_by_host", "call_ended", "shutdown")
    if path == "auth_refused":
        loop.run_ready()
        fake.auth("AUTHRET_JWTTOKENWRONG")
        loop.run_ready()
        outcome, expect_leave = "Zoom refused the app credentials: AUTHRET_JWTTOKENWRONG", False
    elif path == "join_refused":
        loop.run_ready()
        fake.auth()
        loop.run_ready()
        fake.status("MEETING_STATUS_FAILED", fake.m.MEETING_FAIL_PASSWORD_ERR)
        loop.run_ready()
        outcome, expect_leave = "the meeting refused the join: MEETING_FAIL_PASSWORD_ERR", False
    elif path.startswith("privilege"):
        fake.codes["CanStartRawRecording"] = "SDKERR_NO_PERMISSION"
        drive_to_audio(fake, loop, raw_allowed=False)
        fake.answer("Denied" if path == "privilege_denied" else "Timeout")
        loop.advance(2)
        outcome = ("the host declined Aika's audio request; Aika left the meeting" if path == "privilege_denied"
                   else "the host did not answer Aika's audio request in time; Aika left the meeting")
    else:
        drive_to_audio(fake, loop)
        fake.mixed(b"\x01\x00" * 320)
        if path == "call_refused":
            loop.pump(lambda: "Leave" in fake.calls)
            loop.advance(2)
            outcome = "the call could not open: call mode is off for this binding; Aika left the meeting"
        else:
            loop.pump(lambda: bot.state == "in_call")
            if path == "leave":
                assert bot.handle_control("leave", {})["left"] is True
                outcome = "left the meeting"
            elif path in ("ended_by_host", "removed_by_host"):
                fake.end("EndMeetingReason_EndByHost" if path == "ended_by_host" else "EndMeetingReason_KickByHost")
                expect_leave = False
                outcome = ("the host ended the meeting" if path == "ended_by_host"
                           else "the host removed Aika from the meeting")
            elif path == "call_ended":
                peers[0].callbacks["on_closed"]("budget")
                outcome = "the call ended (budget); Aika left the meeting"
            else:
                bot.request_shutdown()
                assert bot.heartbeat() is False
                outcome = "the Zoom companion stopped; Aika left the meeting"
            loop.advance(2)
    assert bot.state == "idle" and bot.last_outcome["message"] == outcome
    assert fake.calls.count("CleanUPSDK") == 1 and fake.calls[-1] == "CleanUPSDK"
    assert ("Leave" in fake.calls) is expect_leave
    if expect_leave:
        assert fake.leave_cmd == fake.m.LEAVE_MEETING
        assert fake.calls.index("Leave") < fake.calls.index("CleanUPSDK")
    if "subscribe" in fake.calls and path not in ("ended_by_host", "removed_by_host"):
        assert fake.calls.index("unSubscribe") < fake.calls.index("CleanUPSDK")
    if expect_peer:
        assert peers[0].closed == outcome
    if path == "shutdown":
        assert loop.quit_called
    assert zoom.companion_link.read_status(tmp_path)["state"] == "idle"


def test_join_refusals_from_the_companion_itself(zoom, tmp_path):
    fake = FakeZoom()
    bot, loop, _peers, _bodies = make_bot(zoom, tmp_path, fake)
    assert bot.handle_control("join", join_args(zoom, account_id="zoom-sdk-app-other"))["code"] == "ZOOM_COMPANION_STALE"
    assert bot.handle_control("join", join_args(zoom, binding_id="nope"))["code"] == "ZOOM_BINDING_INVALID"
    assert bot.handle_control("join", join_args(zoom))["ok"]
    busy = bot.handle_control("join", join_args(zoom))
    assert busy["code"] == "ZOOM_BUSY" and MEETING in busy["error"]
    bare = zoom.zoom_bot.ZoomBot(state_dir=tmp_path / "bare", loop=None, sdk=None, sdk_error="no module named zoom")
    refused = bare.handle_control("join", join_args(zoom))
    assert refused["code"] == "ZOOM_SDK_UNAVAILABLE" and "no module named zoom" in refused["error"]
    assert bare.snapshot()["message"] == "this host cannot run the Zoom Meeting SDK: no module named zoom"
    assert bot.handle_control("teleport", {})["code"] == "ZOOM_UNKNOWN_OP"


def test_a_restarted_companion_starts_idle_and_never_rejoins(zoom, tmp_path):
    zoom.companion_link.write_status(tmp_path, {"state": "in_call", "message": "in the call", "pid": -1,
                                                "meeting_number": MEETING, "join_id": "j1"})
    fake = FakeZoom()
    bot, loop, _peers, _bodies = make_bot(zoom, tmp_path, fake)
    loop.advance(60)
    assert bot.state == "idle" and fake.calls == []
    assert "did not re-join" in bot.last_outcome["message"] and MEETING in bot.last_outcome["message"]


# ---- the call: door, peer socket, pacing ------------------------------------------------

def test_call_opens_through_a_real_door_with_the_true_rate_and_carries_audio_both_ways(zoom, tmp_path, door):
    fake = FakeZoom()
    bot, loop, _peers, _bodies = make_bot(
        zoom, tmp_path, fake, door=door.url, open_call=zoom.meeting_peer.open_call,
        attach_peer=zoom.meeting_peer.PeerSession.attach, identity_name=zoom.meeting_peer.identity_name,
        pacer_factory=lambda send, rate: zoom.meeting_peer.OutboundPacer(send, rate=rate))
    assert bot.handle_control("join", join_args(zoom))["display_name"] == "Aika"  # from the door's /identity
    drive_to_audio(fake, loop)
    open_call_via_audio(fake, loop, bot)
    body, headers = door.bodies[0], door.headers[0]
    assert headers["x-skill-token"] == HOST_TOKEN
    assert set(body) == {"binding_id", "role", "event", "label", "mic", "speaker"}
    assert (body["binding_id"], body["role"], body["mic"], body["speaker"]) == (BINDING, "meeting", True, True)
    event = body["event"]
    assert set(event) == {"source_event_id", "provider", "account_id", "conversation_id", "thread_id",
                          "conversation_key", "actor", "conversation", "message", "text"}
    account = zoom.companion_link.app_account_id(CLIENT_ID)
    assert (event["provider"], event["account_id"], event["conversation_id"], event["thread_id"]) == (
        "zoom", account, MEETING, "")
    assert event["conversation_key"] == f"zoom:{account}:{MEETING}:0"
    assert event["source_event_id"] == f"zoom-join-{bot.snapshot()['join_id']}"
    assert event["actor"]["id"] == "task:t-1" and event["text"] == "Joined the Zoom meeting Weekly sync"
    assert door.hello == {"type": "hello", "protocol": 1, "role": "meeting", "mic": True, "speaker": True,
                          "rate": 32000, "label": "Zoom meeting Weekly sync"}
    assert bot.snapshot()["call_id"] == "call-1"

    fake.mixed(b"\x02\x00" * 320)
    fake.mixed(b"\x03\x00" * 320)
    loop.pump(lambda: len(door.frames) >= 2)
    up = [cp.decode_frame(frame) for frame in door.frames[:2]]
    assert [(f.kind, f.rate, f.pcm) for f in up] == [(cp.FRAME_AUDIO_IN, 32000, b"\x02\x00" * 320),
                                                    (cp.FRAME_AUDIO_IN, 32000, b"\x03\x00" * 320)]
    assert up[1].seq == up[0].seq + 1

    door.send(cp.encode_frame(cp.AudioFrame(cp.FRAME_AUDIO_OUT, 0, 1, 32000, 0, b"\x04\x00" * 1600)))
    loop.pump(lambda: len(fake.sent) >= 3)
    assert [(len(data), rate, channel) for data, rate, channel in fake.sent] == [
        (1280, 32000, fake.m.ZoomSDKAudioChannel_Mono)] * 3
    assert fake.sent[2][0] == b"\x04\x00" * 320 + b"\x00" * 640  # the utterance tail, padded to one frame

    door.send(cp.encode_frame(cp.AudioFrame(cp.FRAME_AUDIO_OUT, 0, 2, 32000, 0, b"\x05\x00" * 30000)))
    door.send(cp.encode_control({"type": "flush", "epoch": 1}))
    loop.pump(lambda: bot.snapshot()["stats"].get("voice_bytes_flushed", 0) > 0)
    time.sleep(0.2)
    assert len(fake.sent) < 3 + 20  # far fewer than the 47 frames that were queued

    fake.one_way(42)
    loop.advance(0.2)
    loop.pump(lambda: {"type": "participant", "label": "Anna", "speaking": True} in door.controls)
    loop.advance(1.0)
    loop.pump(lambda: {"type": "participant", "label": "Anna", "speaking": False} in door.controls)
    fake.chat(7, "the numbers are on slide 4")
    fake.host_mute()
    loop.pump(lambda: len([c for c in door.controls if c["type"] == "context"]) == 2)
    chat, mute = [c for c in door.controls if c["type"] == "context"]
    assert chat == {"type": "context", "source": "zoom_chat",
                    "text": "Zoom chat from Boris (to everyone): the numbers are on slide 4"}
    assert mute["source"] == "zoom" and "muted you" in mute["text"]

    bot.handle_control("leave", {})
    loop.pump(lambda: door.closed)
    assert door.controls[-1] == {"type": "bye", "reason": "left the meeting"}
    loop.advance(2)
    assert bot.state == "idle" and fake.calls[-3:] == ["Leave", "unSubscribe", "CleanUPSDK"]


def test_server_hang_up_ends_the_meeting_and_a_refusal_never_attaches(zoom, tmp_path, door):
    fake = FakeZoom()
    bot, loop, _peers, _bodies = make_bot(zoom, tmp_path, fake, door=door.url, open_call=zoom.meeting_peer.open_call,
                                          attach_peer=zoom.meeting_peer.PeerSession.attach)
    bot.handle_control("join", join_args(zoom))
    drive_to_audio(fake, loop)
    open_call_via_audio(fake, loop, bot)
    door.send(cp.encode_control({"type": "state", "state": "closed", "reason": "owner stop"}))
    door.close()
    loop.pump(lambda: "Leave" in fake.calls)
    loop.advance(2)
    assert bot.last_outcome["message"] == "the call ended (owner stop); Aika left the meeting"

    door.refuse = ("presence_call_busy", "2 Presence turns are active")
    bot.handle_control("join", join_args(zoom))
    drive_to_audio(fake, loop)
    fake.mixed(b"\x01\x00" * 320)
    loop.pump(lambda: bot.state == "idle")
    assert bot.last_outcome["message"] == ("the call could not open: 2 Presence turns are active; "
                                           "Aika left the meeting")
    assert door.peers == 1


def test_pacer_sends_exact_20ms_frames_in_real_time_and_pads_only_the_tail(zoom):
    now, sent = [0.0], []
    stop = threading.Event()

    def sleep(seconds):
        now[0] += seconds
        if len(sent) == 3:
            stop.set()

    pacer = zoom.meeting_peer.OutboundPacer(lambda frame: sent.append((round(now[0], 6), frame)), rate=32000,
                                            clock=lambda: now[0], sleep=sleep)
    assert (pacer.frame_bytes, pacer.frame_sec) == (1280, 0.02)
    speech = bytes(range(256)) * 11 + bytes(184)  # 3,000 bytes: two full frames and a 440-byte tail
    pacer.feed(speech, 0)
    pacer.run(stop)
    assert [t for t, _frame in sent] == [0.0, 0.02, 0.04]
    assert [frame for _t, frame in sent] == [speech[:1280], speech[1280:2560], speech[2560:] + b"\x00" * 840]


def test_pacer_flush_drops_queued_voice_and_stale_epochs(zoom):
    peer_mod = zoom.meeting_peer
    pacer = peer_mod.OutboundPacer(lambda frame: None, rate=32000)
    pacer.feed(b"\x01\x00" * 6400, 0)
    assert len(pacer.take_frame()) == 1280
    assert pacer.flush(1) == 11520 and pacer.take_frame() is None
    pacer.feed(b"\x02\x00" * 640, 0)
    assert pacer.take_frame() is None and pacer.stale_frames == 1
    pacer.feed(b"\x03\x00" * 640, 1)
    assert pacer.flush(0) == 0  # an older epoch's late flush changes nothing
    assert pacer.take_frame() == b"\x03\x00" * 640
    pacer.feed(b"\x04\x00" * 700, 1)
    pacer.feed(b"\x05\x00" * 640, 2)  # a newer epoch supersedes what was queued
    assert pacer.take_frame() == b"\x05\x00" * 640
    assert peer_mod.epoch_is_newer(0, 0xFFFF) and not peer_mod.epoch_is_newer(0xFFFF, 0)
    assert peer_mod.OutboundPacer(lambda frame: None, rate=16000).frame_bytes == 640


def test_frames_match_the_frozen_core_codec_byte_for_byte(zoom):
    peer_mod = zoom.meeting_peer
    for kind, epoch, seq, rate, stream, pcm in ((cp.FRAME_AUDIO_IN, 0, 1, 32000, 0, b"\x01\x02" * 320),
                                                (cp.FRAME_AUDIO_OUT, 0x1FFFF, 0x1FFFFFFFF, 32000, 3, b""),
                                                (cp.FRAME_AUDIO_OUT, 7, 9, 48000, 0, b"\x00" * 65536)):
        raw = peer_mod.encode_frame(kind, epoch, seq, rate, stream, pcm)
        assert raw == cp.encode_frame(cp.AudioFrame(kind, epoch, seq, rate, stream, pcm))
        decoded = cp.decode_frame(raw)
        assert peer_mod.decode_frame(raw) == (decoded.kind, decoded.epoch, decoded.seq, decoded.rate,
                                              decoded.stream, decoded.pcm)
    assert peer_mod.HEADER_SIZE == cp.HEADER_SIZE and peer_mod.MAX_CONTROL_BYTES == cp.MAX_CONTROL_BYTES
    for bad in (b"XX" + raw[2:], raw[:10], raw + b"\x00"):
        with pytest.raises(ValueError):
            peer_mod.decode_frame(bad)


def test_control_messages_fit_the_protocol_limit_with_a_named_omission(zoom):
    peer_mod = zoom.meeting_peer
    fitted = peer_mod.fit_control({"type": "context", "text": "é" * 20000, "source": "zoom_chat"})
    assert len(fitted.encode("utf-8")) <= cp.MAX_CONTROL_BYTES
    message = cp.decode_control(fitted)
    assert message["source"] == "zoom_chat" and message["text"].endswith("more characters omitted]")
    small = {"type": "participant", "label": "Anna", "speaking": True}
    assert json.loads(peer_mod.fit_control(small)) == small
    assert peer_mod.fit_control({"type": "participant", "label": "x" * 20000, "speaking": True}) is None


# ---- the control channel and the tools -------------------------------------------------

@pytest.mark.parametrize("transport", ["unix", "tcp"])
def test_control_channel_needs_the_token_and_reports_a_missing_companion(zoom, tmp_path, monkeypatch, transport):
    link = zoom.companion_link
    if transport == "tcp":
        monkeypatch.setattr(link, "UNIX_PATH_LIMIT", 0)
    elif len(os.fsencode(str(tmp_path / link.SOCKET_FILE))) >= link.UNIX_PATH_LIMIT:
        pytest.skip("this temp path is too long for an AF_UNIX socket; the tcp case covers the channel")
    with pytest.raises(link.ControlUnavailable):
        link.control_request(tmp_path, "status")
    seen = []
    server = link.ControlServer(tmp_path, lambda op, args: seen.append((op, args)) or {"ok": True, "op": op})
    server.start()
    try:
        assert server.address["transport"] == transport
        assert oct((tmp_path / link.CONTROL_FILE).stat().st_mode & 0o777) == "0o600"
        assert link.control_request(tmp_path, "status", {"x": 1}) == {"ok": True, "op": "status"}
        info = json.loads((tmp_path / link.CONTROL_FILE).read_text())
        if transport == "tcp":
            conn = socket.create_connection(("127.0.0.1", info["port"]), timeout=5)
        else:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.connect(info["path"])
        with conn:
            conn.sendall(json.dumps({"token": "guess", "op": "leave"}).encode() + b"\n")
            assert json.loads(conn.makefile().readline())["code"] == "ZOOM_CONTROL_DENIED"
        assert seen == [("status", {"x": 1})]
    finally:
        server.close()
    assert not (tmp_path / link.CONTROL_FILE).exists()
    with pytest.raises(link.ControlUnavailable):
        link.control_request(tmp_path, "status")


class FakeApi:
    def __init__(self, state_dir, data_dir, settings):
        self.state_dir, self.data_dir, self.settings = state_dir, data_dir, settings
        self.tools, self.companions = {}, []

    def register_companion_process(self, name):
        self.companions.append(name)

    def register_tool(self, name, handler, *, description, schema, timeout_sec=60):
        self.tools[name] = types.SimpleNamespace(handler=handler, description=description, schema=schema)

    def get_state_dir(self):
        return str(self.state_dir)

    def get_settings(self, keys):
        return {key: value for key, value in self.settings.items() if key in keys}

    def get_runtime_info(self):
        return {"data_dir": str(self.data_dir)}


class FakeCompanion:
    def __init__(self, account):
        self.account, self.requests = account, []
        self.state, self.sdk, self.after_join, self.join_outcome = "idle", True, "waiting_for_audio", None

    def __call__(self, op, args):
        self.requests.append((op, args))
        if op == "join":
            self.state = self.after_join
            return {"ok": True, "join_id": "j-9", **self.status()}
        if op == "leave":
            left, self.state = self.state != "idle", "idle"
            return {"ok": True, "left": left, **self.status()}
        return {"ok": True, **self.status()}

    def status(self):
        messages = {"idle": "not in a Zoom meeting", "joining": "joining",
                    "waiting_for_audio": "in the meeting, waiting for the host to allow Aika's audio"}
        return {"state": self.state, "message": messages[self.state], "meeting_number": MEETING if self.state != "idle"
                else "", "account_id": self.account, "credentials": True,
                "sdk": {"available": self.sdk, "detail": "" if self.sdk else "zoom_meeting_sdk is not importable"},
                "last_outcome": self.join_outcome}


@pytest.fixture
def tools(plugin, zoom, tmp_path, monkeypatch):
    state_dir, data_dir = tmp_path / "state", tmp_path / "data"
    state_dir.mkdir()
    (data_dir / "state").mkdir(parents=True)
    account = zoom.companion_link.app_account_id(CLIENT_ID)
    save_presence_binding(data_dir, _binding(account))
    api = FakeApi(state_dir, data_dir, {"ZOOM_CLIENT_ID": CLIENT_ID, "ZOOM_CLIENT_SECRET": CLIENT_SECRET})
    plugin.register(api)
    companion = FakeCompanion(account)
    server = zoom.companion_link.ControlServer(state_dir, companion)
    server.start()
    monkeypatch.setattr(plugin, "_sleep", lambda seconds: None)
    ctx = types.SimpleNamespace(task_id="t-1", task_metadata={"source": "web"}, current_task_type="task",
                                drive_root=data_dir)
    call = lambda name, **kwargs: api.tools[name].handler(ctx, **kwargs)  # noqa: E731
    yield types.SimpleNamespace(api=api, call=call, companion=companion, server=server, ctx=ctx, account=account,
                                data_dir=data_dir, state_dir=state_dir)
    server.close()


def test_plugin_registers_the_companion_and_three_tools(tools):
    assert tools.api.companions == ["zoom_bot"]
    assert sorted(tools.api.tools) == ["zoom_join", "zoom_leave", "zoom_status"]
    schema = tools.api.tools["zoom_join"].schema
    assert schema["required"] == ["meeting"]
    assert set(schema["properties"]) == {"meeting", "passcode", "display_name", "binding_id", "on_behalf_token"}
    assert "OBF" in schema["properties"]["on_behalf_token"]["description"]


def test_zoom_join_hands_over_the_join_and_never_echoes_a_secret(tools):
    result = tools.call("zoom_join", meeting=f"https://us02web.zoom.us/j/{MEETING}?pwd={PASSCODE}",
                        on_behalf_token=OBF)
    assert result.splitlines()[0] == "in the meeting, waiting for the host to allow Aika's audio"
    op, args = tools.companion.requests[-2]
    assert op == "join" and tools.companion.requests[-1][0] == "status"
    assert (args["meeting_number"], args["passcode"], args["on_behalf_token"], args["binding_id"]) == (
        MEETING, PASSCODE, OBF, BINDING)
    assert args["account_id"] == tools.account
    assert args["requested_by"] == {"id": "task:t-1", "display_name": "", "requested_via": "web", "task_id": "t-1"}
    for secret in (PASSCODE, OBF, CLIENT_SECRET, CLIENT_ID):
        assert secret not in result
    tools.companion.state = "idle"
    tools.ctx.task_metadata = {"presence": {"event": {"provider": "telegram", "actor": {
        "platform_actor_id": "u77", "display_name": "Lena"}}}}
    tools.call("zoom_join", meeting=MEETING, passcode="explicit-1")
    args = tools.companion.requests[-2][1]
    assert args["passcode"] == "explicit-1"
    assert args["requested_by"]["id"] == "telegram:u77" and args["requested_by"]["display_name"] == "Lena"
    assert tools.call("zoom_status").splitlines()[0] == "in the meeting, waiting for the host to allow Aika's audio"
    assert tools.call("zoom_leave").splitlines()[0] == "not in a Zoom meeting"


def test_zoom_join_refusals_are_typed_and_say_what_to_do(tools, plugin, zoom):
    assert tools.call("zoom_join", meeting="https://zoom.us/my/anton").startswith("⚠️ ZOOM_MEETING_INVALID:")
    tools.api.settings = {"ZOOM_CLIENT_ID": CLIENT_ID}
    assert tools.call("zoom_join", meeting=MEETING) == (
        "⚠️ ZOOM_CREDENTIALS_MISSING: Zoom app credentials are not set: save the Meeting SDK app's ZOOM_CLIENT_ID "
        "and ZOOM_CLIENT_SECRET in Settings → Secrets and grant them to zoom_meeting")
    tools.api.settings = {"ZOOM_CLIENT_ID": CLIENT_ID, "ZOOM_CLIENT_SECRET": CLIENT_SECRET}
    save_presence_binding(tools.data_dir, _binding(tools.account, binding_id="c" * 32, behavior="other_persona"))
    assert tools.call("zoom_join", meeting=MEETING).startswith("⚠️ ZOOM_BINDING_REQUIRED:")
    tools.companion.sdk = False
    refused = tools.call("zoom_join", meeting=MEETING, binding_id=BINDING)
    assert refused.startswith("⚠️ ZOOM_SDK_UNAVAILABLE:") and "not importable" in refused
    tools.companion.sdk, tools.companion.account = True, "zoom-sdk-app-stale"
    assert tools.call("zoom_join", meeting=MEETING, binding_id=BINDING).startswith("⚠️ ZOOM_COMPANION_STALE:")
    tools.companion.account, tools.companion.state = tools.account, "waiting_for_audio"
    busy = tools.call("zoom_join", meeting=MEETING, binding_id=BINDING)
    assert busy.startswith("⚠️ ZOOM_BUSY:") and "zoom_leave" in busy
    tools.companion.state, tools.companion.after_join = "idle", "idle"
    tools.companion.join_outcome = {"message": "the meeting refused the join: MEETING_FAIL_PASSWORD_ERR",
                                    "join_id": "j-9", "at": "now"}
    assert tools.call("zoom_join", meeting=MEETING, binding_id=BINDING) == (
        "⚠️ ZOOM_JOIN_FAILED: the meeting refused the join: MEETING_FAIL_PASSWORD_ERR")
    tools.server.close()
    zoom.companion_link.write_status(tools.state_dir, {"state": "in_call", "message": "in the call",
                                                       "meeting_number": MEETING})
    down = tools.call("zoom_status")
    assert down.startswith("⚠️ ZOOM_COMPANION_UNAVAILABLE:") and "Last published state: in the call" in down
    assert all(op != "join" or args["binding_id"] == BINDING for op, args in tools.companion.requests)


# ---- secrets, the probe and the GLib loop ---------------------------------------------------

def test_no_secret_reaches_a_log_line_the_status_file_or_a_reply(zoom, tmp_path):
    fake = FakeZoom()
    redactor = zoom.zoom_bot.SecretRedactor()
    for value in (CLIENT_SECRET, HOST_TOKEN):
        redactor.add(value)
    stream = __import__("io").StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(redactor)
    logger = logging.getLogger("zoom_bot")
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        bot, loop, peers, bodies = make_bot(zoom, tmp_path, fake, register_secret=redactor.add)
        replies = [bot.handle_control("join", join_args(zoom))]
        drive_to_audio(fake, loop)
        open_call_via_audio(fake, loop, bot)
        replies += [bot.handle_control("status", {}), bot.handle_control("leave", {})]
        loop.advance(2)
        logging.getLogger("zoom_bot.session").error("unexpected: %s", fake.jwt)
        try:
            raise RuntimeError(f"wrapped {PASSCODE} and {OBF}")
        except RuntimeError:
            logging.getLogger("zoom_bot.peer").exception("traceback carries %s", CLIENT_SECRET)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    written = stream.getvalue() + (tmp_path / "status.json").read_text() + json.dumps(replies) + json.dumps(
        bodies) + json.dumps(peers[0].hello)
    assert "[redacted]" in stream.getvalue()
    for secret in (CLIENT_SECRET, PASSCODE, OBF, HOST_TOKEN, fake.jwt):
        assert secret and secret not in written


def test_probe_reports_pass_fail_and_not_run_without_printing_a_secret(zoom, capsys, monkeypatch):
    probe = zoom.probe
    run_probe = probe.run_probe
    env = {"ZOOM_CLIENT_ID": CLIENT_ID, "ZOOM_CLIENT_SECRET": CLIENT_SECRET}

    def run(fake, **kwargs):
        loop = ManualLoop()
        if fake is not None and fake.on_sdkauth is None:
            fake.on_sdkauth = lambda: loop.idle_add(lambda: fake.auth())
        options = dict(env=env, import_sdk=lambda: (fake.m, "zoom-meeting-sdk 0.0.27") if fake else (None, "ImportError"),
                       make_loop=lambda: loop, platform_name=lambda: "linux-x86_64", clock=lambda: 1_800_000_000)
        options.update(kwargs)
        return {step["step"]: (step["status"], step["detail"]) for step in run_probe(**options)}

    fake = FakeZoom()
    steps = run(fake)
    assert {name: status for name, (status, _detail) in steps.items()} == dict.fromkeys(probe.STEPS, "PASS")
    assert fake.calls == ["InitSDK", "CreateAuthService", "AuthService.SetEvent", "SDKAuth", "CleanUPSDK"]
    assert "Join" not in fake.calls and zoom.companion_link.app_account_id(CLIENT_ID) in steps["credentials"][1]
    missing = run(FakeZoom(), env={}, platform_name=lambda: "darwin-arm64")
    assert missing["platform"][0] == "FAIL" and missing["credentials"][0] == "FAIL"
    assert (missing["init_sdk"][0], missing["authenticate"][0], missing["cleanup"][0]) == ("PASS", "NOT_RUN", "PASS")
    absent = run(None)
    assert absent["import_sdk"][0] == "FAIL" and {absent[s][0] for s in ("glib", "init_sdk", "authenticate",
                                                                         "cleanup")} == {"NOT_RUN"}
    refused = FakeZoom()
    refused.on_sdkauth = lambda: None
    timed_out = run(refused, auth_timeout_ms=30000)
    assert timed_out["authenticate"] == ("FAIL", "no answer from Zoom within 30 s")
    wrong = FakeZoom()
    loop_box = {}
    wrong.on_sdkauth = lambda: loop_box["loop"].idle_add(lambda: wrong.auth("AUTHRET_KEYORSECRETWRONG"))
    steps = run(wrong, make_loop=lambda: loop_box.setdefault("loop", ManualLoop()))
    assert steps["authenticate"] == ("FAIL", "AUTHRET_KEYORSECRETWRONG")

    monkeypatch.setattr(probe, "run_probe", lambda **kwargs: [
        {"step": s, "status": "PASS", "detail": d} for s, (_st, d) in run(FakeZoom()).items()])
    assert probe.main(["--json"]) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)["result"] == "PASS"
    assert CLIENT_SECRET not in printed and CLIENT_ID not in printed


def test_ctypes_glib_loop_runs_callbacks_on_its_own_thread_and_releases_them(zoom):
    glib = zoom.glib_loop
    library = glib.find_libglib()
    if library is None:
        pytest.skip("libglib-2.0 is not installed on this host; the ctypes loop is exercised where it is")
    loop = glib.make_loop(prefer_gi=False)
    assert isinstance(loop, glib.CtypesGLibLoop) and loop.backend == "ctypes"
    here, seen, ticks = threading.get_ident(), [], []

    def from_other_thread():
        worker = threading.Thread(target=lambda: loop.idle_add(lambda: seen.append(("cross", threading.get_ident()))))
        worker.start()
        worker.join()
        return False

    def boom():
        raise RuntimeError("a failing callback is removed, the loop goes on")

    loop.idle_add(lambda: seen.append(("idle", threading.get_ident())))
    loop.idle_add(boom)
    loop.timeout_add(5, lambda: ticks.append(1) or len(ticks) < 3)
    loop.timeout_add(1, from_other_thread)
    loop.timeout_add(150, lambda: loop.quit())
    watchdog = threading.Timer(5, loop.quit)
    watchdog.start()
    try:
        loop.run()
    finally:
        watchdog.cancel()
    assert ticks == [1, 1, 1] and ("idle", here) in seen and ("cross", here) in seen
    assert loop.pending() == 0 and not loop.is_running()
    with pytest.raises(glib.GLibUnavailable):
        glib.make_loop(prefer_gi=False, library="/nonexistent/libglib-2.0.so.0")


def test_find_libglib_reports_absence(zoom, monkeypatch):
    monkeypatch.setattr(zoom.glib_loop.ctypes.util, "find_library", lambda name: None)
    assert zoom.glib_loop.find_libglib(candidates=["/nonexistent/libglib-2.0.so.0"]) is None


# ---- the fake Host door -----------------------------------------------------------------------

@pytest.fixture
def door():
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route, WebSocketRoute

    state = types.SimpleNamespace(bodies=[], headers=[], hello=None, frames=[], controls=[], ws=None,
                                  loop=None, refuse=None, closed=False, peers=0)

    async def identity(request):
        if request.headers.get("x-skill-token") != HOST_TOKEN:
            return JSONResponse({"ok": False}, status_code=403)
        return JSONResponse({"ok": True, "name": "Aika", "description": ""})

    async def call_open(request):
        state.headers.append(dict(request.headers))
        state.bodies.append(await request.json())
        if state.refuse:
            return JSONResponse({"ok": False, "code": state.refuse[0], "error": state.refuse[1]}, status_code=409)
        return JSONResponse({"call_id": "call-1", "peer_path": "/call/call-1/peer"})

    async def peer(websocket):
        if websocket.headers.get("x-skill-token") != HOST_TOKEN:
            await websocket.close(code=4403)
            return
        await websocket.accept()
        state.ws, state.loop, state.closed, state.hello = websocket, asyncio.get_running_loop(), False, None
        state.peers += 1
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    state.frames.append(message["bytes"])
                elif message.get("text") is not None:
                    control = json.loads(message["text"])
                    if state.hello is None:
                        state.hello = control
                    else:
                        state.controls.append(control)
        finally:
            state.closed = True

    def send(data):
        coroutine = state.ws.send_bytes(data) if isinstance(data, bytes) else state.ws.send_text(data)
        asyncio.run_coroutine_threadsafe(coroutine, state.loop).result(5)

    def close():
        asyncio.run_coroutine_threadsafe(state.ws.close(), state.loop).result(5)

    app = Starlette(routes=[Route("/identity", identity), Route("/call/open", call_open, methods=["POST"]),
                            WebSocketRoute("/call/{call_id}/peer", peer)])
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        port = probe_socket.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="zoom-fake-door", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "fake door did not start"
        time.sleep(0.02)
    state.url, state.send, state.close = f"http://127.0.0.1:{port}", send, close
    try:
        yield state
    finally:
        server.should_exit = True
        thread.join(10)
