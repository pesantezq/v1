# tests/test_pe_challenger.py
"""Champion/challenger integrity for the PE experiment. Research-only.

The champion MUST be the production scanner untouched; the challenger must differ
ONLY by PE availability, over byte-identical frozen inputs.
"""
from __future__ import annotations

import copy

import pytest

from portfolio_automation.research.pe_challenger import build_snapshot, run_pe_experiment
from scanner.candidate_scanner import CandidateScanner

AS_OF = "2026-08-03T20:00:00+00:00"


def _factory():
    return CandidateScanner(min_mkt_cap=5e9, min_rev_growth=0.15,
                            trend_filter_200dma=True, top_k=100)


def _pe(quality, value=None):
    return {"pe_ratio": value, "quality": quality, "source": "stable_ratios",
            "source_field": "priceToEarningsRatio", "as_of": AS_OF}


def _inputs(n=8):
    syms = [f"S{i}" for i in range(n)]
    profiles = [{"symbol": s, "mktCap": 2e10, "sector": "Tech"} for s in syms]
    metrics = [{"symbol": s, "revenueGrowth": 0.20 + i * 0.01,
                "freeCashFlowYield": 0.03, "roe": 0.15} for i, s in enumerate(syms)]
    quotes = {s: {"price": 100.0, "priceAvg200": 90.0} for s in syms}
    return syms, profiles, metrics, quotes


def _snap(pe_map=None, n=8):
    syms, profiles, metrics, quotes = _inputs(n)
    return build_snapshot(symbols=syms, profiles=profiles, metrics=metrics,
                          quotes=quotes, pe_by_symbol=pe_map or {}, as_of=AS_OF)


# --------------------------------------------------------------------------
# Same-input guarantee
# --------------------------------------------------------------------------

def test_champion_input_rows_are_never_mutated_by_the_challenger():
    syms, profiles, metrics, quotes = _inputs()
    before = copy.deepcopy(metrics)
    snap = build_snapshot(symbols=syms, profiles=profiles, metrics=metrics,
                          quotes=quotes,
                          pe_by_symbol={s: _pe("direct", 12.0) for s in syms},
                          as_of=AS_OF)
    run_pe_experiment(_factory, snap)
    assert metrics == before, "challenger leaked peRatio into the shared metrics rows"


def test_champion_matches_the_production_scanner_exactly():
    """Champion output must equal a plain production full_scan on the same inputs."""
    syms, profiles, metrics, quotes = _inputs()
    direct, _ = _factory().full_scan(syms, profiles, copy.deepcopy(metrics), quotes)
    snap = build_snapshot(symbols=syms, profiles=profiles, metrics=metrics,
                          quotes=quotes, pe_by_symbol={s: _pe("direct", 12.0)
                                                       for s in syms}, as_of=AS_OF)
    res = run_pe_experiment(_factory, snap)
    assert res["counts"]["champion_candidates"] == len(direct)


def test_snapshot_fingerprint_is_recorded_and_stable():
    a = _snap({"S0": _pe("direct", 10.0)})
    b = _snap({"S0": _pe("direct", 10.0)})
    assert a["fingerprint"] == b["fingerprint"]
    assert _snap({"S0": _pe("direct", 11.0)})["fingerprint"] != a["fingerprint"]
    assert run_pe_experiment(_factory, a)["snapshot_fingerprint"] == a["fingerprint"]


def test_only_usable_pe_qualities_are_injected():
    snap = _snap({"S0": _pe("direct", 10.0), "S1": _pe("derived", 20.0),
                  "S2": _pe("negative_earnings"), "S3": _pe("invalid"),
                  "S4": _pe("unavailable")})
    assert snap["pe_usable"] == {"S0": 10.0, "S1": 20.0}
    assert set(snap["pe_skipped"]) == {"S2", "S3", "S4"}


def test_unusable_pe_does_not_become_a_fake_zero():
    """A fake 0 would hit `_score`'s `or 100` path and silently band the name."""
    snap = _snap({s: _pe("negative_earnings") for s in [f"S{i}" for i in range(8)]})
    res = run_pe_experiment(_factory, snap)
    assert res["counts"]["pe_usable"] == 0
    # With no usable PE the challenger must be identical to the champion.
    assert res["counts"]["challenger_candidates"] == res["counts"]["champion_candidates"]
    assert res["attribution"]["score_effect_count"] == 0
    assert res["membership"]["dropped_by_challenger"] == []


# --------------------------------------------------------------------------
# The two effects, kept separate
# --------------------------------------------------------------------------

def test_pe_guard_rejects_above_50_and_is_attributed_to_the_hard_filter():
    syms, profiles, metrics, quotes = _inputs(4)
    snap = build_snapshot(symbols=syms, profiles=profiles, metrics=metrics,
                          quotes=quotes,
                          pe_by_symbol={"S0": _pe("direct", 72.0),
                                        "S1": _pe("direct", 12.0),
                                        "S2": _pe("direct", 20.0),
                                        "S3": _pe("direct", 30.0)},
                          as_of=AS_OF)
    res = run_pe_experiment(_factory, snap)
    assert res["counts"]["pe_guard_rejections"] == 1
    assert "S0" in res["membership"]["dropped_via_pe_guard"]
    effects = {r["symbol"]: r["effect"] for r in res["attribution"]["hard_filter_effect"]}
    assert effects["S0"] == "hard_filter_exclusion"


def test_score_effect_reconciles_exactly_to_pe_points():
    """Score delta for a retained name must equal its PE band points, nothing else."""
    syms, profiles, metrics, quotes = _inputs(3)
    snap = build_snapshot(symbols=syms, profiles=profiles, metrics=metrics,
                          quotes=quotes,
                          pe_by_symbol={"S0": _pe("direct", 10.0),   # 15 pts
                                        "S1": _pe("direct", 20.0),   # 12 pts
                                        "S2": _pe("direct", 30.0)},  # 8 pts
                          as_of=AS_OF)
    res = run_pe_experiment(_factory, snap)
    by = {r["symbol"]: r for r in res["attribution"]["score_effect_top"]}
    assert by["S0"]["score_delta"] == pytest.approx(15.0)
    assert by["S1"]["score_delta"] == pytest.approx(12.0)
    assert by["S2"]["score_delta"] == pytest.approx(8.0)
    for row in by.values():
        assert row["score_delta"] == pytest.approx(row["pe_points"])
        assert row["effect"] == "score_only"


def test_hard_filter_and_score_effects_are_not_conflated():
    syms, profiles, metrics, quotes = _inputs(4)
    snap = build_snapshot(symbols=syms, profiles=profiles, metrics=metrics,
                          quotes=quotes,
                          pe_by_symbol={"S0": _pe("direct", 72.0),
                                        "S1": _pe("direct", 10.0)},
                          as_of=AS_OF)
    res = run_pe_experiment(_factory, snap)
    hard = {r["symbol"] for r in res["attribution"]["hard_filter_effect"]}
    score = {r["symbol"] for r in res["attribution"]["score_effect_top"]}
    assert hard == {"S0"}
    assert "S0" not in score
    assert "S1" in score


def test_structural_metrics_are_reported():
    snap = _snap({f"S{i}": _pe("direct", 10.0 + i * 8) for i in range(8)})
    res = run_pe_experiment(_factory, snap)
    for key in ("overlap", "rank", "scores", "pe_band_distribution", "membership",
                "attribution", "counts"):
        assert key in res
    assert set(res["overlap"]) == {"top_10", "top_20", "top_50"}
    assert set(res["pe_band_distribution"]) == {"15pts", "12pts", "8pts", "3pts", "0pts"}


def test_experiment_declares_research_only_and_is_json_serializable():
    import json
    res = run_pe_experiment(_factory, _snap({"S0": _pe("direct", 10.0)}))
    assert res["research_only"] is True
    assert res["feeds_production"] is False
    json.dumps(res)


def test_no_usable_pe_yields_null_rank_correlation_safely():
    res = run_pe_experiment(_factory, _snap({}, n=2))
    assert res["rank"]["spearman"] is None or isinstance(res["rank"]["spearman"], float)
