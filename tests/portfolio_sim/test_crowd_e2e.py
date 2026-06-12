"""E2E: crowd proxy backtest artifact is emitted + labeled."""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.portfolio_sim.run_portfolio_backtest import run_portfolio_backtest


def _archive(root, ticker, g):
    d = root / "outputs" / "backtest" / "historical"
    d.mkdir(parents=True, exist_ok=True)
    dates = [f"2024-{mo:02d}-15" for mo in range(1, 13)] + \
            [f"2025-{mo:02d}-15" for mo in range(1, 13)] + ["2026-01-15", "2026-06-15"]
    rows = [{"date": dt, "close": round(100 * (g ** i), 4), "volume": 1000 + i * 10}
            for i, dt in enumerate(dates)]
    (d / f"{ticker}_5y.json").write_text(json.dumps({"symbol": ticker, "rows": list(reversed(rows))}))


def _seed(root):
    cfg = {
        "portfolio": {"holdings": [
            {"symbol": "QQQ", "shares": 6, "asset_class": "us_equity", "is_leveraged": False},
            {"symbol": "GLD", "shares": 4, "asset_class": "commodity", "is_leveraged": False},
        ]},
        "growth_mode": {"concentration_cap": 0.60, "leverage_cap": 0.25},
        "rebalance_rules": {"band_threshold": 0.12},
        "portfolio_sim": {"enabled": True, "primary_benchmark": "SPY", "secondary_benchmarks": ["QQQ"],
                          "monthly_contribution": 1000, "contribution_scenarios": [1000],
                          "windows": ["trailing_1y"], "rebalance_policies": ["periodic"],
                          "universe": {"proxy_etfs": ["BND", "SCHD"]}},
    }
    (root / "config.json").write_text(json.dumps(cfg))
    for t, g in [("QQQ", 1.02), ("GLD", 1.005), ("SPY", 1.015), ("BND", 1.001), ("SCHD", 1.008)]:
        _archive(root, t, g)


def test_crowd_proxy_artifact_labeled(tmp_path):
    _seed(tmp_path)
    r = run_portfolio_backtest(root=tmp_path, run_mode="discovery")
    assert r["status"] == "ok"
    doc = json.loads((tmp_path / "outputs" / "sandbox" / "crowd_tactic_backtest.json").read_text())
    assert doc["proxy"] is True
    assert "NOT real crowd" in doc["measures"]
    assert doc["observe_only"] is True
