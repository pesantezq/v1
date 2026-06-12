"""End-to-end test for the backtest orchestrator."""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.portfolio_sim.run_portfolio_backtest import run_portfolio_backtest


def _archive(root, ticker, base_growth):
    d = root / "outputs" / "backtest" / "historical"
    d.mkdir(parents=True, exist_ok=True)
    dates = [f"2024-{mo:02d}-15" for mo in range(1, 13)] + \
            [f"2025-{mo:02d}-15" for mo in range(1, 13)] + ["2026-01-15", "2026-06-15"]
    rows = [{"date": dt, "close": round(100 * (base_growth ** i), 4), "volume": 1000}
            for i, dt in enumerate(dates)]
    (d / f"{ticker}_5y.json").write_text(json.dumps({"symbol": ticker, "rows": list(reversed(rows))}))


def _config(root, enabled=True):
    cfg = {
        "portfolio": {"holdings": [
            {"symbol": "QQQ", "shares": 6, "asset_class": "us_equity", "is_leveraged": False},
            {"symbol": "GLD", "shares": 4, "asset_class": "commodity", "is_leveraged": False},
            {"symbol": "QLD", "shares": 8, "asset_class": "us_equity_leveraged", "is_leveraged": True},
        ]},
        "growth_mode": {"concentration_cap": 0.60, "leverage_cap": 0.25},
        "rebalance_rules": {"band_threshold": 0.12},
        "portfolio_sim": {
            "enabled": enabled, "primary_benchmark": "SPY", "secondary_benchmarks": ["QQQ"],
            "monthly_contribution": 1000, "contribution_scenarios": [500, 1000],
            "windows": ["trailing_1y", "ytd"], "rebalance_policies": ["buy_and_hold", "periodic"],
            "universe": {"proxy_etfs": ["BND", "SCHD", "USMV"]},
        },
    }
    (root / "config.json").write_text(json.dumps(cfg))


def _seed(root, enabled=True):
    _config(root, enabled)
    for t, g in [("QQQ", 1.02), ("GLD", 1.005), ("QLD", 1.03), ("SPY", 1.015),
                 ("BND", 1.001), ("SCHD", 1.008), ("USMV", 1.01)]:
        _archive(root, t, g)


def test_disabled_writes_degraded_no_crash(tmp_path):
    _seed(tmp_path, enabled=False)
    r = run_portfolio_backtest(root=tmp_path, run_mode="discovery")
    assert r["status"] == "disabled"
    assert (tmp_path / "outputs" / "sandbox" / "portfolio_backtest.json").exists()


def test_e2e_produces_leaderboard_and_catalog(tmp_path):
    _seed(tmp_path, enabled=True)
    r = run_portfolio_backtest(root=tmp_path, run_mode="discovery")
    assert r["status"] == "ok"
    assert r["wrote_files"] is True
    assert r["result_count"] > 0
    disc = tmp_path / "outputs" / "sandbox"
    bt = json.loads((disc / "portfolio_backtest.json").read_text())
    assert bt["observe_only"] is True and bt["sandbox_only"] is True
    assert bt["objective"] == "maximize_excess_vs_sp500"
    assert "leaderboard" in bt and bt["leaderboard"]
    # contribution sensitivity present for both scenarios
    cs = bt["contribution_sensitivity"]["by_window"]
    any_win = next(iter(cs.values()))
    assert "500" in any_win and "1000" in any_win
    cat = json.loads((disc / "strategy_catalog.json").read_text())
    assert cat["coverage_complete"] is True
    assert (tmp_path / "docs" / "STRATEGY_CATALOG.md").exists()


def test_no_decision_plan_mutation(tmp_path):
    _seed(tmp_path, enabled=True)
    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    plan = latest / "decision_plan.json"
    plan.write_text(json.dumps({"decisions": ["UNTOUCHED"]}))
    before = plan.read_text()
    run_portfolio_backtest(root=tmp_path, run_mode="discovery")
    assert plan.read_text() == before


def test_daily_run_mode_cannot_write_sandbox(tmp_path):
    _seed(tmp_path, enabled=True)
    r = run_portfolio_backtest(root=tmp_path, run_mode="daily")
    assert r["wrote_files"] is False
    assert any("write_skipped" in w for w in r["warnings"])
