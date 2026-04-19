"""
Tests for execution-level attribution.
Covers: ingestion, outcome matching, missing data, metrics correctness,
mixed action types, and no regression in coverage-based attribution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from profit_attribution.models import (
    ExecutionAttributionSummary,
    ExecutionLedgerEntry,
)
from profit_attribution.execution_metrics import compute_execution_attribution
from profit_attribution.execution_ledger import (
    _build_outcome_index,
    _event_to_entry,
    _find_best_match,
    _parse_event_date,
    build_execution_ledger,
)
from profit_attribution.execution_metrics import _confidence_band


# ---------------------------------------------------------------------------
# Minimal CoverageOutcome stub (avoids importing the full evaluator)
# ---------------------------------------------------------------------------

@dataclass
class _Obs:
    run_id: str
    obs_date: date
    price: float
    forward_return: float
    hold_days: int


@dataclass
class _Outcome:
    symbol: str
    entry_run_id: str
    entry_date: date
    entry_price: float
    label: str = "momentum"
    score: float = 75.0
    events: List[str] = field(default_factory=list)
    drawdown_regime: str = "normal"
    action_bucket: str = ""
    observations: List[_Obs] = field(default_factory=list)
    forward_return_1d: Optional[float] = None
    forward_return_3d: Optional[float] = None
    forward_return_5d: Optional[float] = None
    forward_return_10d: Optional[float] = None
    mfe: Optional[float] = None
    mae: Optional[float] = None
    latest_return: Optional[float] = None
    exit_quality: Optional[float] = None
    hit: Optional[bool] = None
    attributable: bool = True


def _make_outcome(
    symbol: str = "NVDA",
    entry_date: date = date(2026, 4, 10),
    return_5d: float = 0.05,
    exit_quality: float = 0.85,
    mfe: float = 0.06,
    mae: float = -0.01,
) -> _Outcome:
    obs = _Obs(
        run_id=f"{entry_date.isoformat()}_daily",
        obs_date=entry_date + timedelta(days=5),
        price=105.0,
        forward_return=return_5d,
        hold_days=5,
    )
    return _Outcome(
        symbol=symbol,
        entry_run_id=f"{entry_date.isoformat()}_daily",
        entry_date=entry_date,
        entry_price=100.0,
        forward_return_5d=return_5d,
        mfe=mfe,
        mae=mae,
        exit_quality=exit_quality,
        observations=[obs],
    )


def _make_event(
    symbol: str = "NVDA",
    action: str = "BUY",
    run_id: str = "2026-04-10_daily",
    strategy_type: str = "momentum",
    score: float = 82.0,
    confidence: float = 0.80,
    regime: str = "normal",
) -> dict:
    return {
        "symbol": symbol,
        "action": action,
        "run_id": run_id,
        "timestamp": f"{run_id[:10]}T09:00:00",
        "run_mode": "daily",
        "strategy_type": strategy_type,
        "score": score,
        "confidence": confidence,
        "suggested_allocation_pct": 0.08,
        "suggested_allocation_amount": 8000.0,
        "drawdown_regime": regime,
        "degraded_mode": False,
        "degraded_reason": None,
        "rationale": ["breakout"],
        "related_symbol": None,
        "exit_plan": None,
    }


# ---------------------------------------------------------------------------
# _parse_event_date
# ---------------------------------------------------------------------------

def test_parse_date_from_run_id():
    d = _parse_event_date("2026-04-16_daily", "")
    assert d == date(2026, 4, 16)


def test_parse_date_from_timestamp_fallback():
    d = _parse_event_date("", "2026-04-15T09:30:00")
    assert d == date(2026, 4, 15)


def test_parse_date_run_id_takes_priority():
    d = _parse_event_date("2026-04-10_daily", "2026-04-16T09:00:00")
    assert d == date(2026, 4, 10)


def test_parse_date_invalid_returns_none():
    d = _parse_event_date("INVALID", "ALSO_INVALID")
    assert d is None


# ---------------------------------------------------------------------------
# _find_best_match
# ---------------------------------------------------------------------------

def test_exact_match_by_run_id():
    outcome = _make_outcome("NVDA", entry_date=date(2026, 4, 10))
    index = _build_outcome_index([outcome])
    match, quality = _find_best_match("NVDA", date(2026, 4, 10), index)
    assert match is outcome
    assert quality == "exact"


def test_nearest_match_within_tolerance():
    outcome = _make_outcome("AAPL", entry_date=date(2026, 4, 10))
    index = _build_outcome_index([outcome])
    # Event is 3 days after — within default 7-day window
    match, quality = _find_best_match("AAPL", date(2026, 4, 13), index)
    assert match is outcome
    assert quality == "nearest"


def test_no_match_beyond_tolerance():
    outcome = _make_outcome("MSFT", entry_date=date(2026, 4, 1))
    index = _build_outcome_index([outcome])
    # Event is 15 days away — outside default 7-day window
    match, quality = _find_best_match("MSFT", date(2026, 4, 16), index)
    assert match is None
    assert quality == "none"


def test_no_match_unknown_symbol():
    index = _build_outcome_index([_make_outcome("NVDA")])
    match, quality = _find_best_match("GOOG", date(2026, 4, 10), index)
    assert match is None
    assert quality == "none"


def test_case_insensitive_symbol_lookup():
    outcome = _make_outcome("nvda", entry_date=date(2026, 4, 10))
    index = _build_outcome_index([outcome])
    match, quality = _find_best_match("NVDA", date(2026, 4, 10), index)
    assert match is outcome


# ---------------------------------------------------------------------------
# _event_to_entry
# ---------------------------------------------------------------------------

def test_event_to_entry_matched():
    outcome = _make_outcome("NVDA", entry_date=date(2026, 4, 10), return_5d=0.05, exit_quality=0.85)
    index = _build_outcome_index([outcome])
    ev = _make_event("NVDA", "BUY", run_id="2026-04-10_daily")
    entry = _event_to_entry(ev, index)
    assert entry is not None
    assert entry.symbol == "NVDA"
    assert entry.action == "BUY"
    assert entry.matched is True
    assert entry.return_5d == pytest.approx(0.05)
    assert entry.exit_quality == pytest.approx(0.85)
    assert entry.mfe == pytest.approx(0.06)


def test_event_to_entry_unmatched():
    index = _build_outcome_index([])  # no outcomes
    ev = _make_event("UNKNOWN", "BUY")
    entry = _event_to_entry(ev, index)
    assert entry is not None
    assert entry.matched is False
    assert entry.match_quality == "none"
    assert entry.return_5d is None


def test_event_to_entry_missing_symbol_returns_none():
    entry = _event_to_entry({"action": "BUY"}, {})
    assert entry is None


def test_event_to_entry_hold_days_computed():
    outcome = _make_outcome("NVDA", entry_date=date(2026, 4, 10))
    index = _build_outcome_index([outcome])
    ev = _make_event("NVDA", "BUY", run_id="2026-04-10_daily")
    entry = _event_to_entry(ev, index)
    assert entry is not None
    assert entry.hold_days == 5


def test_event_to_entry_sell_action():
    outcome = _make_outcome("AAPL", entry_date=date(2026, 4, 10), exit_quality=0.4)
    index = _build_outcome_index([outcome])
    ev = _make_event("AAPL", "SELL", run_id="2026-04-10_daily")
    entry = _event_to_entry(ev, index)
    assert entry is not None
    assert entry.action == "SELL"
    assert entry.exit_quality == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# build_execution_ledger (integration — mocked coverage outcomes)
# ---------------------------------------------------------------------------

def test_build_execution_ledger_empty_when_no_file(tmp_path):
    ledger = build_execution_ledger(events_path=tmp_path / "nonexistent.jsonl")
    assert ledger == []


def test_build_execution_ledger_sorted_by_timestamp(tmp_path):
    events_file = tmp_path / "trade_events.jsonl"
    events = [
        {**_make_event("NVDA", run_id="2026-04-12_daily"), "timestamp": "2026-04-12T09:00:00"},
        {**_make_event("AAPL", run_id="2026-04-10_daily"), "timestamp": "2026-04-10T09:00:00"},
    ]
    events_file.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )

    outcomes = [
        _make_outcome("NVDA", entry_date=date(2026, 4, 12)),
        _make_outcome("AAPL", entry_date=date(2026, 4, 10)),
    ]
    with patch("profit_attribution.execution_ledger.build_coverage_outcomes", return_value=outcomes):
        ledger = build_execution_ledger(events_path=events_file)

    assert len(ledger) == 2
    assert ledger[0].symbol == "AAPL"
    assert ledger[1].symbol == "NVDA"


def test_build_execution_ledger_skips_non_loggable_actions(tmp_path):
    events_file = tmp_path / "trade_events.jsonl"
    events = [
        _make_event("NVDA", "BUY"),
        _make_event("TSLA", "HOLD"),
        _make_event("AAPL", "ADD_TO_WATCHLIST"),
    ]
    events_file.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )
    with patch("profit_attribution.execution_ledger.build_coverage_outcomes", return_value=[]):
        ledger = build_execution_ledger(events_path=events_file)

    assert len(ledger) == 1
    assert ledger[0].symbol == "NVDA"


def test_build_execution_ledger_malformed_line_skipped(tmp_path):
    events_file = tmp_path / "trade_events.jsonl"
    good = _make_event("NVDA", "BUY")
    events_file.write_text(
        json.dumps(good) + "\nNOT_JSON\n",
        encoding="utf-8",
    )
    with patch("profit_attribution.execution_ledger.build_coverage_outcomes", return_value=[]):
        ledger = build_execution_ledger(events_path=events_file)

    assert len(ledger) == 1


# ---------------------------------------------------------------------------
# compute_execution_attribution — metrics correctness
# ---------------------------------------------------------------------------

def _make_entry(
    symbol: str = "NVDA",
    action: str = "BUY",
    strategy_type: str = "momentum",
    score: float = 80.0,
    confidence: Optional[float] = 0.80,
    regime: str = "normal",
    return_5d: Optional[float] = None,
    exit_quality: Optional[float] = None,
    mfe: Optional[float] = None,
    matched: bool = True,
) -> ExecutionLedgerEntry:
    return ExecutionLedgerEntry(
        event_id=f"{symbol}_2026-04-10_daily",
        symbol=symbol,
        action=action,
        run_id="2026-04-10_daily",
        timestamp="2026-04-10T09:00:00",
        run_mode="daily",
        strategy_type=strategy_type,
        score=score,
        confidence=confidence,
        suggested_allocation_pct=0.08,
        suggested_allocation_amount=8000.0,
        drawdown_regime=regime,
        degraded_mode=False,
        return_5d=return_5d,
        exit_quality=exit_quality,
        mfe=mfe,
        matched=matched,
    )


def test_empty_ledger_returns_valid_summary():
    summary = compute_execution_attribution([])
    assert isinstance(summary, ExecutionAttributionSummary)
    assert summary.total_events == 0
    assert summary.matched_events == 0
    assert len(summary.data_quality_notes) > 0


def test_buy_win_rate_computed_correctly():
    ledger = [
        _make_entry("NVDA", "BUY", return_5d=0.05, matched=True),
        _make_entry("AAPL", "BUY", return_5d=0.03, matched=True),
        _make_entry("MSFT", "BUY", return_5d=-0.02, matched=True),
        _make_entry("GOOG", "BUY", return_5d=-0.04, matched=True),
    ]
    summary = compute_execution_attribution(ledger)
    buy = next(a for a in summary.by_action if a.action == "BUY")
    assert buy.total_events == 4
    assert buy.matched_events == 4
    assert buy.win_rate == pytest.approx(0.5, rel=0.01)
    assert buy.avg_gain > 0
    assert buy.avg_loss < 0


def test_sell_exit_quality_reported():
    ledger = [
        _make_entry("AAPL", "SELL", exit_quality=0.85, matched=True),
        _make_entry("MSFT", "SELL", exit_quality=0.40, matched=True),
    ]
    summary = compute_execution_attribution(ledger)
    sell = next(a for a in summary.by_action if a.action == "SELL")
    assert sell.avg_exit_quality == pytest.approx(0.625, rel=0.01)
    # SELL win_rate is None when no return_5d data
    assert sell.win_rate is None


def test_mixed_actions_grouped_separately():
    ledger = [
        _make_entry("NVDA", "BUY", return_5d=0.05, matched=True),
        _make_entry("AAPL", "SELL", exit_quality=0.80, matched=True),
        _make_entry("MSFT", "TRIM", exit_quality=0.50, matched=True),
        _make_entry("GOOG", "PROMOTE_TO_PORTFOLIO", return_5d=0.08, matched=True),
    ]
    summary = compute_execution_attribution(ledger)
    action_names = {a.action for a in summary.by_action}
    assert action_names == {"BUY", "SELL", "TRIM", "PROMOTE_TO_PORTFOLIO"}


def test_unmatched_events_excluded_from_metrics():
    ledger = [
        _make_entry("NVDA", "BUY", return_5d=0.05, matched=True),
        _make_entry("AAPL", "BUY", return_5d=None, matched=False),  # no coverage match
    ]
    summary = compute_execution_attribution(ledger)
    buy = next(a for a in summary.by_action if a.action == "BUY")
    assert buy.total_events == 2
    assert buy.matched_events == 1
    assert buy.win_rate == pytest.approx(1.0)   # only 1 attributable, 1 win


def test_by_strategy_grouping():
    ledger = [
        _make_entry("NVDA", "BUY", strategy_type="momentum", return_5d=0.05, matched=True),
        _make_entry("AAPL", "BUY", strategy_type="compounder", return_5d=0.03, matched=True),
        _make_entry("MSFT", "BUY", strategy_type="momentum", return_5d=-0.02, matched=True),
    ]
    summary = compute_execution_attribution(ledger)
    strats = {b.name: b for b in summary.by_strategy}
    assert "momentum" in strats
    assert "compounder" in strats
    assert strats["momentum"].total_entries == 2
    assert strats["compounder"].total_entries == 1


def test_by_score_band_grouping():
    ledger = [
        _make_entry("NVDA", "BUY", score=85.0, return_5d=0.05, matched=True),
        _make_entry("AAPL", "BUY", score=55.0, return_5d=0.02, matched=True),
        _make_entry("MSFT", "BUY", score=30.0, return_5d=-0.01, matched=True),
    ]
    summary = compute_execution_attribution(ledger)
    bands = {b.name: b for b in summary.by_score_band}
    assert bands["high"].total_entries == 1
    assert bands["medium"].total_entries == 1
    assert bands["low"].total_entries == 1


def test_by_regime_grouping():
    ledger = [
        _make_entry("NVDA", "BUY", regime="normal", return_5d=0.05, matched=True),
        _make_entry("AAPL", "BUY", regime="significant_dip", return_5d=-0.03, matched=True),
    ]
    summary = compute_execution_attribution(ledger)
    regimes = {b.name for b in summary.by_regime}
    assert "normal" in regimes
    assert "significant_dip" in regimes


def test_match_rate_note_on_zero_matches():
    ledger = [_make_entry("NVDA", "BUY", matched=False)]
    summary = compute_execution_attribution(ledger)
    assert summary.match_rate == 0.0
    assert any("matched" in note.lower() for note in summary.data_quality_notes)


def test_match_rate_note_on_low_match():
    ledger = [
        _make_entry("NVDA", "BUY", matched=True),
        _make_entry("A2", "BUY", matched=False),
        _make_entry("A3", "BUY", matched=False),
        _make_entry("A4", "BUY", matched=False),
    ]
    summary = compute_execution_attribution(ledger)
    assert summary.match_rate == pytest.approx(0.25)
    assert any("low match rate" in note.lower() for note in summary.data_quality_notes)


def test_expectancy_formula():
    # win_rate=0.6, avg_gain=0.05, avg_loss=-0.02
    # expectancy = 0.6*0.05 + 0.4*(-0.02) = 0.03 - 0.008 = 0.022
    ledger = [
        _make_entry(f"W{i}", "BUY", return_5d=0.05, matched=True) for i in range(3)
    ] + [
        _make_entry(f"L{i}", "BUY", return_5d=-0.02, matched=True) for i in range(2)
    ]
    summary = compute_execution_attribution(ledger)
    buy = next(a for a in summary.by_action if a.action == "BUY")
    assert buy.win_rate == pytest.approx(0.6)
    assert buy.expectancy == pytest.approx(0.022, rel=0.01)


# ---------------------------------------------------------------------------
# Confidence-band breakdown
# ---------------------------------------------------------------------------

def test_confidence_band_assignment():
    assert _confidence_band(0.40) == "low"
    assert _confidence_band(0.64) == "low"
    assert _confidence_band(0.65) == "medium"
    assert _confidence_band(0.72) == "medium"
    assert _confidence_band(0.80) == "medium"
    assert _confidence_band(0.81) == "high"
    assert _confidence_band(0.99) == "high"


def test_confidence_band_none_falls_into_low():
    ledger = [_make_entry("NVDA", "BUY", confidence=None, return_5d=0.05, matched=True)]
    summary = compute_execution_attribution(ledger)
    bands = {b.name: b for b in summary.by_confidence_band}
    assert bands["low"].total_entries == 1
    assert bands["medium"].total_entries == 0
    assert bands["high"].total_entries == 0


def test_confidence_band_grouping():
    ledger = [
        _make_entry("A", "BUY", confidence=0.50, return_5d=0.04, matched=True),
        _make_entry("B", "BUY", confidence=0.70, return_5d=0.03, matched=True),
        _make_entry("C", "BUY", confidence=0.70, return_5d=-0.01, matched=True),
        _make_entry("D", "BUY", confidence=0.90, return_5d=0.06, matched=True),
        _make_entry("E", "BUY", confidence=0.95, return_5d=0.07, matched=True),
    ]
    summary = compute_execution_attribution(ledger)
    bands = {b.name: b for b in summary.by_confidence_band}
    assert bands["low"].total_entries == 1
    assert bands["medium"].total_entries == 2
    assert bands["high"].total_entries == 2


def test_confidence_band_win_rate_metrics():
    # high band: 2 wins out of 3 → win_rate ≈ 0.667
    ledger = [
        _make_entry("A", "BUY", confidence=0.85, return_5d=0.05, matched=True),
        _make_entry("B", "BUY", confidence=0.90, return_5d=0.03, matched=True),
        _make_entry("C", "BUY", confidence=0.92, return_5d=-0.02, matched=True),
        # low band: 0 wins out of 1
        _make_entry("D", "BUY", confidence=0.40, return_5d=-0.03, matched=True),
    ]
    summary = compute_execution_attribution(ledger)
    bands = {b.name: b for b in summary.by_confidence_band}
    assert bands["high"].win_rate == pytest.approx(2 / 3, rel=0.01)
    assert bands["low"].win_rate == pytest.approx(0.0, abs=0.01)
    assert bands["medium"].win_rate is None   # no medium entries


def test_confidence_band_in_to_dict():
    ledger = [_make_entry("NVDA", "BUY", confidence=0.90, return_5d=0.05, matched=True)]
    summary = compute_execution_attribution(ledger)
    d = summary.to_dict()
    assert "by_confidence_band" in d
    assert isinstance(d["by_confidence_band"], list)
    assert len(d["by_confidence_band"]) == 3  # low / medium / high always present
    band_names = [b["name"] for b in d["by_confidence_band"]]
    assert band_names == ["low", "medium", "high"]
    import json
    json.dumps(d)


# ---------------------------------------------------------------------------
# to_dict round-trip
# ---------------------------------------------------------------------------

def test_execution_ledger_entry_to_dict():
    entry = _make_entry("NVDA", "BUY", return_5d=0.05, matched=True)
    d = entry.to_dict()
    assert d["action"] == "BUY"
    assert d["symbol"] == "NVDA"
    assert d["matched"] is True
    assert d["return_5d"] == pytest.approx(0.05)


def test_execution_attribution_summary_to_dict():
    ledger = [_make_entry("NVDA", "BUY", return_5d=0.05, matched=True)]
    summary = compute_execution_attribution(ledger)
    d = summary.to_dict()
    assert "total_events" in d
    assert "by_action" in d
    assert "by_strategy" in d
    assert "execution_ledger" in d
    assert isinstance(d["by_action"], list)
    # JSON serializable
    json.dumps(d)


# ---------------------------------------------------------------------------
# No regression: coverage attribution unaffected
# ---------------------------------------------------------------------------

def test_coverage_attribution_unaffected_by_execution_layer():
    """
    Verify that AttributionSummary.execution = None does not break
    to_dict() or any existing coverage-level fields.
    """
    from profit_attribution.models import (
        AttributionMetrics,
        AttributionSummary,
    )

    metrics = AttributionMetrics(
        total_entries=10,
        attributable_entries=8,
        entries_with_5d=6,
        coverage_rate=0.8,
        win_rate=0.6,
        avg_gain=0.05,
        avg_loss=-0.03,
        risk_reward=1.67,
        expectancy=0.018,
        capital_efficiency=0.625,
        avg_mfe=0.07,
        avg_mae=-0.02,
        avg_exit_quality=0.75,
        avg_hold_days=5.2,
        strong_win_rate=0.3,
        adverse_rate=0.2,
    )
    summary = AttributionSummary(
        generated_at="2026-04-16T09:00:00",
        metrics=metrics,
        by_strategy=[],
        by_score_band=[],
        by_regime=[],
        trade_ledger=[],
        exit_summary={},
        exit_classified=[],
        missed_opportunities=[],
        total_opportunity_cost=None,
        best_trades=[],
        worst_trades=[],
        data_quality_notes=[],
        execution=None,   # no execution data
    )
    d = summary.to_dict()
    assert d["execution"] is None
    assert d["metrics"]["win_rate"] == pytest.approx(0.6)
    # JSON serializable
    json.dumps(d)


def test_attribution_summary_with_execution_field():
    """execution field is included in to_dict when present."""
    from profit_attribution.models import (
        AttributionMetrics,
        AttributionSummary,
    )

    ledger = [_make_entry("NVDA", "BUY", return_5d=0.05, matched=True)]
    exec_summary = compute_execution_attribution(ledger)

    metrics = AttributionMetrics(
        total_entries=0, attributable_entries=0, entries_with_5d=0,
        coverage_rate=0.0, win_rate=None, avg_gain=None, avg_loss=None,
        risk_reward=None, expectancy=None, capital_efficiency=None,
        avg_mfe=None, avg_mae=None, avg_exit_quality=None, avg_hold_days=None,
        strong_win_rate=None, adverse_rate=None,
    )
    summary = AttributionSummary(
        generated_at="2026-04-16T09:00:00",
        metrics=metrics,
        by_strategy=[], by_score_band=[], by_regime=[],
        trade_ledger=[], exit_summary={}, exit_classified=[],
        missed_opportunities=[], total_opportunity_cost=None,
        best_trades=[], worst_trades=[], data_quality_notes=[],
        execution=exec_summary,
    )
    d = summary.to_dict()
    assert d["execution"] is not None
    assert d["execution"]["total_events"] == 1
    json.dumps(d)
