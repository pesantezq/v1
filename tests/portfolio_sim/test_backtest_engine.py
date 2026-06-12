"""Tests for the backtest engine."""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.portfolio_sim.backtest_engine import (
    benchmark_total_return,
    run_backtest,
)
from portfolio_automation.portfolio_sim.prices import load_price_panel
from portfolio_automation.portfolio_sim.rebalance import BuyAndHold, Periodic
from portfolio_automation.portfolio_sim.tactics import Tactic
from portfolio_automation.portfolio_sim.windows import Window


def _archive(root, ticker, rows):
    d = root / "outputs" / "backtest" / "historical"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ticker}_5y.json").write_text(json.dumps({"symbol": ticker, "rows": list(reversed(rows))}))


def _panel(tmp_path):
    # AAA doubles over the window; BBB flat. Monthly-ish daily points.
    aaa, bbb = [], []
    price_a, price_b = 100.0, 100.0
    dates = [f"2025-{mo:02d}-15" for mo in range(1, 13)] + ["2026-01-15"]
    for i, dt in enumerate(dates):
        aaa.append({"date": dt, "close": round(100 * (2 ** (i / 12)), 4), "volume": 1000})
        bbb.append({"date": dt, "close": 100.0, "volume": 1000})
    _archive(tmp_path, "AAA", aaa)
    _archive(tmp_path, "BBB", bbb)
    return load_price_panel(["AAA", "BBB"], tmp_path)


def _full_window(panel):
    return Window("full", "Full", panel.dates[0], panel.dates[-1],
                  max((int(panel.dates[-1][:4]) - int(panel.dates[0][:4])), 1))


def test_buy_and_hold_growth(tmp_path):
    panel = _panel(tmp_path)
    win = _full_window(panel)
    t = Tactic("aaa", "AAA only", "test", {"AAA": 1.0})
    res = run_backtest(t, BuyAndHold(), panel, win, start_value=10000, monthly_contribution=0)
    assert res.metrics["status"] == "ok"
    # AAA doubled → ~100% time-weighted return
    assert abs(res.metrics["time_weighted_return"] - 1.0) < 0.05
    assert res.metrics["max_drawdown"] == 0.0  # monotonic up


def test_dca_adds_contributions(tmp_path):
    panel = _panel(tmp_path)
    win = _full_window(panel)
    t = Tactic("aaa", "AAA only", "test", {"AAA": 1.0})
    res = run_backtest(t, BuyAndHold(), panel, win, start_value=10000, monthly_contribution=1000)
    # 12 monthly contributions of 1000 added to the 10k base
    assert res.metrics["total_contributed"] >= 10000 + 11 * 1000
    assert res.metrics["final_balance_dca"] > res.metrics["total_contributed"]  # positive drift


def test_excess_vs_spy(tmp_path):
    panel = _panel(tmp_path)
    win = _full_window(panel)
    t = Tactic("aaa", "AAA only", "test", {"AAA": 1.0})
    spy_ret = benchmark_total_return(panel, "BBB", win)  # flat benchmark → 0
    res = run_backtest(t, BuyAndHold(), panel, win, benchmark_returns={"SPY": spy_ret})
    assert res.metrics["excess_vs_spy"] > 0.5   # AAA beat the flat benchmark


def test_missing_ticker_degraded_renormalized(tmp_path):
    panel = _panel(tmp_path)
    win = _full_window(panel)
    t = Tactic("mix", "Mix", "test", {"AAA": 0.5, "ZZZ": 0.5})  # ZZZ absent
    res = run_backtest(t, BuyAndHold(), panel, win)
    assert "ZZZ" in res.degraded
    assert res.metrics["status"] == "ok"  # AAA renormalized to 100%


def test_insufficient_data(tmp_path):
    panel = _panel(tmp_path)
    win = Window("empty", "Empty", "2030-01-01", "2030-12-31", 1)
    t = Tactic("aaa", "AAA", "test", {"AAA": 1.0})
    res = run_backtest(t, BuyAndHold(), panel, win)
    assert res.metrics["status"] == "insufficient_data"
