"""E2E for the research-backed strategy lab orchestrator."""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.portfolio_sim.run_strategy_lab import run_strategy_lab


def _archive(root, ticker, g):
    d = root / "outputs" / "backtest" / "historical"
    d.mkdir(parents=True, exist_ok=True)
    dates = [f"2023-{mo:02d}-15" for mo in range(1, 13)] + \
            [f"2024-{mo:02d}-15" for mo in range(1, 13)] + \
            [f"2025-{mo:02d}-15" for mo in range(1, 13)] + ["2026-01-15", "2026-06-15"]
    rows = [{"date": dt, "close": round(100 * (g ** i), 4), "volume": 1000 + i} for i, dt in enumerate(dates)]
    (d / f"{ticker}_5y.json").write_text(json.dumps({"symbol": ticker, "rows": list(reversed(rows))}))


def _seed(root, enabled=True):
    cfg = {
        "portfolio": {"holdings": [
            {"symbol": "QQQ", "shares": 6, "asset_class": "us_equity", "is_leveraged": False},
            {"symbol": "GLD", "shares": 4, "asset_class": "commodity", "is_leveraged": False},
            {"symbol": "QLD", "shares": 8, "asset_class": "us_equity_leveraged", "is_leveraged": True},
        ]},
        "growth_mode": {"concentration_cap": 0.60, "leverage_cap": 0.25, "target_cagr": 0.09},
        "rebalance_rules": {"band_threshold": 0.12},
        "portfolio_sim": {"enabled": enabled, "primary_benchmark": "SPY",
                          "monthly_contribution": 1000,
                          "universe": {"proxy_etfs": ["BND", "SCHD", "USMV"]},
                          "strategy_lab": {"enabled": enabled, "windows": ["trailing_1y", "trailing_3y"]}},
    }
    (root / "config.json").write_text(json.dumps(cfg))
    for t, g in [("QQQ", 1.02), ("GLD", 1.004), ("QLD", 1.03), ("SPY", 1.013),
                 ("BND", 1.001), ("SCHD", 1.007), ("USMV", 1.006)]:
        _archive(root, t, g)


def test_disabled(tmp_path):
    _seed(tmp_path, enabled=False)
    r = run_strategy_lab(root=tmp_path, run_mode="discovery")
    assert r["status"] == "disabled"


def test_e2e_leaderboard_ranked_by_score(tmp_path):
    _seed(tmp_path, enabled=True)
    r = run_strategy_lab(root=tmp_path, run_mode="discovery")
    assert r["status"] == "ok"
    assert r["wrote_files"] is True
    lb = json.loads((tmp_path / "outputs" / "sandbox" / "strategy_leaderboard.json").read_text())
    assert lb["observe_only"] is True
    assert lb["objective"] == "maximize_excess_vs_sp500"
    rows = lb["leaderboard"]
    assert len(rows) >= 8   # 6 shadow + 8 profiles + 2 benchmarks + 6 research (minus any degraded)
    # sorted by strategy_score descending
    scores = [row["strategy_score"] for row in rows]
    assert scores == sorted(scores, reverse=True)
    # research tactics carry academic_basis
    research = [row for row in rows if row["tactic_id"].startswith("research_")]
    assert research and all(row["academic_basis"] for row in research)
    # catalog coverage
    cat = json.loads((tmp_path / "outputs" / "sandbox" / "research_strategy_catalog.json").read_text())
    assert cat["coverage_complete"] is True


def test_no_decision_plan_mutation(tmp_path):
    _seed(tmp_path, enabled=True)
    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    plan = latest / "decision_plan.json"
    plan.write_text(json.dumps({"x": 1}))
    before = plan.read_text()
    run_strategy_lab(root=tmp_path, run_mode="discovery")
    assert plan.read_text() == before


def test_daily_cannot_write(tmp_path):
    _seed(tmp_path, enabled=True)
    r = run_strategy_lab(root=tmp_path, run_mode="daily")
    assert r["wrote_files"] is False
