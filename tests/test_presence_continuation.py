"""#1536: the shared reviewed Presence turn harness.

The real gate (cap 1, cross-process files), Host execution registry, ``run_presence_turn``,
loop, review coordinator, direct wait, mailbox settlement and pipeline terminal run here;
only the model and the reviewer transport are scripted. Git subprocesses create the
disposable repository fixture. ``test_presence_continuation_host`` and
``test_presence_queue_observations`` build on this harness.

The scenarios that parked a Presence author at its own result's acceptance review and
handed it the conversation back left with the task-acceptance machine: Presence speech is
never held now, and a Required result review of it runs with nobody waiting and settles as
a late mark (``review_result._presence_result_review``). The early release and the
author's typed stance went with them. The continuation's seams (gate stop order, lend and
reacquire, reentry, projection and pointer) stay covered in
``test_presence_continuation_seams``.
"""

from __future__ import annotations

import contextvars
import copy
import json
import queue
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from ouroboros import agent_task_pipeline as pipeline, loop, review_substrate
from ouroboros.model_wait import task_model_wait_scope
from ouroboros.presence_runner import (
    PresenceTurnEvent, PresenceTurnExecutions, PresenceTurnGate, run_presence_turn,
)
from ouroboros.review_records import ReviewSlot
from ouroboros.task_results import write_task_result
from ouroboros.tools.registry import ToolRegistry
from tests.test_presence_runner import _admission

ANSWER = "The status report is complete: all three checks passed."
NEW_WORDS = "Please also mention the backup window."
KEY = "telegram:bot-1:room-1:topic-1"
pytestmark = pytest.mark.serial


def call(name, arguments, identifier):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def finish(identifier, **arguments):
    return {"content": None, "tool_calls": [call("presence_finish", {"outcome": "message", **arguments}, identifier)]}


def event(number=42, text="Please prepare the status report", version=1):
    return PresenceTurnEvent(
        source_event_id=f"telegram:bot-1:{number}", provider="telegram", account_id="bot-1",
        conversation_id="room-1", thread_id="topic-1", conversation_key=KEY,
        actor={"platform_actor_id": "user-7", "username": "alex"}, conversation={"title": "Community"},
        message={"message_id": str(number)}, text=text, continuation_version=version)


def wait_for(predicate, timeout=20.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """A reviewed Presence turn: a held reviewer, a scripted model, the real everything else."""
    from ouroboros import review_custody
    from ouroboros.review_execution import ReviewAttemptResult

    for name, value in {"OUROBOROS_TASK_REVIEW_MODE": "required", "OUROBOROS_REVIEW_ENFORCEMENT": "blocking",
                        "OUROBOROS_REVIEW_MAX_CYCLES": "3", "OUROBOROS_MAX_ROUNDS": "12",
                        "OUROBOROS_SAFETY_MODE": "off", "MCP_ENABLED": "false"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(loop, "_maybe_inject_finalization_nudges", lambda *_a: False)
    monkeypatch.setattr("ouroboros.tools.review_helpers.review_wave_budget_gate", lambda *_a, **_k: None)
    # One held reviewer seat is the whole review pool a result review draws from.
    slots = [ReviewSlot(slot_id="reviewer-one", model="fixture/reviewer", effort="high", timeout_sec=30)]
    monkeypatch.setattr("ouroboros.reviewer_slot_config.review_pool_slots", lambda *_a, **_kw: list(slots))
    data, repo = tmp_path / "data", tmp_path / "repo"
    data.mkdir()
    repo.mkdir()
    for args in (["init"], ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                           "commit", "--allow-empty", "-m", "fixture baseline"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    h = SimpleNamespace(data=data, repo=repo, gate=PresenceTurnGate(1, state_root=data / "state"),
                        executions=PresenceTurnExecutions(), release=threading.Event(), entered=threading.Event(),
                        verdict="PASS", reviews=[], inputs=[], script=[], calls=0, ctxs=[], progress=[],
                        lock=threading.Lock())
    settled = threading.Event()
    original_settle = review_custody._settle_review_attempt

    def settle(*a, **kw):
        try:
            return original_settle(*a, **kw)
        finally:
            settled.set()

    monkeypatch.setattr(review_custody, "_settle_review_attempt", settle)

    class HeldExecutor:
        def __init__(self, assignment):
            self.assignment = assignment

        def restore_custody(self, _state):
            return None

        def set_pending_invocation_checkpoint(self, _checkpoint):
            return None

        def prompt_payload(self):
            return {"messages": []}

        def prompt_chars(self):
            return 0

        def failure_custody(self):
            return {}

        def execute(self):
            from ouroboros.review_dispatch import invoke_review_paid_stamp

            invoke_review_paid_stamp(self.assignment.dispatch_stamp)
            h.reviews.append(copy.deepcopy(self.assignment.request))
            h.entered.set()
            assert h.release.wait(30), "the test never released the reviewer"
            # review_change's answer contract: an empty array is a clean answer.
            text = "[]" if h.verdict == "PASS" else json.dumps([{
                "item": "backup_window", "verdict": "FAIL", "severity": "critical",
                "reason": "The status report should mention the backup window."}])
            return ReviewAttemptResult(message={"content": text}, raw_text=text,
                                       usage={"prompt_tokens": 5, "completion_tokens": 3,
                                              "physical_attempt_state": "settled"})

    monkeypatch.setattr(review_substrate, "_review_route_executor", lambda assignment, **_kw: HeldExecutor(assignment))

    def model(_llm, messages, *_a, **_kwargs):
        h.inputs.append(copy.deepcopy(messages))
        with h.lock:
            h.calls += 1
            step = h.script.pop(0) if isinstance(h.script, list) else h.script
        response = step(messages) if callable(step) else step
        return response, 0.0

    monkeypatch.setattr(loop, "call_llm_with_retry", model)

    class Agent:
        def handle_task(self, task):
            registry = ToolRegistry(repo_dir=repo, drive_root=data)
            ctx = registry._ctx
            ctx.is_direct_chat = True
            ctx.task_metadata = dict(task["metadata"])
            ctx.task_contract = {**task["task_contract"], "expected_output": "A complete status report."}
            # The agent's own RUNNING write: the contract is the root's review-cap authority.
            write_task_result(data, task["id"], "running", task_contract=ctx.task_contract, root_task_id=task["id"])
            # Required reviews the report the turn retained (#1731); a declared output alone buys no review.
            from ouroboros.artifacts import store_task_artifact_bytes
            store_task_artifact_bytes(data, task["id"], "report.txt", ANSWER.encode())
            ctx.task_attempt, ctx.current_chat_id = 1, task["chat_id"]
            ctx.review_wait_callback = getattr(self, "review_wait_callback", None)
            registry.override_handler("chat_history", lambda *_a, **_kw: "Synthetic history")
            h.ctxs.append(ctx)
            task["_skip_post_task_synthesis"] = True
            with task_model_wait_scope(task=task, drive_root=data, event_queue=None, worker_slot_held=False) as waiter:
                ctx.model_wait_context, waiter.tool_context = waiter, ctx
                text, usage, trace = loop.run_llm_loop(
                    [{"role": "system", "content": "Presence turn."}, {"role": "user", "content": task["text"]}],
                    registry, SimpleNamespace(default_model=lambda: "fixture/main"), data / "logs",
                    lambda text, **_kw: h.progress.append(text), queue.Queue(), task_id=task["id"], drive_root=data)
            events = []
            pipeline.emit_task_results(SimpleNamespace(drive_root=data, repo_dir=repo), None, None, events, task,
                                       text, usage, trace, 0.0, data / "logs", ctx=ctx)
            return events

    h.Agent = Agent

    def start(turn_event, agent=None):
        """Start one turn exactly as the Host does: its own execution, admission and thread."""
        from ouroboros.presence_runner import presence_event_identity, presence_turn_task_id

        admission = _admission()
        return h.executions.start_thread(
            presence_turn_task_id(admission.binding_id, turn_event.source_event_id),
            identity=presence_event_identity(admission.binding_id, turn_event),
            admit=lambda: h.gate.acquire(turn_event.conversation_key),
            run=lambda lease: run_presence_turn(admission=admission, event=turn_event, repo_dir=repo, drive_root=data,
                                                agent_factory=lambda **_kw: agent or Agent(), admitted=lease),
            context=contextvars.copy_context())[0]

    h.start = start
    yield h
    h.release.set()
    if h.entered.is_set():
        assert settled.wait(15)
