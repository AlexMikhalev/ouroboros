"""Actual Settings save and owner context ACK on exact subscription accounts.

Only model metadata and unrelated save side effects are fixtures. The browser
uses real Python save/ACK endpoints, persistent settings and capability evidence.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from starlette.routing import Route
from starlette.testclient import TestClient

from tests import test_owner_settings_write_seam as settings_fixture
from tests import test_subscription_setup_browser as browser_fixture

subscription_ui = browser_fixture.subscription_ui
isolated_settings = settings_fixture.isolated_settings
pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
MODEL = "claudexor::codex=gpt-test"
ACK_PATH = "/api/owner/capability-ack"


@pytest.fixture
def ack_ui(subscription_ui, isolated_settings, monkeypatch):
    from ouroboros import capability_evidence as evidence, config
    from ouroboros.gateway import settings as gateway
    from ouroboros.gateway.owner_settings import _owner_read_settings_raw
    from ouroboros.llm import LLMClient
    from supervisor import message_bus

    ui = subscription_ui
    page = ui["page"]
    root = isolated_settings.parent
    rows = {"triad": [{"slot_id": "triad_1", "route": {"kind": "api_chat", "target_id": "openai::gpt-api"}}],
            "scope": [{"slot_id": "scope_1", "route": {"kind": "api_chat", "target_id": "openai::gpt-before"}}],
            "advisory": {"enabled": True, "route": {"kind": "api_chat", "target_id": "openai::gpt-api"}},
            "deep_review": {"route": {"kind": "api_chat", "target_id": "openai::gpt-api"}}}
    settings = {**ui["settings"], "OUROBOROS_REVIEWER_SLOTS": json.dumps(rows),
                "OUROBOROS_SUBAGENTS": json.dumps({"enabled": True, "items": []}),
                "OUROBOROS_RUNTIME_MODE": "advanced", "OUROBOROS_CONTEXT_MODE": "max",
                "OUROBOROS_MODEL_ACCOUNTS": json.dumps({"main": "personal"}),
                "OUROBOROS_MODEL_CONTEXT_WINDOWS": "{}"}
    settings.pop("_meta", None)
    isolated_settings.write_text(json.dumps(settings), encoding="utf-8")
    for key in ("OUROBOROS_REVIEWER_SLOTS", "OUROBOROS_SUBAGENTS", "OUROBOROS_RUNTIME_MODE",
                "OUROBOROS_CONTEXT_MODE", "OUROBOROS_MODEL_ACCOUNTS", "OUROBOROS_MODEL_CONTEXT_WINDOWS"):
        monkeypatch.setenv(key, str(settings[key]))
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(isolated_settings))
    app = settings_fixture._settings_app(monkeypatch, isolated_settings)
    monkeypatch.setattr(gateway, "load_settings", _owner_read_settings_raw)
    # Preserve the existing review-row reader's env input without exporting
    # the entire settings document or starting any real background service.
    def project_env(saved):
        for key in ("OUROBOROS_REVIEWER_SLOTS", "OUROBOROS_SUBAGENTS",
                    "OUROBOROS_MODEL_ACCOUNTS", "OUROBOROS_MODEL_CONTEXT_WINDOWS"):
            value = saved.get(key, "")
            monkeypatch.setenv(key, value if isinstance(value, str) else json.dumps(value))
    monkeypatch.setattr(gateway, "_apply_settings_to_env", project_env)
    monkeypatch.setattr(message_bus, "get_bridge", lambda: SimpleNamespace(configure_from_settings=lambda _: None))
    monkeypatch.setattr(gateway, "_has_started_agent_tasks", lambda: False)
    monkeypatch.setattr(evidence, "_provider_metadata_window", lambda *_a, **_kw: 0)
    monkeypatch.setattr(evidence, "_metadata_fetch_transport_failed", lambda *_a, **_kw: False)
    monkeypatch.setattr(evidence, "_generative_probe_window", lambda *_a, **_kw: pytest.fail("No generation probe"))
    identities = {"personal": "identity-a", "work": "identity-b"}
    catalog_reads = []
    def catalog(source, credential_profile_id=None, *, requested_model=None):
        account = credential_profile_id or "personal"
        catalog_reads.append((source, account))
        return {"source": source, "credentialProfileId": account,
                "accountFingerprint": identities[account],
                "observedAt": evidence.utc_now_iso(), "provenance": "controlled exact-account metadata",
                "models": [{"id": "gpt-test", "contextWindow": 272000, "maxContextWindow": 872000}]}
    monkeypatch.setattr(LLMClient, "claudexor_model_catalog", staticmethod(catalog))
    app.routes.extend([
        Route("/api/settings", gateway.api_settings_get, methods=["GET"]),
        Route("/api/reviewer-slots", gateway.api_reviewer_slots),
        Route(ACK_PATH, gateway.api_acknowledge_capability, methods=["POST"]),
    ])
    requests, responses = [], []
    with TestClient(app) as client:
        def forward(route):
            path = urlparse(route.request.url).path
            if route.request.method == "POST":
                payload = route.request.post_data_json
                requests.append((path, payload))
                response = client.post(path, json=payload)
                responses.append((path, response.status_code, response.json()))
            else:
                response = client.get(path)
            route.fulfill(status=response.status_code, content_type="application/json", body=response.content)
        for path in ("/api/settings", "/api/reviewer-slots", ACK_PATH):
            page.route("**" + path, forward)
        page.goto(ui["url"] + "/#settings")
        page.locator('[data-settings-tab="agents"]').click()
        page.wait_for_selector('[data-slot-id="scope_1"] [data-slot-custom-api]')
        yield SimpleNamespace(ui=ui, page=page, root=root, settings_path=isolated_settings,
                              client=client, requests=requests, responses=responses,
                              identities=identities, catalog_reads=catalog_reads, evidence=evidence)
    assert config.SETTINGS_PATH == isolated_settings


def select_subscription_scope(flow, account="personal"):
    scope = flow.page.locator('[data-slot-id="scope_1"]')
    scope.locator("[data-slot-route]").select_option("subscription:codex")
    scope.locator("[data-slot-custom-api]").fill("gpt-test")
    scope.locator("[data-slot-profile]").select_option(account)
    return scope


def save_dialog(flow):
    flow.page.locator("#btn-save-settings").click()
    flow.page.wait_for_selector(".confirm-dialog")
    dialog = flow.page.locator(".confirm-dialog")
    assert dialog.locator("h3").inner_text() == "Confirm scope-reviewer context window"
    assert flow.responses[-1][1] == 200, flow.responses[-1]
    assert flow.responses[-1][0] == "/api/settings"
    return dialog


def save_settled(flow):
    flow.page.wait_for_function("() => !document.querySelector('#btn-save-settings').disabled")
    assert flow.page.locator(".confirm-dialog").count() == 0


def ack_requests(flow):
    return [body for path, body in flow.requests if path == ACK_PATH]


def test_scope_ack_is_explicit_account_scoped_and_stale_identity_cannot_authorize_b(ack_ui):
    flow = ack_ui
    select_subscription_scope(flow)
    dialog = save_dialog(flow)
    text = dialog.inner_text()
    assert "account: personal" in text and "identity: identity-a" in text
    assert "1,000,000-token" in text and "872000 tokens" in text
    assert ack_requests(flow) == []
    before_ack = flow.settings_path.read_bytes()
    browser_fixture.capture(flow.page, "scope-ack-account-a")
    dialog.locator("[data-confirm-ok]").click()
    save_settled(flow)
    first = ack_requests(flow)[0]
    assert first["options"] == {"source_id": "codex", "credential_profile_id": "personal",
                                "account_fingerprint": "identity-a"}
    assert first["window_tokens"] == 1000000 and first["route_fp"]
    assert flow.settings_path.read_bytes() == before_ack, "ACK owns evidence, not settings"
    acks = flow.evidence.list_owner_acks(flow.root)
    assert len(acks) == 1 and acks[0]["route_fp"] == first["route_fp"]

    scope = flow.page.locator('[data-slot-id="scope_1"]')
    scope.locator("[data-slot-profile]").select_option("work")
    dialog = save_dialog(flow)
    assert "account: work" in dialog.inner_text() and "identity: identity-b" in dialog.inner_text()
    notice_b = flow.responses[-1][2]["review_capability_notices"][0]["needs_ack"]
    assert notice_b["route_fp"] != first["route_fp"]
    assert len(ack_requests(flow)) == 1, "A never auto-confirms B"
    browser_fixture.capture(flow.page, "scope-ack-account-b")
    flow.page.set_viewport_size({"width": 390, "height": 844})
    assert flow.page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert dialog.locator("[data-confirm-ok]").is_visible()
    browser_fixture.capture(flow.page, "scope-ack-account-b-narrow")
    flow.page.set_viewport_size({"width": 1360, "height": 900})
    # An old A fingerprint cannot be combined with B's selected binding.
    wrong = {**first, "options": notice_b["options"]}
    refused = flow.client.post(ACK_PATH, json=wrong)
    assert refused.status_code == 400 and "fingerprint mismatch" in refused.json()["error"]
    assert len(flow.evidence.list_owner_acks(flow.root)) == 1
    # The account identity changes after the real dialog read, before its
    # confirm POST. Metadata revalidation must reject the stale browser claim.
    flow.identities["work"] = "identity-b-new"
    dialog.locator("[data-confirm-ok]").click()
    save_settled(flow)
    assert flow.responses[-1][0] == ACK_PATH and flow.responses[-1][1] == 400
    assert "not saved" in flow.page.locator("#settings-status").inner_text()
    assert len(flow.evidence.list_owner_acks(flow.root)) == 1
    b = flow.evidence.probe(flow.root, provider="claudexor", model=MODEL,
                            options={"source_id": "codex", "credential_profile_id": "work"})
    assert b.account_fingerprint == "identity-b-new" and not flow.evidence.confirms_at_least(b)
    browser_fixture.capture(flow.page, "scope-ack-stale-b-refused")


def test_direct_api_scope_ack_keeps_legacy_route_shape(ack_ui):
    flow = ack_ui
    scope = flow.page.locator('[data-slot-id="scope_1"]')
    scope.locator("[data-slot-custom-api]").fill("openai::gpt-after")
    dialog = save_dialog(flow)
    assert "provider: openai" in dialog.inner_text()
    assert "account:" not in dialog.inner_text()
    browser_fixture.capture(flow.page, "scope-ack-direct-api")
    before = flow.settings_path.read_bytes()
    dialog.locator("[data-confirm-ok]").click()
    save_settled(flow)
    body = ack_requests(flow)[0]
    assert body["provider"] == "openai" and body["model"] == "openai::gpt-after"
    assert "options" not in body
    assert flow.responses[-1][1] == 200
    assert flow.settings_path.read_bytes() == before
    assert len(flow.evidence.list_owner_acks(flow.root)) == 1


def test_manual_main_context_never_authors_scope_ack(ack_ui):
    flow = ack_ui
    select_subscription_scope(flow)
    flow.page.locator('[data-settings-tab="models"]').click()
    main = flow.page.locator('[data-model-role="main"]')
    main.locator("summary").click()
    main.locator("[data-model-role-context]").fill("1200000")
    flow.page.locator("#btn-save-settings").click()
    flow.page.wait_for_function("""() => !document.querySelector('#btn-save-settings').disabled
        || document.querySelector('.confirm-dialog')""")
    # The first save may canonicalize existing reviewer bytes and offer their
    # ordinary scope confirmation. A sizing value must never answer it.
    if flow.page.locator(".confirm-dialog").count():
        assert ack_requests(flow) == []
        assert flow.evidence.list_owner_acks(flow.root) == []
        flow.page.locator(".confirm-dialog [data-confirm-cancel]").last.click()
    save_settled(flow)
    assert ack_requests(flow) == []
    assert flow.evidence.list_owner_acks(flow.root) == []
    saved = json.loads(flow.settings_path.read_text())
    windows = saved["OUROBOROS_MODEL_CONTEXT_WINDOWS"]
    if isinstance(windows, str):
        windows = json.loads(windows)
    assert windows["main"] == 1200000
    reviewer = flow.evidence.probe(flow.root, provider="claudexor", model=MODEL,
        options={"source_id": "codex", "credential_profile_id": "personal"})
    assert reviewer.window_tokens == 872000 and not flow.evidence.confirms_at_least(reviewer)
    main.locator("summary").click()
    assert "set by you" in main.inner_text()
    browser_fixture.capture(flow.page, "scope-ack-manual-context")
