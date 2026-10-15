"""Live call wire protocol: what every audio peer and the live driver agree on.

A call is one admitted turn (ARCHITECTURE §6 "Live calls"); its audio reaches the
server through peers — the owner's own surface (the SPA on any shell) and reviewed
skill companions (system-audio capture, a browser participant, a Zoom SDK client)
— over one framed socket per peer. This module is the shared vocabulary: the call
specification the host admits, the binary audio frame, and the small JSON control
messages. Mixing, resampling and routing live in ``call_peers``; the socket routes
live in the gateway; the turn itself lives in ``live_turn``.

Who spoke is a host fact (the peer's authenticated identity), never a model claim.
An owner surface's microphone is the owner; a meeting capture is "meeting audio"
whose individual speakers stay unknown unless the peer reports them as context.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any, Dict, Optional

PROTOCOL_VERSION = 1

PEER_OWNER = "owner"        # the owner's own surface: mic is owner speech, speaker plays the call
PEER_MEETING = "meeting"    # a capture or participant companion: meeting audio in, optional speech out
PEER_ROLES = (PEER_OWNER, PEER_MEETING)

OPENED_BY_OWNER = "owner_surface"
OPENED_BY_SKILL = "skill"

STATE_CONNECTING = "connecting"
STATE_LIVE = "live"
STATE_RECONNECTING = "reconnecting"
STATE_CLOSING = "closing"
STATE_CLOSED = "closed"
CALL_STATES = (STATE_CONNECTING, STATE_LIVE, STATE_RECONNECTING, STATE_CLOSING, STATE_CLOSED)

# Binary frame: fixed 16-byte header + PCM16LE mono payload.
#   magic  2s  b"OC"
#   ver    B   PROTOCOL_VERSION
#   kind   B   FRAME_AUDIO_IN (peer -> server) | FRAME_AUDIO_OUT (server -> peer)
#   epoch  H   output epoch (server bumps it on interruption; peers drop stale epochs)
#   seq    I   per-peer, per-direction sequence
#   rate   I   sample rate of the payload
#   stream H   peer-local stream id (0 = the peer's primary track)
_HEADER = struct.Struct("<2sBBHIIH")
HEADER_SIZE = _HEADER.size
_MAGIC = b"OC"
FRAME_AUDIO_IN = 1
FRAME_AUDIO_OUT = 2
MAX_FRAME_PAYLOAD = 64 * 1024


@dataclass(frozen=True)
class AudioFrame:
    kind: int
    epoch: int
    seq: int
    rate: int
    stream: int
    pcm: bytes


def encode_frame(frame: AudioFrame) -> bytes:
    if len(frame.pcm) > MAX_FRAME_PAYLOAD or len(frame.pcm) % 2:
        raise ValueError("audio payload must be PCM16 and at most 64 KiB")
    return _HEADER.pack(_MAGIC, PROTOCOL_VERSION, frame.kind, frame.epoch & 0xFFFF,
                        frame.seq & 0xFFFFFFFF, frame.rate, frame.stream & 0xFFFF) + frame.pcm


def decode_frame(data: bytes) -> AudioFrame:
    if len(data) < HEADER_SIZE:
        raise ValueError("audio frame shorter than its header")
    magic, version, kind, epoch, seq, rate, stream = _HEADER.unpack_from(data)
    if magic != _MAGIC or version != PROTOCOL_VERSION:
        raise ValueError("not a live call audio frame of this protocol version")
    pcm = data[HEADER_SIZE:]
    if len(pcm) > MAX_FRAME_PAYLOAD or len(pcm) % 2 or kind not in (FRAME_AUDIO_IN, FRAME_AUDIO_OUT):
        raise ValueError("malformed live call audio frame")
    return AudioFrame(kind, epoch, seq, rate, stream, pcm)


# JSON control messages (text frames). Peer -> server:
#   {"type": "hello", "protocol": 1, "role": "owner"|"meeting", "mic": bool, "speaker": bool,
#    "rate": int, "label": str}
#   {"type": "mute", "muted": bool}
#   {"type": "played", "item_id": str, "played_ms": int}     # playback acknowledgement
#   {"type": "context", "text": str, "source": str}           # a peer-observed fact for the mind
#   {"type": "participant", "label": str, "speaking": bool}   # who the platform says is speaking
#   {"type": "bye", "reason": str}
# Server -> peer:
#   {"type": "state", "state": <CALL_STATES>, "reason": str}
#   {"type": "caption", "speaker": "owner"|"ouroboros", "text": str, "final": bool, "item_id": str}
#   {"type": "flush", "epoch": int}                           # stop playback of older epochs now
PEER_MESSAGE_TYPES = ("hello", "mute", "played", "context", "participant", "bye")
SERVER_MESSAGE_TYPES = ("state", "caption", "flush")
MAX_CONTROL_BYTES = 16 * 1024


def encode_control(message: Dict[str, Any]) -> str:
    if message.get("type") not in PEER_MESSAGE_TYPES + SERVER_MESSAGE_TYPES:
        raise ValueError(f"unknown live call control type: {message.get('type')!r}")
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def decode_control(text: str) -> Dict[str, Any]:
    if len(text.encode("utf-8")) > MAX_CONTROL_BYTES:
        raise ValueError("live call control message too large")
    message = json.loads(text)
    if not isinstance(message, dict) or message.get("type") not in PEER_MESSAGE_TYPES + SERVER_MESSAGE_TYPES:
        raise ValueError("unknown live call control message")
    return message


@dataclass(frozen=True)
class CallSpec:
    """What the host admits as one call.

    ``chat_id`` is the room. ``engine``/``model``/``voice`` select the realtime
    route. ``opened_by`` records whether the owner's own surface or a reviewed
    skill opened it; a skill-opened call for a Presence binding carries
    ``presence_binding_id`` and runs under that binding's ceiling. ``max_minutes``
    is the owner's configured ceiling (``None`` = none); the provider's own
    session limits are handled as reconnects, not as the end of the call.
    """

    call_id: str
    chat_id: int
    engine: str
    model: str
    voice: str = ""
    opened_by: str = OPENED_BY_OWNER
    presence_binding_id: str = ""
    max_minutes: Optional[int] = None
    label: str = ""
    client_message_id: str = ""

    def as_metadata(self) -> Dict[str, Any]:
        """The ``task["metadata"]["live_call"]`` form the driver selection reads."""
        return {
            "call_id": self.call_id, "chat_id": self.chat_id, "engine": self.engine,
            "model": self.model, "voice": self.voice, "opened_by": self.opened_by,
            "presence_binding_id": self.presence_binding_id, "max_minutes": self.max_minutes,
            "label": self.label, "client_message_id": self.client_message_id,
        }

    @classmethod
    def from_metadata(cls, data: Dict[str, Any]) -> "CallSpec":
        return cls(
            call_id=str(data["call_id"]), chat_id=int(data["chat_id"]), engine=str(data["engine"]),
            model=str(data["model"]), voice=str(data.get("voice") or ""),
            opened_by=str(data.get("opened_by") or OPENED_BY_OWNER),
            presence_binding_id=str(data.get("presence_binding_id") or ""),
            max_minutes=(int(data["max_minutes"]) if data.get("max_minutes") not in (None, "") else None),
            label=str(data.get("label") or ""),
            client_message_id=str(data.get("client_message_id") or ""),
        )
