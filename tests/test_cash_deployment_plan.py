"""Tests for portfolio_automation/cash_deployment_plan.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portfolio_automation.cash_deployment_plan import (
    _MAX_POSITION_PCT,
    allocate_deployment,
    build_plan,
    compute_available_cash,
    rank_deployable_decisions,
    run_cash_deployment_plan,
)


# ---------------------------------------------------------------------------
# compute_available_cash
# ---------------------------------------------------------------------------


def test_excess_cash_above_target():
    s = compute_available_cash(
        portfolio_value=10_000.0, cash_available=1_500.0,
        target_cash_pct=0.05, monthly_contribution=1_000.0,
    )
    assert s["excess_cash_pct"] == pytest.approx(0.10, rel=1e-3)  # 15%-5%
    assert s["incoming_pct"] == pytest.approx(0.10, rel=1e-3)
    assert s["total_deployable_amount"] == pytest.approx(2_000.0, rel=1e-3)


def test_no_excess_no_contribution_zero_deployable():
    s = compute_available_cash(
        portfolio_value=10_000.0, cash_available=500.0,
        target_cash_pct=0.05, monthly_contribution=0.0,
    )
    assert s["total_deployable_amount"] == 0.0


def test_below_safety_floor_uses_contribution_minus_refill():
    s = compute_available_cash(
        portfolio_value=10_000.0, cash_available=100.0,  # 1% cash
        target_cash_pct=0.05, monthly_contribution=1_000.0,
        safety_floor_pct=0.05,
    )
    assert s["below_safety_floor"] is True
    # incoming 10% - refill 4% = 6% deployable
    assert s["total_deployable_pct"] == pytest.approx(0.06, rel=1e-3)


def test_zero_portfolio_value_returns_zero():
    s = compute_available_cash(
        portfolio_value=0.0, cash_available=0.0,
        target_cash_pct=0.05, monthly_contribution=1_000.0,
    )
    assert s["total_deployable_amount"] == 0.0


# ---------------------------------------------------------------------------
# rank_deployable_decisions
# ---------------------------------------------------------------------------


def test_rank_filters_non_buy_scale():
    decisions = [
        {"symbol": "A", "decision": "BUY",   "priority": 0.4},
        {"symbol": "B", "decision": "HOLD",  "priority": 0.9},
        {"symbol": "C", "decision": "WAIT",  "priority": 0.8},
        {"symbol": "D", "decision": "SCALE", "priority": 0.7},
        {"symbol": "E", "decision": "AVOID", "priority": 0.6},
    ]
    ranked = rank_deployable_decisions(decisions)
    assert [r["symbol"] for r in ranked] == ["D", "A"]


def test_rank_handles_empty():
    assert rank_deployable_decisions([]) == []
    assert rank_deployable_decisions(None) == []


def test_rank_caps_at_max():
    decisions = [
        {"symbol": f"S{i}", "decision": "BUY", "priority": i / 100.0}
        for i in range(30)
    ]
    ranked = rank_deployable_decisions(decisions)
    assert len(ranked) == 10
    # Highest priority comes first
    assert ranked[0]["symbol"] == "S29"


# ---------------------------------------------------------------------------
# allocate_deployment
# ---------------------------------------------------------------------------


def test_allocate_respects_recommended_pct():
    rows = allocate_deployment(
        deployable_amount=2_000.0,
        portfolio_value=10_000.0,
        ranked_decisions=[
            {
                "symbol": "QQQ", "decision": "BUY", "priority": 0.9,
                "recommended_allocation_pct": 0.05,
                "inputs_used": {"conviction_band": "high_conviction"},
            },
        ],
    )
    # 5% × $10k × 1.00 (high_conviction) = $500 → below 8% cap → $500
    assert rows[0]["suggested_amount"] == pytest.approx(500.0, rel=1e-3)


def test_allocate_respects_position_cap():
    rows = allocate_deployment(
        deployable_amount=5_000.0,
        portfolio_value=10_000.0,
        ranked_decisions=[
            {
                "symbol": "QQQ", "decision": "BUY", "priority": 0.9,
                "recommended_allocation_pct": 0.20,   # 20% asked
                "inputs_used": {"conviction_band": "high_conviction"},
            },
        ],
    )
    # 20% × $10k = $2000 capped at 8% × $10k = $800
    assert rows[0]["suggested_amount"] == pytest.approx(800.0, rel=1e-3)


def test_allocate_band_multiplier_for_starter():
    rows = allocate_deployment(
        deployable_amount=2_000.0,
        portfolio_value=10_000.0,
        ranked_decisions=[
            {
                "symbol": "X", "decision": "BUY", "priority": 0.5,
                "recommended_allocation_pct": 0.04,
                "inputs_used": {"conviction_band": "starter"},
            },
        ],
    )
    # 4% × 10k × 0.25 = $100
    assert rows[0]["suggested_amount"] == pytest.approx(100.0, rel=1e-3)


def test_allocate_budget_exhaustion():
    rows = allocate_deployment(
        deployable_amount=300.0,
        portfolio_value=10_000.0,
        ranked_decisions=[
            {
                "symbol": "A", "decision": "BUY", "priority": 0.9,
                "recommended_allocation_pct": 0.05,
                "inputs_used": {"conviction_band": "high_conviction"},
            },
            {
                "symbol": "B", "decision": "BUY", "priority": 0.5,
                "recommended_allocation_pct": 0.05,
                "inputs_used": {"conviction_band": "normal"},
            },
        ],
    )
    # A gets ~300 (capped by budget), B gets skipped
    assert rows[0]["suggested_amount"] == pytest.approx(300.0, rel=1e-3)
    assert rows[1]["suggested_amount"] == 0.0
    assert rows[1]["skipped_reason"] == "budget exhausted"


def test_allocate_empty_decisions():
    rows = allocate_deployment(
        deployable_amount=1_000.0,
        portfolio_value=10_000.0,
        ranked_decisions=[],
    )
    assert rows == []


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------


def test_build_plan_envelope():
    plan = build_plan(
        cash_summary={"total_deployable_amount": 1000.0},
        deployment_rows=[{"symbol": "A", "suggested_amount": 500.0}],
        degraded_mode=False, data_mode="live", notes=["hello"],
    )
    assert plan["observe_only"] is True
    assert plan["total_deployed_amount"] == 500.0
    assert plan["remaining_budget"] == 500.0
    assert plan["notes"] == ["hello"]
    assert plan["schema_version"] == "1"


# ---------------------------------------------------------------------------
# run_cash_deployment_plan integration
# ---------------------------------------------------------------------------


def _setup_repo(tmp_path: Path, cfg_overrides=None, decisions=None,
                system_health=None) -> Path:
    cfg = {
        "portfolio": {
            "holdings": [{"symbol": "QQQ", "shares": 6}],
            "cash_available": 1_500.0,
            "target_cash_weight": 0.05,
            "monthly_contribution": 1_000.0,
        }
    }
    if cfg_overrides:
        cfg["portfolio"].update(cfg_overrides)
    (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    outputs = tmp_path / "outputs" / "latest"
    outputs.mkdir(parents=True, exist_ok=True)
    decision_plan = {
        "observe_only": True,
        "decisions": decisions if decisions is not None else [
            {
                "symbol": "QQQ", "decision": "BUY", "priority": 0.9,
                "recommended_allocation_pct": 0.05,
                "inputs_used": {
                    "conviction_band": "high_conviction",
                    "portfolio_context": {"total_portfolio_value": 10_000.0},
                },
            },
        ],
    }
    (outputs / "decision_plan.json").write_text(json.dumps(decision_plan), encoding="utf-8")
    if system_health is not None:
        (outputs / "system_decision_summary.json").write_text(
            json.dumps({"data_health": system_health}), encoding="utf-8"
        )
    return tmp_path


def test_run_plan_deploys_when_budget_available(tmp_path):
    repo = _setup_repo(tmp_path)
    plan = run_cash_deployment_plan(repo, base_dir=repo / "outputs")
    assert plan["observe_only"] is True
    assert plan["total_deployed_amount"] > 0
    assert plan["deployment_rows"][0]["symbol"] == "QQQ"
    out_json = repo / "outputs" / "latest" / "cash_deployment_plan.json"
    assert out_json.exists()


def test_run_plan_suspends_when_degraded(tmp_path):
    repo = _setup_repo(tmp_path, system_health={"degraded_mode": True, "data_mode": "fallback"})
    plan = run_cash_deployment_plan(repo, base_dir=repo / "outputs")
    assert plan["degraded_mode"] is True
    assert plan["total_deployed_amount"] == 0.0
    assert any("degraded_mode" in n for n in plan["notes"])


def test_run_plan_no_eligible_decisions(tmp_path):
    repo = _setup_repo(
        tmp_path,
        decisions=[
            {"symbol": "X", "decision": "HOLD", "priority": 0.5,
             "inputs_used": {"portfolio_context": {"total_portfolio_value": 10_000.0}}}
        ],
    )
    plan = run_cash_deployment_plan(repo, base_dir=repo / "outputs")
    assert plan["total_deployed_amount"] == 0.0
    assert any("no BUY/SCALE" in n for n in plan["notes"])


def test_run_plan_missing_files_safe(tmp_path):
    # No config.json, no decision_plan.json
    plan = run_cash_deployment_plan(tmp_path, base_dir=tmp_path / "outputs")
    assert plan["observe_only"] is True
    assert plan["deployment_rows"] == []


def test_artifact_observe_only_field_is_hardcoded(tmp_path):
    repo = _setup_repo(tmp_path)
    run_cash_deployment_plan(repo, base_dir=repo / "outputs")
    payload = json.loads(
        (repo / "outputs" / "latest" / "cash_deployment_plan.json")
        .read_text("utf-8")
    )
    assert payload["observe_only"] is True


def test_max_position_cap_enforced_at_8pct(tmp_path):
    # Portfolio value 10k, request 20% allocation, high_conviction band.
    repo = _setup_repo(
        tmp_path,
        decisions=[
            {
                "symbol": "X", "decision": "BUY", "priority": 0.9,
                "recommended_allocation_pct": 0.20,
                "inputs_used": {
                    "conviction_band": "high_conviction",
                    "portfolio_context": {"total_portfolio_value": 10_000.0},
                },
            }
        ],
    )
    plan = run_cash_deployment_plan(repo, base_dir=repo / "outputs")
    # 8% of $10k = $800 max
    assert plan["deployment_rows"][0]["suggested_amount"] <= _MAX_POSITION_PCT * 10_000.0 + 0.01
