"""
Tests for backtesting/historical_signal_recon.py — sub-project F point-in-time
historical signal reconstruction.

Fully offline/deterministic. Asserts pattern-family detection from OHLCV
(STRONG_MOVE_UP/DOWN, VOLUME_SPIKE) using documented thresholds, newest-first
input handling, the point-in-time `today` guard, and empty/short-series safety.
"""

from __future__ import annotations

import json
from pathlib import Path

from backtesting.historical_signal_recon import (
    assert_no_lookahead,
    reconstruct_signals,
    reconstruct_universe,
)


def _row(d, close, volume=1_000_000):
    return {"date": d, "close": close, "volume": volume}


# --------------------------------------------------------------------------
# reconstruct_signals (per-ticker, pure)
# --------------------------------------------------------------------------

def test_strong_move_up_emitted_on_threshold_breach():
    rows = [_row("2026-01-02", 100.0), _row("2026-01-03", 104.0)]  # +4% >= 3%
    sigs = reconstruct_signals("AAA", rows)
    assert len(sigs) == 1
    s = sigs[0]
    assert s["ticker"] == "AAA" and s["scan_time"] == "2026-01-03"
    assert "price_move" in s["alert_basis"]
    assert s["pattern"] == "STRONG_MOVE"
    assert s["direction"] == "up"
    assert s["signal_score"] is None and s["source"] == "historical_reconstruction"


def test_strong_move_down_direction():
    rows = [_row("2026-01-02", 100.0), _row("2026-01-03", 96.0)]  # -4%
    s = reconstruct_signals("AAA", rows)[0]
    assert s["direction"] == "down"


def test_sub_threshold_emits_nothing():
    rows = [_row("2026-01-02", 100.0), _row("2026-01-03", 101.0)]  # +1% < 3%
    assert reconstruct_signals("AAA", rows) == []


def test_volume_spike_emitted():
    rows = [_row(f"2026-01-{d:02d}", 100.0, volume=1_000_000) for d in range(2, 22)]
    rows.append(_row("2026-01-22", 100.5, volume=3_000_000))  # 3x avg, price flat
    sigs = reconstruct_signals("AAA", rows, vol_window=20)
    spike = [s for s in sigs if "volume_spike" in s["alert_basis"]]
    assert spike and spike[-1]["scan_time"] == "2026-01-22"


def test_newest_first_input_is_sorted():
    rows = [_row("2026-01-03", 104.0), _row("2026-01-02", 100.0)]  # reversed
    assert reconstruct_signals("AAA", rows)[0]["scan_time"] == "2026-01-03"


def test_today_guard_excludes_future_dates():
    rows = [_row("2026-01-02", 100.0), _row("2026-01-03", 104.0), _row("2026-01-04", 110.0)]
    sigs = reconstruct_signals("AAA", rows, today="2026-01-03")
    assert all(s["scan_time"] <= "2026-01-03" for s in sigs)


def test_empty_and_short_series_no_raise():
    assert reconstruct_signals("AAA", []) == []
    assert reconstruct_signals("AAA", [_row("2026-01-02", 100.0)]) == []
