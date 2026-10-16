"""The live voice-call driver across sessions: what a call heard, saw and was asked.

The round-trip cases of ``test_live_turn``'s harness that span a reconnect, a
connection still opening, a repeated provider request, a Presence call's rounds
and the route's instruction ceiling. The fakes and the ``harness`` fixture are
``test_live_turn``'s own.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

from ouroboros.call_protocol import (
    STATE_CLOSING, STATE_CONNECTING, STATE_LIVE, PeerAudio, PeerInfo, PeerJoined, PeerLeft,
)
from ouroboros.gateways.realtime_engine import (
    ENGINE_GOOGLE, ENGINE_OPENAI, SPEAKER_OUROBOROS, SPEAKER_OWNER,
    AudioIn, AudioOut, Caption, ContextItem, GoAway, Interrupt, PlayedAudio,
    ResponseDone, ResponseStarted, SpeechStarted, ToolCall, ToolResult,
)
from ouroboros.usage_accounting import BudgetExceeded
from tests import test_live_turn as base
from tests.test_live_turn import OWNER, FakeEngine, _spec, _wait_for

harness = base.harness  # the same fixture, registered in this module


# --- what reaches the session: images, context the room heard, refused results ----------

PIXEL = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
         b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03\x02\x00\xef\xa3\x08[\x00\x00\x00\x00IEND\xaeB`\x82")


def _image_row(note: str, pixels: bytes = PIXEL) -> dict:
    import base64

    return {"role": "user", "content": [{"type": "text", "text": note},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(pixels).decode()}}]}


def test_pixels_a_tool_viewed_ride_on_that_calls_result(harness):
    h = harness()

    def hook(call, messages, schemas):
        if call["id"] == "view-1":  # as vision.attach_local_image_to_context does, before the tool row
            messages.append(_image_row("[Image attached: pixel.png]"))
        messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["function"]["name"],
                         "content": f"viewed by {call['id']}"})

    h.tool_hook = hook
    engine = h.start()
    engine.push(ToolCall("view-1", "web_search", "{}"))
    assert _wait_for(lambda: engine.sent_of(ToolResult))
    result, = engine.sent_of(ToolResult)
    assert result.call_id == "view-1" and result.images == (PIXEL,)
    # The image row's caption travels as context; the pixels do not travel twice.
    assert [c.text for c in engine.sent_of(ContextItem) if c.source == "tool_batch:user"] == ["[Image attached: pixel.png]"]
    engine.push(ToolCall("plain-2", "web_search", "{}"))
    assert _wait_for(lambda: len(engine.sent_of(ToolResult)) == 2)
    assert engine.sent_of(ToolResult)[1].images == ()
    # A reopened session gets the image on the user row of its history view, never dropped.
    engine.end_stream()
    assert _wait_for(lambda: len(h.llm.engines) == 2 and h.engine().config is not None)
    history = h.engine().config.history
    image_rows = [row for row in history if isinstance(row["content"], list)]
    assert len(image_rows) == 1 and image_rows[0]["content"][1]["type"] == "image_url"
    h.stop()
    h.finish()


def test_pixels_with_no_result_to_carry_them_are_a_named_omission(harness):
    h = harness()

    def hook(call, messages, schemas):
        messages.append(_image_row("[Image attached: orphan.png]"))  # the tool never wrote its own row

    h.tool_hook = hook
    engine = h.start()
    engine.push(ToolCall("view-orphan", "web_search", "{}"))
    assert _wait_for(lambda: any(c.source == "tool_batch:images_omitted" for c in engine.sent_of(ContextItem)))
    omitted, = [c for c in engine.sent_of(ContextItem) if c.source == "tool_batch:images_omitted"]
    assert "1 image(s)" in omitted.text and engine.sent_of(ToolResult) == []
    h.stop()
    h.finish()


def test_a_refused_tool_result_becomes_a_typed_failure_the_model_hears(harness, monkeypatch):
    from ouroboros.gateways.realtime_transport import LiveEngineRefusal

    h = harness()
    engine = h.start()
    original = FakeEngine.send

    async def refusing_send(self, command):
        if isinstance(command, ToolResult) and command.output.startswith("huge"):
            raise LiveEngineRefusal("wire_size", "Message of 9000000 bytes exceeds the 8388608-byte frame limit.")
        await original(self, command)

    monkeypatch.setattr(FakeEngine, "send", refusing_send)

    def hook(call, messages, schemas):
        messages.append(_image_row("[Image attached: big.png]"))
        messages.append({"role": "tool", "tool_call_id": call["id"], "name": "web_search", "content": "huge result"})

    h.tool_hook = hook
    engine.push(ToolCall("big-1", "web_search", "{}"))
    assert _wait_for(lambda: engine.sent_of(ToolResult))
    notice, = engine.sent_of(ToolResult)
    assert notice.call_id == "big-1" and notice.images == ()
    assert notice.output.startswith("⚠️ TOOL_RESULT_REFUSED (wire_size)")
    assert "11-character text and 1 image(s)" in notice.output and "frame limit" in notice.output
    assert h.thread.is_alive()  # the session continues
    h.stop()
    h.finish()


def test_a_refused_result_for_an_unknown_call_travels_as_context(harness, monkeypatch):
    from ouroboros.gateways.realtime_transport import LiveEngineRefusal

    h = harness()
    engine = h.start()
    original = FakeEngine.send

    async def refusing_send(self, command):
        if isinstance(command, ToolResult):
            raise LiveEngineRefusal("unknown_call", "No pending function call with this id.")
        await original(self, command)

    monkeypatch.setattr(FakeEngine, "send", refusing_send)
    engine.push(ToolCall("gone-1", "web_search", "{}"))
    assert _wait_for(lambda: any(c.source == "tool_result_refused" for c in engine.sent_of(ContextItem)))
    assert "TOOL_RESULT_REFUSED (unknown_call)" in [c for c in engine.sent_of(ContextItem) if c.source == "tool_result_refused"][0].text
    h.stop()
    h.finish()


def test_what_the_room_said_is_part_of_the_transcript_a_reopened_session_replays(harness, tmp_path):
    from ouroboros.live_awareness import notify_chat_row
    from ouroboros.utils import utc_now_iso

    h = harness()
    engine = h.start()
    phrase = "The meeting moved to Thursday at 14:00."
    notify_chat_row(tmp_path, {"chat_id": 1, "direction": "in", "text": phrase, "ts": utc_now_iso(),
                               "client_message_id": "typed-during-call", "source": "web"})
    assert _wait_for(lambda: any(phrase in c.text for c in engine.sent_of(ContextItem)))
    h.hub.control("context", text="slide 3 is on screen", source="screen")
    assert _wait_for(lambda: any(c.text == "slide 3 is on screen" for c in engine.sent_of(ContextItem)))
    engine.push(GoAway(time_left_s=30))
    assert _wait_for(lambda: len(h.llm.engines) == 2 and h.engine().config is not None)
    history = h.engine().config.history
    assert any(phrase in str(row["content"]) for row in history)
    assert any(row["content"] == "slide 3 is on screen" for row in history)
    # Replayed in history, delivered once: the second session is not sent them again as items.
    time.sleep(0.3)
    assert not any(phrase in c.text for c in h.engine().sent_of(ContextItem))
    rows = [m for m in h.messages if m.get("_context_source")]
    assert {m["_context_source"] for m in rows} >= {"peer:owner:screen"} and all(m["_call_row"] for m in rows)
    h.stop()
    h.finish()


def test_a_room_row_between_context_capture_and_the_listener_is_delivered(harness, tmp_path):
    from supervisor.message_bus import log_chat

    h = harness()  # the turn's context is captured here; the driver has not started
    phrase = "The owner added this while the call context was being prepared."
    assert log_chat("in", 1, 1, phrase, drive_root=tmp_path, source="web") is not None
    time.sleep(0.02)
    engine = h.start()
    assert _wait_for(lambda: any(phrase in c.text for c in engine.sent_of(ContextItem)))
    h.stop()
    h.finish()


def test_the_feed_does_not_catch_up_from_before_the_context_was_captured(harness, tmp_path):
    from supervisor.message_bus import log_chat
    from ouroboros.utils import utc_now_iso

    assert log_chat("in", 1, 1, "already in the captured context", drive_root=tmp_path, source="web") is not None
    time.sleep(0.02)
    h = harness()
    h.context_captured_at = utc_now_iso()
    time.sleep(0.02)
    engine = h.start()
    assert _wait_for(lambda: (STATE_LIVE, "") in h.hub.states)
    time.sleep(0.4)
    assert not any("already in the captured context" in c.text for c in engine.sent_of(ContextItem))
    h.stop()
    h.finish()


# --- while the connection is pending ---------------------------------------------------


def _slow_opening(monkeypatch):
    original = FakeEngine.open

    async def slow_open(self, config):
        await original(self, config)
        self.release_open = asyncio.Event()
        await self.release_open.wait()

    monkeypatch.setattr(FakeEngine, "open", slow_open)


def test_stop_during_the_connection_cancels_the_opening_and_closes_what_was_opened(harness, monkeypatch):
    _slow_opening(monkeypatch)
    h = harness()
    engine = h.start()
    assert h.hub.states == [(STATE_CONNECTING, "")]
    stopped_at = time.monotonic()
    h.stop()
    # Acted on within one mailbox tick, while the provider connection is still pending.
    assert _wait_for(lambda: engine.closed, timeout=1.0)
    assert time.monotonic() - stopped_at < 1.0 and (STATE_CLOSING, "owner_stop") in h.hub.states
    text, usage, _trace = h.finish()
    assert usage["live_call"]["ended"] == "owner_stop" and "execution_status" not in usage
    assert engine.closed == ["cancelled"] and engine.sent == []  # no session is paid for
    assert (STATE_LIVE, "") not in h.hub.states and h.hub.closed == ["owner_stop"]
    assert h.ledger.reserved == [] and h.ledger.abandoned == []



def test_a_stop_beside_typed_words_closes_without_waiting_for_their_send(harness, monkeypatch, tmp_path):
    from ouroboros.owner_mailbox import KIND_FINALIZE_NOW, write_owner_message

    original = FakeEngine.send

    async def held_send(self, command):  # a transport waiting to be ready again holds this send
        if isinstance(command, ContextItem) and command.source == "mailbox":
            await asyncio.Event().wait()
        await original(self, command)

    monkeypatch.setattr(FakeEngine, "send", held_send)
    assert write_owner_message(tmp_path, "one more thing", "t-call", msg_id="owner-typed-2")
    assert write_owner_message(tmp_path, "stop", "t-call", msg_id="owner-stop", kind=KIND_FINALIZE_NOW)
    h = harness()
    engine = h.start()
    text, usage, _trace = h.finish(timeout=10.0)
    assert usage["live_call"]["ended"] == "owner_stop"
    assert not [c for c in engine.sent_of(ContextItem) if c.source == "mailbox"]

def test_joins_leaves_and_context_while_connecting_wait_for_the_live_session(harness, monkeypatch):
    _slow_opening(monkeypatch)
    h = harness()
    engine = h.start()
    h.hub.present = []
    h.hub.feed(PeerLeft(peer_id="owner-1"))
    time.sleep(0.05)
    h.hub.present = [OWNER]
    h.hub.feed(PeerJoined(peer=OWNER))
    h.hub.control("context", text="seen before the session opened", source="screen")
    h.hub.feed(PeerAudio(peer_id="owner-1", role="owner", pcm=b"\x01" * 20))  # transient: not queued
    assert _wait_for(lambda: any(m.get("_context_source") == "peer:owner:screen" for m in h.messages))
    assert engine.sent == [] and engine.config.history == (
        {"role": "user", "content": "[Live call] A live voice call is open in this room."},)
    engine._loop.call_soon_threadsafe(engine.release_open.set)
    assert _wait_for(lambda: engine.sent_of(ContextItem))
    assert engine.sent_of(ContextItem) == [ContextItem(text="seen before the session opened", source="peer:owner:screen")]
    assert engine.sent_of(AudioIn) == [] and (STATE_LIVE, "") in h.hub.states
    h.stop()
    h.finish()


def test_typed_words_while_connecting_are_queued_and_acknowledged_only_once_live(harness, monkeypatch, tmp_path):
    from ouroboros.owner_mailbox import drain_owner_entries, write_owner_message

    _slow_opening(monkeypatch)
    h = harness()
    engine = h.start()
    assert write_owner_message(tmp_path, "typed while connecting", "t-call", msg_id="owner-early-1")
    assert _wait_for(lambda: any("typed while connecting" in str(m.get("content")) for m in h.messages))
    time.sleep(0.3)
    assert engine.sent == []
    assert list(drain_owner_entries(tmp_path, "t-call", set(), 1)) != []  # nothing acknowledged while no engine is live
    engine._loop.call_soon_threadsafe(engine.release_open.set)
    assert _wait_for(lambda: [c for c in engine.sent_of(ContextItem) if c.source == "mailbox"])
    assert "typed while connecting" in engine.sent_of(ContextItem)[0].text
    assert _wait_for(lambda: list(drain_owner_entries(tmp_path, "t-call", set(), 1)) == [])
    h.stop()
    h.finish()


# --- provider call ids, rounds and who spoke ----------------------------------------------


def test_a_repeated_request_for_a_running_call_joins_it_and_runs_the_effect_once(harness):
    entered, release = threading.Event(), threading.Event()
    h = harness()

    def effect(call, messages, schemas):
        entered.set()
        assert release.wait(5)
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": "effect complete"})

    h.tool_hook = effect
    first = h.start()
    first.push(ToolCall("one-physical-effect", "web_search", "{}"))
    assert entered.wait(3)
    first.push(GoAway(time_left_s=30))
    assert _wait_for(lambda: len(h.llm.engines) == 2 and h.engine().config is not None)
    second = h.engine()
    second.push(ToolCall("one-physical-effect", "web_search", "{}"))  # the new session asks again
    time.sleep(0.15)
    release.set()
    assert _wait_for(lambda: second.sent_of(ToolResult))
    assert second.sent_of(ToolResult)[0].output == "effect complete" and first.sent_of(ToolResult) == []
    time.sleep(0.2)
    assert h.batches == [["one-physical-effect"]]
    h.stop()
    h.finish()


def test_a_presence_calls_autonomous_rounds_are_capped_per_input_and_reset_by_speech(harness):
    h = harness(spec=_spec(presence_binding_id="b" * 32, opened_by="skill"))
    h.ctx.inline_max_rounds = 1
    engine = h.start()
    engine.push(ToolCall("round-1", "web_search", "{}"))
    assert _wait_for(lambda: len(engine.sent_of(ToolResult)) == 1)
    engine.push(ToolCall("round-2", "web_search", "{}"))  # no participant input in between
    assert _wait_for(lambda: len(engine.sent_of(ToolResult)) == 2)
    refused = engine.sent_of(ToolResult)[1]
    assert refused.call_id == "round-2" and refused.output.startswith("⚠️ TOOL_ROUND_LIMIT")
    assert "1 autonomous tool round(s)" in refused.output and h.batches == [["round-1"]]
    assert h.thread.is_alive() and (STATE_CLOSING, "") not in h.hub.states  # never a session close
    engine.push(Caption(speaker=SPEAKER_OWNER, text="and the budget?", final=True, item_id="u1"))
    assert _wait_for(lambda: h.messages[-1].get("content") == "[Heard in the call] and the budget?")
    engine.push(ToolCall("round-3", "web_search", "{}"))
    assert _wait_for(lambda: len(h.batches) == 2)
    assert h.batches == [["round-1"], ["round-3"]]
    h.stop()
    h.finish()


def test_an_owner_call_has_no_round_ceiling(harness):
    h = harness()
    h.ctx.inline_max_rounds = 1
    engine = h.start()
    for call_id in ("r1", "r2", "r3"):
        engine.push(ToolCall(call_id, "web_search", "{}"))
    assert _wait_for(lambda: len(engine.sent_of(ToolResult)) == 3)
    assert not any(r.output.startswith("⚠️ TOOL_ROUND_LIMIT") for r in engine.sent_of(ToolResult))
    h.stop()
    h.finish()


def test_meeting_audio_already_sent_makes_the_delayed_caption_heard_not_owner_words(harness):
    h = harness()
    engine = h.start()
    h.hub.feed(PeerJoined(PeerInfo("meeting-audio", "meeting", True, False)))
    h.hub.feed(PeerAudio("meeting-audio", "meeting", b"\x01\x00" * 240))
    assert _wait_for(lambda: engine.sent_of(AudioIn))
    h.hub.feed(PeerLeft("meeting-audio", "listen_stop"))
    h.hub.control("context", text="after-leave-marker")
    assert _wait_for(lambda: any(c.text == "after-leave-marker" for c in engine.sent_of(ContextItem)))
    engine.push(Caption(SPEAKER_OWNER, "words the room said before the capture stopped", True, "delayed"))
    assert _wait_for(lambda: any("words the room said" in str(m.get("content")) for m in h.messages))
    row, = [m for m in h.messages if "words the room said" in str(m.get("content"))]
    assert row["content"].startswith("[Heard in the call]") and h.chat_rows() == []
    # The audio that fed the engine is spent: the owner's next words are the owner's.
    engine.push(Caption(SPEAKER_OWNER, "thanks", True, "u2"))
    assert _wait_for(lambda: len(h.chat_rows()) == 1) and h.chat_rows()[0]["text"] == "thanks"
    h.stop()
    h.finish()


def test_barge_in_leaves_a_finished_fully_played_item_alone(harness):
    h = harness()
    engine = h.start()
    engine.push(AudioOut(bytes(48000), 24000, "done"))        # one second, finished and heard whole
    engine.push(Caption(SPEAKER_OUROBOROS, "A finished sentence.", True, "done"))
    engine.push(AudioOut(bytes(48000), 24000, "live"))        # the next item, still playing
    engine.push(Caption(SPEAKER_OUROBOROS, "", False, "live"))
    h.hub.control("played", item_id="done", played_ms=1000)
    h.hub.control("played", item_id="live", played_ms=200)
    assert _wait_for(lambda: len(engine.sent_of(PlayedAudio)) == 2)
    assert _wait_for(lambda: len(h.progress) == 1)
    assert h.progress[0][1]["_progress_meta_override"]["spoken"]["status"] == "played"
    engine.push(SpeechStarted())
    assert _wait_for(lambda: engine.sent_of(Interrupt))
    assert engine.sent_of(Interrupt) == [Interrupt("live", 200)]
    engine.push(SpeechStarted())  # the owner's next turn: nothing finished is cut again
    time.sleep(0.1)
    assert engine.sent_of(Interrupt) == [Interrupt("live", 200)]
    h.stop()
    h.finish()


def test_barge_in_still_cuts_an_item_whose_playback_caught_up_while_it_generates(harness):
    h = harness()
    engine = h.start()
    engine.push(AudioOut(bytes(4800), 24000, "gen"))  # 100 ms delivered so far, no final caption yet
    h.hub.control("played", item_id="gen", played_ms=100)
    assert _wait_for(lambda: engine.sent_of(PlayedAudio))
    engine.push(SpeechStarted())
    assert _wait_for(lambda: engine.sent_of(Interrupt))
    assert engine.sent_of(Interrupt) == [Interrupt("gen", 100)]
    engine.push(AudioOut(bytes(4800), 24000, "gen"))  # late frames of the cut item are not played
    time.sleep(0.1)
    assert len(h.hub.played) == 1
    h.stop()
    h.finish()


# --- the ledger: unknown stays unknown, refusals keep the charge, no fetch on the loop ------


def test_the_quote_is_warmed_on_the_turn_thread_before_the_loop_starts(harness):
    h = harness()
    h.start()
    assert _wait_for(lambda: h.ledger.warmed)
    (fields, thread_name), = h.ledger.warmed
    assert fields == {"engine": ENGINE_OPENAI, "model": "gpt-realtime-2.1", "drive_root": h.root}
    assert thread_name == "call-under-test"
    h.stop()
    h.finish()


def test_a_budget_refused_response_the_provider_billed_keeps_its_charge(harness):
    h = harness()
    h.ledger.refuse = True
    engine = h.start()
    engine.push(ResponseDone(item_id="a1", status="completed", usage={"audio_output_tokens": 20},
                             raw_usage={"output_tokens": 20}, response_id="r1"))
    h.thread.join(15)
    assert not h.thread.is_alive() and isinstance(h.error, BudgetExceeded)
    refused, = h.ledger.refused
    assert refused["response_key"] == "r1" and refused["status"] == "completed"
    assert refused["usage"] == {"audio_output_tokens": 20} and refused["raw_usage"] == {"output_tokens": 20}
    assert refused["reason"] == "the call's budget is spent" and refused["call_id"] == "call-1"


# --- the route's known instruction ceiling shapes the first open ------------------------------


def _ladder(h, full, addressed, nano=None):
    from ouroboros.context_fit import ContextFitPlan, ContextFitProjection

    projection = lambda mode, text: ContextFitProjection(mode, json.dumps(text), len(text) // 4, len(text) // 4, 1)
    h.ctx.context_fit_plan = ContextFitPlan("capture", "max", "max", "model", "openai", "route", "asserted",
        False, 128000, 32000, json.dumps("original input"), projection("max", full), projection("low", addressed),
        nano_projection=projection("nano", nano if nano is not None else addressed))
    h.messages[0]["content"] = full


def test_the_first_open_uses_a_projection_that_fits_the_known_instruction_ceiling(harness):
    h = harness()
    core = "Protected tier zero. "
    full = core + "Reference text. " * 20_000           # ~80k estimated tokens, above the 65,536 ceiling
    addressed = core + "Read the full reference source."
    _ladder(h, full, addressed)
    engine = h.start()
    assert engine.config.instructions == addressed and len(h.llm.engines) == 1
    assert h.ctx.active_context_mode == "low" and engine.closed == []
    h.stop()
    _, _usage, trace = h.finish()
    step, = trace["live_context_prefit"]
    assert step["basis"] == "estimate" and step["limit_tokens"] == 65_536 and step["input_tokens"] > 65_536
    assert (step["from"], step["to"]) == ("max", "low") and "live_context_recovery" not in trace


def test_a_projection_under_the_ceiling_opens_as_prepared_and_an_unknown_route_has_no_prefit(harness):
    h = harness()
    full = "Protected tier zero. " + "Reference text. " * 2_000  # well under the ceiling
    _ladder(h, full, "addressed")
    engine = h.start()
    assert engine.config.instructions == full and h.ctx.active_context_mode == "max"
    h.stop()
    _, _usage, trace = h.finish()
    assert "live_context_prefit" not in trace
    g = harness(spec=_spec(engine=ENGINE_GOOGLE, model="gemini-3.8-live", voice="Kore"))
    huge = "Protected tier zero. " + "Reference text. " * 20_000
    _ladder(g, huge, "addressed")
    engine = g.start()
    assert engine.config.instructions == huge  # no documented instruction ceiling: nothing is pre-fitted
    g.stop()
    g.finish()


def test_an_estimate_above_the_smallest_projection_never_refuses_the_open(harness):
    h = harness()
    full = "Protected tier zero. " + "Reference text. " * 20_000
    low = "Protected tier zero. " + "Reference text. " * 18_000
    nano = "Protected tier zero. " + "Reference text. " * 17_000  # the ladder's floor is itself above the ceiling
    _ladder(h, full, low, nano)
    engine = h.start()
    assert engine.config.instructions == nano and h.ctx.active_context_mode == "nano"
    h.stop()
    _, _usage, trace = h.finish()
    assert [(s["from"], s["to"]) for s in trace["live_context_prefit"]] == [("max", "low"), ("low", "nano")]


def test_a_slow_ledger_write_never_holds_the_audio_and_settles_after_its_reservation(harness):
    h = harness()
    engine = h.start()
    gate = threading.Event()
    reserve = h.ledger.reserve_live_response

    def slow_reserve(**fields):  # a store busy with another writer
        gate.wait(5)
        return reserve(**fields)

    h.ledger.reserve_live_response = slow_reserve
    engine.push(ResponseStarted(item_id="a1", response_id="r1"))
    engine.push(AudioOut(bytes(4800), 24000, "a1"))
    engine.push(ResponseDone(item_id="a1", status="completed", usage={"audio_output_tokens": 3}, response_id="r1"))
    assert _wait_for(lambda: len(h.hub.played) == 1, timeout=2.0)  # the audio flows while the write waits
    assert h.ledger.reserved == [] and h.ledger.settled == []
    gate.set()
    assert _wait_for(lambda: h.ledger.settled == [("r1", "completed", {"audio_output_tokens": 3})])
    assert [attempt["key"] for attempt in h.ledger.reserved] == ["r1"]
    h.stop()
    h.finish()
