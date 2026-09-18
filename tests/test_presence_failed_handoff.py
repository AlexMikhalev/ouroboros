"""A failed parent retains already admitted work without reviving stale speech."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from ouroboros import agent_task_pipeline as pipeline
from ouroboros.presence_runner import PresenceTurnGate, run_presence_turn
from ouroboros.task_results import load_task_result
from ouroboros.tools.control_routing import _finish_swarm_handoff
from ouroboros.tools.presence import _finish_presence
from tests.test_presence_runner import _admission, _event


def _failed_parent(tmp_path, *, outcome="deferred", admission="scheduled", usage=None, accepted=False,
                   request_text="Outdated proposed reply"):
    """Real completion/admission producers and durable terminal pipeline."""
    created = []
    text = "Current partial result after the parent stopped."
    terminal_usage = {"execution_status": "infra_failed", "reason_code": "provider_unavailable",
                      "terminal_provider_notice": "Provider unavailable; child work remains admitted."} if usage is None else usage

    class Agent:
        def handle_task(self, task):
            created.append(task["id"])
            task["_skip_post_task_synthesis"] = True
            ctx = SimpleNamespace(task_contract=task["task_contract"], task_metadata=task["metadata"])
            _finish_presence(ctx, outcome, request_text)
            ctx._presence_completion_accepted = accepted
            if admission:
                _finish_swarm_handoff(ctx, {"task_id": "managed-work"}, "Admission receipt", status=admission)
            pending = []
            pipeline.emit_task_results(
                SimpleNamespace(drive_root=tmp_path, repo_dir=tmp_path), None, None, pending, task, text,
                dict(terminal_usage), {"tool_calls": [], "reasoning_notes": []}, 0.0, tmp_path / "logs", ctx=ctx,
            )
            return pending

    kwargs = dict(admission=_admission(), event=replace(_event(), delivery_reporting_version=1),
                  repo_dir=tmp_path, drive_root=tmp_path, agent_factory=lambda **_kw: Agent(),
                  gate=PresenceTurnGate(1))
    first = run_presence_turn(**kwargs)
    assert run_presence_turn(**kwargs) == first
    assert len(created) == 1
    return first, load_task_result(tmp_path, first.task_id), text


@pytest.mark.parametrize("outcome", ["deferred", "message", "silent", "tool_delivered"])
@pytest.mark.parametrize("request_text", ["", "Outdated proposed reply"])
def test_failed_parent_and_cached_result_keep_admitted_child_pollable(tmp_path, outcome, request_text):
    result, stored, text = _failed_parent(tmp_path, outcome=outcome, request_text=request_text)
    assert result.outcome == "deferred" and result.work_ref == "managed-work"
    assert result.delivery_reporting_version == 1
    assert result.text.startswith(text) and result.text.count("[Host status]") == 1
    assert "Outdated proposed reply" not in result.text
    assert stored["metadata"]["presence_outcome"] == "deferred"
    assert stored["metadata"]["presence_result_text"] == result.text
    assert stored["status"] == "failed" and stored["outcome_axes"]["execution"]["status"] == "infra_failed"
    assert stored["reason_code"] == "provider_unavailable"


@pytest.mark.parametrize("admission", ["scheduled", "unconfirmed", "rejected", ""])
def test_forced_best_effort_requires_positive_admission_and_keeps_current_body(tmp_path, admission):
    usage = {"execution_status": "failed", "reason_code": "round_limit", "_best_effort_extracted": True}
    result, stored, text = _failed_parent(tmp_path, outcome="silent", admission=admission,
                                        usage=usage, accepted=True)
    assert result.outcome == ("deferred" if admission == "scheduled" else "message")
    assert result.work_ref == ("managed-work" if admission == "scheduled" else "")
    assert result.text == text
    assert stored["outcome_axes"]["execution"]["status"] == "best_effort"
    assert stored["reason_code"] == "round_limit"


@pytest.mark.parametrize("outcome", ["deferred", "silent", "tool_delivered"])
def test_successful_replacement_answer_does_not_inherit_old_outcome(tmp_path, outcome):
    result, stored, text = _failed_parent(tmp_path, outcome=outcome, usage={})
    assert result.outcome == "message" and result.text == text
    assert result.work_ref == "managed-work"
    assert stored["status"] == "completed" and stored["outcome_axes"]["execution"]["status"] == "ok"


@pytest.mark.parametrize("outcome", ["silent", "tool_delivered"])
def test_accepted_successful_nonmessage_remains_nonmessage(tmp_path, outcome):
    result, stored, _text = _failed_parent(tmp_path, outcome=outcome, usage={}, accepted=True)
    assert result.outcome == outcome and result.text == ""
    assert stored["status"] == "completed"
