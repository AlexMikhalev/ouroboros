"""Model-source onboarding uses the same atomic transaction fixtures as API setup."""

import json

from ouroboros.settings_setup_contract import ONBOARDING_COMPLETED_KEY
from tests.test_onboarding_complete_endpoint import (
    LIVE_SNAPSHOT, WIZARD_PAYLOAD, _profile, _profile_account,
    onboarding as onboarding,  # explicit fixture re-export, not another settings writer
)


def test_invalid_manual_reviewer_draft_refuses_the_whole_onboarding_write(onboarding):
    response = onboarding.client.post("/api/onboarding/complete", json={
        **WIZARD_PAYLOAD, "OUROBOROS_REVIEWER_SLOTS": '{"triad":[],"scope":[]}',
    })
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_reviewer_slots"
    assert not onboarding.settings_path.exists()


def test_api_only_preview_includes_effective_reviewer_assignments(onboarding):
    response = onboarding.client.post("/api/onboarding/subagents/preview", json=WIZARD_PAYLOAD)
    assert response.status_code == 200, response.text
    from ouroboros.reviewer_slot_config import parse_reviewer_slots
    slots = parse_reviewer_slots(response.json()["reviewer_slots"])
    assert slots.triad and slots.scope and slots.deep_review
    assert not onboarding.settings_path.exists()


def test_codex_only_preview_and_finish_share_models_agents_and_atomic_settings(onboarding):
    """One managed account supplies real model defaults and agent review, without API keys."""
    model = "claudexor::codex=provider-default"
    onboarding.calls["snapshot_payload"] = {
        **LIVE_SNAPSHOT,
        "harnesses": [LIVE_SNAPSHOT["harnesses"][1]],
        "profiles": {"harnessAccounts": [_profile_account("codex", "shared")],
                     "profiles": [_profile("codex", "shared")]},
        "model_catalog": [{"value": model, "is_default": True, "input_modalities": ["text", "image"],
                           "credential_profile_id": "shared", "max_context_window": 872000}],
    }
    draft = {"subscriptionsConnected": True, "OUROBOROS_MODEL": "", "TOTAL_BUDGET": 25.0}
    preview = onboarding.client.post("/api/onboarding/subagents/preview", json=draft)
    assert preview.status_code == 200, preview.text
    proposed = preview.json()
    assert proposed["model_settings"]["OUROBOROS_MODEL"] == model
    assert json.loads(proposed["reviewer_slots"])["deep_review"]
    assert not onboarding.settings_path.exists()
    completed = onboarding.client.post("/api/onboarding/complete", json={
        **draft, **proposed["model_settings"],
        "OUROBOROS_REVIEWER_SLOTS": proposed["reviewer_slots"],
        "OUROBOROS_MODEL_ACCOUNTS": {"main": "shared", "light": ""},
        "OUROBOROS_MODEL_CONTEXT_WINDOWS": {"main": 1000000},
    })
    assert completed.status_code == 200, completed.text
    saved = onboarding.saved()
    assert saved["OUROBOROS_MODEL"] == saved["OUROBOROS_MODEL_LIGHT"] == model
    assert not saved["OPENAI_API_KEY"] and not saved["OPENROUTER_API_KEY"]
    assert json.loads(saved["OUROBOROS_MODEL_ACCOUNTS"])["main"] == "shared"
    assert json.loads(saved["OUROBOROS_MODEL_CONTEXT_WINDOWS"])["main"] == 1000000
    assert saved[ONBOARDING_COMPLETED_KEY]
    assert onboarding.calls["supervisor"] == 1
    assert json.loads(saved["OUROBOROS_REVIEWER_SLOTS"]) == json.loads(proposed["reviewer_slots"])


def test_codex_only_finish_computes_omitted_quick_path_models(onboarding):
    onboarding.calls["snapshot_payload"] = {
        **LIVE_SNAPSHOT,
        "model_catalog": [{"value": "claudexor::codex=default", "is_default": True,
                           "input_modalities": ["text", "image"]}],
    }
    response = onboarding.client.post("/api/onboarding/complete", json={"subscriptionsConnected": True})
    assert response.status_code == 200, response.text
    assert onboarding.saved()["OUROBOROS_MODEL"] == "claudexor::codex=default"


def test_finish_preserves_visible_empty_inheritance_and_shipped_value(onboarding):
    from ouroboros.settings_defaults import SETTINGS_DEFAULTS

    onboarding.calls["snapshot_payload"] = {
        **LIVE_SNAPSHOT,
        "model_catalog": [{"value": "claudexor::codex=default", "is_default": True}],
    }
    light = SETTINGS_DEFAULTS["OUROBOROS_MODEL_LIGHT"]
    response = onboarding.client.post("/api/onboarding/complete", json={
        "subscriptionsConnected": True, "OUROBOROS_MODEL": "",
        "OUROBOROS_MODEL_LIGHT": light, "OUROBOROS_MODEL_VISION": "",
        "OUROBOROS_MODEL_FALLBACKS": "",
    })
    assert response.status_code == 200, response.text
    saved = onboarding.saved()
    assert saved["OUROBOROS_MODEL_LIGHT"] == light
    assert saved["OUROBOROS_MODEL_VISION"] == ""
    assert saved["OUROBOROS_MODEL_FALLBACKS"] == ""
