"""OpenRouter's typed parameter-filter refusal, and what the wire-recovery driver does with it.

Under ``provider.require_parameters`` OpenRouter routes only to endpoints that accept EVERY
requested parameter; when none is left it answers HTTP 404 with the typed receipt
``error.metadata.failed_routing_step == "Filter by Parameters"`` and names no field. Found by
the planning trials: every default Opus review seat (plan review and commit review alike) sent
``temperature=0.2`` to a model none of whose eleven endpoints takes ``temperature``, and the
field-less 404 left the driver with nothing to drop, so the seat failed on both builds. The
driver now reads the typed step: the optional sampling knobs go first, the remaining optional
carriers only if a sampling-free resend is refused the same way; a model that accepts
``temperature`` keeps it, and a refusal of another kind is not touched. Live probe 2026-10-15
(``anthropic/claude-opus-5.5``, max_tokens 16, require_parameters true): with temperature 0.2
→ 404 + this typed step; without → 200, "ok", cost $0.000152."""
from __future__ import annotations

import copy
import json

import pytest

from ouroboros.llm import LLMClient
from ouroboros.request_wire_recovery import plan_nonlearning_optional_retry, request_wire_call_scope
from ouroboros.usage_accounting import UsageScope, usage_scope
from tests.test_request_wire_recovery_phase2b import _Response, _store, _target, _tools
from tests.test_request_wire_recovery_phase2b import evidence_root as _evidence_root

evidence_root = _evidence_root  # noqa: F811 - pytest fixture re-export

ROUTED_OUT = {"error": {
    "code": 404,
    "message": "No endpoints found that can handle the requested parameters. To learn more about provider routing, "
               "visit: https://openrouter.ai/docs/provider-routing",
    "metadata": {"routing_funnel": [{"step": "Initial Endpoints", "endpoint_count": 11},
                                    {"step": "Filter by Regional Surcharge", "endpoint_count": 6}],
                 "failed_routing_step": "Filter by Parameters"},
}}


class _RoutedOut(RuntimeError):
    """The OpenAI SDK's shape of the refusal: ``status_code`` and the JSON ``body``."""

    def __init__(self, body=None, status: int = 404):
        body = copy.deepcopy(body or ROUTED_OUT)
        super().__init__(f"Error code: {status} - {json.dumps(body)}")
        self.status_code = status
        self.body = body


def _other_step():
    body = copy.deepcopy(ROUTED_OUT)
    body["error"]["metadata"]["failed_routing_step"] = "Filter by Regional Surcharge"
    return body


def _review_payload(**extra):
    """What a default review seat sends: the host's sampling hint, the reasoning carrier, tools."""
    payload = {
        "model": "anthropic/claude-opus-5.5",
        "messages": [{"role": "user", "content": "Weigh the plan before the work starts."}],
        "temperature": 0.2, "max_tokens": 64, "tools": _tools(), "tool_choice": "auto",
        "extra_body": {"reasoning": {"effort": "high", "exclude": False}, "provider": {"require_parameters": True}},
    }
    payload.update(extra)
    return payload


def _run(create, payload, tmp_path, label):
    target = _target(model="anthropic/claude-opus-5.5")
    client = LLMClient(api_key="unused")
    with request_wire_call_scope(), usage_scope(UsageScope(drive_root=tmp_path / label, task_id=label)):
        response = client._create_chat_completion_with_retries(create, payload, target)
        return client._normalize_remote_response(response.model_dump(), target, skip_cost_fetch=True)


def test_a_field_less_parameter_filter_404_drops_the_sampling_knob_and_keeps_the_rest(evidence_root, tmp_path):
    sent = []

    def create(**candidate):
        sent.append(copy.deepcopy(candidate))
        if "temperature" in candidate:
            raise _RoutedOut()
        return _Response()

    _msg, usage = _run(create, _review_payload(), tmp_path, "filtered")
    assert len(sent) == 2 and "temperature" in sent[0] and "temperature" not in sent[1]
    # Only the sampling knob left: the reasoning carrier, the tools and the require_parameters pin stay.
    assert sent[1]["extra_body"]["reasoning"] == sent[0]["extra_body"]["reasoning"]
    assert sent[1]["extra_body"]["provider"] == {"require_parameters": True} and sent[1]["tools"] == sent[0]["tools"]
    # Disclosed on the returned usage, and learned for this exact route after the exact success.
    assert "temperature" in json.dumps(usage.get("request_wire") or {})
    assert "temperature" in json.dumps(_store(evidence_root) or {})


def test_sampling_stays_where_endpoints_accept_it_and_other_refusals_are_untouched(evidence_root, tmp_path):
    accepted = []

    def accept(**candidate):
        accepted.append(copy.deepcopy(candidate))
        return _Response()

    _run(accept, _review_payload(), tmp_path, "accepted")
    assert len(accepted) == 1 and accepted[0]["temperature"] == 0.2, "a model that takes the knob keeps it"

    for label, error, payload in (
        ("other-step", _RoutedOut(_other_step()), _review_payload()),            # a 404 of another typed step
        ("rate-limit", _RoutedOut(status=429), _review_payload()),              # not a compatibility refusal
        ("nothing-optional", _RoutedOut(), _review_payload(temperature=None)),  # nothing optional was sent
    ):
        if label == "nothing-optional":
            payload.pop("temperature")
            payload["extra_body"].pop("reasoning")
        sent = []

        def refuse(**candidate):
            sent.append(copy.deepcopy(candidate))
            raise error

        with pytest.raises(_RoutedOut):
            _run(refuse, payload, tmp_path, label)
        assert len(sent) == 1, label  # one send, no blind resend


def test_the_remaining_optional_carriers_go_only_when_a_sampling_free_resend_is_refused_the_same_way(evidence_root, tmp_path):
    sent = []

    def create(**candidate):
        sent.append(copy.deepcopy(candidate))
        if "temperature" in candidate or "top_p" in candidate or "reasoning" in (candidate.get("extra_body") or {}):
            raise _RoutedOut()
        return _Response()

    _run(create, _review_payload(top_p=0.9), tmp_path, "staged")
    assert len(sent) == 3
    assert "temperature" not in sent[1] and "top_p" not in sent[1] and "reasoning" in sent[1]["extra_body"]
    assert "reasoning" not in sent[2]["extra_body"] and sent[2]["tools"] == sent[0]["tools"]


def test_the_non_learning_path_reads_the_same_typed_step():
    carrierless = {"model": "m", "messages": [{"role": "user", "content": "x"}], "temperature": 0.2, "max_tokens": 8}
    assert plan_nonlearning_optional_retry(carrierless, error=_RoutedOut()) == {
        "model": "m", "messages": [{"role": "user", "content": "x"}], "max_tokens": 8}
    assert plan_nonlearning_optional_retry(carrierless, error=_RoutedOut(_other_step())) is None
    assert plan_nonlearning_optional_retry({"model": "m", "messages": [], "max_tokens": 8}, error=_RoutedOut()) is None
