"""Persistent Project pins use the existing partial update and sidebar projection."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from ouroboros.gateway.router import collect_routes
from ouroboros import projects_registry as registry


@pytest.fixture
def pin_client(tmp_path):
    app = Starlette(routes=collect_routes(data_dir=tmp_path))
    app.state.drive_root = tmp_path
    with TestClient(app) as client:
        yield client, tmp_path


def test_pin_partial_update_persists_without_changing_room_authority(pin_client):
    client, root = pin_client
    before = registry.create_project(root, "alpha", name="Alpha", working_dir="/example")
    saved = client.post("/api/projects/alpha/update", json={"pinned": True})
    assert saved.status_code == 200
    pinned = saved.json()["project"]
    for key in ("name", "working_dir", "chat_id", "routing_generation", "routing_incarnation", "last_active_at", "visible_revision"):
        assert pinned[key] == before[key]
    assert pinned["pinned"] is True
    assert client.get("/api/projects").json()["projects"][0]["pinned"] is True
    assert client.post("/api/projects/alpha/update", json={"name": "Renamed"}).json()["project"]["pinned"] is True
    assert client.post("/api/projects/alpha/update", json={"pinned": False}).json()["project"]["name"] == "Renamed"
    assert registry.get_project(root, "alpha")["pinned"] is False


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None, [], {}])
def test_pin_is_strict_boolean_and_refuses_atomically(pin_client, value):
    client, root = pin_client
    registry.create_project(root, "alpha", name="Original")
    path = root / "state/projects.json"
    before = path.read_bytes()
    response = client.post("/api/projects/alpha/update", json={"name": "Should not save", "pinned": value})
    assert response.status_code == 400
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="boolean"):
        registry.update_project(root, "alpha", pinned=value)
    assert path.read_bytes() == before


def test_pin_missing_deleting_and_racing_delete_are_not_success(pin_client, monkeypatch):
    client, root = pin_client
    assert client.post("/api/projects/missing/update", json={"pinned": True}).status_code == 404
    registry.create_project(root, "alpha")
    original = registry.update_project

    def delete_first(*args, **kwargs):
        registry.begin_project_deletion(root, "alpha")
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "update_project", delete_first)
    assert client.post("/api/projects/alpha/update", json={"pinned": True}).status_code == 409
    assert registry.get_reserved_project(root, "alpha")["pinned"] is False
    assert client.post("/api/projects/alpha/update", json={"pinned": True}).status_code == 404


def test_empty_or_invalid_updates_preserve_pin(pin_client):
    client, root = pin_client
    registry.create_project(root, "alpha")
    registry.update_project(root, "alpha", pinned=True)
    for body in ({}, {"other": True}, {"name": ""}, {"name": "x" * 81}):
        assert client.post("/api/projects/alpha/update", json=body).status_code == 400
        assert registry.get_project(root, "alpha")["pinned"] is True


def test_pins_do_not_exhaust_recent_slice_or_disappear_at_cap(pin_client):
    client, root = pin_client
    # One registry write seeds scale without making the test about creation cost.
    registry.create_project(root, "seed", origin="owner_ui")
    path = root / "state/projects.json"
    data = json.loads(path.read_text())
    template = data["projects"][0]
    data["projects"] = [{**template, "id": f"pin-{i}", "chat_id": 10000 + i,
                         "pinned": True, "last_active_at": "2026-01-01T00:00:00Z"}
                        for i in range(55)]
    data["projects"] += [{**template, "id": f"recent-{i}", "chat_id": 20000 + i,
                          "last_active_at": f"2026-10-01T00:{i:02}:00Z"} for i in range(55)]
    path.write_text(json.dumps(data))
    listed = registry.projects_summary(root, include_pinned=True)
    assert len(listed) == 105
    assert all(row["pinned"] for row in listed[:55])
    assert [row["id"] for row in listed[55:]] == [f"recent-{i}" for i in reversed(range(5, 55))]
    api_rows = client.get("/api/projects").json()["projects"]
    assert len(api_rows) == 110 and sum(row["pinned"] for row in api_rows) == 55
    assert api_rows[0]["id"] == "recent-54"  # ordinary enumeration keeps recency
    assert all(not row["pinned"] for row in registry.projects_summary(root))
    addressed = registry.projects_summary(root, chat_id=10000)
    assert addressed[0]["id"] == "pin-0" and addressed[0]["pinned"] is True


def test_legacy_and_concurrent_partial_updates_keep_neighbour_fields(pin_client):
    client, root = pin_client
    for pid in ("alpha", "beta"):
        registry.create_project(root, pid, name=pid)
    path = root / "state/projects.json"
    raw = json.loads(path.read_text())
    for row in raw["projects"]:
        row.pop("pinned")
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    assert not any(row["pinned"] for row in registry.projects_summary(root))
    assert path.read_bytes() == before  # legacy reads never migrate the file
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda pid: registry.update_project(root, pid, pinned=True), ("alpha", "beta")))
    assert all(registry.get_project(root, pid)["pinned"] for pid in ("alpha", "beta"))
    assert [registry.get_project(root, pid)["name"] for pid in ("alpha", "beta")] == ["alpha", "beta"]
