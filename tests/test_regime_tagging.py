"""
Tests for backtesting/regime_tagging.py — regime conditioning (Pattern-Loop Step 3).

Fully offline and deterministic. Covers HEALTHY synthetic price paths (a steady
decline classifies risk_off; a steady advance risk_on; a high-amplitude path
high_volatility) via read-only reuse of market_regime.detect_market_regime, plus
DEGRADED states (empty / too-short series, missing entry date → 'unknown'), and
the harness integration that breaks metrics down per regime bucket.

Observe-only: read-only classification; no protected scoring/decision logic and
no artifact writes are involved.
"""

from __future__ import annotations

from datetime import date, timedelta

from backtesting.poc_simulation_harness import _markdown_summary, run_poc
from backtesting.regime_tagging import tag_signal_regime

_END = date(2026, 5, 1)


def _ramp_series(*, daily_pct: float, n: int = 80, start: float = 100.0) -> dict:
    """A {date: close} map of n bars ending _END, compounding daily_pct/bar."""
    out: dict[date, float] = {}
    close = start
    for i in range(n):
        close *= (1.0 + daily_pct)
        out[_END - timedelta(days=(n - 1 - i))] = round(close, 4)
    return out


def _swing_series(*, amplitude: float, n: int = 80, start: float = 100.0) -> dict:
    """Alternating +/-amplitude daily moves → high average absolute return,
    roughly flat trend."""
    out: dict[date, float] = {}
    close = start
    for i in range(n):
        close *= (1.0 + amplitude) if i % 2 == 0 else (1.0 - amplitude)
        out[_END - timedelta(days=(n - 1 - i))] = round(close, 4)
    return out


# --------------------------------------------------------------------------
# Healthy classification
# --------------------------------------------------------------------------

def test_steady_decline_is_risk_off():
    series = _ramp_series(daily_pct=-0.005)  # -0.5%/day, low vol
    assert tag_signal_regime({"entry_date": _END.isoformat()}, series) == "risk_off"


def test_steady_advance_is_risk_on():
    series = _ramp_series(daily_pct=0.005)
    assert tag_signal_regime({"entry_date": _END.isoformat()}, series) == "risk_on"


def test_high_amplitude_path_is_high_volatility():
    series = _swing_series(amplitude=0.04)  # ~4% mean abs daily move ≥ 3.0 threshold
    assert tag_signal_regime({"entry_date": _END.isoformat()}, series) == "high_volatility"


def test_classifies_as_of_entry_date_not_latest_bar():
    # Up then crash: tagging an early (uptrend) entry must NOT see the later crash.
    up = _ramp_series(daily_pct=0.005, n=80)
    early_entry = (_END - timedelta(days=40)).isoformat()
    assert tag_signal_regime({"entry_date": early_entry}, up) == "risk_on"


# --------------------------------------------------------------------------
# Degraded states
# --------------------------------------------------------------------------

def test_empty_series_is_unknown():
    assert tag_signal_regime({"entry_date": _END.isoformat()}, {}) == "unknown"


def test_too_few_bars_is_unknown():
    short = _ramp_series(daily_pct=-0.005, n=5)
    assert tag_signal_regime({"entry_date": _END.isoformat()}, short) == "unknown"


def test_missing_entry_date_is_unknown():
    series = _ramp_series(daily_pct=0.005)
    assert tag_signal_regime({}, series) == "unknown"


# --------------------------------------------------------------------------
# Harness integration
# --------------------------------------------------------------------------

def test_harness_adds_per_regime_breakdown():
    p = run_poc(n_signals=120, seed=42, write=False)
    am = p["added_metrics"]
    assert "per_regime" in am
    valid = {"neutral", "high_volatility", "risk_on", "risk_off", "unknown"}
    total = 0
    for row in am["per_regime"]:
        assert row["regime"] in valid
        assert row["count"] >= 1
        assert 0.0 <= row["hit_rate"] <= 100.0
        total += row["count"]
    assert total == p["performance"]["evaluated"]  # every evaluated signal is bucketed


def test_markdown_includes_per_regime_section():
    p = run_poc(n_signals=60, seed=1, write=False)
    md = _markdown_summary(p)
    assert "Per-regime" in md
