"""Offline diagnostics for the live smoke's failed and successful review paths."""

import json
from types import SimpleNamespace

import pytest

from ouroboros.skill_review import _skill_quorum_failure_outcome
from ouroboros.skill_review_prompt import _SKILL_REVIEW_ITEMS
from ouroboros.triad_review import parse_model_review_results
from tests import test_skill_smoke_official as smoke
from tests._skill_review_shared import _make_actor


@pytest.mark.parametrize("prior_review", [False, True])
@pytest.mark.parametrize("provider_error", [False, True])
def test_smoke_diagnostics_show_current_quorum_failure(tmp_path, monkeypatch,
                                                     prior_review, provider_error):
    monkeypatch.setattr(smoke, "DATA_DIR", tmp_path)
    name = "smoke-diagnostic"
    state = smoke._skill_state_dir(name)
    if prior_review:
        state.mkdir(parents=True)
        (state / "review.json").write_text(json.dumps({
            "status": "clean", "content_hash": "old-hash", "raw_actor_records": [],
        }), encoding="utf-8")
    # Both transport failures and malformed answers miss the same quorum. Keep
    # enough text to expose accidental diagnostic truncation, including its end.
    raw = ("403 Inference is blocked on this account" if provider_error
           else "I have not completed the review") + " evidence" * 2000 + " END-WITNESS"
    actor = _make_actor(smoke._REVIEW_MODEL, raw)
    if provider_error:
        actor["verdict"] = "ERROR"
    result = {"results": [actor]}
    parsed = parse_model_review_results(result, required_items=_SKILL_REVIEW_ITEMS)
    assert not parsed.responsive_models
    outcome = _skill_quorum_failure_outcome(
        SimpleNamespace(name=name), findings=parsed.findings,
        models=[smoke._REVIEW_MODEL], content_hash="current-hash", required_quorum=1,
        result_json_text=json.dumps(result), parsed_review=parsed, advisory_evidence={},
        single_reviewer_no_diversity=True, drive_root=tmp_path, persist=True,
    )
    assert outcome.status == "pending"
    assert (state / "review.json").exists() is prior_review
    diagnostic = smoke._review_diag(name, outcome.status, outcome.error,
                                   {"review_findings": outcome.findings})
    assert json.dumps(raw) in diagnostic, diagnostic
    assert "current-hash" in diagnostic, diagnostic


def test_smoke_diagnostics_keep_successful_review_actors(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "DATA_DIR", tmp_path)
    state = smoke._skill_state_dir("clean")
    state.mkdir(parents=True)
    actors = [{"raw_text": "complete successful review"}]
    (state / "review.json").write_text(json.dumps({
        "reviewer_models": [smoke._REVIEW_MODEL], "raw_actor_records": actors,
    }), encoding="utf-8")
    diagnostic = smoke._review_diag("clean", "clean", "", {"review_findings": []})
    assert smoke._REVIEW_MODEL in diagnostic
    assert json.dumps(actors) in diagnostic
