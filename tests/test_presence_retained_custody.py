"""Input custody and late result observation are independent of a failed turn."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from ouroboros.gateway import host_service
from ouroboros.presence_runner import PresenceTurnGate, presence_turn_task_id, run_presence_turn
from ouroboros.task_results import load_task_result, write_task_result
from tests.test_host_service_responsiveness import _event, _presence_app, _request, _turn


def _poll(app, binding, reference):
    request = _request(app)
    request.method = "GET"
    request.path_params = {"work_ref": reference}
    request.query_params = {"binding_id": binding}
    response = asyncio.run(host_service._api_presence_work(request))
    return response.status_code, json.loads(response.body)


def _host(tmp_path, *, cause="unknown", child=False, proof=False):
    started = []

    class Agent:
        def handle_task(self, task):
            started.append(task["id"])
            meta = dict(task["metadata"])
            if meta["presence"]["event"]["source_event_id"] == "old":
                if child:
                    meta["presence_work_ref"] = "child"
                    write_task_result(tmp_path, "child", "running", metadata={"presence": meta["presence"]})
                reason = "resource_refusal_no_resend" if cause == "quota" else "provider_unavailable"
                if cause == "unknown":
                    meta["presence_unknown_outcome"] = {"source": "provider_outcome_unknown_no_resend"}
                if proof:
                    meta["presence_retry_proof"] = {
                        "kind": "first_round_engine_not_started", "task_id": task["id"],
                        "event_identity": meta["presence_event_identity"],
                        "reset_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    }
                write_task_result(tmp_path, task["id"], "failed", metadata=meta, reason_code=reason,
                                  terminal_origin="model_final", result="unselected draft")
            else:
                meta.update(presence_outcome="message", presence_result_text="new reply")
                write_task_result(tmp_path, task["id"], "completed", metadata=meta,
                                  terminal_origin="model_final", result="new reply")
            return [{"type": "presence_result", "outcome": "message", "text": "new reply"}]

    app, binding, ctx = _presence_app(tmp_path, lambda **kwargs: run_presence_turn(
        repo_dir=tmp_path, drive_root=tmp_path, agent_factory=lambda **kw: Agent(),
        gate=PresenceTurnGate(1), **kwargs))
    return app, binding, ctx, started


def test_retained_receipt_releases_next_event_preserves_original_and_late_child(tmp_path):
    app, binding, ctx, started = _host(tmp_path, child=True)
    old = presence_turn_task_id(binding, "old")
    first = asyncio.run(host_service._api_presence_turn(_request(app, {
        "binding_id": binding, "event": _event("old"), "delivery_reporting_version": 1})))
    receipt = json.loads(first.body)
    assert first.status_code == 409 and receipt["disposition"] == "retained"
    assert receipt["turn_ref"] == old and receipt["work_ref"] == "child"
    assert receipt["delivery_reporting_version"] == 1 and "outcome" not in receipt
    assert ctx.presence_turns.live() == []
    later = asyncio.run(_turn(app, binding, "new"))
    assert json.loads(later.body)["text"] == "new reply"
    again = asyncio.run(_turn(app, binding, "old"))
    assert json.loads(again.body)["disposition"] == "retained"
    assert started == [old, presence_turn_task_id(binding, "new")]
    status, pending = _poll(app, binding, old)
    assert status == 200 and pending["status"] == "unresolved"
    assert pending["turn_ref"] == old and pending["work_ref"] == "child"
    assert "outcome" not in pending and "text" not in pending
    assert _poll(app, binding, "child")[1]["status"] == "pending"
    child = load_task_result(tmp_path, "child")
    write_task_result(tmp_path, "child", "completed", metadata={**child["metadata"],
                      "presence_outcome": "message", "presence_result_text": "child answer"},
                      terminal_origin="model_final", result="child answer")
    assert _poll(app, binding, "child")[1]["text"] == "child answer"
    assert _poll(app, binding, old)[1]["status"] == "unresolved"
    # A canonical late publication may enrich the existing failed result with its
    # source-bound selected answer; task lifecycle remains monotonic. Observation
    # neither regenerates the old event nor rewrites it as a silent completion.
    previous = load_task_result(tmp_path, old)
    metadata = dict(previous["metadata"])
    metadata["presence_unknown_outcome"] = None  # canonical producer supersedes the unknown marker
    metadata.update(presence_outcome="message", presence_result_text="late original")
    write_task_result(tmp_path, old, "failed", metadata=metadata, reason_code="",
                      terminal_origin="model_final", result="late original")
    assert _poll(app, binding, old)[1]["text"] == "late original"
    assert len(started) == 2


@pytest.mark.parametrize("proof,disposition", [(False, "retained"), (True, "retry")])
def test_only_source_bound_no_start_proof_keeps_quota_event_retryable(tmp_path, proof, disposition):
    app, binding, _, started = _host(tmp_path, cause="quota", proof=proof)
    for _ in range(2):
        response = asyncio.run(_turn(app, binding, "old"))
        body = json.loads(response.body)
        assert response.status_code == 409 and body["disposition"] == disposition
        assert body["code"] == "presence_resources_unavailable"
    assert len(started) == 1
