"""Phase 11A — Multi-Strategy Portfolio Objective Engine.

Safety-critical: strategies cannot trade or mutate holdings; comparison runs
sandbox-first; tax strategy degrades without tax-lot data; aggressive/boom honor
hard caps; approval is artifact-based.
"""
from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path

import portfolio_automation.strategy.strategy_comparator as sc
from portfolio_automation.strategy.profiles import SEED_PROFILES, build_strategy_profiles
from portfolio_automation.strategy.objective_functions import compute_strategy_metrics
from portfolio_automation.strategy.tax_scorecard import build_tax_scorecard, has_tax_lot_data
from portfolio_automation.next_stage.contracts import (
    StrategyId, BOOM_BUCKET_TOTAL_CAP, BOOM_BUCKET_PER_IDEA_CAP, BLOCKED_STRATEGY_ACTIONS,
)


def _now():
    return datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


def _seed(tmp_path: Path):
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    sb = tmp_path / "outputs" / "sandbox"; sb.mkdir(parents=True)
    (tmp_path / "config.json").write_text(json.dumps({"portfolio": {"holdings": [
        {"symbol": "QQQ", "shares": 10, "target_weight": 0.5},
        {"symbol": "QLD", "shares": 2, "target_weight": 0.1, "is_leveraged": True},
        {"symbol": "GLD", "shares": 5, "target_weight": 0.4}], "cash_available": 500}}))
    sb.joinpath("opportunity_radar.json").write_text(json.dumps({"opportunities": [
        {"candidate": "AMD", "candidate_type": "public_ticker", "final_status": "QUALIFIED",
         "boom_score": 0.6}]}))


# ---------------------------------------------------------------------------
# Profiles + completeness
# ---------------------------------------------------------------------------


def test_eight_profiles_defined():
    assert len(SEED_PROFILES) == 8
    assert set(SEED_PROFILES) == {s.value for s in StrategyId}


def test_aggressive_and_boom_respect_hard_caps():
    for sid in (StrategyId.AGGRESSIVE_GROWTH.value, StrategyId.BOOM_BUCKET.value):
        p = SEED_PROFILES[sid]
        assert p.max_total_speculative <= BOOM_BUCKET_TOTAL_CAP
        assert p.max_per_idea <= BOOM_BUCKET_PER_IDEA_CAP


def test_defensive_has_zero_speculative_cap():
    assert SEED_PROFILES[StrategyId.DEFENSIVE.value].max_total_speculative == 0.0


def test_metrics_have_all_18_fields():
    m = compute_strategy_metrics(SEED_PROFILES[StrategyId.LONG_TERM_COMPOUNDING.value],
                                 {"weights": {"QQQ": 0.6, "GLD": 0.4}, "radar_opportunities": []})
    for k in ("expected_objective_fit", "expected_risk_level", "expected_volatility",
              "max_drawdown_estimate", "concentration_risk", "leverage_exposure", "cash_drag",
              "turnover", "tax_efficiency", "after_tax_return_estimate",
              "opportunity_capture_score", "diversification_score", "liquidity_score",
              "implementation_complexity", "behavioral_difficulty", "confidence_score",
              "data_quality_score", "final_strategy_rank"):
        assert k in m, k


def test_deterministic_metrics():
    ctx = {"weights": {"QQQ": 0.6, "GLD": 0.4}, "radar_opportunities": []}
    a = compute_strategy_metrics(SEED_PROFILES[StrategyId.TAX_AWARE.value], ctx)
    b = compute_strategy_metrics(SEED_PROFILES[StrategyId.TAX_AWARE.value], ctx)
    assert a == b


# ---------------------------------------------------------------------------
# Tax scorecard degradation (§23.11)
# ---------------------------------------------------------------------------


def test_tax_scorecard_degrades_without_tax_lot_data():
    sc1 = build_tax_scorecard("2026-06-09T00:00:00", {"positions": [{"symbol": "QQQ"}]})
    assert sc1["degraded_mode"] is True
    assert sc1["scorecards"] == []
    assert "placeholders" in sc1


def test_tax_scorecard_computes_with_cost_basis():
    pos = {"positions": [{"symbol": "QQQ", "average_cost": 300, "market_value": 400,
                          "unrealized_gain": 100},
                         {"symbol": "X", "average_cost": 50, "market_value": 40,
                          "unrealized_gain": -10}]}
    assert has_tax_lot_data(pos) is True
    sc2 = build_tax_scorecard("2026-06-09T00:00:00", pos)
    assert sc2["degraded_mode"] is False
    tlh = [c for c in sc2["scorecards"] if c["tlh_candidate"]]
    assert any(c["symbol"] == "X" for c in tlh)


# ---------------------------------------------------------------------------
# Writes: sandbox + latest; never decision_plan; review queue gated
# ---------------------------------------------------------------------------


def test_writes_all_strategy_artifacts(tmp_path):
    _seed(tmp_path)
    res = sc.write_strategy_artifacts(tmp_path, _now())
    sb = tmp_path / "outputs" / "sandbox"
    for fn in ("strategy_profiles.json", "strategy_comparison.json",
               "strategy_risk_scorecard.json", "strategy_shadow_results.json",
               "strategy_tax_scorecard.json"):
        assert (sb / fn).exists(), fn
    assert (tmp_path / "outputs" / "latest" / "strategy_review_queue.json").exists()
    assert res["degraded"] is False


def test_comparison_owned_by_comparator_with_produced_by(tmp_path):
    _seed(tmp_path)
    sc.write_strategy_artifacts(tmp_path, _now())
    comp = json.loads((tmp_path / "outputs" / "sandbox" / "strategy_comparison.json").read_text())
    assert comp["produced_by"] == "strategy_comparator"
    assert comp["evidence_preference"] == "sandbox_backtest_over_narrative"


def test_never_writes_decision_plan(tmp_path):
    _seed(tmp_path)
    sc.write_strategy_artifacts(tmp_path, _now())
    assert not (tmp_path / "outputs" / "latest" / "decision_plan.json").exists()


def test_review_queue_blocks_execution_actions(tmp_path):
    _seed(tmp_path)
    sc.write_strategy_artifacts(tmp_path, _now())
    q = json.loads((tmp_path / "outputs" / "latest" / "strategy_review_queue.json").read_text())
    assert q["queue"]
    for item in q["queue"]:
        for blocked in ("place_trade", "submit_order", "move_money",
                        "broker_write_action", "auto_rebalance", "modify_real_holdings"):
            assert blocked in item["blocked_actions"]
        assert "mark_as_preferred_profile" in item["allowed_actions"]


# ---------------------------------------------------------------------------
# AST: the strategy package contains no trade/order/broker-write primitives
# ---------------------------------------------------------------------------


def test_strategy_package_has_no_execution_primitives():
    pkg = Path(__file__).resolve().parents[1] / "portfolio_automation" / "strategy"
    forbidden = ("place_order", "submit_order", "execute_trade", "buy", "sell",
                 "broker_write", "rebalance_now", "modify_holdings")
    for py in pkg.glob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name.lower()
                assert not any(f in name for f in forbidden), f"{py.name}:{node.name}"


def test_degrades_with_empty_repo(tmp_path):
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    (tmp_path / "outputs" / "sandbox").mkdir(parents=True)
    res = sc.write_strategy_artifacts(tmp_path, _now())
    # no config/radar → still writes valid artifacts (8 profiles always defined)
    comp = json.loads((tmp_path / "outputs" / "sandbox" / "strategy_comparison.json").read_text())
    assert comp["observe_only"] is True
