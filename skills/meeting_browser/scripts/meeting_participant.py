"""Meeting participant companion: Ouroboros's own browser window as a meeting participant.

Experimental. The host starts it when the skill is enabled; it stays idle until
``meeting_join`` arrives over the private control channel. A join needs the
live call it belongs to: the companion asks the Host Service door to attach it
to that call as a ``meeting`` peer with a speaker, opens the meeting link in
Ouroboros's own Chrome window, and then carries audio both ways: the call's
output feeds the page's replaced microphone (the meeting hears Ouroboros), and
the page's remote audio goes to the call as meeting audio (Ouroboros hears the
meeting). When the call ends, on ``meeting_leave``, when the window is closed or
when the companion exits, the window closes and the peer says bye.

Whether the meeting really hears Ouroboros is reported from facts the page
observes (the page asked for the microphone, put it on a WebRTC sender, the
track is live): until then the participant is honestly "listen-only".
``probe`` runs the same join without a call, plays a test tone into the
microphone and records PASS or FAIL for the vendor (owner-run, real meeting).
"""

from __future__ import annotations

import math
import os
import pathlib
import signal
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from meeting_control import ControlServer  # noqa: E402
from meeting_peer import DoorPeer, PeerRefused  # noqa: E402
from participant_browser import RATE, BrowserSession, SessionFailed  # noqa: E402
from vendors import (  # noqa: E402
    FAIL,
    PASS,
    VENDORS,
    VendorRefused,
    belongs_to,
    record_probe,
    require_probed,
    vendor_for,
)

PARENT_PID_ENV = "OUROBOROS_PARENT_PID"
LIFELINE_SEC = 0.5
PROBE_TONE_HZ = 440.0
PROBE_CHUNK_SEC = 0.04
MIN_PROBE_SEC = 5.0
MAX_PROBE_SEC = 600.0

_DOOR_REFUSALS = {
    "call_not_found": ("MEETING_CALL_GONE", "The call is no longer open, so there is nothing to join."),
    "call_peer_forbidden": ("MEETING_NOT_GRANTED",
                            "The host refused the participant: this skill needs its reviewed call_peer grant."),
}


def _refusal(code: str, message: str) -> Dict[str, Any]:
    return {"ok": False, "code": code, "message": message}


def describe_presence(name: str, facts: Dict[str, Any]) -> tuple:
    """(state, plain sentence) from what the page itself observed; no guessing."""
    if facts.get("published") and facts.get("micLive"):
        return "speaking", f"in {name}: the meeting receives Ouroboros's voice from this window"
    if facts.get("published"):
        return "listen-only", f"in {name}: the page muted Ouroboros's microphone, so the meeting does not hear it"
    if facts.get("microphoneRequests"):
        return "listen-only", (f"{name} took Ouroboros's microphone but has not sent it yet (still joining, or "
                               "the page refused the replaced microphone); listen-only for now")
    return "listen-only", (f"{name} has not asked for a microphone yet (the page may still be on its join or "
                           "sign-in screen); listen-only for now")


@dataclass
class Participation:
    call_id: str
    vendor: str
    url: str
    session: BrowserSession
    peer: Optional[DoorPeer]
    started_at: float = field(default_factory=time.time)
    ending: bool = False


class ParticipantCompanion:
    def __init__(self, state_dir: pathlib.Path, host_url: str, host_token: str, *,
                 session_factory: Callable[..., BrowserSession] = BrowserSession,
                 peer_factory: Callable[..., DoorPeer] = DoorPeer):
        self.state_dir = pathlib.Path(state_dir)
        self.host_url = host_url
        self.host_token = host_token
        self.session_factory = session_factory
        self.peer_factory = peer_factory
        self.stop_event = threading.Event()
        self.last_event = ""
        self._lock = threading.RLock()
        self._operations = threading.Lock()
        self._current: Optional[Participation] = None
        self._control: Optional[ControlServer] = None

    def start(self) -> None:
        self._control = ControlServer(self.state_dir, self.handle)
        self._control.start()

    def shutdown(self) -> None:
        current = self._current
        if current is not None:
            self._end(current, "the participant companion stopped")
        if self._control is not None:
            self._control.stop()
            self._control = None

    def handle(self, op: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if op == "status":
            return self.status()
        operations = {
            "join": lambda: self.join(str(args.get("call_id") or ""), str(args.get("url") or "")),
            "leave": self.leave,
            "probe": lambda: self.probe(str(args.get("url") or ""), float(args.get("seconds") or 60)),
        }
        if op not in operations:
            return _refusal("MEETING_CONTROL_REFUSED", f"unknown operation {op!r}")
        with self._operations:
            return operations[op]()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            current = self._current
        if current is None:
            return {"ok": True, "state": "idle",
                    "message": "not in a meeting" + (f"; last: {self.last_event}" if self.last_event else "")}
        name = VENDORS[current.vendor][0]
        facts = dict(current.session.page_status or {})
        state, message = describe_presence(name, facts)
        return {"ok": True, "state": state, "message": message, "vendor": name, "call_id": current.call_id,
                "page_title": current.session.title, "page_url": current.session.page_url or current.url,
                "remote_audio_tracks": facts.get("tappedTracks", 0), "audio_mode": facts.get("mode", ""),
                "frames_sent": current.peer.stats["frames_sent"] if current.peer else 0}

    def join(self, call_id: str, url: str) -> Dict[str, Any]:
        if not call_id:
            return _refusal("MEETING_NO_CALL", "Joining as a participant needs the live call it belongs to.")
        if not self.host_url or not self.host_token:
            return _refusal("MEETING_HOST_UNAVAILABLE", "The companion has no Host Service address or token.")
        try:
            vendor, origin = vendor_for(url)
            require_probed(vendor, self.state_dir)
        except VendorRefused as exc:
            return _refusal(exc.code, exc.message)
        with self._lock:
            current = self._current
        if current is not None:
            self._end(current, "replaced by a new meeting")
        name = VENDORS[vendor][0]
        holder: Dict[str, Participation] = {}
        peer = self.peer_factory(
            self.host_url, self.host_token, call_id, rate=RATE, label=f"Ouroboros in {name}", speaker_grant=True,
            on_audio_out=lambda pcm, rate, epoch: self._feed(holder.get("p"), pcm, rate, epoch),
            on_flush=lambda epoch: self._flush(holder.get("p"), epoch),
            on_closed=lambda attachment, reason: self._peer_closed(holder.get("p"), attachment, reason))
        try:
            peer.open()
        except PeerRefused as exc:
            code, message = _DOOR_REFUSALS.get(exc.code, ("MEETING_DOOR_REFUSED",
                                                          f"The host refused the participant: {exc.message}"))
            return _refusal(code, message)
        session = self.session_factory(
            self.state_dir, url, origin, on_remote_pcm=lambda pcm, stream: peer.send_audio(pcm, stream),
            on_closed=lambda reason: self._later(self._session_closed, holder.get("p"), reason),
            belongs=lambda address: belongs_to(vendor, address))
        try:
            session.open()
        except SessionFailed as exc:
            return _refusal(exc.code, exc.message)
        participation = Participation(call_id, vendor, url, session, peer)
        holder["p"] = participation
        try:
            peer.attach(speaker=True)
        except PeerRefused as exc:
            session.close("the call refused the participant")
            return _refusal("MEETING_DOOR_REFUSED", f"The call refused the participant: {exc.message}")
        with self._lock:
            self._current = participation
            self.last_event = ""
        if not session.alive:  # the window closed while the call socket was being attached
            self._end(participation, "the browser window closed while joining")
            return _refusal("MEETING_BROWSER_FAILED", "The browser window closed while joining.")
        return {**self.status(), "state": "joining", "message": (
            f"opened {name} in Ouroboros's browser window; if the page asks, join from that window (sign in "
            "once with Ouroboros's own account; a screenshot and a click on 'Join' with the computer-use tools "
            "work). meeting_status tells when the meeting can hear Ouroboros.")}

    def leave(self) -> Dict[str, Any]:
        with self._lock:
            current = self._current
        if current is None:
            return {"ok": True, "state": "idle", "message": "not in a meeting"}
        self._end(current, "left on request")
        return {"ok": True, "state": "idle", "message": f"left {VENDORS[current.vendor][0]} and closed the window"}

    # -- events --------------------------------------------------------------------------
    @staticmethod
    def _later(function: Callable[..., None], *args: Any) -> None:
        threading.Thread(target=function, args=args, name="meeting-event", daemon=True).start()

    def _feed(self, participation: Optional[Participation], pcm: bytes, rate: int, epoch: int) -> None:
        if participation is not None and rate == RATE:
            participation.session.feed(pcm, epoch)

    def _flush(self, participation: Optional[Participation], epoch: int) -> None:
        if participation is not None:
            participation.session.flush(epoch)

    def _peer_closed(self, participation: Optional[Participation], attachment: int, reason: str) -> None:
        if participation is None or participation.peer is None or attachment != participation.peer.attachment:
            return
        text = reason if reason.startswith("the call") else f"the call ended ({reason})" if reason else "the call ended"
        self._later(self._end, participation, text)

    def _session_closed(self, participation: Optional[Participation], reason: str) -> None:
        if participation is not None:
            self._end(participation, reason)

    def _end(self, participation: Participation, reason: str) -> None:
        with self._lock:
            if participation.ending:
                return
            participation.ending = True
            if self._current is participation:
                self._current = None
            self.last_event = f"{reason} (was in {VENDORS[participation.vendor][0]})"
        if participation.peer is not None:
            participation.peer.detach("left")
        participation.session.close(reason)

    # -- owner-run probe -------------------------------------------------------------------
    def probe(self, url: str, seconds: float) -> Dict[str, Any]:
        """Join ``url`` without a call, speak a test tone, record PASS or FAIL for its vendor."""
        try:
            vendor, origin = vendor_for(url)
        except VendorRefused as exc:
            return _refusal(exc.code, exc.message)
        if self._current is not None:
            return _refusal("MEETING_BUSY", "Leave the current meeting before probing a vendor.")
        heard = {"chunks": 0, "nonzero": 0}

        def remote(pcm: bytes, stream: int) -> None:
            heard["chunks"] += 1
            heard["nonzero"] += sum(1 for (sample,) in struct.iter_unpack("<h", pcm) if sample)

        session = self.session_factory(self.state_dir, url, origin, on_remote_pcm=remote,
                                       on_closed=lambda reason: None, belongs=lambda address: belongs_to(vendor, address))
        try:
            session.open()
        except SessionFailed as exc:
            record_probe(self.state_dir, vendor, FAIL, {}, exc.message)
            return _refusal(exc.code, exc.message)
        duration = max(MIN_PROBE_SEC, min(MAX_PROBE_SEC, seconds))
        deadline = time.monotonic() + duration
        frames = int(RATE * PROBE_CHUNK_SEC)
        tone = struct.pack(f"<{frames}h", *(int(8000 * math.sin(2 * math.pi * PROBE_TONE_HZ * index / RATE))
                                            for index in range(frames)))
        while time.monotonic() < deadline:
            session.feed(tone, None)
            time.sleep(PROBE_CHUNK_SEC)
        try:
            sent = session.call(lambda page: page.evaluate(
                "() => window.__ouroborosParticipant ? window.__ouroborosParticipant.outboundAudioBytes() : 0"))
        except Exception:
            sent = 0
        facts = {**dict(session.page_status or {}), "outbound_audio_bytes": sent, "remote_chunks": heard["chunks"],
                 "remote_nonzero_samples": heard["nonzero"], "page_title": session.title,
                 "page_url": session.page_url, "seconds": round(duration, 1)}
        session.close("probe finished")
        missing = [label for label, held in (
            ("the page never asked for the microphone", facts.get("microphoneRequests")),
            ("the page never sent the microphone over WebRTC", facts.get("published")),
            ("no outbound audio bytes were sent", sent),
            ("no remote meeting audio arrived (not admitted, or nobody spoke)", heard["nonzero"]),
        ) if not held]
        result = PASS if not missing else FAIL
        reason = "; ".join(missing)
        record_probe(self.state_dir, vendor, result, facts, reason)
        return {"ok": True, "vendor": VENDORS[vendor][0], "result": result, "reason": reason, "facts": facts}


def _parent_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    if sys.platform == "win32":  # pragma: no cover - Windows only
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    return os.getppid() == pid


def main() -> int:
    state_dir = pathlib.Path(os.environ.get("OUROBOROS_SKILL_STATE_DIR") or ".").resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    companion = ParticipantCompanion(state_dir, os.environ.get("HOST_SERVICE_URL", ""),
                                     os.environ.get("HOST_SERVICE_TOKEN", ""))
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: companion.stop_event.set())
    companion.start()
    try:
        parent = int(os.environ.get(PARENT_PID_ENV) or 0) or os.getppid()
    except ValueError:
        parent = os.getppid()
    try:
        while not companion.stop_event.wait(LIFELINE_SEC):
            if not _parent_alive(parent):
                break
    finally:
        companion.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
