"""The rolling-24h consciousness allowance and the fold horizon that makes it readable (P3).

``consciousness_allowance.allowance_window`` reads the money consciousness
(its wakes plus every root it started) accounted in the last 24 h straight off
the ledger: roots are named by the category of their FINAL rows younger than
the fold horizon, the window sums every money row kind under those roots, the
row selection is fingerprint-memoized while the time filter runs on every
call (owner decisions В11/В18; PLAN 5.5, 5.13 п.11, 5.14 п.1). The compactor
keeps attempts younger than ``USAGE_LEDGER_FOLD_MIN_AGE_SEC`` unfolded so their
``ts`` stays the true spend time; every other compaction pin is unchanged and
runs on an aged clock (``fixtures_usage_compaction``).
"""

from __future__ import annotations

import datetime as _dt

import pytest

from ouroboros import consciousness_allowance as allowance
from ouroboros import usage_accounting as ua
from ouroboros import usage_compaction as uc
from ouroboros import usage_ledger
from tests import fixtures_usage_compaction as _fixtures
from tests.fixtures_usage_compaction import _compact, _ledger_rows, _request, _settle

data_root = _fixtures.data_root
data_root_any_tier = _fixtures.data_root_any_tier

T0 = _dt.datetime(2026, 9, 16, 12, 0, tzinfo=_dt.timezone.utc).timestamp()
HOUR = 3600.0


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()


def _at(monkeypatch, ts: float) -> None:
    """Every row written from here on carries this ledger stamp."""
    monkeypatch.setattr(usage_ledger, "utc_now_iso", lambda: _iso(ts))


def _wake(root, monkeypatch, ts, cost, task_id="wake-1"):
    _at(monkeypatch, ts)
    return _settle(root, cost=cost, cost_final=True, task_id=task_id, root_task_id=task_id,
                   category="consciousness")


def _started(root, monkeypatch, ts, cost, task_id="c-root", category="consciousness_task"):
    _at(monkeypatch, ts)
    return _settle(root, cost=cost, cost_final=True, task_id=task_id, root_task_id="c-root",
                   category=category)


# --- the window ----------------------------------------------------------------


def test_window_sums_the_whole_consciousness_tree_and_nothing_else(data_root, monkeypatch):
    monkeypatch.setenv("OUROBOROS_CONSCIOUSNESS_DAILY_USD", "20")
    _wake(data_root, monkeypatch, T0 - 2 * HOUR, 1.0)
    _started(data_root, monkeypatch, T0 - 3 * HOUR, 2.0)
    # A consolidation/review row of the SAME started tree keeps its own category yet counts.
    _started(data_root, monkeypatch, T0 - 1 * HOUR, 0.5, task_id="c-root-consolidation", category="consolidation")
    # The owner's own work never enters the window.
    _at(monkeypatch, T0 - 1 * HOUR)
    _settle(data_root, cost=50.0, cost_final=True, task_id="owner", root_task_id="owner", category="task")
    window = allowance.allowance_window(data_root, now=T0)
    assert window["status"] == "available"
    assert window["accounted_usd"] == pytest.approx(3.5)
    assert window["remaining_usd"] == pytest.approx(16.5)
    assert window["roots"] == ["c-root", "wake-1"]
    assert window["window_rows"] == 3 and window["unknown_unmetered"] == 0
    assert window["resets_at"] == _iso(T0 - 3 * HOUR + 24 * HOUR)  # the oldest counted spend leaves first
    assert allowance.remaining_allowance_usd(data_root, now=T0) == pytest.approx(16.5)


def test_every_money_row_kind_counts_but_folded_aggregates_never_do(data_root, monkeypatch):
    monkeypatch.setenv("OUROBOROS_CONSCIOUSNESS_DAILY_USD", "20")
    _started(data_root, monkeypatch, T0 - 5 * HOUR, 1.0)
    _at(monkeypatch, T0 - 4 * HOUR)
    ua.record_subscription_session("sess-c", drive_root=data_root, route="claudexor:claude", model="fable",
                                   task_id="c-child", root_task_id="c-root", spend_usd=0.75,
                                   reset_at="2026-09-17T00:00:00Z", category="subagent")
    ua.record_unmetered_external_dispatch("ext-c", drive_root=data_root, model="ext-model", task_id="c-child",
                                          root_task_id="c-root", prompt_tokens=7, completion_tokens=3)
    _fixtures._append_raw_row(data_root, {
        "kind": "usage_baseline_group", "attempt_id": "baseline-x-g0001", "state": "settled",
        "ts": _iso(T0 - HOUR), "cost_usd": "99", "cost_final": True, "model": "m", "provider": "p",
        "category": "consciousness_task", "source": "test", "task_id": "c-root", "root_task_id": "c-root",
        "parent_task_id": "", "folded_attempt_count": 3, "baseline_id": "baseline-x",
    })
    window = allowance.allowance_window(data_root, now=T0)
    assert window["accounted_usd"] == pytest.approx(1.75)
    assert window["unknown_unmetered"] == 1  # the external dispatch: cost unknown, counted as "at least"
    assert window["window_rows"] == 3


def test_the_time_filter_runs_on_every_call_while_the_selection_is_memoized(data_root, monkeypatch):
    monkeypatch.setenv("OUROBOROS_CONSCIOUSNESS_DAILY_USD", "3")
    _wake(data_root, monkeypatch, T0 - 20 * HOUR, 2.0)
    _wake(data_root, monkeypatch, T0 - 2 * HOUR, 1.5, task_id="wake-2")
    exhausted = allowance.allowance_window(data_root, now=T0)
    assert exhausted["status"] == "exhausted" and exhausted["accounted_usd"] == pytest.approx(3.5)
    assert exhausted["resets_at"] == _iso(T0 + 4 * HOUR)
    renders = 0
    original = allowance._candidate_rows

    def counting(final, degraded):
        nonlocal renders
        renders += 1
        return original(final, degraded)

    monkeypatch.setattr(allowance, "_candidate_rows", counting)
    # No new ledger rows: the same memoized selection, re-filtered by the clock.
    later = allowance.allowance_window(data_root, now=T0 + 4 * HOUR + 1)
    assert later["status"] == "available" and later["accounted_usd"] == pytest.approx(1.5)
    empty = allowance.allowance_window(data_root, now=T0 + 24 * HOUR + 1)
    assert empty["status"] == "available" and empty["accounted_usd"] == 0.0
    assert empty["resets_at"] == "" and empty["window_rows"] == 0
    # The roots are still attributable inside the 48 h horizon; past it they drop out too.
    assert empty["roots"] == ["wake-1", "wake-2"]
    assert allowance.allowance_window(data_root, now=T0 + 48 * HOUR + 1)["roots"] == []
    assert renders == 0, "the selection was served from the fingerprint memo"


def test_a_root_whose_consciousness_rows_aged_past_the_horizon_drops_out(data_root, monkeypatch):
    monkeypatch.setenv("OUROBOROS_CONSCIOUSNESS_DAILY_USD", "20")
    # The started root's own (category-bearing) rows are 50 h old; a later consolidation
    # row of the same tree is fresh. Without a root index the tree is no longer attributable.
    _started(data_root, monkeypatch, T0 - 49 * HOUR, 5.0)
    _started(data_root, monkeypatch, T0 - 4 * HOUR, 4.0, task_id="c-root-consolidation", category="consolidation")
    window = allowance.allowance_window(data_root, now=T0)
    assert window["roots"] == [] and window["accounted_usd"] == 0.0
    # Two hours earlier the tree was still inside the horizon and its consolidation counted.
    inside = allowance.allowance_window(data_root, now=T0 - 2 * HOUR)
    assert inside["roots"] == ["c-root"] and inside["accounted_usd"] == pytest.approx(4.0)


def test_zero_allowance_means_consciousness_may_not_spend(data_root, monkeypatch):
    monkeypatch.setenv("OUROBOROS_CONSCIOUSNESS_DAILY_USD", "0")
    window = allowance.allowance_window(data_root, now=T0)
    assert window["status"] == "exhausted" and window["limit_usd"] == 0.0
    assert allowance.remaining_allowance_usd(data_root, now=T0) == 0.0


def test_an_unreadable_ledger_is_the_typed_unknown_outcome(data_root, monkeypatch):
    monkeypatch.setenv("OUROBOROS_CONSCIOUSNESS_DAILY_USD", "20")

    def boom(root, key, render):
        raise OSError("ledger locked")

    monkeypatch.setattr(allowance, "_render_cached", boom)
    window = allowance.allowance_window(data_root, now=T0)
    assert window["status"] == "allowance_unknown" and "ledger locked" in window["error"]
    assert window["limit_usd"] == 20.0 and window["remaining_usd"] is None
    assert allowance.remaining_allowance_usd(data_root, now=T0) is None


def test_row_ts_epoch_reads_the_appender_stamp_and_refuses_to_guess():
    from ouroboros._usage_rows import row_ts_epoch

    assert row_ts_epoch({"ts": "2026-09-16T12:00:00+00:00"}) == T0
    assert row_ts_epoch({"ts": "2026-09-16T12:00:00Z"}) == T0
    assert row_ts_epoch({"ts": "2026-09-16T12:00:00"}) == T0  # naive = UTC
    assert row_ts_epoch({"ts": "not a time"}) is None
    assert row_ts_epoch({}) is None and row_ts_epoch(None) is None


# --- the fold horizon --------------------------------------------------------------


def test_compaction_keeps_attempts_younger_than_the_horizon_unfolded(data_root, monkeypatch):
    """Fresh chains stay in the live file with their own ``ts``; the money is untouched."""
    monkeypatch.setattr(uc, "_fold_clock", lambda: T0)
    _at(monkeypatch, T0 - 3 * 24 * HOUR)
    old = _settle(data_root, cost=1.25, cost_final=True, task_id="old", root_task_id="old")
    _at(monkeypatch, T0 - 2 * HOUR)
    fresh = _settle(data_root, cost=0.75, cost_final=True, task_id="fresh", root_task_id="fresh",
                    category="consciousness")
    before = ua.usage_projection(data_root)["settled_usd"]
    receipt = _compact(data_root)
    assert receipt is not None and receipt["folded_attempt_count"] == 1
    live = {str(row.get("attempt_id")): row for row in _ledger_rows(data_root)}
    assert old.attempt_id not in live and fresh.attempt_id in live
    assert live[fresh.attempt_id]["ts"] == _iso(T0 - 2 * HOUR)
    assert ua.usage_projection(data_root)["settled_usd"] == pytest.approx(before)
    # The allowance still sees the fresh spend after the pass.
    assert allowance.allowance_window(data_root, now=T0)["accounted_usd"] == pytest.approx(0.75)


def test_nothing_folds_while_every_attempt_is_inside_the_horizon(data_root, monkeypatch):
    monkeypatch.setattr(uc, "_fold_clock", lambda: T0)
    _at(monkeypatch, T0 - HOUR)
    _settle(data_root, cost=1.0, cost_final=True)
    _settle(data_root, cost=2.0, cost_final=True, task_id="t2")
    assert _compact(data_root) is None  # "nothing foldable" is an honest abort
    assert all(row.get("kind", "attempt") == "attempt" for row in _ledger_rows(data_root))
    # The same rows fold once the horizon has passed (the fixtures' aged clock).
    monkeypatch.setattr(uc, "_fold_clock", lambda: T0 + uc.USAGE_LEDGER_FOLD_MIN_AGE_SEC + 1)
    assert _compact(data_root)["folded_attempt_count"] == 2


def test_the_horizon_is_the_config_ssot_constant():
    from ouroboros import config, runtime_limits

    assert config.USAGE_LEDGER_FOLD_MIN_AGE_SEC == runtime_limits.USAGE_LEDGER_FOLD_MIN_AGE_SEC == 48 * 3600
    assert uc.USAGE_LEDGER_FOLD_MIN_AGE_SEC == allowance.ROOT_HORIZON_SEC == 2 * allowance.WINDOW_SEC


def test_foldable_ids_take_an_explicit_clock(data_root, monkeypatch):
    _at(monkeypatch, T0 - HOUR)
    _settle(data_root, cost=1.0, cost_final=True)
    with ua._locked(data_root):
        records = usage_ledger._read_records_locked(data_root)
    assert uc._foldable_attempt_ids(records, now_ts=T0) == set()
    assert len(uc._foldable_attempt_ids(records, now_ts=T0 + 49 * HOUR)) == 1
    assert _request(data_root).category == ""  # the fixture request leaves category to the scope
