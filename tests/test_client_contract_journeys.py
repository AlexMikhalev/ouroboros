"""Unknown-outcome and retry facts docs/CLIENT_CONTRACT.md states, pinned on the real handlers.

A client decides whether to resend from these answers alone, so each published rule is checked
end to end: the refusal, what a read shows afterwards, and what the same request does again.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros.gateway.contracts import ErrorResponse, TaskCreateResponse
from ouroboros.gateway.schema import validate_ingress
from ouroboros.gateway.tasks import api_task_get, api_tasks_create
from tests._headless_cli_shared import _managed_worker_pool_available  # noqa: F401  (autouse)


def _task_client(tmp_path) -> TestClient:
    data, repo = tmp_path / "data", tmp_path / "repo"
    (data / "memory").mkdir(parents=True)
    repo.mkdir()
    app = Starlette(routes=[
        Route("/api/tasks", endpoint=api_tasks_create, methods=["POST"]),
        Route("/api/tasks/{task_id}", endpoint=api_task_get, methods=["GET"]),
    ])
    app.state.drive_root = data
    app.state.repo_dir = repo
    return TestClient(app)


def test_a_503_without_unconfirmed_scheduled_nothing_and_the_same_id_can_be_sent_again(tmp_path, monkeypatch):
    import supervisor.queue as queue
    from ouroboros.gateway import tasks

    monkeypatch.setattr(queue, "enqueue_task", lambda task: task)
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda **_kwargs: True)
    real = tasks.stage_initial_task_attachments
    monkeypatch.setattr(tasks, "stage_initial_task_attachments",
                        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("disk unavailable")))
    client = _task_client(tmp_path)
    refused = client.post("/api/tasks", json={"task_id": "staging-503", "description": "plan it"})
    assert refused.status_code == 503 and refused.json().get("status") != "unconfirmed"
    assert not validate_ingress(refused.json(), ErrorResponse)
    assert client.get("/api/tasks/staging-503").status_code == 404
    monkeypatch.setattr(tasks, "stage_initial_task_attachments", real)
    accepted = client.post("/api/tasks", json={"task_id": "staging-503", "description": "plan it"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["task_id"] == "staging-503" and accepted.json()["status"] == "scheduled"
    assert not validate_ingress(accepted.json(), TaskCreateResponse)


def test_an_unconfirmed_admission_reads_as_absent_but_is_never_started_twice(tmp_path, monkeypatch):
    import supervisor.queue as queue

    pending: list = []
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path / "data")
    monkeypatch.setattr(queue, "PENDING", pending)
    monkeypatch.setattr(queue, "RUNNING", {})
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": reason == "api_task_create_rollback")
    client = _task_client(tmp_path)
    unknown = client.post("/api/tasks", json={"task_id": "maybe", "description": "plan it", "memory_mode": "empty"})
    assert unknown.status_code == 503
    assert unknown.json()["status"] == "unconfirmed" and unknown.json()["task_id"] == "maybe"
    # The task may still run: it sits in the live queue while its record reads as absent.
    assert [row["id"] for row in pending] == ["maybe"]
    assert client.get("/api/tasks/maybe").status_code == 404, "a 404 here is not proof it will not run"
    again = client.post("/api/tasks", json={"task_id": "maybe", "description": "plan it", "memory_mode": "empty"})
    assert again.status_code == 409, "the same task_id never starts a second copy"
    assert [row["id"] for row in pending] == ["maybe"]
