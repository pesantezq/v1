"""
Tests for observe-only confidence calibration layer.

Covers:
  A. Healthy separation
  B. Insufficient data (sample size)
  C. Insufficient data (no 5d returns)
  D. Weak high-band separation
  E. Medium/low overlap
  F. Inverted band order
  G. Observe-only guarantee
  H. to_dict + JSON serialization
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional

import pytest

from profit_attribution.models import ConfidenceCalibrationResult, StrategyPerformance
from profit_attribution.confidence_calibration import (
    MIN_BAND_MATCHED,
    MIN_TOTAL_MATCHED,
    SEPARATION_THRESHOLD,
    STRONG_SEPARATION,
    calibrate_confidence_bands,
)


# ---------------------------------------------------------------------------
# Helpers — build StrategyPerformance buckets from raw numbers
# ---------------------------------------------------------------------------

def _make_band(
    name: str,
    attributable: int = 0,
    returns_5d: Optional[List[float]] = None,
    mfe_values: Optional[List[float]] = None,
) -> StrategyPerformance:
    """Build a minimal StrategyPerformance bucket for calibration tests."""
    b = StrategyPerformance(name=name, dimension="exec_confidence_band")
    b.attributable = attributable
    b.total_entries = attributable
    if returns_5d:
        b.returns_5d = returns_5d
        b.entries_with_5d = len(returns_5d)
        b.gains  = [r for r in returns_5d if r > 0]
        b.losses = [r for r in returns_5d if r <= 0]
        b.hit_count = len(b.gains)
    if mfe_values:
        b.mfe_values = mfe_values
    return b


def _bands(
    low_matched: int, low_returns: List[float],
    med_matched: int, med_returns: List[float],
    high_matched: int, high_returns: List[float],
) -> List[StrategyPerformance]:
    return [
        _make_band("low",    low_matched,  low_returns),
        _make_band("medium", med_matched,  med_returns),
        _make_band("high",   high_matched, high_returns),
    ]


# ---------------------------------------------------------------------------
# A. Healthy separation
# ---------------------------------------------------------------------------

def test_healthy_strong_separation():
    """High clearly better than medium, medium clearly better than low."""
    bs = _bands(
        low_matched=8,  low_returns=[0.03, -0.02, -0.03, -0.01, 0.01, -0.04, -0.02, 0.02],  # 3/8 wins ≈ 37%
        med_matched=10, med_returns=[0.04, 0.03, 0.05, -0.01, 0.02, 0.03, -0.02, 0.01, 0.04, -0.01],  # 8/10 wins = 60%
        high_matched=12, high_returns=[0.05]*9 + [-0.01]*3,  # 9/12 wins = 75%
    )
    result = calibrate_confidence_bands(bs)
    assert result.status == "healthy"
    assert result.band_order_valid is True
    assert result.strongest_band == "high"
    assert result.weakest_band == "low"
    assert result.observe_only is True
    assert "calibrated" in result.recommendation.lower()


def test_healthy_strong_separation_triggers_strong_message():
    """Gap >= STRONG_SEPARATION (10pp) gets the 'materially outperform' message."""
    # high=80%, medium=55%, low=30% → 25pp and 25pp gaps
    bs = _bands(
        low_matched=10, low_returns=[0.03]*3 + [-0.03]*7,   # 30% win
        med_matched=10, med_returns=[0.03]*5 + [-0.02]*5,   # 50% ... let's make it explicit
        high_matched=10, high_returns=[0.04]*8 + [-0.01]*2, # 80% win
    )
    # medium here is 50%, high is 80% → 30pp gap >= 10pp
    result = calibrate_confidence_bands(bs)
    assert result.status == "healthy"
    assert "materially" in result.recommendation


def test_healthy_band_order_valid_flag():
    bs = _bands(
        low_matched=6,  low_returns=[0.01, -0.02, -0.03, 0.02, -0.01, -0.01],
        med_matched=8,  med_returns=[0.03, 0.02, 0.04, -0.01, 0.01, 0.03, -0.02, 0.02],
        high_matched=10, high_returns=[0.05]*8 + [-0.01]*2,
    )
    result = calibrate_confidence_bands(bs)
    assert result.band_order_valid is True


# ---------------------------------------------------------------------------
# B. Insufficient data — sample size
# ---------------------------------------------------------------------------

def test_insufficient_data_total_too_small():
    """Total matched < MIN_TOTAL_MATCHED → insufficient_data."""
    bs = _bands(
        low_matched=2, low_returns=[0.03, -0.02],
        med_matched=2, med_returns=[0.04, -0.01],
        high_matched=3, high_returns=[0.05, 0.03, -0.01],
    )
    result = calibrate_confidence_bands(bs)
    assert result.status == "insufficient_data"
    assert result.band_order_valid is None
    assert result.observe_only is True
    assert "insufficient" in result.recommendation.lower()


def test_insufficient_data_high_band_too_small():
    """Total adequate but high band < MIN_BAND_MATCHED → insufficient_data."""
    bs = _bands(
        low_matched=8,  low_returns=[-0.01]*8,
        med_matched=8,  med_returns=[0.03]*5 + [-0.02]*3,
        high_matched=2, high_returns=[0.05, 0.03],   # too few
    )
    result = calibrate_confidence_bands(bs)
    assert result.status == "insufficient_data"
    assert str(MIN_BAND_MATCHED) in result.recommendation


def test_insufficient_data_medium_band_too_small():
    """Total adequate but medium band < MIN_BAND_MATCHED → insufficient_data."""
    bs = _bands(
        low_matched=10, low_returns=[-0.01]*10,
        med_matched=3,  med_returns=[0.03]*2 + [-0.02],   # too few
        high_matched=10, high_returns=[0.05]*8 + [-0.01]*2,
    )
    result = calibrate_confidence_bands(bs)
    assert result.status == "insufficient_data"


def test_insufficient_data_empty_bands():
    bs = _bands(0, [], 0, [], 0, [])
    result = calibrate_confidence_bands(bs)
    assert result.status == "no_data"
    assert result.band_order_valid is None
    assert result.strongest_band is None


def test_no_data_empty_list():
    result = calibrate_confidence_bands([])
    assert result.status == "no_data"
    assert result.observe_only is True


# ---------------------------------------------------------------------------
# C. Insufficient data — no 5d returns yet
# ---------------------------------------------------------------------------

def test_insufficient_data_no_win_rate_for_high():
    """Enough matched events but no 5d return data in high band."""
    bs = [
        _make_band("low",    8, returns_5d=[-0.01, -0.02, 0.01, -0.03, -0.01, 0.02, -0.01, -0.01]),
        _make_band("medium", 8, returns_5d=[0.03, 0.02, -0.01, 0.01, 0.04, -0.02, 0.03, 0.02]),
        _make_band("high",   8, returns_5d=None),  # no 5d data
    ]
    result = calibrate_confidence_bands(bs)
    assert result.status == "insufficient_data"
    assert "5-day" in result.recommendation or "5d" in result.recommendation_reason


def test_insufficient_data_no_win_rate_for_medium():
    """Enough matched events but no 5d return data in medium band."""
    bs = [
        _make_band("low",    6, returns_5d=[-0.01]*6),
        _make_band("medium", 8, returns_5d=None),   # no 5d data
        _make_band("high",   8, returns_5d=[0.05]*7 + [-0.01]),
    ]
    result = calibrate_confidence_bands(bs)
    assert result.status == "insufficient_data"


# ---------------------------------------------------------------------------
# D. Weak high-band separation
# ---------------------------------------------------------------------------

def test_weak_high_medium_gap():
    """High barely better than medium — less than SEPARATION_THRESHOLD."""
    # high=58%, medium=55%, low=40%
    bs = _bands(
        low_matched=10, low_returns=[0.02]*4 + [-0.02]*6,   # 40%
        med_matched=10, med_returns=[0.03]*5 + [-0.02]*4 + [-0.01],  # ~55%... let me be precise
        high_matched=10, high_returns=[0.03]*5 + [-0.02]*4 + [-0.01],  # same as medium
    )
    # Both medium and high are 50% win rate here → 0pp gap < 5pp
    result = calibrate_confidence_bands(bs)
    assert result.status == "weak_separation"
    assert "high" in result.recommendation.lower()
    assert "medium" in result.recommendation.lower()
    assert result.observe_only is True


def test_weak_high_medium_gap_recommendation_text():
    """Weak high-medium gap recommendation mentions raising the threshold."""
    # high=62%, medium=60%, low=40% → high-medium gap = 2pp < 5pp
    bs = _bands(
        low_matched=10, low_returns=[0.03]*4 + [-0.02]*6,
        med_matched=10, med_returns=[0.03]*6 + [-0.02]*4,
        high_matched=10, high_returns=[0.03]*6 + [-0.02]*4,  # same win rate as medium
    )
    result = calibrate_confidence_bands(bs)
    assert result.status == "weak_separation"
    # Should recommend raising the high threshold
    assert "0.80" in result.recommendation or "high" in result.recommendation.lower()


# ---------------------------------------------------------------------------
# E. Medium/low overlap
# ---------------------------------------------------------------------------

def test_medium_low_overlap():
    """Medium and low bands have similar win rates — weak separation."""
    # high=80%, medium=42%, low=40%
    bs = _bands(
        low_matched=10,  low_returns=[0.02]*4 + [-0.02]*6,    # 40%
        med_matched=10,  med_returns=[0.02]*4 + [-0.02]*5 + [-0.01],  # ~40%
        high_matched=10, high_returns=[0.05]*8 + [-0.01]*2,   # 80%
    )
    result = calibrate_confidence_bands(bs)
    assert result.status == "weak_separation"
    assert "medium" in result.recommendation.lower() or "low" in result.recommendation.lower()
    # medium-low overlap issue should be mentioned
    assert "0.65" in result.recommendation or "medium" in result.recommendation.lower()


def test_medium_low_overlap_but_high_healthy():
    """High clearly differentiates but medium/low are flat — still weak_separation."""
    bs = _bands(
        low_matched=10,  low_returns=[0.02]*5 + [-0.02]*5,   # 50%
        med_matched=10,  med_returns=[0.02]*5 + [-0.02]*5,   # 50% (same as low)
        high_matched=10, high_returns=[0.04]*8 + [-0.01]*2,  # 80%
    )
    result = calibrate_confidence_bands(bs)
    assert result.status == "weak_separation"
    assert "medium" in result.recommendation.lower() or "low" in result.recommendation.lower()


# ---------------------------------------------------------------------------
# F. Inverted band order
# ---------------------------------------------------------------------------

def test_inverted_band_order():
    """High worse than medium or medium worse than low → weak_separation."""
    # High is actually performing worst
    bs = _bands(
        low_matched=10,  low_returns=[0.03]*7 + [-0.02]*3,   # 70%
        med_matched=10,  med_returns=[0.03]*6 + [-0.02]*4,   # 60%
        high_matched=10, high_returns=[0.03]*3 + [-0.02]*7,  # 30% (inverted!)
    )
    result = calibrate_confidence_bands(bs)
    assert result.status == "weak_separation"
    assert result.band_order_valid is False
    assert "inverted" in result.recommendation.lower()


def test_inverted_band_order_recommendation():
    """Inverted order recommendation mentions confidence scoring review."""
    bs = _bands(
        low_matched=8,   low_returns=[0.03]*6 + [-0.02]*2,   # 75%
        med_matched=8,   med_returns=[0.03]*5 + [-0.02]*3,   # 62%
        high_matched=8,  high_returns=[0.03]*2 + [-0.02]*6,  # 25% (worst!)
    )
    result = calibrate_confidence_bands(bs)
    assert result.band_order_valid is False
    assert "review" in result.recommendation.lower() or "inverted" in result.recommendation.lower()


# ---------------------------------------------------------------------------
# G. Observe-only guarantee
# ---------------------------------------------------------------------------

def test_observe_only_always_true_healthy():
    bs = _bands(
        low_matched=8,  low_returns=[0.02]*3 + [-0.02]*5,
        med_matched=8,  med_returns=[0.03]*5 + [-0.02]*3,
        high_matched=10, high_returns=[0.05]*8 + [-0.01]*2,
    )
    result = calibrate_confidence_bands(bs)
    assert result.observe_only is True


def test_observe_only_always_true_weak():
    bs = _bands(
        low_matched=8,  low_returns=[0.02]*4 + [-0.02]*4,
        med_matched=8,  med_returns=[0.02]*4 + [-0.02]*4,
        high_matched=8, high_returns=[0.02]*4 + [-0.02]*4,
    )
    result = calibrate_confidence_bands(bs)
    assert result.observe_only is True


def test_observe_only_always_true_no_data():
    result = calibrate_confidence_bands([])
    assert result.observe_only is True


def test_result_carries_no_mutable_side_effects():
    """Calling calibrate_confidence_bands twice with the same input is idempotent."""
    bs = _bands(
        low_matched=8,  low_returns=[0.02]*3 + [-0.02]*5,
        med_matched=8,  med_returns=[0.03]*5 + [-0.02]*3,
        high_matched=10, high_returns=[0.05]*8 + [-0.01]*2,
    )
    r1 = calibrate_confidence_bands(bs)
    r2 = calibrate_confidence_bands(bs)
    assert r1.status == r2.status
    assert r1.recommendation == r2.recommendation
    assert r1.band_order_valid == r2.band_order_valid


# ---------------------------------------------------------------------------
# H. to_dict + JSON serialization
# ---------------------------------------------------------------------------

def test_to_dict_keys_present():
    bs = _bands(
        low_matched=8,  low_returns=[0.02]*3 + [-0.02]*5,
        med_matched=8,  med_returns=[0.03]*5 + [-0.02]*3,
        high_matched=10, high_returns=[0.05]*8 + [-0.01]*2,
    )
    result = calibrate_confidence_bands(bs)
    d = result.to_dict()
    expected_keys = {
        "observe_only", "status", "sample_summary",
        "low_win_rate", "medium_win_rate", "high_win_rate",
        "low_expectancy", "medium_expectancy", "high_expectancy",
        "band_order_valid", "strongest_band", "weakest_band",
        "recommendation", "recommendation_reason",
    }
    assert expected_keys <= set(d.keys())
    assert d["sample_summary"]["total_matched"] == 8 + 8 + 10


def test_to_dict_sample_summary_totals():
    bs = _bands(
        low_matched=5,  low_returns=[0.02]*2 + [-0.02]*3,
        med_matched=6,  med_returns=[0.03]*4 + [-0.02]*2,
        high_matched=7, high_returns=[0.05]*6 + [-0.01],
    )
    result = calibrate_confidence_bands(bs)
    d = result.to_dict()
    assert d["sample_summary"]["low_matched"] == 5
    assert d["sample_summary"]["medium_matched"] == 6
    assert d["sample_summary"]["high_matched"] == 7
    assert d["sample_summary"]["total_matched"] == 18


def test_to_dict_observe_only_always_true():
    result = calibrate_confidence_bands([])
    assert result.to_dict()["observe_only"] is True


def test_json_serializable_healthy():
    bs = _bands(
        low_matched=8,  low_returns=[0.02]*3 + [-0.02]*5,
        med_matched=8,  med_returns=[0.03]*5 + [-0.02]*3,
        high_matched=10, high_returns=[0.05]*8 + [-0.01]*2,
    )
    result = calibrate_confidence_bands(bs)
    json.dumps(result.to_dict())  # must not raise


def test_json_serializable_no_data():
    result = calibrate_confidence_bands([])
    json.dumps(result.to_dict())  # must not raise


# ---------------------------------------------------------------------------
# Integration: calibration flows through compute_execution_attribution
# ---------------------------------------------------------------------------

def test_calibration_present_in_execution_summary():
    """ExecutionAttributionSummary.confidence_calibration is always populated."""
    from profit_attribution.models import ExecutionLedgerEntry
    from profit_attribution.execution_metrics import compute_execution_attribution

    def _entry(sym, conf, ret):
        return ExecutionLedgerEntry(
            event_id=f"{sym}_r", symbol=sym, action="BUY",
            run_id="2026-04-16_daily", timestamp="2026-04-16T09:00:00",
            run_mode="daily", strategy_type="momentum",
            score=80.0, confidence=conf,
            suggested_allocation_pct=0.08, suggested_allocation_amount=8000.0,
            drawdown_regime="normal", degraded_mode=False,
            return_5d=ret, matched=True,
        )

    ledger = (
        [_entry(f"H{i}", 0.90, 0.05) for i in range(10)] +
        [_entry(f"M{i}", 0.72, 0.03) for i in range(8)] +
        [_entry(f"L{i}", 0.50, -0.01) for i in range(6)]
    )
    summary = compute_execution_attribution(ledger)
    cal = summary.confidence_calibration

    assert isinstance(cal, ConfidenceCalibrationResult)
    assert cal.observe_only is True
    assert cal.status in {"healthy", "weak_separation", "insufficient_data", "no_data"}


def test_calibration_in_summary_to_dict():
    """confidence_calibration key present and JSON-serializable in full summary to_dict."""
    from profit_attribution.models import ExecutionLedgerEntry
    from profit_attribution.execution_metrics import compute_execution_attribution

    entry = ExecutionLedgerEntry(
        event_id="NVDA_r", symbol="NVDA", action="BUY",
        run_id="2026-04-16_daily", timestamp="2026-04-16T09:00:00",
        run_mode="daily", strategy_type="momentum",
        score=80.0, confidence=0.90,
        suggested_allocation_pct=0.08, suggested_allocation_amount=8000.0,
        drawdown_regime="normal", degraded_mode=False,
        return_5d=0.05, matched=True,
    )
    summary = compute_execution_attribution([entry])
    d = summary.to_dict()
    assert "confidence_calibration" in d
    assert d["confidence_calibration"]["observe_only"] is True
    json.dumps(d)
