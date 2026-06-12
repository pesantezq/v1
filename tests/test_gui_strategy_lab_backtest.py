"""Tests for the Strategy Lab backtest + projection sections."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from gui_v2.data.dash_next_stage import collect_strategy_lab_view

_FORBIDDEN = ("buy now", "sell now", "place order", "rebalance now")


def test_loader_empty_when_absent(tmp_path):
    v = collect_strategy_lab_view(tmp_path)
    assert v["backtest"]["available"] is False
    assert v["projection"]["available"] is False


def test_loader_reads_backtest(tmp_path):
    sb = tmp_path / "outputs" / "sandbox"
    sb.mkdir(parents=True)
    (sb / "portfolio_backtest.json").write_text(json.dumps({
        "status": "ok", "objective": "maximize_excess_vs_sp500", "primary_benchmark": "SPY",
        "windows": ["trailing_3y"],
        "leaderboard": {"trailing_3y": [
            {"tactic_id": "profile_aggressive_growth", "name": "Aggressive Growth",
             "policy": "periodic", "excess_vs_spy": 0.08, "cagr": 0.17,
             "max_drawdown": -0.25, "sharpe": 1.1, "final_balance_dca": 50000},
        ]},
        "contribution_sensitivity": {}, "created_at": "2026-06-12T00:00:00Z",
    }))
    v = collect_strategy_lab_view(tmp_path)
    assert v["backtest"]["available"] is True
    assert v["backtest"]["headline_window"] == "trailing_3y"
    assert v["backtest"]["leaderboard"][0]["name"] == "Aggressive Growth"


def test_route_renders_with_backtest_and_no_trade_verbs():
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/strategy-lab")
    assert r.status_code == 200
    text = r.text.lower()
    for verb in _FORBIDDEN:
        assert verb not in text
