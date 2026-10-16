"""Tool calls of a live call: one serialized batch worker over the unchanged tool execution.

The realtime session asks for tools while it speaks; the turn's tools run exactly
as in a text turn (``handle_tool_calls`` over the turn's own messages, with the
same measured delivery frame), on ONE worker thread so only one batch mutates the
tool context at a time. Every message change a batch made (tool rows, images a
tool viewed, review or dialogue updates) goes back to the session typed: a tool
row as ``ToolResult`` to the session that asked, anything else as a
``ContextItem``. A result whose asking session is gone (a reconnect) or that the
provider withdrew is retained and resent as context; a call already executed is
answered from its retained result and never runs twice.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import copy
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Tuple

from ouroboros.gateways.realtime_engine import ContextItem, ToolCall, ToolResult, UpdateTools
from ouroboros.usage_accounting import BudgetExceeded

log = logging.getLogger(__name__)


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content
                         if isinstance(part, dict) and part.get("type") == "text")
    return "" if content is None else str(content)


def images_of(content: Any) -> Tuple[bytes, ...]:
    images: List[bytes] = []
    for part in content if isinstance(content, list) else ():
        url = str(((part or {}).get("image_url") or {}).get("url") or "") if isinstance(part, dict) else ""
        if url.startswith("data:") and "," in url:
            try:
                images.append(base64.b64decode(url.split(",", 1)[1]))
            except Exception:
                log.debug("undecodable inline image skipped", exc_info=True)
    return tuple(images)


class ToolBatchWorker:
    """The call's tool lane: ``call`` is the live call whose messages, tools and session it serves."""

    def __init__(self, call: Any) -> None:
        self.call = call
        self.pending: List[Tuple[int, ToolCall]] = []
        self.cancelled: set = set()
        self.executed: Dict[str, ToolResult] = {}
        self.running = False
        self.batches = 0
        self.tool_calls = 0
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"call-tools-{call.task_id}")

    def shutdown(self) -> None:
        # Wait for an idle worker; a tool still running is not waited on twice (the
        # call already gave it its settle window).
        self._pool.shutdown(wait=not self.running, cancel_futures=True)

    def enqueue(self, tool_call: ToolCall, epoch: int) -> None:
        if tool_call.call_id in self.executed:
            # The provider asked again for a call already executed (a resent request
            # after a reconnect): the retained result answers, nothing runs twice.
            asyncio.create_task(self.call._send(self.executed[tool_call.call_id]))
            return
        self.pending.append((epoch, tool_call))

    def cancel(self, call_ids: Tuple[str, ...]) -> None:
        self.cancelled.update(call_ids)
        self.pending = [(epoch, tc) for epoch, tc in self.pending if tc.call_id not in call_ids]

    async def run_forever(self) -> None:
        call = self.call
        while True:
            if not self.pending:
                await asyncio.sleep(0.02)
                continue
            batch, self.pending = self.pending, []
            self.running = True  # cleared by the worker thread itself, so a cancelled await never hides a running tool
            try:
                changes, grown = await asyncio.get_running_loop().run_in_executor(
                    self._pool, contextvars.copy_context().run, self._run_batch, [tc for _epoch, tc in batch])
            except BudgetExceeded as exc:
                call.budget_error = exc
                call._begin_close("budget_exceeded")
                return
            except Exception:
                log.warning("live call %s tool batch failed", call.spec.call_id, exc_info=True)
                changes, grown = [], False
            await self._forward_changes(changes, {tc.call_id: epoch for epoch, tc in batch})
            if grown:
                await call._send(UpdateTools(tuple(call.tool_schemas)))
            call._checkpoint()

    def _run_batch(self, calls: List[ToolCall]) -> Tuple[List[Dict[str, Any]], bool]:
        """On the worker thread: the unchanged tool execution over the turn's own messages."""
        from ouroboros import loop as loop_mod

        call = self.call
        try:
            tool_calls = [{"id": tc.call_id, "type": "function",
                           "function": {"name": tc.name, "arguments": tc.arguments}} for tc in calls]
            call.messages.append({"role": "assistant", "content": "", "tool_calls": tool_calls})
            before = copy.deepcopy(call.messages)
            names_before = {schema["function"]["name"] for schema in call.tool_schemas}
            self.batches += 1
            self.tool_calls += len(calls)
            loop_mod.handle_tool_calls(
                tool_calls, call.tools, call.drive_logs, call.task_id, call.stateful_executor, call.messages,
                call.llm_trace, call.emit_progress, tool_schemas=call.tool_schemas,
                fit_candidate=functools.partial(loop_mod._measure_tool_result_batch, call.ctx,
                                                round_id=f"{call.task_id}:call:{self.batches}"))
            changes = [msg for index, msg in enumerate(call.messages) if not msg.get("_call_row")
                       and (index >= len(before) or msg != before[index])]
            grown = {schema["function"]["name"] for schema in call.tool_schemas} != names_before
            return changes, grown
        finally:
            self.running = False

    async def _forward_changes(self, changes: List[Dict[str, Any]], epochs: Dict[str, int]) -> None:
        """Every message change the batch made goes back to the session, typed."""
        call = self.call
        for msg in changes:
            role, content = str(msg.get("role") or ""), msg.get("content")
            if role == "tool":
                call_id = str(msg.get("tool_call_id") or "")
                result = ToolResult(call_id=call_id, output=text_of(content), images=images_of(content))
                self.executed[call_id] = result
                if epochs.get(call_id) == call.epoch and call_id not in self.cancelled:
                    await call._send(result)
                else:  # the session that asked is gone or withdrew the call: the result stays as context
                    await call._send(ContextItem(text=f"[result of tool call {call_id}]\n{result.output}",
                                                 source="tool_result_retained"))
            else:
                text = text_of(content)
                if text.strip() or images_of(content):
                    await call._send(ContextItem(text=text, source=f"tool_batch:{role or 'note'}"))
