"""Regression tests for skill authoring / repair guardrails."""

import json
from types import SimpleNamespace
import queue

from ouroboros import loop as loop_mod
from ouroboros.contracts.task_constraint import TaskConstraint, normalize_task_constraint
from ouroboros.tool_access import active_tool_profile
from ouroboros.utils import sanitize_tool_args_for_log


def test_selected_skill_is_resource_context_not_a_reduced_profile():
    messages = [{"role": "user", "content": "Please run tests for task_constraint handling"}]
    plain = SimpleNamespace(messages=messages, task_constraint=None)

    constraint = TaskConstraint(mode="skill_repair", skill_name="target", payload_root="skills/external/target")
    assert constraint.has_selected_skill
    assert active_tool_profile(SimpleNamespace(messages=messages, task_constraint=constraint)) == active_tool_profile(plain)


def test_normalize_task_constraint_from_command_payload():
    constraint = normalize_task_constraint({
        "mode": "skill_repair",
        "skill_name": "target",
        "payload_root": "skills/external/target",
        "allow_enable": False,
    })
    assert constraint.mode == "skill_repair"
    assert constraint.skill_name == "target"
    assert constraint.payload_root == "skills/external/target"
    assert constraint.allow_enable is False


def test_normalize_task_constraint_strict_bool_and_local_readonly_canonicalization():
    repair = normalize_task_constraint({
        "mode": "skill_repair",
        "skill_name": "target",
        "payload_root": "skills/external/target",
        "allow_enable": "false",
        "allow_review": "0",
    })
    assert repair.allow_enable is False
    assert repair.allow_review is False

    readonly = normalize_task_constraint({
        "mode": "local_readonly_subagent",
        "skill_name": "ignored",
        "payload_root": "skills/external/ignored",
        "allow_enable": "true",
        "allow_review": "true",
    })
    assert readonly.mode == "local_readonly_subagent"
    assert readonly.skill_name == ""
    assert readonly.payload_root == ""
    assert readonly.allow_enable is False
    assert readonly.allow_review is False


def test_long_tool_args_log_as_placeholder_not_content_object():
    args = {"path": "skills/external/demo/plugin.py", "content": "x" * 4000}

    sanitized = sanitize_tool_args_for_log("write_file", args, threshold=100)

    assert isinstance(sanitized["content"], str)
    assert sanitized["content"].startswith("<TRUNCATED:content:")
    assert "content_len" not in sanitized


def test_skill_finalization_rearms_after_tool_round(monkeypatch, tmp_path):
    calls = iter([
        ({"content": "done", "tool_calls": []}, {}),
        ({"content": "", "tool_calls": [{"id": "c1", "function": {"name": "noop", "arguments": "{}"}}]}, {}),
        ({"content": json.dumps({
            "delivery_control": "replace",
            "full_answer": "done again",
        }), "tool_calls": []}, {}),
        ({"content": json.dumps({
            "delivery_control": "replace",
            "full_answer": "final",
        }), "tool_calls": []}, {}),
    ])
    progress = []
    seen_message_tails = []
    seen_messages = []

    class _Tools:
        CODE_TOOLS = set()

        def __init__(self):
            self._ctx = SimpleNamespace(
                event_queue=None,
                drive_root=tmp_path,
                task_id="task",
                messages=[],
                active_model_override=None,
                active_use_local_override=None,
                active_effort_override=None,
                _skill_finalization_injected=False,
            )

        def schemas(self):
            return [{"type": "function", "function": {"name": "noop", "description": "", "parameters": {}}}]

        def get_timeout(self, _name):
            return 1

        def execute(self, _name, _args):
            return "OK"

        def execute_result(self, name, args):
            # Typed dispatch seam (D02): adapt like the real registry.
            from ouroboros.tools.tool_result import LegacyTextResultAdapter

            return LegacyTextResultAdapter.from_text(name, self.execute(name, args))

        def override_handler(self, _name, _handler):
            return None

    class _LLM:
        def default_model(self):
            return "test-model"

    monkeypatch.setenv("OUROBOROS_MAX_ROUNDS", "6")
    monkeypatch.setattr(loop_mod, "_skill_finalization_message", lambda *_args, **_kwargs: "SKILL_NOT_FINALIZED")
    def fake_call(_llm, messages, *_args, **_kwargs):
        seen_message_tails.append([m.get("role") for m in messages[-3:]])
        seen_messages.append([dict(m) for m in messages])
        return next(calls)

    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)

    result, _usage, trace = loop_mod.run_llm_loop(
        [{"role": "user", "content": "create skill"}],
        _Tools(),
        _LLM(),
        tmp_path,
        lambda text, *, incident=None: progress.append(text),
        queue.Queue(),
        task_id="task",
        drive_root=tmp_path,
    )

    assert result == "final"
    assert len(seen_messages) == 4
    assert progress.count("SKILL_NOT_FINALIZED") == 2
    assert trace["reasoning_notes"].count("SKILL_NOT_FINALIZED") == 2
    assert trace["delivery_candidate"]["revision"] == 3
    assert trace["delivery_candidate"]["finalization_control"] == "replace"
    assert any(
        "[DELIVERY_FINALIZATION_CONTROL]" in str(message.get("content") or "")
        for request in seen_messages
        for message in request
    )
    assert any(tail[-2:] == ["assistant", "user"] for tail in seen_message_tails)
    assert all(tail[-2:] != ["assistant", "system"] for tail in seen_message_tails)


def test_skill_action_and_effect_round_cannot_erase_complete_candidate(monkeypatch, tmp_path):
    original = "Complete skill delivery answer with all required details."
    responses = iter([
        ({"content": original, "tool_calls": []}, {}),
        ({"content": "", "tool_calls": [{
            "id": "finalize-1",
            "function": {"name": "finalize_skill", "arguments": "{}"},
        }]}, {}),
        ({"content": "Skill review completed.", "tool_calls": []}, {}),
        ({"content": "Everything is done now.", "tool_calls": []}, {}),
    ])
    finalized = {"value": False}
    seen_messages = []

    class _Tools:
        CODE_TOOLS = set()

        def __init__(self):
            self._ctx = SimpleNamespace(
                event_queue=None,
                drive_root=tmp_path,
                task_id="task",
                messages=[],
                active_model_override=None,
                active_use_local_override=None,
                active_effort_override=None,
                _skill_finalization_injected=False,
            )

        def schemas(self):
            return [{
                "type": "function",
                "function": {
                    "name": "finalize_skill",
                    "description": "",
                    "parameters": {},
                },
            }]

        def get_timeout(self, _name):
            return 1

        def execute(self, name, _args):
            assert name == "finalize_skill"
            finalized["value"] = True
            return "OK"

        def execute_result(self, name, args):
            # Typed dispatch seam (D02): adapt like the real registry.
            from ouroboros.tools.tool_result import LegacyTextResultAdapter

            return LegacyTextResultAdapter.from_text(name, self.execute(name, args))

        def override_handler(self, _name, _handler):
            return None

    class _LLM:
        def default_model(self):
            return "test-model"

    def fake_call(_llm, messages, *_args, **_kwargs):
        seen_messages.append([dict(message) for message in messages])
        return next(responses)

    monkeypatch.setenv("OUROBOROS_MAX_ROUNDS", "7")
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.setattr(
        loop_mod,
        "_skill_finalization_message",
        lambda *_args, **_kwargs: "" if finalized["value"] else "SKILL_NOT_FINALIZED",
    )
    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)

    result, _usage, trace = loop_mod.run_llm_loop(
        [{"role": "user", "content": "create and finalize the skill"}],
        _Tools(),
        _LLM(),
        tmp_path,
        lambda _msg, *, incident=None: None,
        queue.Queue(),
        task_id="task",
        drive_root=tmp_path,
    )

    assert finalized["value"] is True
    assert result == original
    assert len(seen_messages) == 4
    assert trace["delivery_candidate"]["revision"] == 1
    assert trace["delivery_candidate"]["finalization_control"] == "degraded_preserve"
    assert trace["delivery_candidate"]["degraded_reason"] == (
        "invalid_delivery_control_after_repair"
    )
    assert all(
        "[DELIVERY_FINALIZATION_CONTROL]" not in str(message.get("content") or "")
        for message in seen_messages[1]
    )
    assert any(
        "keep is NOT allowed" in str(message.get("content") or "")
        for message in seen_messages[2]
    )


def test_skill_finalization_empty_text_does_not_append_empty_assistant(monkeypatch, tmp_path):
    calls = iter([
        ({"content": "", "tool_calls": []}, {}),
        ({"content": "final", "tool_calls": []}, {}),
    ])
    seen_messages = []

    class _Tools:
        CODE_TOOLS = set()

        def __init__(self):
            self._ctx = SimpleNamespace(
                event_queue=None,
                drive_root=tmp_path,
                task_id="task",
                messages=[],
                active_model_override=None,
                active_use_local_override=None,
                active_effort_override=None,
                _skill_finalization_injected=False,
            )

        def schemas(self):
            return []

        def override_handler(self, _name, _handler):
            return None

    class _LLM:
        def default_model(self):
            return "test-model"

    def fake_call(_llm, messages, *_args, **_kwargs):
        seen_messages.append([dict(message) for message in messages])
        return next(calls)

    monkeypatch.setenv("OUROBOROS_MAX_ROUNDS", "3")
    monkeypatch.setattr(loop_mod, "_skill_finalization_message", lambda *_args, **_kwargs: "SKILL_NOT_FINALIZED")
    monkeypatch.setattr(loop_mod, "call_llm_with_retry", fake_call)

    result, _usage, _trace = loop_mod.run_llm_loop(
        [{"role": "user", "content": "create skill"}],
        _Tools(),
        _LLM(),
        tmp_path,
        lambda _msg, *, incident=None: None,
        queue.Queue(),
        task_id="task",
        drive_root=tmp_path,
    )

    assert result == "final"
    assert len(seen_messages) == 2
    assert seen_messages[1][-1]["role"] == "user"
    assert not any(message.get("role") == "assistant"
                   and not message.get("content") and not message.get("tool_calls")
                   for request in seen_messages for message in request)
