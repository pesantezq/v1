"""Tests for crowd forward shadow-tracking (snapshot + resolve)."""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.portfolio_sim.crowd_forward_track import (
    resolve_records,
    snapshot_sleeve,
)
from portfolio_automation.portfolio_sim.prices import load_price_panel


def _archive(root, ticker, closes):
    d = root / "outputs" / "backtest" / "historical"
    d.mkdir(parents=True, exist_ok=True)
    rows = [{"date": f"2026-01-{i+1:02d}", "close": c, "volume": 100} for i, c in enumerate(closes)]
    (d / f"{ticker}_5y.json").write_text(json.dumps({"symbol": ticker, "rows": list(reversed(rows))}))


def test_snapshot_only_useful_states(tmp_path):
    _archive(tmp_path, "NVDA", [100] * 10)
    _archive(tmp_path, "GME", [50] * 10)
    panel = load_price_panel(["NVDA", "GME"], tmp_path)
    states = [{"ticker": "NVDA", "crowd_state": "emerging_dd", "crowd_research_priority_score": 3},
              {"ticker": "GME", "crowd_state": "hype_acceleration"}]
    appended = snapshot_sleeve(tmp_path, "2026-01-01", states, panel)
    tickers = {r["ticker"] for r in appended}
    assert "NVDA" in tickers
    assert "GME" not in tickers   # caution state not tracked as a long


def test_resolve_computes_forward_returns(tmp_path):
    # NVDA +10% by 5d; SPY flat
    closes = [100, 101, 102, 103, 104, 110, 110, 110, 110, 110]
    _archive(tmp_path, "NVDA", closes)
    _archive(tmp_path, "SPY", [100] * 10)
    panel = load_price_panel(["NVDA", "SPY"], tmp_path)
    recs = snapshot_sleeve(tmp_path, "2026-01-01",
                           [{"ticker": "NVDA", "crowd_state": "emerging_dd"}], panel)
    resolve_records(recs, panel, benchmark="SPY")
    nvda = recs[0]
    assert "5D" in nvda["raw_returns"]
    assert abs(nvda["raw_returns"]["5D"] - 0.10) < 1e-6   # 100→110 by offset 5
    assert nvda["returns"]["5D"]["vs_spy"] > 0            # beat flat SPY
    assert nvda["resolved"] is True


def test_snapshot_idempotent(tmp_path):
    _archive(tmp_path, "NVDA", [100] * 5)
    panel = load_price_panel(["NVDA"], tmp_path)
    states = [{"ticker": "NVDA", "crowd_state": "emerging_dd"}]
    a1 = snapshot_sleeve(tmp_path, "2026-01-01", states, panel)
    # simulate that a1 is already persisted
    hist = tmp_path / "outputs" / "sandbox" / "discovery"
    hist.mkdir(parents=True, exist_ok=True)
    (hist / "social_signal_history.json").write_text(json.dumps({"records": a1}))
    a2 = snapshot_sleeve(tmp_path, "2026-01-01", states, panel)
    assert a2 == []   # already recorded for that signal_date
