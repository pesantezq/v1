"""Phase 11 — risk + scenario comparison (observe-only).

Deterministic stress scenarios applied to portfolio weights, with pre/post-action
risk and per-action marginal contribution. Illustrations, NOT forecasts; honest
degrade when covariance / ETF constituents are unavailable (no fabrication).

TDD: written before portfolio_automation/scenario_risk.py existed.
"""
from __future__ import annotations

import portfolio_automation.scenario_risk as sr


def test_named_scenarios_present():
    for name in ("broad_market_decline", "nasdaq_growth_decline",
                 "semiconductor_drawdown", "volatility_spike", "rate_shock",
                 "gold_decline", "liquidity_shock"):
        assert name in sr.SCENARIOS


def test_apply_scenario_is_deterministic_and_weighted():
    weights = {"QQQ": 0.5, "GLD": 0.5}
    r1 = sr.apply_scenario(weights, "broad_market_decline")
    r2 = sr.apply_scenario(weights, "broad_market_decline")
    assert r1 == r2                       # deterministic
    assert r1["portfolio_return_pct"] < 0  # a decline scenario hurts a long book
    assert "by_position" in r1 and set(r1["by_position"]) == {"QQQ", "GLD"}


def test_gold_scenario_hits_gold_not_broad_equity():
    g = sr.apply_scenario({"GLD": 1.0}, "gold_decline")["portfolio_return_pct"]
    q = sr.apply_scenario({"QQQ": 1.0}, "gold_decline")["portfolio_return_pct"]
    assert g < q  # gold-specific shock hurts GLD more than QQQ


def test_pre_post_action_risk_and_marginal():
    before = {"QQQ": 1.0}
    after = {"QQQ": 0.5, "GLD": 0.5}   # trim QQQ, add GLD
    cmp = sr.pre_post_action_risk(before, after, "nasdaq_growth_decline")
    assert cmp["pre_return_pct"] < cmp["post_return_pct"]  # diversifying softens the hit
    assert "marginal_contribution" in cmp


def test_marginal_contribution_aggregates_sensibly():
    weights = {"QQQ": 0.6, "GLD": 0.4}
    mc = sr.marginal_contribution(weights, "broad_market_decline")
    total = sr.apply_scenario(weights, "broad_market_decline")["portfolio_return_pct"]
    assert abs(sum(mc.values()) - total) < 1e-6  # contributions sum to the total


def test_build_scenario_risk_degrades_without_holdings(tmp_path):
    res = sr.build_scenario_risk(tmp_path, now="2026-06-30T09:00:00+00:00")
    assert res["observe_only"] is True
    assert res["is_forecast"] is False           # illustration, not a forecast
    assert res["scenarios"]  # all scenarios present even with no holdings
    assert res.get("degraded") in (True, False)


def test_etf_lookthrough_not_fabricated(tmp_path):
    res = sr.build_scenario_risk(tmp_path, now="2026-06-30T09:00:00+00:00")
    assert res["etf_lookthrough_available"] is False  # honest: no constituent data
