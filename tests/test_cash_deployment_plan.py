"""Tests for portfolio_automation/cash_deployment_plan.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import date

from portfolio_automation.cash_deployment_plan import (
    _MAX_POSITION_PCT,
    STATUS_DEFERRED_BY_MONTHLY_BUDGET,
    STATUS_DEFERRED_BY_WEEKLY_PACING,
    allocate_deployment,
    allocate_within_envelope,
    build_plan,
    capital_config,
    compute_available_cash,
    compute_monthly_envelope,
    compute_weekly_pacing,
    iso_weeks_remaining_in_cycle,
    rank_deployable_decisions,
    run_cash_deployment_plan,
    weekday_days_remaining_in_week,
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
    assert s["incoming_pct"] == pytest.approx(0.10, rel=1e-3)     # display only
    # Deposited-contribution model: the contribution is already inside
    # cash_available, so deployable = excess above target only (NOT excess+incoming).
    assert s["total_deployable_amount"] == pytest.approx(1_000.0, rel=1e-3)


def test_no_excess_no_contribution_zero_deployable():
    s = compute_available_cash(
        portfolio_value=10_000.0, cash_available=500.0,
        target_cash_pct=0.05, monthly_contribution=0.0,
    )
    assert s["total_deployable_amount"] == 0.0


def test_below_safety_floor_nothing_deployable():
    s = compute_available_cash(
        portfolio_value=10_000.0, cash_available=100.0,  # 1% cash
        target_cash_pct=0.05, monthly_contribution=1_000.0,
        safety_floor_pct=0.05,
    )
    assert s["below_safety_floor"] is True
    # Deposited-contribution model: below the reserve floor, all cash (contribution
    # already included) is needed to restore the floor → nothing deployable.
    assert s["total_deployable_pct"] == 0.0


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
    # 20% × $10k = $2000 capped at 12% × $10k = $1200 (2026-06-26 partial revert)
    assert rows[0]["suggested_amount"] == pytest.approx(1200.0, rel=1e-3)


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
    # schema v2 adds monthly_capital_envelope + concentration (additive; v1 fields kept)
    assert plan["schema_version"] == "2"


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


def test_run_plan_prefers_top_level_portfolio_context(tmp_path):
    """Top-level decision_plan.portfolio_context wins over per-decision inputs_used."""
    repo = _setup_repo(tmp_path)
    # Rewrite decision_plan.json with top-level context different from per-row.
    decision_plan_path = repo / "outputs" / "latest" / "decision_plan.json"
    decision_plan = json.loads(decision_plan_path.read_text(encoding="utf-8"))
    decision_plan["portfolio_context"] = {
        "total_portfolio_value": 50_000.0,
        "cash": 5_000.0,
    }
    # Per-row inputs_used has 10_000 which would otherwise be picked.
    decision_plan_path.write_text(json.dumps(decision_plan), encoding="utf-8")
    plan = run_cash_deployment_plan(repo, base_dir=repo / "outputs")
    assert plan["cash_summary"]["portfolio_value"] == 50_000.0


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


def test_max_position_cap_enforced_at_15pct(tmp_path):
    # Portfolio value 10k, request 20% allocation, high_conviction band.
    # _MAX_POSITION_PCT (0.15 post-retune) mirrors allocation_engine.max_position_cap.
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
    # 15% of $10k = $1,500 max
    assert plan["deployment_rows"][0]["suggested_amount"] <= _MAX_POSITION_PCT * 10_000.0 + 0.01


# ---------------------------------------------------------------------------
# Glide-in excess cash + weekly deployment pacing (spec 2026-07-07)
# ---------------------------------------------------------------------------


def _envelope(**overrides):
    """compute_monthly_envelope with the spec's worked-example defaults."""
    kw = dict(
        portfolio_value=10_480.0, cash_on_hand=3_151.0,
        monthly_contribution_gross=1_000.0, reserve_pct=0.05,
        deployed_before_today=0.0, capital_funded_today=0.0,
        cycle_id="2026-07", cycle_start="2026-07-01", cycle_end="2026-07-31",
        monthly_history_status="ok", excess_cash_glide_fraction=0.25,
    )
    kw.update(overrides)
    return compute_monthly_envelope(**kw)


def test_glide_math_matches_spec_example():
    env = _envelope()
    assert env["cash_reserve_target_amount"] == 524.0
    assert env["deployable_cash"] == 2_627.0
    assert env["idle_excess"] == 1_627.0
    assert env["glide_slice"] == pytest.approx(406.75, abs=0.01)
    assert env["monthly_contribution_net_investable_base"] == 1_000.0
    assert env["monthly_contribution_net_investable"] == pytest.approx(1_406.75, abs=0.01)


def test_glide_fraction_zero_reproduces_contribution_only():
    env = _envelope(excess_cash_glide_fraction=0.0)
    assert env["glide_slice"] == 0.0
    # Back-compat: with no glide, net-investable == contribution-only base.
    assert env["monthly_contribution_net_investable"] == 1_000.0
    assert env["monthly_contribution_net_investable"] == \
        env["monthly_contribution_net_investable_base"]


def test_glide_reserve_shortfall_reduces_base_before_glide():
    # Cash below the reserve target → contribution restores the floor first, and
    # there is no deployable excess so glide adds nothing.
    env = _envelope(cash_on_hand=300.0)  # reserve target 524 > cash → shortfall 224
    assert env["cash_reserve_shortfall"] == 224.0
    assert env["monthly_contribution_net_investable_base"] == 776.0  # 1000 - 224
    assert env["deployable_cash"] == 0.0
    assert env["idle_excess"] == 0.0
    assert env["glide_slice"] == 0.0
    assert env["monthly_contribution_net_investable"] == 776.0


def test_glide_double_count_guard_subtracts_contribution():
    # idle_excess must exclude the contribution already sitting in cash.
    env = _envelope(cash_on_hand=3_151.0, monthly_contribution_gross=1_000.0)
    # deployable_cash 2627 - contribution 1000 = 1627 (NOT 2627).
    assert env["idle_excess"] == 1_627.0


def test_glide_fraction_clamped_to_unit_interval():
    assert _envelope(excess_cash_glide_fraction=5.0)["excess_cash_glide_fraction"] == 1.0
    assert _envelope(excess_cash_glide_fraction=-1.0)["excess_cash_glide_fraction"] == 0.0


# --- weekly pacing math ---


def test_weekly_tranche_divides_cycle_over_weeks():
    p = compute_weekly_pacing(
        cycle_net_investable=1_406.75, deployed_before_today=0.0,
        deployed_this_week_before_today=0.0, weeks_remaining_in_cycle=4,
        weekday_days_remaining=5, deploy_cadence="weekly", monthly_history_status="ok",
    )
    assert p["weekly_tranche"] == pytest.approx(351.69, abs=0.01)  # 1406.75/4
    assert p["weekly_remaining"] == pytest.approx(351.69, abs=0.01)
    assert p["budget_today"] == p["weekly_remaining"]


def test_weekly_remaining_decrements_as_week_deploys():
    p = compute_weekly_pacing(
        cycle_net_investable=1_400.0, deployed_before_today=100.0,
        deployed_this_week_before_today=100.0, weeks_remaining_in_cycle=4,
        weekday_days_remaining=5, deploy_cadence="weekly", monthly_history_status="ok",
    )
    # cycle_remaining = 1400 - 100 = 1300; tranche = 325; remaining = 325 - 100 = 225
    assert p["cycle_remaining"] == 1_300.0
    assert p["weekly_tranche"] == 325.0
    assert p["weekly_remaining"] == 225.0


def test_monthly_cadence_no_weekly_subcap():
    p = compute_weekly_pacing(
        cycle_net_investable=1_400.0, deployed_before_today=200.0,
        deployed_this_week_before_today=0.0, weeks_remaining_in_cycle=4,
        weekday_days_remaining=5, deploy_cadence="monthly", monthly_history_status="ok",
    )
    # Whole cycle remaining available any day.
    assert p["budget_today"] == 1_200.0
    assert p["weekly_tranche"] == 1_200.0


def test_daily_cadence_divides_over_weekdays():
    p = compute_weekly_pacing(
        cycle_net_investable=1_400.0, deployed_before_today=0.0,
        deployed_this_week_before_today=0.0, weeks_remaining_in_cycle=4,
        weekday_days_remaining=5, deploy_cadence="daily", monthly_history_status="ok",
    )
    # weekly_remaining = 350; daily = 350 / 5 = 70
    assert p["weekly_remaining"] == 350.0
    assert p["daily_budget"] == 70.0
    assert p["budget_today"] == 70.0


def test_pacing_disabled_when_history_unavailable():
    p = compute_weekly_pacing(
        cycle_net_investable=1_400.0, deployed_before_today=None,
        deployed_this_week_before_today=0.0, weeks_remaining_in_cycle=4,
        weekday_days_remaining=5, deploy_cadence="weekly", monthly_history_status="unavailable",
    )
    assert p["budget_today"] is None   # allocator falls back to full net_investable
    assert p["cycle_remaining"] is None
    assert "unavailable" in (p["note"] or "")


def test_iso_weeks_remaining_bounds():
    # A full calendar month spans 5-6 ISO weeks; always >= 1, never divides by zero.
    assert iso_weeks_remaining_in_cycle(date(2026, 7, 1), date(2026, 7, 31)) >= 4
    assert iso_weeks_remaining_in_cycle(date(2026, 7, 31), date(2026, 7, 31)) == 1
    # cycle_end in the past → guarded to 1.
    assert iso_weeks_remaining_in_cycle(date(2026, 7, 15), date(2026, 7, 1)) == 1


def test_weekday_days_remaining_in_week():
    assert weekday_days_remaining_in_week(date(2026, 7, 6)) == 5    # Monday → 5
    assert weekday_days_remaining_in_week(date(2026, 7, 10)) == 1   # Friday → 1
    assert weekday_days_remaining_in_week(date(2026, 7, 11)) == 1   # Saturday → guarded to 1


# --- deferral-status split ---


def _decision(sym, priority=0.9):
    return {"symbol": sym, "decision": "BUY", "priority": priority,
            "inputs_used": {"conviction_band": "high_conviction"}}


def test_deferral_weekly_pacing_when_cycle_has_room():
    bands = capital_config(None)
    # Weekly budget tiny (10), but cycle has plenty (1000) → week-paced deferral.
    rows = allocate_within_envelope(
        monthly_capital_remaining_before_today=10.0,
        net_investable=1_000.0, portfolio_value=10_000.0,
        ranked_decisions=[_decision("A"), _decision("B")],
        bands=bands, cycle_remaining=1_000.0,
    )
    # A funded up to the $10 weekly cap; B deferred by WEEKLY pacing (cycle has room).
    assert rows[0]["suggested_amount"] > 0
    assert rows[1]["status"] == STATUS_DEFERRED_BY_WEEKLY_PACING


def test_deferral_monthly_budget_when_cycle_exhausted():
    bands = capital_config(None)
    # Weekly budget == cycle remaining == 10 → once spent, cycle is exhausted.
    rows = allocate_within_envelope(
        monthly_capital_remaining_before_today=10.0,
        net_investable=1_000.0, portfolio_value=10_000.0,
        ranked_decisions=[_decision("A"), _decision("B")],
        bands=bands, cycle_remaining=10.0,
    )
    assert rows[1]["status"] == STATUS_DEFERRED_BY_MONTHLY_BUDGET


# --- end-to-end run with glide config ---


def test_run_plan_emits_glide_and_weekly_pacing(tmp_path):
    repo = _setup_repo(
        tmp_path,
        cfg_overrides={"excess_cash_glide_fraction": 0.25, "deploy_cadence": "weekly",
                       "cash_available": 3_000.0},
    )
    # Put a live portfolio_context so PV/cash resolve to a meaningful basis.
    dp = repo / "outputs" / "latest" / "decision_plan.json"
    payload = json.loads(dp.read_text("utf-8"))
    payload["portfolio_context"] = {"total_portfolio_value": 10_480.0, "cash": 3_151.0}
    dp.write_text(json.dumps(payload), encoding="utf-8")

    plan = run_cash_deployment_plan(repo, base_dir=repo / "outputs", as_of_date=date(2026, 7, 7))
    env = plan["monthly_capital_envelope"]
    assert env["status"] == "ok"
    assert env["glide_slice"] > 0                      # glide is active
    assert env["monthly_contribution_net_investable"] > env[
        "monthly_contribution_net_investable_base"]   # net includes glide
    pacing = env["weekly_pacing"]
    assert pacing["deploy_cadence"] == "weekly"
    assert pacing["weekly_tranche"] is not None
    assert plan["observe_only"] is True


def test_run_plan_backcompat_glide_zero_monthly(tmp_path):
    repo = _setup_repo(
        tmp_path,
        cfg_overrides={"excess_cash_glide_fraction": 0.0, "deploy_cadence": "monthly",
                       "cash_available": 3_000.0},
    )
    dp = repo / "outputs" / "latest" / "decision_plan.json"
    payload = json.loads(dp.read_text("utf-8"))
    payload["portfolio_context"] = {"total_portfolio_value": 10_480.0, "cash": 3_151.0}
    dp.write_text(json.dumps(payload), encoding="utf-8")

    plan = run_cash_deployment_plan(repo, base_dir=repo / "outputs", as_of_date=date(2026, 7, 7))
    env = plan["monthly_capital_envelope"]
    # Exact legacy behavior: net-investable == contribution-only base, no glide.
    assert env["glide_slice"] == 0.0
    assert env["monthly_contribution_net_investable"] == \
        env["monthly_contribution_net_investable_base"]
    assert env["weekly_pacing"]["deploy_cadence"] == "monthly"


def test_run_plan_does_not_mutate_decision_plan(tmp_path):
    repo = _setup_repo(tmp_path)
    dp = repo / "outputs" / "latest" / "decision_plan.json"
    before = dp.read_text("utf-8")
    run_cash_deployment_plan(repo, base_dir=repo / "outputs", as_of_date=date(2026, 7, 7))
    assert dp.read_text("utf-8") == before  # decision source of truth untouched
