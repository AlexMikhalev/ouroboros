"""Recorded self-check and owner update skip share exact-release acceptance and its floor."""
from __future__ import annotations

import json
import pathlib

import pytest

from ouroboros import skill_source_install as installer
from ouroboros.skill_install_record import read_install_record, update_install_record
from ouroboros.skill_loader import find_skill
from ouroboros.skill_source_catalog import tree_release_digest
from tests._skill_source_fixtures import write_skill


def test_owner_skip_summary_describes_exact_bytes_without_install_timing():
    from ouroboros.skill_review_status import skill_review_gate

    gate = skill_review_gate("pending", current_hash="release", acceptance={
        "basis": "owner_skip", "release_digest": "release"})
    assert gate["summary"] == "The owner skipped the review for these exact bytes."


def test_check_guidance_defers_panel_blockers_to_owner_skip():
    from ouroboros.tools.skill_source_tools import _SCHEMAS

    for tool in ("skill_install", "skill_update"):
        description = _SCHEMAS[tool]["properties"]["check"]["description"]
        assert "panel review found blockers" in description
        assert "owner's Skip" in description
        assert "self-check does not overrule a panel" in description
    doc = (pathlib.Path(__file__).resolve().parents[1] / "docs/CREATING_SKILLS.md").read_text(encoding="utf-8")
    doc = " ".join(doc.split())
    assert "panel review found blockers needs the owner's Skip" in doc
    assert "own check does not overrule a panel" in doc


@pytest.fixture
def release(tmp_path, monkeypatch):
    drive = tmp_path / "data"
    folder = write_skill(tmp_path / "source", "demo", extra="env_from_settings: [GITHUB_TOKEN]\n")
    row = {"id": "team", "name": "Team", "kind": "hub", "url": "https://example.invalid/team",
           "trust": "review", "confirmed": True}
    item = {"id": "team/demo", "name": "demo", "version": "1.0.0", "digest": tree_release_digest(folder)}
    monkeypatch.setenv("OUROBOROS_SKILL_SOURCES", json.dumps([row]))
    monkeypatch.setattr(installer, "find_item", lambda *a, **kw: (row, item, {}))
    monkeypatch.setattr(installer, "entry_payload_dir", lambda *a, **kw: folder)
    monkeypatch.setattr("ouroboros.config.SETTINGS_PATH", drive / "settings.json")
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    monkeypatch.delenv("OUROBOROS_SKILLS_REPO_PATH", raising=False)
    return drive, folder, row, item


@pytest.mark.parametrize("grants", [True, False])
def test_self_check_records_reason_accepts_exact_release_and_obeys_auto_grants(release, monkeypatch, grants):
    drive, _folder, _row, item = release
    monkeypatch.setenv("OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS", str(grants).lower())
    result = installer.install_item(drive, item["id"], check="self", check_notes="Read all files; only formats text.",
                                    actor="ouroboros", task_id="task-1")
    record = read_install_record(drive, "demo")
    assert record["basis"] == result["basis"] == "self_check"
    assert record["check"]["by"] == "ouroboros" and record["check"]["task_id"] == "task-1"
    assert record["check"]["notes"] == "Read all files; only formats text." and record["check"]["at"]
    assert result["acceptance"]["accepted"] and not result.get("needs_review")
    assert result["acceptance"]["granted_keys"] == (["GITHUB_TOKEN"] if grants else [])
    loaded = find_skill(drive, "demo")
    assert loaded.review.gate_for(loaded.content_hash)["blocking_reason"] == "self_check"
    (loaded.skill_dir / "added.txt").write_text("edit", encoding="utf-8")
    edited = find_skill(drive, "demo")
    assert not edited.review.gate_for(edited.content_hash)["executable_review"]


@pytest.mark.parametrize("notes", ["", "   ", "x" * 2001], ids=["empty", "whitespace", "too_long"])
def test_self_check_requires_bounded_nonempty_notes_before_landing(release, notes):
    drive, _folder, _row, item = release
    with pytest.raises(installer.SkillInstallError) as exc:
        installer.install_item(drive, item["id"], check="self", check_notes=notes)
    assert exc.value.code == "bad_check"
    assert read_install_record(drive, "demo") is None
    assert not (drive / "skills" / "ouroboroshub" / "demo").exists()


def test_trusted_source_ignores_check_and_reports_it(release):
    drive, _folder, row, item = release
    row["trust"] = "trusted"
    result = installer.install_item(drive, item["id"], check="self", check_notes="")
    assert result["basis"] == "trusted" and result["check_ignored"]
    assert "check" not in read_install_record(drive, "demo")


def test_default_check_still_requests_the_configured_review(release):
    drive, _folder, _row, item = release
    result = installer.install_item(drive, item["id"])
    assert result["basis"] == "review" and result["needs_review"]
    assert "check" not in read_install_record(drive, "demo")


def test_self_check_keeps_the_deterministic_preflight(release, monkeypatch):
    drive, _folder, _row, item = release
    monkeypatch.setattr("ouroboros.skill_review.run_acceptance_preflight", lambda *a, **k: {"ok": False, "error": "bad"})
    result = installer.install_item(drive, item["id"], check="self", check_notes="read it")
    assert not result["acceptance"]["accepted"] and result["acceptance"]["granted_keys"] == []
    assert result["acceptance"]["turn_on"] == "not_ready"


@pytest.mark.parametrize("choice,basis", [("self", "self_check"), ("owner", "owner_skip")])
def test_update_records_self_check_or_owner_skip_and_clears_rejection(release, choice, basis):
    drive, folder, row, item = release
    installer.install_item(drive, item["id"])
    old = find_skill(drive, "demo").content_hash
    update_install_record(drive, "demo", update_rejected={"version": "2", "digest": "x", "reason": "review_blockers"})
    manifest = folder / "SKILL.md"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("1.0.0", "2.0.0"), encoding="utf-8")
    item.update(version="2.0.0", digest=tree_release_digest(folder))
    kwargs = {"skip_review": True} if choice == "owner" else {"check": "self", "check_notes": "Read the new release."}
    result = installer.update_installed(drive, "demo", mode="replace", local_digest=old, rows=[row], **kwargs)
    record = read_install_record(drive, "demo")
    assert record["basis"] == result["basis"] == basis
    assert record["release"]["digest"] == item["digest"]
    assert "update_rejected" not in record and "pending_update" not in record
    assert result["acceptance"]["accepted"] and not result.get("needs_review")
    loaded = find_skill(drive, "demo")
    assert loaded.review.gate_for(loaded.content_hash)["blocking_reason"] == basis
    (loaded.skill_dir / "local.txt").write_text("owner edit", encoding="utf-8")
    edited = find_skill(drive, "demo")
    assert not edited.review.gate_for(edited.content_hash)["executable_review"]


@pytest.mark.parametrize("mode", ["keep", "merge"])
def test_owner_update_skip_is_replace_only(release, mode):
    drive, _folder, row, item = release
    installer.install_item(drive, item["id"])
    with pytest.raises(installer.SkillInstallError) as exc:
        installer.update_installed(drive, "demo", mode=mode, skip_review=True, rows=[row])
    assert exc.value.code == "bad_mode"
    assert read_install_record(drive, "demo")["basis"] == "review"


def test_public_install_fields_omit_task_context_and_rollback_details():
    from ouroboros.skill_install_record import install_listing_fields

    assert install_listing_fields({}) == {}
    record = {"base_digest": "", "modified": True,
              "check": {"by": "ouroboros", "notes": "read it", "task_id": "private", "at": "now"},
              "pending_update": {"version": "2", "digest": "b", "from_digest": "a", "requested_at": "then"},
              "update_rejected": {"version": "3", "digest": "c", "reason": "review_blockers", "at": "now"}}
    assert install_listing_fields(record) == {
        "modified": True, "check": {"by": "ouroboros", "notes": "read it", "at": "now"},
        "pending_update": {"version": "2", "digest": "b"},
        "update_rejected": {"version": "3", "digest": "c", "reason": "review_blockers", "at": "now"}}


def test_builtin_from_review_row_keeps_recorded_self_check(tmp_path, monkeypatch):
    from tests._skill_source_fixtures import builtin_repo

    drive = tmp_path / "data"
    repo = builtin_repo(tmp_path)
    row = {"id": "builtin", "kind": "builtin", "name": "Built in", "trust": "review", "confirmed": True}
    monkeypatch.setenv("OUROBOROS_SKILL_SOURCES", json.dumps([row]))
    monkeypatch.setattr("ouroboros.config.SETTINGS_PATH", drive / "settings.json")
    result = installer.install_item(drive, "builtin/telegram", rows=[row], repo_dir=repo,
                                    check="self", check_notes="Read the bundled instruction.")
    record = read_install_record(drive, "telegram")
    assert result["basis"] == record["basis"] == "self_check" and result["acceptance"]["accepted"]
    assert record["check"]["notes"] == "Read the bundled instruction."


@pytest.fixture
def registry_release(tmp_path, monkeypatch):
    from ouroboros.marketplace.clawhub import ClawHubSkillSummary
    from tests.test_marketplace_api import _patch_clawhub_install_pipeline

    drive = tmp_path / "data"
    row = {"id": "clawhub", "name": "ClawHub", "kind": "registry", "trust": "review", "confirmed": True}
    monkeypatch.setenv("OUROBOROS_SKILL_SOURCES", json.dumps([row]))
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    monkeypatch.setattr("ouroboros.config.SETTINGS_PATH", drive / "settings.json")
    monkeypatch.delenv("OUROBOROS_SKILLS_REPO_PATH", raising=False)
    storage = _patch_clawhub_install_pipeline(monkeypatch, tmp_path, auto_specs=[])
    registry = {"latest": "1.0.0", "changed": False, "staged_versions": []}
    monkeypatch.setattr(storage, "_registry_info", lambda slug: ClawHubSkillSummary(
        slug=slug, latest_version=registry["latest"]))
    original_stage = storage._stage_archive
    def stage(*args, **kwargs):
        staged = original_stage(*args, **kwargs)
        version = kwargs["version"]
        registry["staged_versions"].append(version)
        manifest = staged.staging_dir / "SKILL.md"
        text = manifest.read_text(encoding="utf-8").replace("1.0.0", version)
        if version != "1.0.0":
            text = text.replace("type: skill", "env_from_settings: [GITHUB_TOKEN]\ntype: skill")
        manifest.write_text(text + ("changed release" if registry["changed"] else ""), encoding="utf-8")
        return staged
    monkeypatch.setattr(storage, "_stage_archive", stage)
    assert storage.install_skill(drive, tmp_path, slug="demo", auto_review=False).ok
    return drive, storage, registry


@pytest.mark.parametrize("choice", ["confirmed", "changed", "newest"])
def test_registry_owner_skip_checks_staged_bytes_and_pins_only_confirmed_release(registry_release, choice):
    from ouroboros.skill_loader import SkillReviewState, save_review_state, skill_state_dir

    drive, _storage, registry = registry_release
    registry["latest"] = "2.0.0"
    assert installer.update_installed(drive, "demo", mode="replace")["needs_review"]
    rejected_digest = find_skill(drive, "demo").content_hash
    save_review_state(drive, "demo", SkillReviewState(status="blockers", content_hash=rejected_digest))
    assert installer.settle_pending_update(drive, "demo")["status"] == "restored"
    before = read_install_record(drive, "demo")
    assert before["update_rejected"]["version"] == "2.0.0"
    saved = {p.name: p.read_bytes() for p in skill_state_dir(drive, "demo").iterdir() if p.is_file()}
    registry.update(latest="3.0.0", changed=choice == "changed")
    digest = "" if choice == "newest" else rejected_digest
    if choice == "changed":
        with pytest.raises(installer.SkillInstallError) as exc:
            installer.update_installed(drive, "demo", mode="replace", digest=digest, skip_review=True)
        assert exc.value.code == "candidate_changed" and exc.value.status == 409
        assert exc.value.facts["candidate"]["version"] == "2.0.0"
        assert exc.value.facts["candidate"]["digest"] != digest
        assert read_install_record(drive, "demo") == before
        assert find_skill(drive, "demo").content_hash == before["release"]["digest"]
        assert {p.name: p.read_bytes() for p in skill_state_dir(drive, "demo").iterdir() if p.is_file()} == saved
    else:
        result = installer.update_installed(drive, "demo", mode="replace", digest=digest, skip_review=True)
        record = read_install_record(drive, "demo")
        assert record["basis"] == result["basis"] == "owner_skip"
        assert record["release"]["version"] == ("3.0.0" if choice == "newest" else "2.0.0")
        assert (record["release"]["digest"] == rejected_digest) is (choice == "confirmed")
        assert result["acceptance"]["accepted"] and result["acceptance"]["granted_keys"] == ["GITHUB_TOKEN"]
        assert "update_rejected" not in record
    assert registry["staged_versions"][-1] == ("3.0.0" if choice == "newest" else "2.0.0")


@pytest.mark.parametrize("via_source", [False, True])
def test_registry_update_without_digest_still_selects_latest(registry_release, via_source):
    drive, storage, registry = registry_release
    registry["latest"] = "3.0.0"
    if via_source:
        assert installer.update_installed(drive, "demo", mode="replace")["needs_review"]
    else:
        assert storage.update_skill(drive, drive.parent, sanitized_name="demo", auto_review=False).ok
    assert registry["staged_versions"][-1] == "3.0.0"
    assert read_install_record(drive, "demo")["release"]["version"] == "3.0.0"


def test_registry_staged_hash_failure_returns_install_result(registry_release, monkeypatch):
    drive, storage, _registry = registry_release
    before = read_install_record(drive, "demo")
    def fail(*args, **kwargs):
        raise OSError("staged tree unreadable")
    monkeypatch.setattr(storage, "_registry_install_record", fail)
    result = storage.install_skill(drive, drive.parent, slug="demo", version="2.0.0", overwrite=True,
                                   auto_review=False)
    assert isinstance(result, storage.InstallResult) and not result.ok
    assert "staged tree unreadable" in result.error
    assert not result.staged.staging_dir.exists()
    assert read_install_record(drive, "demo") == before
    assert find_skill(drive, "demo").content_hash == before["release"]["digest"]
