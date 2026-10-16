"""Room cognition is copied once; owner saves cannot alter another room's future work."""
from __future__ import annotations

import json
import os

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros import config, projects_registry as projects
from ouroboros.composer_cognition import COGNITION_KEYS, cognition_overrides, read_cognition
from ouroboros.gateway.owner_effort import api_owner_effort_range
from ouroboros.gateway.settings import api_owner_context_mode, api_settings_post
from ouroboros.settings_scales import choose_effort, effort_range


@pytest.fixture
def room_settings(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", root)
    monkeypatch.setattr(config, "SETTINGS_PATH", root / "settings.json")
    for key in (*COGNITION_KEYS, "OUROBOROS_CONTEXT_MODE_AUTO_LOW"):
        monkeypatch.delenv(key, raising=False)
    config.reset_runtime_mode_baseline_for_tests()
    config.initialize_runtime_mode_baseline("cyber_pro")
    config.SETTINGS_PATH.write_text(json.dumps({
        "OUROBOROS_CONTEXT_MODE": "max", "OUROBOROS_CONTEXT_MODE_AUTO_LOW": "false",
        "OUROBOROS_EFFORT_TASK": "ultra", "OUROBOROS_RUNTIME_MODE": "cyber_pro",
    }), encoding="utf-8")
    config.apply_settings_to_env(config.load_settings())
    app = Starlette(routes=[
        Route("/api/owner/effort-range", api_owner_effort_range, methods=["POST"]),
        Route("/api/owner/context-mode", api_owner_context_mode, methods=["POST"]),
        Route("/api/settings", api_settings_post, methods=["POST"]),
    ])
    app.state.drive_root = root
    yield root, TestClient(app)
    config.reset_runtime_mode_baseline_for_tests()


def _legacy_rows(root, *ids):
    for pid in ids:
        projects.create_project(root, pid, origin="owner_ui")
    path = root / "state/projects.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for row in data["projects"]:
        row.pop("cognition", None)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_two_projects_keep_independent_choices_and_saves_never_project_to_env(room_settings):
    root, client = room_settings
    a = projects.create_project(root, "a", origin="owner_ui")
    projects.create_project(root, "b", origin="owner_ui")
    baseline = read_cognition(root)
    before_file, before_env = config.SETTINGS_PATH.read_bytes(), {key: os.environ.get(key) for key in COGNITION_KEYS}
    effort = {"min": "low", "recommended": "max", "max": "medium"}
    response = client.post("/api/owner/effort-range", json={"project_id": "a", **effort})
    assert response.status_code == 200, response.text
    assert response.json()["project_id"] == "a" and response.json()["effort_range"] == effort
    response = client.post("/api/owner/context-mode", json={"project_id": "a", "mode": "nano"})
    assert response.status_code == 200, response.text
    assert response.json()["cognition"] == {"context_mode": "nano", "effort_range": effort}
    assert config.SETTINGS_PATH.read_bytes() == before_file
    assert {key: os.environ.get(key) for key in COGNITION_KEYS} == before_env
    assert read_cognition(root, "b") == read_cognition(root) == baseline
    assert projects.create_project(root, "a")["cognition"] == response.json()["cognition"]
    before_registry = (root / "state/projects.json").read_bytes()
    assert projects.projects_summary(root, chat_id=a["chat_id"])[0]["cognition"] == response.json()["cognition"]
    assert (root / "state/projects.json").read_bytes() == before_registry


@pytest.mark.parametrize("endpoint,body", [
    ("/api/owner/context-mode", {"mode": "nano"}),
    ("/api/owner/effort-range", {"min": "low", "recommended": "low", "max": "low"}),
    ("/api/settings", {"OUROBOROS_CONTEXT_MODE": "nano", "OUROBOROS_EFFORT_TASK": "low"}),
])
def test_main_changes_seed_unopened_legacy_projects_before_new_default(room_settings, endpoint, body):
    root, client = room_settings
    _legacy_rows(root, "old-a", "old-b")
    previous = read_cognition(root)
    assert previous["effort_range"] == {"min": "low", "recommended": "ultra", "max": "ultra"}
    response = client.post(endpoint, json=body)
    assert response.status_code == 200, response.text
    assert read_cognition(root) != previous
    assert read_cognition(root, "old-a") == read_cognition(root, "old-b") == previous
    stored = json.loads((root / "state/projects.json").read_text(encoding="utf-8"))
    assert all(row["cognition"] == previous for row in stored["projects"])
    assert projects.create_project(root, "new")["cognition"] == read_cognition(root)


def test_reconcile_and_registry_recovery_retain_the_copied_cognition(room_settings):
    root, client = room_settings
    _legacy_rows(root, "proj_saved")
    (root / "projects/proj_saved").mkdir(parents=True)
    projects.reconcile_projects(root)
    expected = read_cognition(root, "proj_saved")
    assert client.post("/api/owner/context-mode", json={"mode": "low"}).status_code == 200
    projects.reconcile_projects(root)
    path = root / "state/projects.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["projects"] = []
    path.write_text(json.dumps(data), encoding="utf-8")
    assert projects.reconcile_projects(root) == 1
    assert read_cognition(root, "proj_saved") == expected


@pytest.mark.parametrize("primary,bounds,auto", [
    ("max", ("low", "medium"), "medium"), ("none", ("high", "ultra"), "high"),
])
def test_independent_primary_is_not_moved_and_auto_helpers_stay_inside(primary, bounds, auto):
    desired = {"context_mode": "nano", "effort_range": {"min": bounds[0], "recommended": primary, "max": bounds[1]}}
    rng = effort_range(cognition_overrides(desired))
    assert rng == desired["effort_range"]
    assert choose_effort(rng=rng, binds=True) == (auto, "auto")
    assert choose_effort(rng=rng, binds=False) == (auto, "auto")
    assert choose_effort(rng=rng, default_top=True) == (bounds[1], "auto")
    assert choose_effort("low", pin="ultra", binds=True, rng=rng) == ("ultra", "pin")


def test_raw_legacy_bounds_survive_default_merge_and_roundtrip(room_settings):
    root, _client = room_settings
    values = config.load_settings()
    assert values["OUROBOROS_EFFORT_MAX"] == "ultra"
    assert config.normalize_settings_raw(config.normalize_settings_raw({"OUROBOROS_EFFORT_TASK": "ultra"})) == {
        "OUROBOROS_EFFORT_MIN": "low", "OUROBOROS_EFFORT_TASK": "ultra", "OUROBOROS_EFFORT_MAX": "ultra"}
    config.save_settings(values)
    assert read_cognition(root)["effort_range"]["max"] == "ultra"


@pytest.mark.parametrize("project_id,status", [("missing", 404), ("../other", 400), (123, 400)])
def test_unknown_project_never_changes_main(room_settings, project_id, status):
    _root, client = room_settings
    before = config.SETTINGS_PATH.read_bytes()
    response = client.post("/api/owner/context-mode", json={"project_id": project_id, "mode": "nano"})
    assert response.status_code == status, response.text
    assert config.SETTINGS_PATH.read_bytes() == before


def test_owner_context_changes_do_not_wait_for_unrelated_tasks(room_settings, monkeypatch):
    _root, client = room_settings
    from ouroboros.gateway import settings
    config.reset_runtime_mode_baseline_for_tests()
    config.initialize_runtime_mode_baseline("pro")
    monkeypatch.setattr(settings, "_has_running_agent_tasks", lambda: True)
    response = client.post("/api/owner/context-mode", json={"mode": "nano"})
    assert response.status_code == 200, response.text


def test_settings_path_owns_legacy_migration_even_when_data_dir_differs(room_settings, tmp_path, monkeypatch):
    root, _client = room_settings
    _legacy_rows(root, "untouched")
    untouched = (root / "state/projects.json").read_bytes()
    other = tmp_path / "other"
    other.mkdir()
    settings_path = other / "settings.json"
    settings_path.write_bytes(config.SETTINGS_PATH.read_bytes())
    monkeypatch.setattr(config, "SETTINGS_PATH", settings_path)
    _legacy_rows(other, "migrated")
    before = read_cognition(other, "migrated")
    config.save_settings({**config.load_settings(), "OUROBOROS_CONTEXT_MODE": "nano"})
    assert read_cognition(other, "migrated") == before
    assert (root / "state/projects.json").read_bytes() == untouched
