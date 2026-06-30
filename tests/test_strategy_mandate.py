"""Phase 9 — strategy mandates + champion/challenger framing (observe-only).

Each materialized strategy profile gets a structured mandate (objective,
benchmark, hard budgets, holding period, success/failure regime, promotion +
rollback criteria). Champion = production baseline, control = overlays-off,
challengers = the rest. The leaderboard score is NOT CAGR/Sharpe alone.

TDD: written before portfolio_automation/strategy_mandate.py existed.
"""
from __future__ import annotations

import portfolio_automation.strategy_mandate as sm


_PROFILES = ["aggressive_growth", "short_term_tactical", "long_term_compounding",
             "tax_aware", "defensive_capital_preservation", "income_dividend",
             "balanced_core_satellite", "boom_bucket"]

_MANDATE_FIELDS = {"objective", "benchmark", "permitted_inputs", "risk_budget",
                   "turnover_budget", "leverage_limit", "concentration_limit",
                   "holding_period", "success_regime", "failure_regime",
                   "promotion_criteria", "rollback_criteria", "role"}


def test_every_profile_has_a_complete_mandate():
    for pid in _PROFILES:
        m = sm.MANDATES[pid]
        assert _MANDATE_FIELDS.issubset(set(m)), (pid, _MANDATE_FIELDS - set(m))
        assert sm.mandate_complete(m) is True


def test_incomplete_mandate_is_flagged():
    bad = {"objective": "x"}  # missing most fields
    assert sm.mandate_complete(bad) is False
    missing = sm.mandate_missing_fields(bad)
    assert "benchmark" in missing and "rollback_criteria" in missing


def test_champion_control_challenger_roles():
    roles = sm.assign_roles()
    assert roles["champion"] == "production_baseline"
    assert roles["control"] == "overlays_off"
    # challengers are the materialized profiles
    assert set(_PROFILES).issubset(set(roles["challengers"]))


def test_leaderboard_score_is_multi_factor_not_cagr_alone():
    # two entries with identical CAGR but different drawdown/consistency/regime
    strong = {"oos_excess": 0.05, "max_drawdown": 0.10, "consistency": 0.9,
              "regime_stability": 0.9, "turnover": 0.2, "oos_sample": 60}
    weak = {"oos_excess": 0.05, "max_drawdown": 0.40, "consistency": 0.3,
            "regime_stability": 0.2, "turnover": 0.9, "oos_sample": 60}
    assert sm.leaderboard_score(strong) > sm.leaderboard_score(weak)


def test_insufficient_oos_blocks_promotion_eligibility():
    entry = {"oos_excess": 0.2, "max_drawdown": 0.05, "consistency": 0.9,
             "regime_stability": 0.9, "turnover": 0.1, "oos_sample": 5}
    assert sm.promotion_eligible(entry) is False  # n=5 < min OOS
    entry2 = {**entry, "oos_sample": 60}
    assert sm.promotion_eligible(entry2) is True


def test_build_strategy_mandates_flags_unmandated(tmp_path):
    res = sm.build_strategy_mandates(tmp_path, now="2026-06-30T09:00:00+00:00",
                                     profile_ids=_PROFILES + ["mystery_profile"])
    assert res["observe_only"] is True
    assert res["coverage_complete"] is False  # mystery_profile has no mandate
    assert "mystery_profile" in res["unmandated"]
