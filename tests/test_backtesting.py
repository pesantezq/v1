"""
Tests for backtesting/fmp_backtester.py

All tests are fully offline — FMPClient is replaced with a mock.
Historical price data is generated procedurally so tests are deterministic.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from backtesting.fmp_backtester import (
    FMPBacktester,
    _build_price_map,
    _forward_return,
    _nearest_trading_date,
    _parse_date,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_historical(
    symbol: str = "AAPL",
    start: date | None = None,
    days: int = 60,
    start_price: float = 100.0,
    daily_change: float = 0.005,  # +0.5 %/day → trending up
) -> list[dict]:
    """Build a synthetic historical price list (newest-first, like FMP)."""
    start = start or date(2024, 1, 1)
    rows = []
    price = start_price
    for i in range(days):
        d = start + timedelta(days=i)
        close = round(price * (1 + daily_change) ** i, 4)
        rows.append({
            "date":     d.isoformat(),
            "open":     close * 0.99,
            "high":     close * 1.01,
            "low":      close * 0.98,
            "close":    close,
            "adjClose": close,
            "volume":   1_000_000,
        })
    # FMP returns newest-first
    return list(reversed(rows))


def _make_fmp(historical: list[dict] | None = None) -> MagicMock:
    client = MagicMock()
    client.get_historical_prices.return_value = historical if historical is not None else _make_historical()
    return client


def _make_signal(
    ticker: str = "AAPL",
    signal_date: str = "2024-01-10",
    signal_score: float = 0.7,
    confidence_score: float = 0.8,
) -> dict:
    return {
        "ticker":          ticker,
        "scan_time":       signal_date,
        "signal_score":    signal_score,
        "confidence_score": confidence_score,
    }


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

def test_parse_date_iso_string():
    assert _parse_date("2024-03-15") == date(2024, 3, 15)


def test_parse_date_datetime_string():
    assert _parse_date("2024-03-15T10:30:00") == date(2024, 3, 15)


def test_parse_date_none_returns_none():
    assert _parse_date(None) is None
    assert _parse_date("not-a-date") is None


def test_build_price_map():
    hist = _make_historical(start=date(2024, 1, 1), days=5)
    pm = _build_price_map(hist)
    assert date(2024, 1, 1) in pm
    assert date(2024, 1, 5) in pm
    assert all(isinstance(v, float) and v > 0 for v in pm.values())


def test_nearest_trading_date_exact_match():
    pm = {date(2024, 1, 10): 100.0, date(2024, 1, 11): 101.0}
    assert _nearest_trading_date(date(2024, 1, 10), pm) == date(2024, 1, 10)


def test_nearest_trading_date_skips_weekend():
    # Saturday Jan 13 → should find Monday Jan 15
    pm = {date(2024, 1, 15): 105.0}
    result = _nearest_trading_date(date(2024, 1, 13), pm)
    assert result == date(2024, 1, 15)


def test_nearest_trading_date_not_found_returns_none():
    pm = {date(2024, 1, 10): 100.0}
    result = _nearest_trading_date(date(2024, 2, 1), pm)
    assert result is None


def test_forward_return_positive():
    pm = {date(2024, 1, 10): 100.0, date(2024, 1, 20): 105.0}
    ret = _forward_return(100.0, pm, date(2024, 1, 10), 10)
    assert ret == pytest.approx(5.0)


def test_forward_return_negative():
    pm = {date(2024, 1, 10): 100.0, date(2024, 1, 20): 90.0}
    ret = _forward_return(100.0, pm, date(2024, 1, 10), 10)
    assert ret == pytest.approx(-10.0)


def test_forward_return_no_data_returns_none():
    pm = {date(2024, 1, 10): 100.0}
    ret = _forward_return(100.0, pm, date(2024, 1, 10), 30)
    assert ret is None


# ---------------------------------------------------------------------------
# FMPBacktester.get_historical_prices
# ---------------------------------------------------------------------------

def test_get_historical_prices_delegates_to_fmp():
    fmp = _make_fmp()
    bt = FMPBacktester(fmp)
    result = bt.get_historical_prices("AAPL", years=2)
    fmp.get_historical_prices.assert_called_once_with("AAPL", years=2)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# FMPBacktester.simulate_signal_performance
# ---------------------------------------------------------------------------

def test_simulate_empty_signals_returns_empty_report():
    fmp = _make_fmp()
    bt = FMPBacktester(fmp)
    report = bt.simulate_signal_performance([])
    assert report["total_signals"] == 0
    assert report["evaluated"] == 0
    assert report["results"] == []


def test_simulate_basic_returns_structure():
    hist = _make_historical(start=date(2024, 1, 1), days=60, daily_change=0.01)
    fmp = _make_fmp(hist)
    bt = FMPBacktester(fmp)
    signals = [_make_signal("AAPL", "2024-01-10")]
    report = bt.simulate_signal_performance(signals, forward_days=10)
    assert report["total_signals"] == 1
    assert report["evaluated"] == 1
    assert len(report["results"]) == 1
    row = report["results"][0]
    assert "return_10d" in row
    assert "return_30d" in row
    assert row["outcome"] in {"win", "loss", "unknown"}


def test_simulate_winning_signals_hit_rate():
    # Daily change +1% → all 10-day forward returns are positive
    hist = _make_historical(start=date(2024, 1, 1), days=90, daily_change=0.01)
    fmp = _make_fmp(hist)
    bt = FMPBacktester(fmp)
    signals = [
        _make_signal("AAPL", f"2024-01-{i:02d}")
        for i in range(5, 20)
    ]
    report = bt.simulate_signal_performance(signals, forward_days=10)
    assert report["hit_rate"] == 100.0


def test_simulate_losing_signals_hit_rate():
    # Daily change -1% → all forward returns negative
    hist = _make_historical(start=date(2024, 1, 1), days=90, daily_change=-0.01)
    fmp = _make_fmp(hist)
    bt = FMPBacktester(fmp)
    signals = [_make_signal("AAPL", "2024-01-10")]
    report = bt.simulate_signal_performance(signals, forward_days=10)
    assert report["hit_rate"] == 0.0


def test_simulate_missing_symbol_handled():
    fmp = MagicMock()
    fmp.get_historical_prices.return_value = []   # No data for any symbol
    bt = FMPBacktester(fmp)
    signals = [_make_signal("UNKN", "2024-01-10")]
    report = bt.simulate_signal_performance(signals, forward_days=10)
    assert report["evaluated"] == 0


def test_simulate_signal_without_ticker_skipped():
    hist = _make_historical()
    fmp = _make_fmp(hist)
    bt = FMPBacktester(fmp)
    signals = [{"scan_time": "2024-01-10", "signal_score": 0.7}]  # no ticker
    report = bt.simulate_signal_performance(signals)
    assert report["evaluated"] == 0


def test_simulate_uses_price_cache():
    hist = _make_historical(start=date(2024, 1, 1), days=60)
    fmp = _make_fmp(hist)
    bt = FMPBacktester(fmp)
    signals = [
        _make_signal("AAPL", "2024-01-10"),
        _make_signal("AAPL", "2024-01-15"),
    ]
    bt.simulate_signal_performance(signals)
    # get_historical_prices should only be called once despite two AAPL signals
    assert fmp.get_historical_prices.call_count == 1


# ---------------------------------------------------------------------------
# FMPBacktester.evaluate_confidence_calibration
# ---------------------------------------------------------------------------

def test_calibration_empty_returns_defaults():
    fmp = _make_fmp()
    bt = FMPBacktester(fmp)
    result = bt.evaluate_confidence_calibration([])
    assert result["buckets"] == []
    assert result["well_calibrated"] is False


def test_calibration_returns_buckets():
    hist = _make_historical(start=date(2024, 1, 1), days=90, daily_change=0.005)
    fmp = _make_fmp(hist)
    bt = FMPBacktester(fmp)
    # Mix of confidence levels
    signals = [
        _make_signal("AAPL", f"2024-01-{i:02d}", confidence_score=i / 20)
        for i in range(5, 25)
    ]
    result = bt.evaluate_confidence_calibration(signals, forward_days=10)
    assert "buckets" in result
    assert "calibration_slope" in result
    assert isinstance(result["well_calibrated"], bool)
    total_in_buckets = sum(b["count"] for b in result["buckets"])
    assert total_in_buckets > 0
