"""Host acceptance metadata never enters the strict model-message envelope."""
from copy import deepcopy

import pytest

from ouroboros.llm_claudexor import _request


@pytest.mark.parametrize("marker", ["acceptance_observation", "_acceptance_observation"])
def test_host_marker_is_removed_without_rewriting_content_tools_or_native_payload(marker):
    target = {"source": "codex", "resolved_model": "exact-model"}
    content = [{"type": "text", "text": "Exact acceptance evidence\r\nПривет"}]
    route = {"source": "codex", "model": "exact-model", "credentialProfileId": "personal",
             "accountFingerprint": "account"}
    messages = [{"role": "user", "content": content, marker: {"revision": "host-only"}},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "call-one", "type": "function",
                 "function": {"name": "inspect", "arguments": '{"acceptance_observation":"user argument"}'}}],
                 "nativeContinuation": {"route": route, "format": "opaque-v1", "payload": {marker: "native value"}}},
                {"role": "tool", "tool_call_id": "call-one", "content": "Exact tool result"}]
    tools = [{"type": "function", "function": {"name": "inspect", "parameters": {"type": "object",
              "properties": {marker: {"type": "string"}}}}}]
    original, original_tools = deepcopy(messages), deepcopy(tools)
    payload = _request(target, messages, tools, {})
    # ModelMessage in protocol-3 is strict at this outer object only; native
    # continuation and input/tool-schema JSON retain their own opaque fields.
    allowed = {"role", "content", "name", "tool_call_id", "tool_calls", "nativeContinuation"}
    assert all(set(message) <= allowed for message in payload["messages"])
    assert payload["messages"][0] == {"role": "user", "content": content}
    assert payload["messages"][1:] == messages[1:]
    assert payload["tools"] == tools
    assert messages == original and tools == original_tools


def test_direct_provider_projection_also_excludes_host_acceptance_metadata():
    from ouroboros.llm_messages import _MessageShapingMixin

    messages = [{"role": "user", "content": "Exact evidence", "acceptance_observation": {"revision": "host"}},
                {"role": "assistant", "content": "Answer", "_acceptance_observation": "private"}]
    original = deepcopy(messages)
    sent = _MessageShapingMixin._copy_messages_with_cache_policy(
        messages, allow_message_cache_control=False, flatten_tool_content_blocks=True)
    assert sent == [{"role": "user", "content": "Exact evidence"}, {"role": "assistant", "content": "Answer"}]
    assert messages == original
