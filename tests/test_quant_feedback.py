"""Phase 5 — quant feedback attribution (observe-only).

Joins the Phase 4 decision-time context (regime/crowd/strategy) with matured
outcomes and attributes performance by dimension using the standardized
taxonomy + honest denominators + sample sufficiency. Evidence only — never
mutates production.

TDD: written before portfolio_automation/quant_feedback.py existed.
"""
from __future__ import annotations

import portfolio_automation.quant_feedback as qf


def _ctx(symbol, action, regime, crowd, strategy="production"):
    return {"symbol": symbol, "action": action, "regime_at_decision": regime,
            "crowd_state_at_decision": crowd, "strategy_id": strategy,
            "data_quality_state": "ok"}


def test_attribute_by_regime_uses_taxonomy_and_honest_denominator():
    ctx = [
        _ctx("AAPL", "BUY", "bull", "confirmed"),
        _ctx("MSFT", "BUY", "bull", "confirmed"),
        _ctx("NVDA", "BUY", "bull", "divergent"),
        _ctx("TSLA", "BUY", "bear", "confirmed"),
    ]
    # return_pct by symbol (percent units): AAPL hit, MSFT neutral (sub-band),
    # NVDA miss, TSLA unresolved
    outcomes = {"AAPL": 3.0, "MSFT": 0.2, "NVDA": -4.0, "TSLA": None}
    res = qf.attribute_outcomes(ctx, outcomes, dimension="regime_at_decision")
    bull = res["bull"]
    # AAPL hit, MSFT neutral (excluded), NVDA miss -> judgeable 2, hits 1
    assert bull["judgeable"] == 2 and bull["hits"] == 1
    assert abs(bull["hit_rate"] - 0.5) < 1e-9
    assert bull["n_samples"] == 3
    assert bull["neutral"] == 1
    bear = res["bear"]
    assert bear["unresolved"] == 1 and bear["judgeable"] == 0
    assert bear["hit_rate"] is None  # no judgeable -> no fake number


def test_sample_sufficiency_flag():
    ctx = [_ctx(f"S{i}", "BUY", "bull", "x") for i in range(5)]
    outcomes = {f"S{i}": 3.0 for i in range(5)}
    res = qf.attribute_outcomes(ctx, outcomes, dimension="regime_at_decision")
    assert res["bull"]["sample_sufficient"] is False  # n=5 < 30
    assert res["bull"]["mean_return"] == 3.0


def test_attribute_by_crowd_state_and_strategy():
    ctx = [_ctx("AAPL", "BUY", "bull", "confirmed", strategy="aggressive_growth")]
    out = {"AAPL": 2.5}
    by_crowd = qf.attribute_outcomes(ctx, out, dimension="crowd_state_at_decision")
    assert "confirmed" in by_crowd
    by_strat = qf.attribute_outcomes(ctx, out, dimension="strategy_id")
    assert "aggressive_growth" in by_strat


def test_missing_dimension_routes_to_unknown_bucket():
    ctx = [{"symbol": "AAPL", "action": "BUY", "data_quality_state": "ok"}]  # no regime
    res = qf.attribute_outcomes(ctx, {"AAPL": 3.0}, dimension="regime_at_decision")
    assert "unknown" in res


def test_build_quant_feedback_degrades_without_outcomes(tmp_path):
    # no context log, no outcomes -> valid, observe-only, insufficient evidence
    res = qf.build_quant_feedback(tmp_path, now="2026-06-30T09:00:00+00:00")
    assert res["observe_only"] is True
    assert res["evidence_status"] in ("insufficient", "ok")
    assert "by_regime" in res and "by_crowd_state" in res and "by_strategy" in res
    assert res["fallback_rate"] is not None
