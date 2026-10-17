"""Real bundled speech payloads follow the ordinary native seed and catalog path."""
import importlib.util
import logging
from pathlib import Path
import shutil

import pytest

from ouroboros.extension_ui_validation import validate_contributions
from ouroboros.launcher_bootstrap import _seed_skills_into, _stamp_native_seed_trust
from ouroboros.skill_loader import grant_status_for_skill, load_skill, load_enabled, save_enabled

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name,kind,enabled", [("device_voice", "tts", True), ("browser_speech", "stt", False)])
def test_actual_seed_is_valid_hash_bound_and_has_expected_enable_default(tmp_path, monkeypatch, name, kind, enabled):
    monkeypatch.setenv("OUROBOROS_TRUST_NATIVE_SEEDED_SKILLS", "true")
    data = tmp_path / "data"
    target = data / "skills"
    target.mkdir(parents=True)
    monkeypatch.setattr("ouroboros.config.SETTINGS_PATH", data / "settings.json")
    seeds = tmp_path / "seeds"
    shutil.copytree(ROOT / "skills" / name, seeds / name)
    log = logging.getLogger(__name__)
    assert _seed_skills_into(seeds, target, log) == 1
    path = target / "native" / name
    skill = load_skill(path, data)
    assert skill.name == name
    assert skill.manifest.plugin_api["version"] == "2.0"
    assert skill.review.status == "clean"
    assert skill.review.content_hash == skill.content_hash
    assert load_enabled(data, name) is enabled
    assert grant_status_for_skill(data, skill)["all_granted"] is True
    entries, errors = validate_contributions(skill.manifest, skill_dir=path)
    assert not errors and len(entries) == 1
    assert entries[0]["provides"] == kind and entries[0]["runtime"] == "client"
    assert entries[0]["render"]["kind"] == "host"
    assert "page_module" in skill.manifest.permissions
    assert ("net" in skill.manifest.permissions) is (name == "browser_speech")
    assert skill.manifest.model_experience and skill.manifest.when_to_use
    # The new seed never erases a later owner choice on restamp.
    save_enabled(data, name, not enabled)
    _stamp_native_seed_trust(data, path, log)
    assert load_enabled(data, name) is not enabled
    spec = importlib.util.spec_from_file_location(f"test_{name}_plugin", path / "plugin.py")
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    assert plugin.register(object()) is None
