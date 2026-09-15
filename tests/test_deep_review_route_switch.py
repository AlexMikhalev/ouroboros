"""Packed deep review keeps the actual role/account capacity across live waits."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from ouroboros import config, deep_self_review as deep, llm_claudexor, model_wait
from ouroboros.gateway import task_model_wait as wait_gateway
from ouroboros.llm import LLMClient
from ouroboros.reviewer_slot_config import ConfiguredReviewerSlot
from ouroboros.reviewer_window import ReviewerWindow
from ouroboros.task_results import write_task_result
from ouroboros.usage_accounting import UsageScope, usage_scope
from supervisor import queue as task_queue
from tests.test_llm_claudexor import Gateway, ledger, result


MODEL_A = "claudexor::codex=model-a"
MODEL_B = "claudexor::codex=model-b"
REPORT = "Preserved paid deep-review report\nExact complete content."
PACK = "Controlled complete pack"


@pytest.fixture
def packed(monkeypatch, tmp_path):
    root, repo = tmp_path / "data", tmp_path / "repo"
    repo.mkdir()
    task = {"id": "packed-deep", "_attempt": 1, "drive_root": str(root), "chat_id": 1}
    write_task_result(root, task["id"], "running")
    route_a = {"source": "codex", "model": "model-a", "credentialProfileId": "account-a",
               "accountFingerprint": "identity-a"}
    route_b = {**route_a, "model": "model-b", "credentialProfileId": "account-b",
               "accountFingerprint": "identity-b"}
    quota = result(route=route_a, outcome="failed", problem={
        "code": "subscription_window_exhausted", "message": "Controlled quota"})
    completed = result(route=route_b)
    completed["message"] = {"role": "assistant", "content": REPORT}
    engine = Gateway([quota, completed], ["not_started", "response_received"])
    client = LLMClient()
    row = ConfiguredReviewerSlot("deep_review_slot_1", "api_chat", MODEL_A, profile_id="account-a")
    state = SimpleNamespace(root=root, repo=repo, engine=engine, row=row, windows=[], records=[],
                            new_window=1_200_000, observed_window=None, switch_model=MODEL_B)

    def capacity(model, slot=None, *, model_route=None):
        state.windows.append((model, slot.profile_id, model_route))
        window = 1_200_000 if model == MODEL_A else state.new_window
        if model_route and state.observed_window is not None:
            window = state.observed_window
        return ReviewerWindow(window_tokens=window, status="confirmed" if window else "failed",
                              model=model, model_route=model_route or {})

    monkeypatch.setattr(llm_claudexor, "ensure_owned_gateway", lambda: engine)
    monkeypatch.setattr(deep, "provider_has_credentials", lambda _provider: True)
    monkeypatch.setattr(deep, "_resolve_packed_window", capacity)
    monkeypatch.setattr(deep, "build_review_pack", lambda *_args, **_kwargs: (PACK, {
        "file_count": 1, "total_chars": len(PACK), "skipped": [], "context_manifest": {},
        "memory": {"inlined": 0, "total": 7, "dispositions": {}}}))
    monkeypatch.setattr(deep, "_record_execution", lambda slot, usage, **kwargs:
                        state.records.append((slot, dict(usage), kwargs)))
    monkeypatch.setattr(deep, "_run_retrieving_review", lambda *_args, **_kwargs:
                        pytest.fail("packed delivery must not silently become retrieving"))
    monkeypatch.setattr(task_queue, "RUNNING", {task["id"]: {"task": task, "attempt": 1}})
    monkeypatch.setattr(task_queue, "DRIVE_ROOT", root)
    monkeypatch.setattr(config, "CLAUDEXOR_MODEL_POLL_INTERVAL_SEC", 0.001)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(root / "settings.json"))
    monkeypatch.setenv("TOTAL_BUDGET", "100")
    monkeypatch.setattr(client, "claudexor_model_sources", lambda: {
        "sources": [{"id": "codex", "credentialHarness": "codex"}]})

    with usage_scope(UsageScope(drive_root=root, task_id=task["id"], root_task_id=task["id"])):
        with model_wait.task_model_wait_scope(task=task, drive_root=root,
                event_queue=None, worker_slot_held=True) as owner:
            def switch(*_args, **_kwargs):
                current = next(value for value in owner.waits.values() if value["state"] == "waiting")
                response = wait_gateway._decide(root, {
                    "decision_id": f'model_wait:packed-deep:{current["wait_id"]}',
                    "request_id": "switch-deep-model", "revision": current["revision"],
                    "action": "switch", "model": state.switch_model,
                    "credential_profile_id": "account-b", "use_local": False, "persist_role": False})
                assert response.status_code == 202
                return {}

            monkeypatch.setattr(client, "claudexor_model_catalog", switch)
            state.run = lambda: deep.run_deep_self_review(repo, root, client, lambda _text: None, slot=state.row)
            yield state


def test_wide_route_switch_sends_once_and_reports_actual_model(packed):
    text, usage = packed.run()
    assert usage.get("execution_status") != "infra_failed", text
    assert f"model={MODEL_B}" in text and f"model={MODEL_A}" not in text
    assert "window=1200000" in text and "incomplete=none" in text
    assert text.endswith(REPORT)
    assert any(model == MODEL_B for model, _, _ in packed.windows)
    assert packed.engine.uploads[-1][0]["messages"][-1]["content"] == PACK
    assert len(packed.engine.accepted_operations) == 2
    assert sum(row["state"] == "settled" for row in ledger(packed.root)) == 1
    assert packed.records[-1][0].model == MODEL_B
    assert packed.records[-1][0].session_profile == "account-b"


@pytest.mark.parametrize("prior_dispatch", ["not_started", "response_received"])
def test_subfloor_switch_refuses_before_new_send_and_keeps_prior_custody(packed, prior_dispatch):
    packed.new_window = 200_000
    packed.engine.dispatch[0] = prior_dispatch
    text, usage = packed.run()
    assert usage["execution_status"] == "infra_failed"
    assert "200,000" in text and "1,000,000" in text
    assert len(packed.engine.accepted_operations) == 1
    assert sum(row["state"] == "settled" for row in ledger(packed.root)) == int(prior_dispatch == "response_received")
    assert any(path.name.endswith("_model_response.json")
               for path in (packed.root / "observability" / "calls").rglob("*.json"))


def test_observed_only_subfloor_keeps_paid_report_with_actual_account(packed):
    packed.observed_window = 200_000
    text, usage = packed.run()
    assert usage["execution_status"] == "infra_failed"
    assert usage["reason_code"] == "deep_self_review_unavailable"
    assert f"model={MODEL_B}" in text and "window=200000" in text
    assert "incomplete=none" not in text and text.endswith(REPORT)
    assert packed.windows[-1][2]["accountFingerprint"] == "identity-b"
    assert packed.records[-1][2]["status"] == "error"
    assert len(packed.engine.accepted_operations) == 2
    assert sum(row["state"] == "settled" for row in ledger(packed.root)) == 1
    custody = usage["claudexor"]["result_custody"]
    assert custody["state"] == "acknowledged"
    assert custody["retained_manifest_ref"]


@pytest.mark.parametrize("window", [1_200_000, 200_000])
def test_auto_actual_account_is_revalidated_without_another_generation(packed, window):
    packed.row = replace(packed.row, profile_id="")
    packed.observed_window = window
    completed = packed.engine.results[-1]
    completed["route"]["model"] = "model-a"
    packed.engine.results = [completed]
    packed.engine.dispatch = ["response_received"]
    text, usage = packed.run()
    assert (usage.get("execution_status") == "infra_failed") == (window < 1_000_000)
    assert f"model={MODEL_A}" in text and f"window={window}" in text
    assert text.endswith(REPORT)
    assert packed.engine.uploads[0][0]["account"] == {"mode": "auto"}
    assert packed.windows[-1][1] == "account-b"
    assert packed.windows[-1][2]["accountFingerprint"] == "identity-b"
    assert len(packed.engine.accepted_operations) == 1
    assert sum(row["state"] == "settled" for row in ledger(packed.root)) == 1


def test_changed_route_unknown_window_never_inherits_old_capacity(packed):
    packed.new_window = 0
    text, usage = packed.run()
    assert usage.get("execution_status") != "infra_failed", text
    assert f"model={MODEL_B}" in text and "window=unknown" in text
    assert "window=1200000" not in text and text.endswith(REPORT)
    assert "full window assumed" not in text and "window unknown" in text


def test_changed_wide_route_still_checks_full_input_cap(packed, monkeypatch):
    monkeypatch.setattr(deep, "calibrated_input_token_limit", lambda model, **_kwargs:
                        900_000 if model == MODEL_A else 1)
    text, usage = packed.run()
    assert usage["execution_status"] == "infra_failed"
    assert "input" in text.lower() and MODEL_B in text
    assert len(packed.engine.accepted_operations) == 1


def test_resolve_packed_window_forwards_observed_account(monkeypatch):
    from ouroboros import reviewer_window

    seen = []
    route = {"source": "codex", "model": "model-a", "credentialProfileId": "auto-account",
             "accountFingerprint": "observed-identity"}
    row = ConfiguredReviewerSlot("deep_review_slot_1", "api_chat", MODEL_A)
    monkeypatch.setattr(reviewer_window, "resolve_reviewer_window", lambda model, **kwargs:
                        seen.append((model, kwargs)) or ReviewerWindow(model=model))
    deep._resolve_packed_window(MODEL_A, row, model_route=route)
    assert seen == [(MODEL_A, {"use_local": False, "model_role": "reviewer:deep_review_slot_1",
                              "credential_profile_id": "", "model_route": route})]
