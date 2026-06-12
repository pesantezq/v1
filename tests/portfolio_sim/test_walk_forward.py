"""Tests for walk-forward OOS validation."""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.portfolio_sim.prices import load_price_panel
from portfolio_automation.portfolio_sim.research_library import MomentumRotation
from portfolio_automation.portfolio_sim.walk_forward import walk_forward


def _archive(root, ticker, g):
    d = root / "outputs" / "backtest" / "historical"
    d.mkdir(parents=True, exist_ok=True)
    dates = []
    for y in (2022, 2023, 2024, 2025):
        for mo in range(1, 13):
            dates.append(f"{y}-{mo:02d}-15")
    rows = [{"date": dt, "close": round(100 * (g ** i), 4), "volume": 1000} for i, dt in enumerate(dates)]
    (d / f"{ticker}_5y.json").write_text(json.dumps({"symbol": ticker, "rows": list(reversed(rows))}))


def _panel(root):
    for t, g in [("QQQ", 1.02), ("GLD", 1.003), ("SPY", 1.012), ("BND", 1.001)]:
        _archive(root, t, g)
    return load_price_panel(["QQQ", "GLD", "SPY", "BND"], root)


def test_no_params_returns_status():
    # empty grid → no_params
    panel_root = None
    out = walk_forward(lambda p: None, [], _PanelStub(), train_months=12, test_months=3)
    assert out["status"] == "no_params"


class _PanelStub:
    dates = []
    def month_end_dates(self):
        return []


def test_walk_forward_runs_and_reports_oos(tmp_path):
    panel = _panel(tmp_path)
    grid = [{"lookback_months": 3, "top_n": 1}, {"lookback_months": 6, "top_n": 2}]
    build = lambda p: MomentumRotation(["QQQ", "GLD"], lookback_months=p["lookback_months"],
                                       top_n=p["top_n"])
    out = walk_forward(build, grid, panel, train_months=12, test_months=3)
    assert out["status"] == "ok"
    assert out["splits"] >= 1
    assert "oos_mean_excess" in out and "is_oos_gap" in out
    assert "overfit" in out and out["overfit"] >= 0.0
    assert isinstance(out["still_works_oos"], bool)


def test_insufficient_history(tmp_path):
    _archive(tmp_path, "QQQ", 1.01)
    _archive(tmp_path, "SPY", 1.01)
    # only build a tiny panel by trimming: load then check the short-history guard
    panel = load_price_panel(["QQQ", "SPY"], tmp_path)
    out = walk_forward(lambda p: MomentumRotation(["QQQ"], **p),
                       [{"lookback_months": 3, "top_n": 1}], panel,
                       train_months=240, test_months=3)
    assert out["status"] == "insufficient_data"
