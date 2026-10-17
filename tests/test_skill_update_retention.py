"""Reviewed updates retain a durable return path through the ordinary lifecycle."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from ouroboros import skill_source_install as installer
from ouroboros.skill_install_record import read_install_record
from ouroboros.skill_loader import SkillReviewState, find_skill, load_enabled, save_enabled, save_review_state, skill_state_dir
from ouroboros.skill_review import SkillReviewOutcome
from ouroboros.skill_review_runner import run_skill_review_lifecycle_blocking
from ouroboros.skill_source_catalog import tree_release_digest
from tests.test_skill_install_checks import release


def _pending(release):
    drive, folder, row, item = release
    row["trust"] = "trusted"
    installer.install_item(drive, item["id"])
    save_enabled(drive, "demo", True, actor="owner_ui")
    old = find_skill(drive, "demo")
    save_review_state(drive, "demo", SkillReviewState(status="clean", content_hash=old.content_hash))
    saved = {name: (skill_state_dir(drive, "demo") / name).read_bytes()
             for name in ("review.json", "grants.json", "install.json")}
    row["trust"] = "review"
    manifest = folder / "SKILL.md"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("1.0.0", "2.0.0"), encoding="utf-8")
    item.update(version="2.0.0", digest=tree_release_digest(folder))
    result = installer.run_in_lane(drive, "update", "demo", lambda: installer.update_installed(
        drive, "demo", mode="replace", rows=[row]))
    assert result["needs_review"]
    return old.content_hash, saved


def _review(drive, status):
    def impl(ctx, name):
        skill = find_skill(drive, name)
        findings = ([{"item": "skill_preflight", "verdict": "FAIL", "model": "deterministic_preflight"}]
                    if status == "pending" else [])
        save_review_state(drive, name, SkillReviewState(status=status, content_hash=skill.content_hash, findings=findings))
        return SkillReviewOutcome(skill_name=name, status=status, content_hash=skill.content_hash, findings=findings)
    return run_skill_review_lifecycle_blocking(SimpleNamespace(drive_root=drive, repo_dir=drive.parent, messages=[]),
                                              "demo", review_impl=impl, repo_path="")


@pytest.mark.parametrize("status,reason", [("blockers", "review_blockers"), ("pending", "preflight_failed")])
def test_failed_review_restores_previous_payload_state_and_execution(release, status, reason):
    drive, _folder, _row, item = release
    previous, saved = _pending(release)
    record = read_install_record(drive, "demo")
    assert record["pending_update"]["from_digest"] == previous
    assert record["pending_update"]["digest"] == item["digest"]
    assert not find_skill(drive, "demo").review.gate_for(item["digest"])["executable_review"]
    _review(drive, status)
    restored = find_skill(drive, "demo")
    assert restored.content_hash == previous and load_enabled(drive, "demo")
    assert restored.review.gate_for(previous)["executable_review"]
    record = read_install_record(drive, "demo")
    assert record["update_rejected"]["reason"] == reason
    assert record["update_rejected"]["digest"] == item["digest"] and record["update_rejected"]["version"] == "2.0.0"
    assert "pending_update" not in record
    for filename in ("review.json", "grants.json"):
        assert (skill_state_dir(drive, "demo") / filename).read_bytes() == saved[filename]
    assert record["release"] == json.loads(saved["install.json"])["release"]


def test_clean_review_keeps_new_release_and_drops_durable_state(release):
    drive, _folder, _row, item = release
    _pending(release)
    assert (skill_state_dir(drive, "demo") / "update_backup.json").is_file()
    _review(drive, "clean")
    assert find_skill(drive, "demo").content_hash == item["digest"]
    assert "pending_update" not in read_install_record(drive, "demo")
    assert not (skill_state_dir(drive, "demo") / "update_backup.json").exists()


def test_startup_restores_interrupted_update_and_leaves_running_review(release, monkeypatch):
    import os
    from ouroboros.skill_starter import at_startup
    from ouroboros.utils import utc_now_iso

    drive, _folder, _row, item = release
    previous, _ = _pending(release)
    job = skill_state_dir(drive, "demo") / "review_job.json"
    job.write_text(json.dumps({"status": "running", "pid": os.getpid(), "last_heartbeat_at": utc_now_iso()}), encoding="utf-8")
    at_startup(drive, {}, provider_ready=False)
    assert find_skill(drive, "demo").content_hash == item["digest"]
    job.write_text(json.dumps({"status": "interrupted"}), encoding="utf-8")
    at_startup(drive, {}, provider_ready=False)
    assert find_skill(drive, "demo").content_hash == previous
    assert read_install_record(drive, "demo")["update_rejected"]["reason"] == "review_unfinished"


def test_review_exception_restores_previous_without_waiting_for_restart(release):
    drive, _folder, _row, _item = release
    previous, _ = _pending(release)
    def fail(*a):
        raise RuntimeError("review interrupted")
    with pytest.raises(RuntimeError, match="review interrupted"):
        run_skill_review_lifecycle_blocking(SimpleNamespace(drive_root=drive, repo_dir=drive.parent, messages=[]),
                                           "demo", review_impl=fail, repo_path="")
    assert find_skill(drive, "demo").content_hash == previous
    assert read_install_record(drive, "demo")["update_rejected"]["reason"] == "review_unfinished"


@pytest.mark.parametrize("edited", [False, True])
def test_owner_skip_during_review_keeps_new_bytes(release, edited):
    from ouroboros.skill_lifecycle_actions import run_skill_action
    from ouroboros.skill_lifecycle_queue import lend_lifecycle_lane

    drive, _folder, _row, item = release
    _pending(release)
    waiting, finish = threading.Event(), threading.Event()
    errors = []
    def slow_review(ctx, name):
        with lend_lifecycle_lane():
            waiting.set()
            assert finish.wait(20)
        return SkillReviewOutcome(skill_name=name, status="blockers", content_hash=item["digest"])
    ctx = SimpleNamespace(drive_root=drive, repo_dir=drive.parent, messages=[])
    def run():
        try:
            run_skill_review_lifecycle_blocking(ctx, "demo", review_impl=slow_review, repo_path="")
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert waiting.wait(20)
        if edited:
            (find_skill(drive, "demo").skill_dir / "owner.txt").write_text("owner edit", encoding="utf-8")
        accepted = find_skill(drive, "demo").content_hash
        result = run_skill_action(ctx, "demo", "attest", expected_content_hash=accepted,
                                  _owner_actor="owner_ui", repo_path="")
        assert result["ok"], result
        assert "pending_update" not in read_install_record(drive, "demo")
    finally:
        finish.set()
        thread.join(30)
    assert not thread.is_alive() and not errors
    assert find_skill(drive, "demo").content_hash == accepted
    assert find_skill(drive, "demo").review.gate_for(accepted)["executable_review"]


def test_partial_restore_retains_evidence_and_never_replaces_payload_twice(release, monkeypatch):
    from ouroboros.marketplace import install as storage
    from ouroboros.skill_starter import at_startup

    drive, _folder, _row, _item = release
    previous, _ = _pending(release)
    real_restore = storage.restore_lifecycle_state
    with monkeypatch.context() as patch:
        patch.setattr(storage, "restore_lifecycle_state", lambda *a: ["injected state write failure"])
        at_startup(drive, {}, provider_ready=False)
    payload = find_skill(drive, "demo").skill_dir
    assert find_skill(drive, "demo").content_hash == previous
    (payload / "owner.txt").write_text("edit after partial restore", encoding="utf-8")
    assert (skill_state_dir(drive, "demo") / "update_backup.json").exists()
    assert storage.restore_lifecycle_state is real_restore
    at_startup(drive, {}, provider_ready=False)
    assert (payload / "owner.txt").read_text(encoding="utf-8") == "edit after partial restore"
    assert read_install_record(drive, "demo")["update_rejected"]["reason"] == "review_unfinished"


def test_uninstall_pending_update_cannot_resurrect_on_startup(release):
    from ouroboros.skill_starter import at_startup

    drive, _folder, _row, _item = release
    _pending(release)
    assert installer.uninstall_installed(drive, "demo")["ok"]
    at_startup(drive, {}, provider_ready=False)
    assert find_skill(drive, "demo") is None
    assert not (skill_state_dir(drive, "demo") / "update_backup.json").exists()


@pytest.mark.parametrize("trust,auto_review", [("review", False), ("trusted", False), ("review", True)])
def test_registry_update_uses_new_trust_and_retains_review_rollback(tmp_path, monkeypatch, trust, auto_review):
    from ouroboros.marketplace.clawhub import ClawHubSkillSummary
    from tests.test_marketplace_api import _patch_clawhub_install_pipeline

    drive = tmp_path / "data"
    row = {"id": "registry", "name": "Registry", "kind": "registry", "url": "https://example.invalid",
           "trust": "trusted" if trust == "review" else "review", "confirmed": True}
    monkeypatch.setenv("OUROBOROS_SKILL_SOURCES", json.dumps([row]))
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    monkeypatch.setattr("ouroboros.config.SETTINGS_PATH", drive / "settings.json")
    monkeypatch.delenv("OUROBOROS_SKILLS_REPO_PATH", raising=False)
    storage = _patch_clawhub_install_pipeline(monkeypatch, tmp_path, auto_specs=[])
    first = storage.install_skill(drive, tmp_path, slug="demo", auto_review=False)
    assert first.ok
    old = find_skill(drive, "demo")
    save_review_state(drive, "demo", SkillReviewState(status="clean", content_hash=old.content_hash))
    previous = read_install_record(drive, "demo")
    monkeypatch.setattr(storage, "_registry_info", lambda slug: ClawHubSkillSummary(slug=slug, latest_version="2.0.0"))
    original_stage = storage._stage_archive
    def stage(*args, **kwargs):
        staged = original_stage(*args, **kwargs)
        manifest = staged.staging_dir / "SKILL.md"
        manifest.write_text(manifest.read_text(encoding="utf-8").replace("1.0.0", "2.0.0"), encoding="utf-8")
        return staged
    monkeypatch.setattr(storage, "_stage_archive", stage)
    row["trust"] = trust
    monkeypatch.setenv("OUROBOROS_SKILL_SOURCES", json.dumps([row]))
    if auto_review:
        def blockers(drive, repo, name):
            skill = find_skill(drive, name)
            save_review_state(drive, name, SkillReviewState(status="blockers", content_hash=skill.content_hash))
            return "blockers", [], ""
        monkeypatch.setattr(storage, "_run_skill_review", blockers)
        result = installer.run_in_lane(drive, "update", "demo", lambda: storage.update_skill(
            drive, tmp_path, sanitized_name="demo"))
        assert result.ok and result.review_status == "blockers"
        assert find_skill(drive, "demo").content_hash == old.content_hash
        assert read_install_record(drive, "demo")["update_rejected"]["reason"] == "review_blockers"
        return
    result = installer.update_installed(drive, "demo", mode="replace", local_digest=old.content_hash)
    record = read_install_record(drive, "demo")
    assert result["basis"] == record["basis"] == trust
    if trust == "trusted":
        assert result["acceptance"]["accepted"] and not result.get("needs_review")
        assert "pending_update" not in record
    else:
        assert result["needs_review"] and "acceptance" not in result
        assert record["pending_update"]["from_digest"] == old.content_hash
        _review(drive, "blockers")
        assert find_skill(drive, "demo").content_hash == old.content_hash
        restored = read_install_record(drive, "demo")
        assert restored["release"] == previous["release"]
        assert restored["update_rejected"]["reason"] == "review_blockers"
