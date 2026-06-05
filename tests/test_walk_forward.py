"""
Tests for backtesting/walk_forward.py — walk-forward / out-of-sample engine
(Pattern-Loop Step 2).

Fully offline and deterministic (no network, no API keys): signals are evaluated
through the real FMPBacktester driven by the harness's SyntheticPriceProvider, so
the fold-splitting, aggregation, and Wilson-CI logic is exercised against real
forward-return evaluation rather than a mock.

Covers a HEALTHY state (a time-spread signal set → populated OOS test folds, each
annotated with n and a Wilson 95% CI) and DEGRADED states (too few signals →
every fold 'insufficient'; empty / undatable signals → no crash), per the repo's
analysis+health coverage rule.

Observe-only: this engine only reads the public FMPBacktester API and computes
out-of-sample summary stats; nothing is written and no protected scoring/decision
logic is touched.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from backtesting.fmp_backtester import FMPBacktester
from backtesting.poc_simulation_harness import SyntheticPriceProvider
from backtesting.walk_forward import oos_window_status, walk_forward, wilson_interval


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

_END = date(2026, 5, 1)  # fixed provider end → deterministic synthetic price paths


def _make_bt() -> FMPBacktester:
    return FMPBacktester(SyntheticPriceProvider(seed=7, end=_END), years_default=3)


def _spread_signals(*, n: int, span_days: int, symbols: int = 6,
                    latest_offset: int = 40) -> list[dict]:
    """n signals with distinct, evenly-spread scan_time dates ending
    `latest_offset` days before the provider end (so the forward window has
    price data), cycling across a small symbol universe for return variety."""
    latest = _END - timedelta(days=latest_offset)
    step = max(span_days // max(n, 1), 1)
    out: list[dict] = []
    for i in range(n):
        d = latest - timedelta(days=i * step)
        out.append({
            "ticker": f"SYM{i % symbols:02d}",
            "scan_time": d.isoformat(),
            "signal_score": round((i % 10) / 10.0, 4),
            "confidence_score": round((i % 7) / 7.0, 4),
            "pattern": "STRONG_MOVE_UP",
        })
    return out


# --------------------------------------------------------------------------
# Wilson interval (pure function)
# --------------------------------------------------------------------------

def test_wilson_interval_known_value():
    # 8 wins of 10 at 95% → Wilson ≈ (0.490, 0.943); hand-computed above.
    low, high = wilson_interval(8, 10)
    assert math.isclose(low, 0.490, abs_tol=0.01)
    assert math.isclose(high, 0.943, abs_tol=0.01)
    assert low < 0.8 < high  # brackets the point estimate


def test_wilson_interval_clamped_to_unit_range():
    lo0, hi0 = wilson_interval(0, 5)
    lo1, hi1 = wilson_interval(5, 5)
    assert lo0 >= 0.0 and hi0 <= 1.0
    assert lo1 >= 0.0 and hi1 <= 1.0
    assert lo0 == 0.0  # zero wins → lower bound clamped at 0
    assert hi1 == 1.0  # all wins → upper bound clamped at 1


def test_wilson_interval_zero_n_is_safe():
    low, high = wilson_interval(0, 0)
    assert (low, high) == (0.0, 0.0)  # no data → degenerate, no divide-by-zero


# --------------------------------------------------------------------------
# Healthy state
# --------------------------------------------------------------------------

def test_healthy_folds_populated_with_cis():
    signals = _spread_signals(n=60, span_days=120)
    report = walk_forward(
        signals, _make_bt(),
        train_days=40, test_days=30, step_days=30,
        forward_days=5, min_signals_per_fold=3,
    )
    assert report["observe_only"] is True
    ok = [f for f in report["folds"] if f["status"] == "ok"]
    assert ok, "expected at least one populated OOS test fold"
    for f in ok:
        assert f["n"] >= 3
        assert f["hit_rate"] is not None
        ci = f["hit_rate_ci95"]
        assert isinstance(ci, list) and len(ci) == 2
        assert 0.0 <= ci[0] <= f["hit_rate"] <= ci[1] <= 100.0


def test_healthy_aggregate_is_out_of_sample():
    signals = _spread_signals(n=60, span_days=120)
    report = walk_forward(
        signals, _make_bt(),
        train_days=40, test_days=30, step_days=30,
        forward_days=5, min_signals_per_fold=3,
    )
    agg = report["aggregate"]
    assert agg["status"] == "ok"
    assert agg["n"] > 0
    assert agg["folds_ok"] >= 1
    assert isinstance(agg["hit_rate_ci95"], list) and len(agg["hit_rate_ci95"]) == 2
    assert 0.0 <= agg["hit_rate_ci95"][0] <= agg["hit_rate"] <= agg["hit_rate_ci95"][1] <= 100.0


def test_first_train_window_is_excluded_from_oos():
    # Signals only inside the very first train window must never be evaluated OOS.
    signals = _spread_signals(n=60, span_days=120)
    report = walk_forward(
        signals, _make_bt(),
        train_days=40, test_days=30, step_days=30,
        forward_days=5, min_signals_per_fold=3,
    )
    earliest = min(s["scan_time"] for s in signals)
    for f in report["folds"]:
        assert f["test_start"] > earliest  # test windows start after burn-in


# --------------------------------------------------------------------------
# Degraded states
# --------------------------------------------------------------------------

def test_too_few_signals_all_folds_insufficient():
    signals = _spread_signals(n=4, span_days=120)  # below default min of 30
    report = walk_forward(signals, _make_bt(), forward_days=5)  # default gates
    assert all(f["status"] == "insufficient" for f in report["folds"])
    assert all(f["hit_rate"] is None for f in report["folds"])
    assert report["aggregate"]["status"] == "insufficient"
    assert report["aggregate"]["hit_rate"] is None


def test_empty_signals_no_crash():
    report = walk_forward([], _make_bt())
    assert report["observe_only"] is True
    assert report["folds"] == []
    assert report["aggregate"]["status"] == "insufficient"
    assert report["aggregate"]["n"] == 0


def test_undatable_signals_no_crash():
    signals = [{"ticker": "SYM00"}, {"ticker": "SYM01", "scan_time": ""}]
    report = walk_forward(signals, _make_bt(), min_signals_per_fold=1)
    assert report["folds"] == []  # nothing datable → no folds, no crash
    assert report["aggregate"]["status"] == "insufficient"


# --------------------------------------------------------------------------
# oos_window_status — calendar-day maturity countdown (pure)
# --------------------------------------------------------------------------

class TestOosWindowStatus:
    def _sig(self, iso: str) -> dict:
        return {"ticker": "AAA", "scan_time": iso}

    def test_short_history_not_yet_mature(self):
        sigs = [self._sig("2026-04-28"), self._sig("2026-05-15"), self._sig("2026-06-05")]
        ow = oos_window_status(sigs, today=date(2026, 6, 5))
        assert ow["calendar_days_observed"] == 38
        assert ow["first_fold_threshold_days"] == 252
        assert ow["full_window_days"] == 315
        assert ow["folds_possible"] is False
        assert ow["days_until_full_window"] == 277
        assert ow["full_window_eta"] == "2027-03-09"
        assert ow["earliest_signal"] == "2026-04-28"
        assert ow["latest_signal"] == "2026-06-05"
        assert ow["estimate"] is True

    def test_first_fold_boundary(self):
        early = date(2026, 1, 1)
        ow = oos_window_status(
            [self._sig(early.isoformat()),
             self._sig((early + timedelta(days=252)).isoformat())],
            today=date(2026, 9, 10),
        )
        assert ow["calendar_days_observed"] == 252
        assert ow["folds_possible"] is True
        assert ow["days_until_full_window"] == 63

    def test_mature_window_zero_remaining(self):
        ow = oos_window_status(
            [self._sig("2026-01-01"), self._sig("2027-01-01")],
            today=date(2027, 1, 1),
        )
        assert ow["folds_possible"] is True
        assert ow["days_until_full_window"] == 0
        assert ow["full_window_eta"] == "2027-01-01"

    def test_empty_signals_never_raises(self):
        ow = oos_window_status([], today=date(2026, 6, 5))
        assert ow["calendar_days_observed"] == 0
        assert ow["folds_possible"] is False
        assert ow["full_window_eta"] is None
        assert ow["earliest_signal"] is None

    def test_undatable_signals_treated_as_empty(self):
        ow = oos_window_status([{"ticker": "AAA"}, {"scan_time": "not-a-date"}],
                               today=date(2026, 6, 5))
        assert ow["calendar_days_observed"] == 0
        assert ow["folds_possible"] is False
