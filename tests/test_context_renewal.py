"""The refusal path's floor: the same task continues in a fresh view (owner Q1=A, 17.10.2026)."""

from __future__ import annotations

import json
import pathlib
from dataclasses import replace
from types import SimpleNamespace

from ouroboros import context_compaction as cc
from ouroboros.artifacts import read_actor_source_bytes
from ouroboros.context_budget import HOST_CONTEXT_KIND_KEY, OWNER_WORDS_KIND, bound_host_row
from ouroboros.context_renewal import RENEWAL_RECORD_NAME, renew_after_refusal, renewal_seed
from ouroboros.loop_messages import CONTEXT_FACTS_NAME
from ouroboros.main_context_authority import PREDECESSOR_NOTES_NAME
from tests.test_context_attention_mind import _owner, _tool, _transcript
from tests.test_context_reclaim_materializer import _request

OWNER_LATER = "Owner: also keep the CLI flags."
NOTE = "I verified the migration on both code paths; the old API stays."


def _noted(tmp_path):
    """A real transcript with one authored note (the mind's own fold), then later work."""
    messages = _transcript()
    request = replace(_request(messages, 0), working_note=NOTE,
                      expected_view_revision=cc.context_reclaim_transcript_sha256(messages), keep_unit_ids=())
    folded, receipt, _ = cc.compact_tool_history_llm(messages, request=request, observed_messages=messages,
                                                     tool_schemas=[], drive_root=tmp_path, task_id="renewal",
                                                     fit_candidate=lambda _m, _t: {"accepted": True})
    assert receipt.status == "applied"
    return [*folded,
            {"role": "user", HOST_CONTEXT_KIND_KEY: PREDECESSOR_NOTES_NAME, "content": "[Predecessor notes] earlier work"},
            bound_host_row(CONTEXT_FACTS_NAME, "[CONTEXT_FACTS] round 9"),
            *_tool("three", "big body " * 2000),
            {"role": "assistant", "content": "My reasoning since the note."},
            _owner(OWNER_LATER),
            {"role": "user", "content": "A plain notice delivered before typing."}]


def test_the_seed_keeps_people_notes_and_recent_reasoning_and_addresses_the_rest(tmp_path):
    messages = _noted(tmp_path)
    seed, left = renewal_seed(messages)
    texts = [json.dumps(row.get("content"), ensure_ascii=False) for row in seed]
    assert seed[0] == messages[0] and seed[1] == messages[1]  # the system view and the assignment
    assert _owner(OWNER_LATER) in seed and any(NOTE in text for text in texts)  # people's words and my note
    assert any(row.get(HOST_CONTEXT_KIND_KEY) == PREDECESSOR_NOTES_NAME for row in seed)
    assert {"role": "assistant", "content": "My reasoning since the note."} in seed
    assert {"role": "user", "content": "A plain notice delivered before typing."} in seed
    assert not any(row.get("role") == "tool" or row.get("tool_calls") for row in seed)  # bodies by address
    assert not any(row.get(HOST_CONTEXT_KIND_KEY) == CONTEXT_FACTS_NAME for row in seed)  # host state is re-sent fresh
    assert left["tool_rows"] >= 2 and left["host_rows"] >= 1


def test_renewal_publishes_the_seed_with_an_exact_source_and_continues_the_same_task(tmp_path):
    messages = _noted(tmp_path)
    original = json.loads(json.dumps(messages))
    events = []
    tools = SimpleNamespace(_ctx=SimpleNamespace(messages=messages))
    limit_ctx = SimpleNamespace(messages=messages, accumulated_usage={"_last_llm_error_kind": "context_overflow"},
                                drive_root=tmp_path, drive_logs=tmp_path / "logs", task_id="renewal", round_idx=12,
                                event_queue=None, tool_schemas=[])
    import ouroboros.loop as loop
    loop_emit = loop._emit_checkpoint_event
    loop._emit_checkpoint_event = lambda *_a, **_kw: events.append(_a[-1])
    try:
        assert renew_after_refusal(limit_ctx, tools) is True
    finally:
        loop._emit_checkpoint_event = loop_emit
    record = messages[-1]
    assert record[HOST_CONTEXT_KIND_KEY] == RENEWAL_RECORD_NAME and "nothing summarized" in record["content"]
    assert tools._ctx.messages is messages and len(messages) < len(original)  # published in place, same task
    source = json.loads(record["content"].rsplit("rows: ", 1)[1].rstrip("."))
    assert json.loads(read_actor_source_bytes(pathlib.Path(tmp_path), "renewal", source)) == original
    assert events and events[0]["checkpoint_kind"] == "context_view_renewed" and events[0]["owner_visible"]


def test_renewal_is_not_attempted_when_it_cannot_help_or_the_failure_is_not_size(tmp_path):
    def ctx(messages, kind="context_overflow"):
        return SimpleNamespace(messages=messages, accumulated_usage={"_last_llm_error_kind": kind},
                               drive_root=tmp_path, drive_logs=tmp_path / "logs", task_id="renewal", round_idx=3,
                               event_queue=None, tool_schemas=[])

    tools = SimpleNamespace(_ctx=SimpleNamespace())
    assert renew_after_refusal(ctx(_noted(tmp_path), kind="provider_unavailable"), tools) is False
    only_host = [{"role": "system", "content": "S"}, {"role": "user", "content": "go"},
                 bound_host_row(CONTEXT_FACTS_NAME, "[CONTEXT_FACTS] round 1"),
                 {"role": "user", HOST_CONTEXT_KIND_KEY: OWNER_WORDS_KIND, "content": "A"}]
    before = list(only_host)
    assert renew_after_refusal(ctx(only_host), tools) is False and only_host == before  # nothing substantive leaves
