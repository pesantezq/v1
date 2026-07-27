"""
Phase 4 tests — Strat Lab family, walk-forward comparison, promotion gates,
champion/challenger registry, and the no-auto-promotion / human-gated invariants.
Hermetic.
"""
from __future__ import annotations

from datetime import date, timedelta

from portfolio_automation.portfolio_sim.prices import PricePanel
from portfolio_automation.weekly_etf_bundles.config import load_config
from portfolio_automation.weekly_etf_bundles import strat_lab_adapter as SL


def _weekdays(start_iso: str, n: int) -> list[str]:
    d = date.fromisoformat(start_iso)
    out: list[str] = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _panel(series_by_symbol, dates) -> PricePanel:
    closes = {s: {dates[i]: float(p) for i, p in enumerate(ps)} for s, ps in series_by_symbol.items()}
    volumes = {s: {d: 1.0 for d in dates} for s in series_by_symbol}
    return PricePanel(closes, volumes, list(dates), [])


def _cfg(tmp_path):
    body = """
schema_version: 1
defaults:
  benchmark: SPY
bundles:
  - id: ai
    name: AI
    benchmark: QQQ
    members:
      - symbol: SMH
      - symbol: IGV
"""
    p = tmp_path / "wb.yaml"
    p.write_text(body, encoding="utf-8")
    return load_config(p)


def test_family_has_four_variants_incl_required():
    ids = {v.variant_id for v in SL.VARIANTS}
    assert ids == {
        "weekly_etf_bundle_v1_baseline",
        "weekly_etf_bundle_v2_momentum_heavy",
        "weekly_etf_bundle_v3_breadth_adjusted",
        "weekly_etf_bundle_v4_regime_conditioned",
    }
    for v in SL.VARIANTS:
        assert abs(sum(v.base_params.weights.values()) - 1.0) < 1e-6   # weights normalized


def test_regime_conditioned_variant_switches_weights():
    v4 = SL.variant_by_id("weekly_etf_bundle_v4_regime_conditioned")
    p_on = v4.params_for("risk_on")
    p_off = v4.params_for("risk_off")
    assert p_on.weights != p_off.weights
    assert abs(sum(p_on.weights.values()) - 1.0) < 1e-4
    assert abs(sum(p_off.weights.values()) - 1.0) < 1e-4


def test_walk_forward_comparison_runs_and_is_isolated(tmp_path):
    cfg = _cfg(tmp_path)
    dates = _weekdays("2025-01-01", 300)
    smh = [50.0 + 0.5 * i for i in range(300)]
    igv = [80.0 + 0.2 * i for i in range(300)]
    qqq = [100.0 + 0.3 * i for i in range(300)]
    spy = [100.0 + 0.1 * i for i in range(300)]
    panel = _panel({"SMH": smh, "IGV": igv, "QQQ": qqq, "SPY": spy}, dates)
    as_of_dates = [dates[i] for i in range(200, 240, 5)]   # several weekly snapshots
    comp = SL.run_strat_lab_comparison(cfg, panel, as_of_dates, generated_at="2026-07-27T00:00:00Z")

    assert comp["observe_only"] is True
    assert comp["feeds_decision_engine"] is False
    assert comp["champion_id"] == SL.CHAMPION_ID
    assert set(comp["variants"]) == {v.variant_id for v in SL.VARIANTS}
    assert len(comp["leaderboard"]) == 4
    # champion carries no gate_result; challengers do
    assert "gate_result" not in comp["variants"][SL.CHAMPION_ID]
    for vid, s in comp["variants"].items():
        if vid != SL.CHAMPION_ID:
            assert s["gate_result"]["auto_promotion"] is False


def test_insufficient_sample_never_ready(tmp_path):
    cfg = _cfg(tmp_path)
    dates = _weekdays("2025-01-01", 300)
    panel = _panel({s: [100.0 + 0.1 * i for i in range(300)] for s in ("SMH", "IGV", "QQQ", "SPY")}, dates)
    as_of_dates = [dates[210]]
    comp = SL.run_strat_lab_comparison(cfg, panel, as_of_dates)
    for vid, s in comp["variants"].items():
        if vid != SL.CHAMPION_ID:
            assert s["gate_result"]["status"] != "ready_for_human_review"  # thin sample


def test_gates_pass_but_still_requires_human_approval():
    champion_sc = {
        "matured_prediction_count": 200, "calendar_weeks_span": 40,
        "benchmark_relative_hit_rate": 0.50, "information_coefficient": 0.03,
        "top_bottom_score_spread": 0.01, "avg_excess_return": 0.001, "avg_max_drawdown": -0.10,
        "by_market_regime": {"risk_on": {"relative_hit_rate": 0.5, "count": 50}},
    }
    challenger_sc = {
        "matured_prediction_count": 200, "calendar_weeks_span": 40,
        "benchmark_relative_hit_rate": 0.58, "information_coefficient": 0.09,
        "top_bottom_score_spread": 0.04, "avg_excess_return": 0.012, "avg_max_drawdown": -0.09,
        "by_market_regime": {"risk_on": {"relative_hit_rate": 0.6, "count": 40},
                             "risk_off": {"relative_hit_rate": 0.55, "count": 30}},
    }
    res = SL.evaluate_promotion_gates(champion_sc, challenger_sc)
    assert res["passes_all_gates"] is True
    assert res["status"] == "ready_for_human_review"
    assert res["requires_human_approval"] is True     # never auto
    assert res["auto_promotion"] is False


def test_promising_but_insufficient_sample_label():
    champion_sc = {"benchmark_relative_hit_rate": 0.50, "information_coefficient": 0.03,
                   "avg_max_drawdown": -0.10}
    challenger_sc = {
        "matured_prediction_count": 30, "calendar_weeks_span": 10,   # too thin
        "benchmark_relative_hit_rate": 0.60, "information_coefficient": 0.10,
        "top_bottom_score_spread": 0.05, "avg_excess_return": 0.02, "avg_max_drawdown": -0.09,
        "by_market_regime": {"a": {"relative_hit_rate": 0.6, "count": 10},
                             "b": {"relative_hit_rate": 0.6, "count": 10}},
    }
    res = SL.evaluate_promotion_gates(champion_sc, challenger_sc)
    assert res["status"] == "promising_but_insufficient_sample"
    assert res["passes_all_gates"] is False


def test_promotion_candidate_carries_four_invariants():
    gate_result = {"status": "ready_for_human_review"}
    cand = SL.build_promotion_candidate("weekly_etf_bundle_v2_momentum_heavy", gate_result, salt="2026-07-27")
    assert cand["target_lane"] == "simulation"
    assert cand["production_mutation"] is False
    assert cand["feeds_decision_engine"] is False
    assert cand["is_human_approved"] is False
    assert cand["approval_status"] == "pending"
    SL.assert_no_auto_approval(cand)   # would raise if any invariant broke


def test_write_strat_lab_artifacts(tmp_path):
    cfg = _cfg(tmp_path)
    dates = _weekdays("2025-01-01", 300)
    panel = _panel({s: [100.0 + 0.2 * i for i in range(300)] for s in ("SMH", "IGV", "QQQ", "SPY")}, dates)
    comp = SL.run_strat_lab_comparison(cfg, panel, [dates[210], dates[215]])
    paths = SL.write_strat_lab_artifacts(comp, root=tmp_path)
    import json
    reg = json.loads((tmp_path / "outputs" / "weekly_etf_bundles" / "challenger_registry.json").read_text())
    assert reg["champion_locked"] is True
    assert reg["observe_only"] is True
    assert (tmp_path / "outputs" / "weekly_etf_bundles" / "strat_lab_comparison.json").exists()
    assert "strat_lab_comparison" in paths
