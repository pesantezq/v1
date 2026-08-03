# tests/test_scanner_canary.py
"""Deterministic acceptance canary for the scanner-recovery weekly run.

The operator must be able to judge the next weekly run without log archaeology.
This reads the already-published scanner-quality contract and renders one verdict
per dimension — it computes no new financial values and re-derives nothing.

Honesty rule under test: an unavailable input renders `n/a`, never an inferred or
fabricated value. In particular the PREVIOUS candidate count is only shown when an
authoritative prior artifact exists.
"""
from __future__ import annotations

import json

from portfolio_automation import scanner_canary as SC

NOW = "2026-08-10T09:00:00+00:00"


def _summary(**over):
    scanner = {
        "watchlist_source": "fmp",
        "symbol_count": 55,
        "constituent_resolution": {
            "source": "free_scrape", "count": 503, "freshness": "fresh",
            "age_days": 0.0, "degraded": False,
            "fetched_at": "2026-08-10T08:59:00+00:00", "plausibility_floor": 400,
        },
        "screening_sufficiency": {
            "eligible_symbols": 503, "fundamentals_requested": 503,
            "fundamentals_resolved": 498, "screening_coverage": 0.9901,
            "unscreened_count": 5, "status": "healthy", "sufficient": True,
            "minimum_threshold": 0.5, "healthy_threshold": 0.9,
        },
        "universe_sufficiency": {
            "candidate_count": 55, "trust_floor": 5, "sufficient": True, "reasons": [],
        },
        "ranking_quality": {
            "candidate_count": 55, "distinct_score_count": 55,
            "largest_tie_fraction": 0.036, "alphabetical_tie_tail_count": 0,
            "degenerate_ranking": False,
        },
        "factor_liveness": {
            "status": "live", "inert_components": [], "suppresses_sleeve": False,
            "reasons": [],
        },
        "safe_mode": False,
        "safe_mode_reasons": [],
    }
    scanner.update(over.pop("scanner", {}))
    base = {"timestamp": NOW, "run_mode": "weekly", "scanner": scanner}
    base.update(over)
    return base


def _write(tmp_path, summary):
    d = tmp_path / "outputs" / "latest"
    d.mkdir(parents=True, exist_ok=True)
    (d / "scraped_intel_run_summary.json").write_text(json.dumps(summary))
    return tmp_path


# --------------------------------------------------------------------------
# Happy path — the target post-recovery state
# --------------------------------------------------------------------------

def test_full_recovery_reports_pass_on_every_dimension(tmp_path):
    c = SC.build_scanner_canary(_write(tmp_path, _summary()), now=NOW)
    assert c["overall"] == "PASS"
    assert c["constituent"]["plausibility"] == "PASS"
    assert c["constituent"]["freshness"] == "FRESH"
    assert c["screening"]["verdict"] == "PASS"
    assert c["watchlist"]["universe_sufficiency"] == "PASS"
    assert c["watchlist"]["small_dataset"] == "CLEARED"
    assert c["ranking"]["degeneracy"] == "PASS"
    assert c["downstream"]["speculative_sleeve_suppressed"] is False


def test_render_is_human_readable_and_names_every_dimension(tmp_path):
    text = SC.render_canary_text(SC.build_scanner_canary(_write(tmp_path, _summary()), now=NOW))
    for heading in ("SCANNER RECOVERY CANARY", "Constituent resolution",
                    "Screening", "Watchlist", "Ranking quality", "Downstream"):
        assert heading in text
    assert "503" in text and "55" in text


# --------------------------------------------------------------------------
# Each dimension can fail independently
# --------------------------------------------------------------------------

def test_expired_constituent_cache_fails(tmp_path):
    s = _summary(scanner={"constituent_resolution": {
        "source": "cache", "count": 503, "freshness": "expired",
        "age_days": 91.0, "degraded": True, "plausibility_floor": 400}})
    c = SC.build_scanner_canary(_write(tmp_path, s), now=NOW)
    assert c["constituent"]["freshness"] == "EXPIRED"
    assert c["overall"] == "FAIL"


def test_stale_cache_warns_without_failing(tmp_path):
    s = _summary(scanner={"constituent_resolution": {
        "source": "cache", "count": 503, "freshness": "stale",
        "age_days": 12.4, "degraded": True, "plausibility_floor": 400}})
    c = SC.build_scanner_canary(_write(tmp_path, s), now=NOW)
    assert c["constituent"]["freshness"] == "STALE"
    assert c["overall"] == "WARN"


def test_insufficient_screening_fails(tmp_path):
    s = _summary(scanner={"screening_sufficiency": {
        "eligible_symbols": 503, "fundamentals_requested": 100,
        "fundamentals_resolved": 100, "screening_coverage": 0.1988,
        "unscreened_count": 403, "status": "unsafe", "sufficient": False,
        "minimum_threshold": 0.5, "healthy_threshold": 0.9}})
    c = SC.build_scanner_canary(_write(tmp_path, s), now=NOW)
    assert c["screening"]["verdict"] == "FAIL"
    assert c["overall"] == "FAIL"


def test_small_dataset_still_flagged(tmp_path):
    """The pre-fix live condition: 3 candidates on a healthy FMP session."""
    s = _summary(scanner={
        "symbol_count": 3,
        "universe_sufficiency": {"candidate_count": 3, "trust_floor": 5,
                                 "sufficient": False, "reasons": ["small_dataset"]},
        "safe_mode": True, "safe_mode_reasons": ["small_dataset"]})
    c = SC.build_scanner_canary(_write(tmp_path, s), now=NOW)
    assert c["watchlist"]["universe_sufficiency"] == "FAIL"
    assert c["watchlist"]["small_dataset"] == "PRESENT"
    assert c["downstream"]["speculative_sleeve_suppressed"] is True
    assert c["overall"] == "FAIL"


def test_degenerate_ranking_warns(tmp_path):
    s = _summary(scanner={"ranking_quality": {
        "candidate_count": 100, "distinct_score_count": 25,
        "largest_tie_fraction": 0.76, "alphabetical_tie_tail_count": 76,
        "degenerate_ranking": True}})
    c = SC.build_scanner_canary(_write(tmp_path, s), now=NOW)
    assert c["ranking"]["degeneracy"] == "WARN"
    assert c["overall"] in ("WARN", "FAIL")


def test_suppression_reasons_are_listed_verbatim(tmp_path):
    s = _summary(scanner={"safe_mode": True,
                          "safe_mode_reasons": ["small_dataset",
                                                "insufficient_screening_coverage"]})
    c = SC.build_scanner_canary(_write(tmp_path, s), now=NOW)
    assert c["downstream"]["suppression_reasons"] == [
        "small_dataset", "insufficient_screening_coverage"]


# --------------------------------------------------------------------------
# Honesty: n/a, never inference
# --------------------------------------------------------------------------

def test_previous_count_is_na_without_an_authoritative_prior(tmp_path):
    c = SC.build_scanner_canary(_write(tmp_path, _summary()), now=NOW)
    assert c["watchlist"]["previous_candidate_count"] == "n/a"


def test_previous_count_is_read_from_a_prior_history_artifact(tmp_path):
    root = _write(tmp_path, _summary())
    hist = root / "outputs" / "history" / "2026-08-03"
    hist.mkdir(parents=True, exist_ok=True)
    (hist / "scraped_intel_run_summary.json").write_text(
        json.dumps({"scanner": {"symbol_count": 3}}))
    c = SC.build_scanner_canary(root, now=NOW)
    assert c["watchlist"]["previous_candidate_count"] == 3


def test_missing_dimensions_render_na_not_pass(tmp_path):
    s = _summary(scanner={"constituent_resolution": None,
                          "screening_sufficiency": None, "ranking_quality": None})
    c = SC.build_scanner_canary(_write(tmp_path, s), now=NOW)
    assert c["constituent"]["freshness"] == "n/a"
    assert c["screening"]["verdict"] == "n/a"
    assert c["ranking"]["degeneracy"] == "n/a"
    # Unavailable certification must not read as overall PASS.
    assert c["overall"] != "PASS"


def test_missing_run_summary_is_explicit(tmp_path):
    c = SC.build_scanner_canary(tmp_path, now=NOW)
    assert c["overall"] == "UNKNOWN"
    assert "run_summary_missing" in c["reasons"]


def test_corrupt_run_summary_does_not_raise(tmp_path):
    d = tmp_path / "outputs" / "latest"
    d.mkdir(parents=True, exist_ok=True)
    (d / "scraped_intel_run_summary.json").write_text("{not json")
    c = SC.build_scanner_canary(tmp_path, now=NOW)
    assert c["overall"] == "UNKNOWN"


def test_canary_is_observe_only_and_deterministic(tmp_path):
    root = _write(tmp_path, _summary())
    a = SC.build_scanner_canary(root, now=NOW)
    b = SC.build_scanner_canary(root, now=NOW)
    assert a == b
    assert a["observe_only"] is True
    json.dumps(a)


# --------------------------------------------------------------------------
# Factor/filter liveness reported independently (never a hard FAIL)
# --------------------------------------------------------------------------

_LIVENESS_DEGRADED = {
    "status": "degraded", "inert_components": ["pe", "pe_bubble_guard"],
    "suppresses_sleeve": False, "reasons": ["inert:pe,pe_bubble_guard"],
}


def test_inert_factor_warns_but_never_fails(tmp_path):
    """PE has been inert all along while the sleeve was permitted; making it a
    FAIL would retroactively change production authority semantics."""
    s = _summary(scanner={"factor_liveness": _LIVENESS_DEGRADED})
    c = SC.build_scanner_canary(_write(tmp_path, s), now=NOW)
    assert c["factors"]["status"] == "DEGRADED"
    assert c["factors"]["inert"] == ["pe", "pe_bubble_guard"]
    assert c["factors"]["suppresses_sleeve"] is False
    assert c["overall"] == "WARN"
    assert any(r.startswith("inert_factors:") for r in c["reasons"])


def test_all_factors_live_keeps_overall_pass(tmp_path):
    s = _summary(scanner={"factor_liveness": {
        "status": "live", "inert_components": [], "suppresses_sleeve": False,
        "reasons": []}})
    c = SC.build_scanner_canary(_write(tmp_path, s), now=NOW)
    assert c["factors"]["status"] == "LIVE"
    assert c["overall"] == "PASS"


def test_absent_liveness_is_na_and_not_pass(tmp_path):
    c = SC.build_scanner_canary(
        _write(tmp_path, _summary(scanner={"factor_liveness": None})), now=NOW)
    assert c["factors"]["status"] == "n/a"
    assert c["overall"] != "PASS"


def test_render_includes_the_factor_block(tmp_path):
    s = _summary(scanner={"factor_liveness": _LIVENESS_DEGRADED})
    text = SC.render_canary_text(SC.build_scanner_canary(_write(tmp_path, s), now=NOW))
    assert "Factor/filter liveness" in text
    assert "pe" in text
