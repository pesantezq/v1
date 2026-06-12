"""Tests for the strategy catalog producer (documentation rule mechanism)."""
from __future__ import annotations

from portfolio_automation.portfolio_sim.strategy_docs import (
    build_strategy_catalog,
    render_strategy_catalog_md,
)
from portfolio_automation.portfolio_sim.tactics import Tactic


def test_catalog_card_per_tactic_with_rationale():
    tactics = [
        Tactic("benchmark_spy", "S&P 500 (SPY)", "benchmark", {"SPY": 1.0}),
        Tactic("profile_defensive_capital_preservation", "Defensive", "strategy_profile",
               {"GLD": 0.5, "BND": 0.5}, metadata={"materialization": {"rules": ["leveraged ×0.0"]}}),
    ]
    results = {"benchmark_spy": [{"excess_vs_spy": 0.0, "cagr": 0.09, "max_drawdown": -0.2, "window_label": "Trailing 5y"}]}
    cat = build_strategy_catalog(tactics, results)
    assert cat["tactic_count"] == 2
    assert cat["coverage_complete"] is True       # both have built-in rationale
    spy = next(c for c in cat["cards"] if c["tactic_id"] == "benchmark_spy")
    assert spy["rationale"]
    assert spy["metrics_by_window"]


def test_missing_rationale_breaks_coverage():
    tactics = [Tactic("custom_unknown", "Custom", "test", {"AAA": 1.0})]
    cat = build_strategy_catalog(tactics, {})
    assert cat["coverage_complete"] is False
    assert "custom_unknown" in cat["undocumented"]


def test_render_md_contains_names_and_observe_only():
    tactics = [Tactic("benchmark_spy", "S&P 500 (SPY)", "benchmark", {"SPY": 1.0})]
    md = render_strategy_catalog_md(build_strategy_catalog(tactics, {}))
    assert "Strategy Catalog" in md
    assert "observe-only" in md.lower()
    assert "S&P 500 (SPY)" in md
