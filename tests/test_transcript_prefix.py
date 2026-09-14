"""The transcript is append-only between the sends of one loop execution.

OpenAI-family caches (Codex backend, OpenAI API, OpenRouter->OpenAI) reuse a
previous request only when it is a byte-prefix of the next, so a transient
trailing message or an in-place rewrite of an already-sent message discards
the whole conversation cache (issue #906, measured 2026-09-14).  The unit
cases pin the recorder; the loop case pins the invariant on the real
``run_llm_loop`` with the acceptance observation actually injected.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

from ouroboros import loop, transcript_prefix
from ouroboros.transcript_prefix import CHECKPOINT_KIND, message_digest, observe_send

# The heaviest real harness that injects the per-round acceptance observation.
from tests.test_acceptance_async_loop import ANSWER, call, full_loop, keep  # noqa: F401

# ---------------------------------------------------------------------------
# message_digest: content identity, not envelope shape
# ---------------------------------------------------------------------------

def test_cache_control_marker_does_not_change_the_digest():
    plain = {"role": "user", "content": "do the work"}
    marked = {"role": "user", "content": [
        {"type": "text", "text": "do the work", "cache_control": {"type": "ephemeral"}}]}
    assert message_digest(plain) == message_digest(marked)


def test_block_and_string_tool_results_share_one_digest():
    as_string = {"role": "tool", "tool_call_id": "t1", "content": "result body"}
    as_blocks = {"role": "tool", "tool_call_id": "t1",
                 "content": [{"type": "text", "text": "result "}, {"type": "text", "text": "body"}]}
    assert message_digest(as_string) == message_digest(as_blocks)


def test_private_custody_keys_do_not_change_the_digest():
    bare = {"role": "user", "content": "observation"}
    carried = {"role": "user", "content": "observation", "acceptance_observation": True,
               "nativeContinuation": {"id": "x"}, "_context_capsule": {"a": 1},
               "_caption": "cap", "_source_path": "/tmp/x.png"}
    assert message_digest(bare) == message_digest(carried)


def test_role_text_and_tool_calls_do_change_the_digest():
    base = {"role": "user", "content": "a"}
    assert message_digest(base) != message_digest({"role": "assistant", "content": "a"})
    assert message_digest(base) != message_digest({"role": "user", "content": "b"})
    calling = {"role": "assistant", "content": "", "tool_calls": [call("read_file", {"path": "x"}, "c1")]}
    other = {"role": "assistant", "content": "", "tool_calls": [call("read_file", {"path": "y"}, "c1")]}
    assert message_digest(calling) != message_digest(other)
    assert message_digest(calling) != message_digest({"role": "assistant", "content": ""})
    assert message_digest({"role": "tool", "tool_call_id": "a", "content": "r"}) != message_digest(
        {"role": "tool", "tool_call_id": "b", "content": "r"})


# ---------------------------------------------------------------------------
# observe_send: which break, where, and whether it was sanctioned
# ---------------------------------------------------------------------------

SYSTEM = {"role": "system", "content": "Complete the owner task."}
TASK = {"role": "user", "content": "Prepare the report."}
ASSISTANT = {"role": "assistant", "content": "working"}


def test_first_send_and_pure_append_record_nothing():
    slot = SimpleNamespace()
    assert observe_send(slot, [SYSTEM, TASK], round_idx=1) is None
    assert observe_send(slot, [SYSTEM, TASK, ASSISTANT], round_idx=2) is None
    assert observe_send(slot, [SYSTEM, TASK, ASSISTANT], round_idx=3) is None
    assert getattr(slot, transcript_prefix.DIGEST_ATTR) == [
        message_digest(m) for m in (SYSTEM, TASK, ASSISTANT)]


def test_replaced_last_message_is_a_tail_replacement():
    slot = SimpleNamespace()
    observe_send(slot, [SYSTEM, TASK, ASSISTANT], round_idx=1)
    fact = observe_send(slot, [SYSTEM, TASK, {"role": "assistant", "content": "other"}], round_idx=2)
    assert fact == {"checkpoint_kind": CHECKPOINT_KIND, "round": 2, "index": 2, "kind": "tail_replaced",
                    "previous_messages": 3, "current_messages": 3, "sanctioned_by": None}


def test_changed_middle_message_is_a_rewrite():
    slot = SimpleNamespace()
    observe_send(slot, [SYSTEM, TASK, ASSISTANT], round_idx=1)
    fact = observe_send(
        slot, [SYSTEM, {"role": "user", "content": "other task"}, ASSISTANT, TASK], round_idx=2)
    assert fact["kind"] == "rewritten" and fact["index"] == 1
    assert (fact["previous_messages"], fact["current_messages"]) == (3, 4)


def test_changed_system_message_is_a_system_rewrite():
    slot = SimpleNamespace()
    observe_send(slot, [SYSTEM, TASK, ASSISTANT], round_idx=1)
    fact = observe_send(slot, [{"role": "system", "content": "reprojected"}, TASK, ASSISTANT], round_idx=4)
    assert fact["kind"] == "system_rewritten" and fact["index"] == 0 and fact["round"] == 4


def test_shorter_transcript_with_an_equal_prefix_is_a_shrink():
    slot = SimpleNamespace()
    observe_send(slot, [SYSTEM, TASK, ASSISTANT], round_idx=1)
    fact = observe_send(slot, [SYSTEM, TASK], round_idx=2)
    assert fact["kind"] == "shrunk" and fact["index"] == 3
    assert (fact["previous_messages"], fact["current_messages"]) == (3, 2)


def test_sanctioned_by_rides_through_unchanged():
    slot = SimpleNamespace()
    observe_send(slot, [SYSTEM, TASK, ASSISTANT], round_idx=1)
    fact = observe_send(slot, [SYSTEM, TASK], round_idx=2, sanctioned_by="compaction")
    assert fact["sanctioned_by"] == "compaction"


# ---------------------------------------------------------------------------
# The loop-level invariant
# ---------------------------------------------------------------------------

def _describe(message):
    calls = transcript_prefix._tool_call_identity(message.get("tool_calls"))
    return (f"role={message.get('role')!r} tool_calls={[c[1] for c in calls]!r} "
            f"text={transcript_prefix._plain_text(message.get('content'))[:120]!r}")


def _assert_prefix_chain(sends):
    for step in range(1, len(sends)):
        previous, current = sends[step - 1], sends[step]
        for index, (before, after) in enumerate(zip(previous, current)):
            assert message_digest(before) == message_digest(after), (
                f"send {step} is not a prefix extension of send {step - 1}: message {index} of "
                f"{len(previous)} was rewritten\n  before: {_describe(before)}\n  after:  {_describe(after)}")
        assert len(current) >= len(previous), (
            f"send {step} dropped messages: {len(previous)} -> {len(current)}; "
            f"last surviving message {_describe(current[-1])}")


def _prefix_breaks(events):
    rows = []
    for item in list(events.queue):
        data = item.get("data") if isinstance(item, dict) else None
        if isinstance(data, dict) and data.get("type") == "task_checkpoint" \
                and data.get("checkpoint_kind") == CHECKPOINT_KIND:
            rows.append(data)
    return rows


def _four_tool_rounds(f):
    """Script: nominate + one effect, then a read per round, then finalize."""
    def main(_llm, messages, *_args, **_kwargs):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return {"content": "The report is ready; requesting its review.", "tool_calls": [
                call("task_acceptance_review", {"claim": ANSWER}, "nominate"),
                call("write_file", {"root": "task_drive", "path": "proof.txt",
                                    "content": "effect"}, "seed"),
            ]}, 0.0
        if f.model_step in (2, 3, 4):
            if f.model_step == 2:
                assert f.entered.wait(5), f.progress
            return {"content": "", "tool_calls": [
                call("read_file", {"root": "task_drive", "path": "proof.txt"},
                     f"read-{f.model_step}")]}, 0.0
        assert f.model_step < 9, f.progress
        return keep(f), 0.0
    return main


def test_every_send_of_one_execution_extends_the_previous_send(full_loop, monkeypatch):  # noqa: F811 -- imported pytest fixture
    f = full_loop
    monkeypatch.setattr(loop, "call_llm_with_retry", _four_tool_rounds(f))

    result, usage, _trace = f.run()

    assert result == ANSWER, (result, f.progress)
    assert len(f.model_inputs) >= 5, f.model_inputs
    _assert_prefix_chain(f.model_inputs)
    assert _prefix_breaks(f.events) == []
    assert "prompt_prefix_breaks" not in usage


def test_compaction_rewrite_is_recorded_as_a_sanctioned_break(full_loop, monkeypatch):  # noqa: F811 -- imported pytest fixture
    f = full_loop
    monkeypatch.setattr(loop, "call_llm_with_retry", _four_tool_rounds(f))
    original = loop._run_round_compaction

    def compact(messages, context):
        if context.round_idx != 3:
            return original(messages, context)
        rewritten = list(messages)
        for index, message in enumerate(rewritten):
            if message.get("role") == "tool":
                rewritten[index] = {**message, "content": "[compacted tool result]"}
                break
        return rewritten, {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(loop, "_run_round_compaction", compact)

    result, usage, _trace = f.run()

    assert result == ANSWER, (result, f.progress)
    breaks = _prefix_breaks(f.events)
    assert [b["kind"] for b in breaks] == ["rewritten"], breaks
    assert breaks[0]["sanctioned_by"] == "compaction" and breaks[0]["round"] == 3
    # A sanctioned rewrite is disclosed, never counted as an unexplained break.
    assert "prompt_prefix_breaks" not in usage
