"""
Phase 3 tests — scorecard, calibration, attribution + the Phase-1 fix
regressions (multi-benchmark cache, bundle-state guard) flagged in review.
Hermetic.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from portfolio_automation.portfolio_sim.prices import PricePanel
from portfolio_automation.weekly_etf_bundles.analysis import build_weekly_analysis, compute_etf_metrics
from portfolio_automation.weekly_etf_bundles.config import load_config
from portfolio_automation.weekly_etf_bundles import evaluation as E
from portfolio_automation.weekly_etf_bundles import calibration as C
from portfolio_automation.weekly_etf_bundles import attribution as A
from portfolio_automation.weekly_etf_bundles.scoring import _bundle_state


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


def _cfg(tmp_path, body):
    p = tmp_path / "wb.yaml"
    p.write_text(body, encoding="utf-8")
    return load_config(p)


# --------------------------------------------------------------------------- #
# rank / correlation primitives
# --------------------------------------------------------------------------- #
def test_spearman_perfect_monotonic():
    assert E.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert E.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)


def test_spearman_handles_nonlinear_monotonic():
    # Spearman = 1 for any monotonic relation (unlike Pearson)
    assert E.spearman([1, 2, 3, 4], [1, 4, 9, 16]) == pytest.approx(1.0)


def _matured(symbol, bundle, mdd, score, excess, rank, *, direction=None, rel=None,
             regime="risk_on", components=None):
    return {
        "status": "matured", "horizon": "4w", "market_data_date": mdd,
        "bundle_id": bundle, "symbol": symbol, "watch_score": score,
        "label": "leading" if score >= 80 else "mixed",
        "rank_in_bundle": rank, "rank_global": rank, "strategy_variant": "v1",
        "market_regime": regime, "volatility_regime": "normal",
        "excess_return": excess, "forward_return": excess,
        "directional_hit": (excess > 0) if direction is None else direction,
        "relative_hit": (excess > 0) if rel is None else rel,
        "strong_hit": excess > 0.02, "neutral": abs(excess) <= 0.02, "miss": excess < -0.02,
        "max_adverse_excursion": min(0.0, excess), "max_drawdown_in_window": min(0.0, excess),
        "score_components": components or {"momentum_4w": score, "relative_strength_12w": score},
    }


# --------------------------------------------------------------------------- #
# precision@k / IC / spread / scorecard
# --------------------------------------------------------------------------- #
def test_precision_at_k_top_ranked_all_hit():
    rows = [
        _matured("A", "b", "2026-01-02", 90, 0.05, 1, rel=True),
        _matured("B", "b", "2026-01-02", 70, 0.01, 2, rel=True),
        _matured("C", "b", "2026-01-02", 40, -0.03, 3, rel=False),
    ]
    assert E.precision_at_k(rows, 1) == 1.0        # top-1 is a relative hit
    assert E.precision_at_k(rows, 3) == pytest.approx(2 / 3, abs=1e-3)


def test_precision_at_k_skips_thin_periods():
    rows = [_matured("A", "b", "2026-01-02", 90, 0.05, 1, rel=True)]
    assert E.precision_at_k(rows, 5) is None       # fewer than k in the only period


def test_top_bottom_spread():
    rows = [
        _matured("A", "b", "2026-01-02", 90, 0.06, 1),   # top bucket
        _matured("B", "b", "2026-01-02", 10, -0.04, 2),  # bottom bucket
    ]
    assert E.top_bottom_spread(rows) == pytest.approx(0.10, abs=1e-9)


def test_sample_status_labels():
    assert E.sample_status(0, 0) == E.STATUS_INSUFFICIENT
    assert E.sample_status(30, 14) == E.STATUS_PROVISIONAL
    assert E.sample_status(150, 30) == E.STATUS_SUFFICIENT


def test_scorecard_gates_on_sample():
    rows = [_matured("A", "ai", "2026-01-02", 90, 0.05, 1)]
    sc = E.build_scorecard(rows)
    assert sc["sample_status"] == E.STATUS_INSUFFICIENT
    assert sc["matured_prediction_count"] == 1
    assert "by_bundle" in sc and "ai" in sc["by_bundle"]
    assert "by_market_regime" in sc


# --------------------------------------------------------------------------- #
# calibration
# --------------------------------------------------------------------------- #
def test_wilson_interval_bounds():
    lo, hi = C.wilson_interval(8, 10)
    assert 0.0 <= lo < hi <= 1.0


def test_calibration_non_monotonic_detected():
    # bottom bucket outperforms the top bucket → inversion
    rows = []
    for i in range(30):
        rows.append(_matured(f"T{i}", "b", "2026-01-02", 90, -0.05, 1, rel=False))   # top scores miss
        rows.append(_matured(f"B{i}", "b", "2026-01-02", 10, 0.05, 2, rel=True))     # low scores hit
    rep = C.build_calibration(rows, min_bucket_n=10)
    assert rep["calibration_status"] == C.NON_MONOTONIC
    assert rep["higher_buckets_underperform_warning"] is True


def test_calibration_insufficient_sample():
    rows = [_matured("A", "b", "2026-01-02", 90, 0.05, 1)]
    rep = C.build_calibration(rows, min_bucket_n=20)
    assert rep["calibration_status"] == C.INSUFFICIENT_SAMPLE


# --------------------------------------------------------------------------- #
# attribution
# --------------------------------------------------------------------------- #
def test_component_attribution_flags_counterproductive():
    # momentum_4w perfectly anti-correlated with excess → counterproductive
    rows = []
    for i in range(10):
        comp = {"momentum_4w": 100 - i * 10, "relative_strength_12w": i * 10}
        rows.append(_matured(f"S{i}", "b", "2026-01-02", 50, i * 0.01, i + 1, components=comp))
    attr = A.component_attribution(rows)
    assert attr["momentum_4w"]["contribution"] == "counterproductive"
    assert attr["relative_strength_12w"]["contribution"] == "predictive"


def test_attribution_generates_hypotheses_no_autoapply():
    rows = []
    for i in range(10):
        comp = {"momentum_4w": 100 - i * 10}
        rows.append(_matured(f"S{i}", "b", "2026-01-02", 50, i * 0.01, i + 1, components=comp))
    sc = E.build_scorecard(rows)
    attr = A.build_attribution(sc, rows)
    assert attr["observe_only"] is True
    assert any(h["target_parameter"] == "weights.momentum_4w" for h in attr["strat_lab_hypotheses"])
    assert all(h["auto_apply"] is False for h in attr["strat_lab_hypotheses"])


# --------------------------------------------------------------------------- #
# Phase-1 fix regressions (from adversarial review)
# --------------------------------------------------------------------------- #
def test_multi_benchmark_shared_symbol_not_cached_wrong(tmp_path):
    # QQQ is benchmark of bundle A and a MEMBER of bundle B (benchmark SPY).
    # Its score in B must be benchmark-B (SPY)-relative, not QQQ-vs-QQQ (=50).
    body = """
schema_version: 1
defaults:
  benchmark: SPY
bundles:
  - id: a
    name: A
    benchmark: QQQ
    display_order: 10
    members:
      - symbol: SMH
  - id: b
    name: B
    benchmark: SPY
    display_order: 20
    members:
      - symbol: QQQ
"""
    cfg = _cfg(tmp_path, body)
    dates = _weekdays("2025-01-01", 260)
    # QQQ strongly outperforms SPY; if cached vs itself, excess_12w would be 0.
    qqq = [100.0 + 0.6 * i for i in range(260)]
    spy = [100.0] * 260
    smh = [100.0 + 0.3 * i for i in range(260)]
    panel = _panel({"QQQ": qqq, "SPY": spy, "SMH": smh}, dates)
    payload = build_weekly_analysis(cfg, panel, as_of=dates[-1])
    qqq_in_b = next(m for bd in payload["bundles"] if bd["bundle_id"] == "b"
                    for m in bd["members"] if m["symbol"] == "QQQ")
    assert qqq_in_b["metrics"]["excess_return_12w"] is not None
    assert qqq_in_b["metrics"]["excess_return_12w"] > 0.0     # QQQ beats SPY, not 0 vs itself


def test_bundle_state_single_member_never_broad():
    # One-member bundle that is strong must NOT be "Broad leadership".
    state = _bundle_state(
        coverage=1.0, minimum_coverage=0.80,
        pct_above_50=1.0, pct_pos_mom_4w=1.0,
        concentration=None, weekly_score_change=None,
    )
    assert state != "Broad leadership"


def test_bundle_state_broad_requires_low_concentration():
    broad = _bundle_state(coverage=1.0, minimum_coverage=0.80, pct_above_50=0.9,
                          pct_pos_mom_4w=0.9, concentration=0.05, weekly_score_change=None)
    narrow = _bundle_state(coverage=1.0, minimum_coverage=0.80, pct_above_50=0.9,
                           pct_pos_mom_4w=0.9, concentration=0.40, weekly_score_change=None)
    assert broad == "Broad leadership"
    assert narrow == "Narrow leadership"
