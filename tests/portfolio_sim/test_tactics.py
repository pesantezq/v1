"""Tests for the Tactic interface + materializers."""
from __future__ import annotations

import json

from portfolio_automation.portfolio_sim.tactics import (
    Tactic,
    benchmark_tactics,
    tactics_from_strategy_profiles,
)


def _write_config(root):
    cfg = {
        "portfolio": {"holdings": [
            {"symbol": "QQQ", "shares": 6, "asset_class": "us_equity", "is_leveraged": False},
            {"symbol": "GLD", "shares": 4, "asset_class": "commodity", "is_leveraged": False},
            {"symbol": "QLD", "shares": 8, "asset_class": "us_equity_leveraged", "is_leveraged": True},
        ]},
        "growth_mode": {"concentration_cap": 0.60, "leverage_cap": 0.25},
        "portfolio_sim": {"universe": {"proxy_etfs": ["BND", "SCHD", "USMV"]}},
    }
    (root / "config.json").write_text(json.dumps(cfg))


def test_static_tactic_target_weights_asof_is_constant():
    t = Tactic("x", "X", "benchmark", {"SPY": 1.0})
    assert t.target_weights_asof("2026-01-01") == {"SPY": 1.0}
    assert t.target_weights_asof("2099-12-31") == {"SPY": 1.0}


def test_benchmarks():
    bs = {t.tactic_id: t for t in benchmark_tactics()}
    assert bs["benchmark_spy"].target_weights == {"SPY": 1.0}
    assert bs["benchmark_qqq"].target_weights == {"QQQ": 1.0}


def test_eight_profiles_materialize_normalized_and_capped(tmp_path):
    _write_config(tmp_path)
    tactics = tactics_from_strategy_profiles(tmp_path)
    assert len(tactics) == 8
    for t in tactics:
        s = sum(t.target_weights.values())
        assert abs(s - 1.0) < 1e-6, f"{t.tactic_id} not normalized ({s})"
        assert max(t.target_weights.values()) <= 0.60 + 1e-9
        lev = t.target_weights.get("QLD", 0.0)
        assert lev <= 0.25 + 1e-9, f"{t.tactic_id} leverage cap breached"


def test_defensive_de_risks_vs_aggressive(tmp_path):
    _write_config(tmp_path)
    by = {t.tactic_id: t for t in tactics_from_strategy_profiles(tmp_path)}
    agg_qld = by["profile_aggressive_growth"].target_weights.get("QLD", 0.0)
    def_qld = by["profile_defensive_capital_preservation"].target_weights.get("QLD", 0.0)
    assert def_qld < agg_qld          # defensive holds less leverage
    assert def_qld == 0.0             # defensive zeroes leverage


def test_short_term_tactical_flagged_approximate(tmp_path):
    _write_config(tmp_path)
    by = {t.tactic_id: t for t in tactics_from_strategy_profiles(tmp_path)}
    assert by["profile_short_term_tactical"].approximate is True


def test_income_tilts_to_dividend_and_bonds(tmp_path):
    _write_config(tmp_path)
    by = {t.tactic_id: t for t in tactics_from_strategy_profiles(tmp_path)}
    inc = by["profile_income_dividend"].target_weights
    assert inc.get("SCHD", 0.0) > 0   # dividend floor applied
    assert inc.get("BND", 0.0) > 0    # bond floor applied
