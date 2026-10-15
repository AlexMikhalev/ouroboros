"""Live provider wire replay and the credential/transport consumer boundaries."""

import pytest


@pytest.mark.parametrize("configured", [False, True])
def test_google_credential_uses_its_own_settings(configured, monkeypatch):
    from ouroboros import provider_models as providers

    values = {"OPENROUTER_API_KEY": "router-test-key", "GOOGLE_API_KEY": "google-test-key" if configured else ""}
    monkeypatch.setattr(providers, "runtime_setting", lambda key, default="": values.get(key, default))
    assert providers.provider_has_credentials("google") is configured
    assert providers.provider_has_credentials_in_settings("google", values) is configured
    assert providers.PROVIDER_CREDENTIAL_GROUPS["google"] == ("GOOGLE_API_KEY",)


def test_google_secret_has_every_core_boundary():
    from ouroboros.colab_bootstrap import _SECRET_KEYS
    from ouroboros.contracts.plugin_api import FORBIDDEN_SKILL_SETTINGS
    from ouroboros.provider_models import MODEL_PROVIDER_CREDENTIAL_KEYS
    from ouroboros.safety import _PROVIDER_KEY_ENV, _REMOTE_PROVIDER_KEYS
    from ouroboros.secret_masking import MASKED_SECRET_SETTING_KEYS, looks_masked_settings_secret
    from ouroboros.settings_defaults import SETTINGS_DEFAULTS

    for keys in (_SECRET_KEYS, FORBIDDEN_SKILL_SETTINGS, MODEL_PROVIDER_CREDENTIAL_KEYS,
                 _REMOTE_PROVIDER_KEYS, MASKED_SECRET_SETTING_KEYS, SETTINGS_DEFAULTS):
        assert "GOOGLE_API_KEY" in keys
    assert _PROVIDER_KEY_ENV["google"] == "GOOGLE_API_KEY"
    assert looks_masked_settings_secret("GOOGLE_API_KEY", "***set***")
    assert not looks_masked_settings_secret("GOOGLE_API_KEY", "actual-test-value")


def test_colab_collects_google_without_treating_live_as_text_access(monkeypatch):
    from ouroboros import colab_bootstrap as colab

    requested = []

    def secret(name, *, required=True):
        requested.append((name, required))
        return "google-test-key" if name == "GOOGLE_API_KEY" else ""

    monkeypatch.setattr(colab, "get_colab_secret", secret)
    assert colab.collect_colab_secrets()["GOOGLE_API_KEY"] == "google-test-key"
    assert ("GOOGLE_API_KEY", False) in requested
    assert ("OPENROUTER_API_KEY", True) in requested
