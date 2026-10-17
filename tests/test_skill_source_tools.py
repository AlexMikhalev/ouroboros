"""Ouroboros's own skill tools: search and install by catalog id, review rows review, owner-only changes."""

from __future__ import annotations

import json

import pytest

from ouroboros.skill_install_record import read_install_record
from ouroboros.tools import skill_source_tools as tools
from ouroboros.tools.registry import ToolContext
from tests._skill_source_fixtures import HubFixture, builtin_repo, join_source_refreshes


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    import ouroboros.config as cfg

    drive = tmp_path / "data"
    (drive / "skills").mkdir(parents=True)
    monkeypatch.setattr(cfg, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.delenv("OUROBOROS_SKILLS_REPO_PATH", raising=False)
    hub = HubFixture(tmp_path / "remote")
    hub.add_hosted("weather", official=True, recommended=True)
    hub.add_hosted("notes", category=["writing"])
    hub.commit()
    monkeypatch.setenv("OUROBOROS_SKILL_SOURCES", json.dumps(hub.rows()))
    monkeypatch.setattr(cfg, "REPO_DIR", builtin_repo(tmp_path))
    reviews = []
    monkeypatch.setattr("ouroboros.skill_source_install.start_review", lambda d, name, **k: reviews.append(name))
    context = ToolContext(repo_dir=tmp_path, drive_root=drive)
    context.task_id = "task-1"
    yield context, drive, reviews, hub
    join_source_refreshes()


def _call(tool, ctx, **kwargs):
    entry = next(e for e in tools.get_tools() if e.name == tool)
    return entry.handler(ctx, **kwargs)


def test_the_four_tools_are_registered_with_schemas():
    names = {entry.name: entry for entry in tools.get_tools()}
    assert set(names) == {"skill_sources", "skill_search", "skill_install", "skill_update"}
    assert names["skill_install"].schema["parameters"]["required"] == ["id", "reason"]


def test_search_reads_a_new_source_once_and_returns_catalog_ids(ctx):
    context, _drive, _reviews, _hub = ctx
    result = json.loads(_call("skill_search", context, query="weather"))
    assert [item["id"] for item in result["items"]] == ["ouroboros/weather"]
    assert result["items"][0]["trust"] == "trusted" and result["items"][0]["official"] is True
    everything = json.loads(_call("skill_search", context, category="writing"))
    assert [item["id"] for item in everything["items"]] == ["ouroboros-community/notes"]


def test_install_from_a_review_row_starts_the_review_and_records_the_reason(ctx):
    context, drive, reviews, _hub = ctx
    _call("skill_search", context)
    out = json.loads(_call("skill_install", context, id="ouroboros-community/notes", reason="the owner's notes task"))
    assert out["needs_review"] and reviews == ["notes"] and "reviewed before it runs" in out["message"]
    record = read_install_record(drive, "notes")
    assert record["installed_by"] == "ouroboros" and record["reason"] == "the owner's notes task"
    assert record["task_id"] == "task-1" and record["basis"] == "review"


def test_install_from_a_trusted_row_is_accepted_at_once(ctx):
    context, drive, reviews, _hub = ctx
    _call("skill_search", context)
    out = json.loads(_call("skill_install", context, id="ouroboros/weather", reason="forecast for a trip"))
    assert out["basis"] == "trusted" and "acceptance" in out and reviews == []
    assert "installs without review" in out["message"] and "Keys granted:" in out["message"]
    assert read_install_record(drive, "weather")["basis"] == "trusted"


def test_install_refuses_urls_missing_reasons_and_unconfirmed_replacement(ctx):
    context, drive, _reviews, _hub = ctx
    assert "never a URL" in _call("skill_install", context, id="https://github.com/x/y", reason="r")
    assert "reason is required" in _call("skill_install", context, id="ouroboros/weather", reason=" ")
    refused = _call("skill_install", context, id="ouroboros/weather", reason="r", replace=True)
    assert refused.startswith("⚠️ SKILL_INSTALL_REFUSED") and "owner's instruction" in refused
    assert read_install_record(drive, "weather") is None


def test_changing_sources_needs_the_owner_outside_cyber_pro(ctx, tmp_path, monkeypatch):
    context, _drive, _reviews, _hub = ctx
    listed = json.loads(_call("skill_sources", context))
    assert [row["id"] for row in listed["sources"]] == ["builtin", "ouroboros", "ouroboros-community"]
    folder = tmp_path / "folder"
    folder.mkdir()
    refused = _call("skill_sources", context, action="add", path=str(folder), name="Mine")
    assert refused.startswith("⚠️ SKILL_SOURCES_REFUSED")
    monkeypatch.setattr(tools, "_cyber", lambda: True)
    added = json.loads(_call("skill_sources", context, action="add", path=str(folder), name="Mine", trust="review"))
    assert added["sources"][-1]["id"] == "mine" and added["sources"][-1]["trust"] == "review"


def test_update_keep_records_the_skipped_release(ctx):
    context, drive, _reviews, _hub = ctx
    _call("skill_search", context)
    _call("skill_install", context, id="ouroboros-community/notes", reason="r")
    assert "SKILL_UPDATE_ERROR" in _call("skill_update", context, name="notes", mode="later")
    hub_digest = json.loads(_call("skill_search", context, query="notes"))["items"][0]
    out = json.loads(_call("skill_update", context, name="notes", mode="keep"))
    assert out["mode"] == "keep" and read_install_record(drive, "notes")["skipped_release_digest"]
    assert hub_digest["installed"]["state"] == "installed"


def test_a_merge_ouroboros_asks_for_is_its_own_task_never_the_owner_door(ctx, monkeypatch):
    from ouroboros.skill_source_catalog import refresh_source

    context, drive, _reviews, hub = ctx
    _call("skill_search", context)
    _call("skill_install", context, id="ouroboros-community/notes", reason="r")
    manifest = drive / "skills" / "ouroboroshub" / "notes" / "SKILL.md"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8")
    hub.add_hosted("notes", "2.0.0", category=["writing"])
    hub.commit()
    refresh_source(drive, hub.rows()[2])

    def owner_door():
        raise AssertionError("Ouroboros never speaks through the owner's door")

    monkeypatch.setattr("supervisor.message_bus.get_bridge", owner_door)
    out = json.loads(_call("skill_update", context, name="notes", mode="merge"))
    assert out["task"] == "current" and out["base_available"] is True
    assert out["local_tree"] == "skills/ouroboroshub/notes" and "skill_review" in out["instructions"]
    assert read_install_record(drive, "notes")["merge"]["requested_by"] == "ouroboros"
