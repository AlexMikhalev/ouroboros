"""Probe verdicts require observations, and the owner simulator preserves pacing."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from devtools import live_call_probe as probe
from ouroboros.call_peers import LiveCallHub
from ouroboros.call_protocol import CallSpec, PeerAudio
from ouroboros.gateways.realtime_engine import AudioIn, EngineError, EngineRoute, SessionConfig, ToolResult


def test_output_requires_new_external_temp_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "REPO", tmp_path / "repo")
    (tmp_path / "repo").mkdir()
    with pytest.raises(ValueError, match="outside"):
        probe.output_root(str(tmp_path / "repo" / "output"))
    with pytest.raises(ValueError, match="already exists"):
        probe.output_root(str(tmp_path))
    root = probe.output_root(str(tmp_path / "fresh"))
    assert root.is_dir()
    with pytest.raises(ValueError, match="already exists"):
        probe.output_root(str(root))


def test_scenario_selection_rejects_typos():
    assert probe.select_scenarios("opening,money") == {"opening", "money"}
    assert probe.select_scenarios("all") == set(probe.SCENARIOS)
    with pytest.raises(ValueError, match="unknown"):
        probe.select_scenarios("openning")


def attempts():
    counts = {"text_input_tokens": 12, "audio_input_tokens": 44}
    evidence = {"response_key": "response", "usage": counts, "raw_usage": {"input_tokens": 56},
                "tariff_quote": {"url": "https://catalog", "fetched_at": "2026-10-16", "sha256": "digest", "entry": {"rate": 1}}}
    rows = [{"attempt_id": "response-attempt", "source": "live_response", "state": "settled",
             "cost_evidence": evidence, "prompt_tokens": 12, "cost_usd": .1},
            {"attempt_id": "transcription-attempt", "source": "live_transcription", "state": "settled",
             "cost_evidence": {**evidence, "response_key": "transcription"}, "prompt_tokens": 12, "cost_usd": .02}]
    events = [{"kind": "ResponseStarted", "response_id": "response"}, {"kind": "ResponseDone", "response_id": "response"}]
    return rows, events


def test_money_matches_response_id_and_keeps_audio_separate():
    rows, events = attempts()
    ok, facts = probe.money_evidence(rows, events, "openai")
    assert ok and facts["total_cost_usd"] == pytest.approx(.12)
    rows[0]["prompt_tokens"] += 44
    ok, facts = probe.money_evidence(rows, events, "openai")
    assert not ok and any("audio included" in e for e in facts["errors"])


@pytest.mark.parametrize("change", ["duplicate", "missing_done", "no_transcription", "unknown_price", "no_quote", "unresolved"])
def test_money_cannot_pass_incomplete_or_double_billed_evidence(change):
    rows, events = attempts()
    if change == "duplicate":
        rows.append(dict(rows[0]))
    elif change == "missing_done":
        events.pop()
    elif change == "no_transcription":
        rows.pop()
    elif change == "unknown_price":
        rows[0]["cost_usd"] = None
    elif change == "no_quote":
        rows[0]["cost_evidence"]["tariff_quote"] = None
    else:
        rows[0]["state"] = "unresolved"
    ok, facts = probe.money_evidence(rows, events, "openai")
    assert not ok and facts["errors"]
    if change == "unknown_price":
        assert facts["total_cost_usd"] is None
        assert facts["known_estimated_usd"] == .02


def test_observer_forwards_real_commands_and_records_only_audio_lengths(tmp_path):
    class Engine:
        _write = AsyncMock()
        _handle_message = AsyncMock()
        open = AsyncMock()
        send = AsyncMock()
        close = AsyncMock()

        async def events(self):
            yield EngineError("invalid_request", "Rejected instructions")

    async def run():
        engine = Engine()
        evidence = probe.Evidence(tmp_path)
        observed = probe.ObservedEngine(engine, evidence, object())
        audio, result = AudioIn(b"\x01\x00" * 480, 24000), ToolResult("call", "weather result")
        await observed.send(audio)
        await observed.send(result)
        assert engine.send.call_args_list[0].args == (audio,)
        assert engine.send.call_args_list[1].args == (result,)
        assert len(evidence.rows) == 1 and evidence.rows[0]["output"] == "weather result"
        config = SessionConfig(EngineRoute("openai", "test"), "A complete instruction")
        await observed.open(config)
        assert engine.open.call_args.args == (config,)
        assert (tmp_path / "instructions.txt").read_text() == config.instructions
        assert evidence.of("session_config")[0]["instruction_tokens"] > 0
        assert [e.kind async for e in observed.events()] == ["invalid_request"]
        assert evidence.of("EngineError")[0]["error_kind"] == "invalid_request"

    asyncio.run(run())


def test_owner_audio_uses_real_codec_and_engine_rate(tmp_path):
    async def run():
        hub = LiveCallHub(CallSpec("call", 1, "openai", "test"))
        peer = probe.OwnerPeer(hub, probe.Evidence(tmp_path))
        # Drain the genuine mixer concurrently, as the driver does.
        received = bytearray()
        async def drain():
            while not hub.closed:
                event = await asyncio.to_thread(hub.next_event, .025)
                if isinstance(event, PeerAudio):
                    received.extend(event.pcm)
        task = asyncio.create_task(drain())
        await peer.speak(b"\x01\x00" * 480, "fixture")
        await asyncio.sleep(.05)
        hub.close("done")
        await task
        assert received == b"\x01\x00" * 480 + bytes(probe.RATE * 2)
        assert hub.stats()["input_dropped_samples"] == 0
        start = peer.evidence.of("speech_start")[0]["at"]
        end = peer.evidence.of("speech_end")[0]["at"]
        assert end - start >= .019
    asyncio.run(run())


def test_failed_open_keeps_unexecuted_scenarios_not_run(tmp_path):
    runner = probe.Probe(tmp_path, "openai", set(probe.SCENARIOS), 1)
    runner.verdict("opening", False, error="rejected by provider")
    report = json.loads((tmp_path / "result.json").read_text())
    assert report["scenarios"]["opening"]["verdict"] == "FAIL"
    assert report["scenarios"]["tool"]["verdict"] == "NOT_RUN"
    assert report["total_cost_usd"] is None
