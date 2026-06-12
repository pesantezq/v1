"""E2E for the projection orchestrator."""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.portfolio_sim.run_portfolio_projection import run_portfolio_projection


def _archive(root, ticker, g):
    d = root / "outputs" / "backtest" / "historical"
    d.mkdir(parents=True, exist_ok=True)
    dates = [f"2023-{mo:02d}-15" for mo in range(1, 13)] + \
            [f"2024-{mo:02d}-15" for mo in range(1, 13)] + \
            [f"2025-{mo:02d}-15" for mo in range(1, 13)]
    rows = [{"date": dt, "close": round(100 * (g ** i), 4), "volume": 1000} for i, dt in enumerate(dates)]
    (d / f"{ticker}_5y.json").write_text(json.dumps({"symbol": ticker, "rows": list(reversed(rows))}))


def _seed(root, enabled=True):
    cfg = {
        "portfolio": {"holdings": [
            {"symbol": "QQQ", "shares": 6, "asset_class": "us_equity", "is_leveraged": False},
            {"symbol": "GLD", "shares": 4, "asset_class": "commodity", "is_leveraged": False},
        ]},
        "growth_mode": {"concentration_cap": 0.60, "leverage_cap": 0.25, "target_cagr": 0.09},
        "portfolio_sim": {"enabled": enabled, "monthly_contribution": 1000,
                          "universe": {"proxy_etfs": ["BND", "SCHD"]},
                          "projection": {"n_paths": 1000, "seed": 7, "block_months": 1,
                                         "horizons_years": [1, 5]}},
    }
    (root / "config.json").write_text(json.dumps(cfg))
    for t, g in [("QQQ", 1.015), ("GLD", 1.004), ("SPY", 1.012), ("QQQ", 1.015),
                 ("BND", 1.001), ("SCHD", 1.006)]:
        _archive(root, t, g)


def test_disabled(tmp_path):
    _seed(tmp_path, enabled=False)
    r = run_portfolio_projection(root=tmp_path, run_mode="discovery")
    assert r["status"] == "disabled"


def test_e2e_projection(tmp_path):
    _seed(tmp_path, enabled=True)
    r = run_portfolio_projection(root=tmp_path, run_mode="discovery")
    assert r["status"] == "ok"
    assert r["wrote_files"] is True
    doc = json.loads((tmp_path / "outputs" / "sandbox" / "portfolio_projection.json").read_text())
    assert doc["observe_only"] is True
    assert "illustration, not a forecast" in doc["assumptions"]
    assert doc["seed"] == 7
    assert doc["rows"]
    # percentiles monotone on a row
    row = doc["rows"][0]
    assert row["p5_balance"] <= row["p50_balance"] <= row["p95_balance"]


def test_no_decision_plan_mutation(tmp_path):
    _seed(tmp_path, enabled=True)
    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    plan = latest / "decision_plan.json"
    plan.write_text(json.dumps({"decisions": ["UNTOUCHED"]}))
    before = plan.read_text()
    run_portfolio_projection(root=tmp_path, run_mode="discovery")
    assert plan.read_text() == before


def test_daily_cannot_write(tmp_path):
    _seed(tmp_path, enabled=True)
    r = run_portfolio_projection(root=tmp_path, run_mode="daily")
    assert r["wrote_files"] is False
