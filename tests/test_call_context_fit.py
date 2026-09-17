"""One physical request's input, headroom and actual output allowance."""

import pytest

from ouroboros.context_fit import resolve_call_context_fit


def _fit(**changes):
    values = dict(input_tokens=60_826, input_is_exact=True, caller_max_tokens=65_536,
                  total_target_tokens=81_920, route_capacity_tokens=100_000,
                  minimum_free_tokens=8192, tokenizer_template_provenance={"source": "exact test tokenizer/template"},
                  output_limit_enforced=True, reasoning_included_in_limit=True, route_capacity_confirmed=True)
    return resolve_call_context_fit(**{**values, **changes})


def test_nano_uses_available_output_not_fixed_floor_or_quarter_window():
    fit = _fit()
    assert fit.effective_max_tokens == 21_094
    assert fit.strict_bound_proven and fit.fit_status == "fits"
    assert _fit(caller_max_tokens=4096).effective_max_tokens == 4096


@pytest.mark.parametrize("input_tokens,status,proven", [
    (73728, "fits", True), (73729, "insufficient_headroom", False),
    (81921, "unfit", False),
])
def test_exact_headroom_boundary(input_tokens, status, proven):
    fit = _fit(input_tokens=input_tokens)
    assert fit.fit_status == status and fit.strict_bound_proven is proven
    assert fit.effective_max_tokens == max(0, 81920 - input_tokens)


def test_known_smaller_route_wins_and_larger_route_does_not_expand_nano():
    assert _fit(route_capacity_tokens=70000).effective_max_tokens == 9174
    assert _fit(route_capacity_tokens=1_000_000).bound_tokens == 81920


@pytest.mark.parametrize("gap,changes", [
    ("exact_input_measurement", {"input_is_exact": False}),
    ("tokenizer_template_provenance", {"tokenizer_template_provenance": None}),
    ("confirmed_serving_capacity", {"route_capacity_confirmed": False}),
    ("enforced_output_limit", {"output_limit_enforced": False}),
    ("reasoning_within_measured_window", {"reasoning_included_in_limit": None}),
])
def test_unknown_evidence_is_disclosed_without_prohibiting_the_route(gap, changes):
    fit = _fit(**changes)
    assert fit.effective_max_tokens == 21094 and fit.fit_status == "fits"
    assert not fit.strict_bound_proven and gap in fit.missing_evidence


def test_unknown_capacity_keeps_caller_allowance():
    fit = _fit(total_target_tokens=None, route_capacity_tokens=None)
    assert fit.effective_max_tokens == 65536
    assert fit.fit_status == "unknown_capacity" and not fit.strict_bound_proven


def test_unfit_estimate_does_not_manufacture_a_zero_output_request():
    fit = _fit(input_tokens=90000, input_is_exact=False)
    assert fit.fit_status == "unfit" and fit.effective_max_tokens == 65536
    assert not fit.strict_bound_proven
