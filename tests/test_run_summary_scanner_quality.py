# tests/test_run_summary_scanner_quality.py
"""The run-summary must publish all THREE scanner-quality dimensions.

A healthy upstream API does not imply a trustworthy scanner dataset, and a large
candidate count does not imply a fully screened universe. The three questions are
independent and each gets its own published block:

  1. constituent_resolution  — can I resolve a plausible, CURRENT universe?
  2. screening_sufficiency   — did the screen actually bind on it?
  3. universe_sufficiency    — is the resulting dataset big enough to trust?

Transport, not recompute: the renderer must carry the verdicts main.py already
calculated. It may not re-derive freshness from a file mtime or coverage from a
row count.
"""
from __future__ import annotations

import json

from scraped_intel.run_summary import build_run_summary


def _build(tmp_path, symbols=("NVDA", "LLY", "AMD"), **kw):
    return build_run_summary(
        run_mode="daily", fmp_attempted=True, fmp_succeeded=True,
        fallback_used=False, watchlist_source="fmp",
        symbols_processed=list(symbols), output_dir=str(tmp_path), **kw)


_CONSTITUENTS = {
    "source": "free_scrape", "count": 503, "fetched_at": "2026-08-03T12:00:00+00:00",
    "age_days": 0.0, "freshness": "fresh", "degraded": False, "detail": "live free source",
    "plausibility_floor": 400, "fresh_max_days": 7, "usable_max_days": 30,
}

_SCREENING = {
    "eligible_symbols": 503, "fundamentals_requested": 503, "fundamentals_resolved": 498,
    "fundamentals_missing": 5, "screening_coverage": 0.9901, "unscreened_count": 5,
    "rows_complete": 480, "rows_partial": 18, "rows_missing": 5,
    "status": "healthy", "sufficient": True, "reasons": [],
    "primary_field": "revenueGrowth",
    "screening_fields": ["revenueGrowth", "peRatio", "freeCashFlowYield"],
    "healthy_threshold": 0.9, "minimum_threshold": 0.5,
}


def test_all_three_dimensions_are_published(tmp_path):
    s = _build(tmp_path, constituent_resolution=_CONSTITUENTS,
               screening_sufficiency=_SCREENING)["scanner"]
    assert s["constituent_resolution"]["freshness"] == "fresh"
    assert s["screening_sufficiency"]["screening_coverage"] == 0.9901
    assert s["universe_sufficiency"]["candidate_count"] == 3


def test_verdicts_are_transported_verbatim_not_recomputed(tmp_path):
    """Passing a deliberately odd payload proves the renderer does not recalculate."""
    odd = {**_SCREENING, "screening_coverage": 0.4242, "status": "unsafe",
           "sufficient": False}
    s = _build(tmp_path, screening_sufficiency=odd)["scanner"]
    assert s["screening_sufficiency"]["screening_coverage"] == 0.4242
    assert s["screening_sufficiency"]["sufficient"] is False


def test_missing_dimensions_are_explicit_not_zero(tmp_path):
    """Never silently write 0/empty for an unavailable dimension."""
    s = _build(tmp_path)["scanner"]
    assert s["constituent_resolution"] is None
    assert s["screening_sufficiency"] is None
    # And definitely not a fabricated healthy default:
    assert s["screening_sufficiency"] != {}


def test_universe_sufficiency_is_preserved_unchanged(tmp_path):
    """The dimension shipped in e36fa01c must keep its existing contract."""
    s = _build(tmp_path)["scanner"]
    us = s["universe_sufficiency"]
    for key in ("candidate_count", "trust_floor", "sufficient", "reasons"):
        assert key in us
    assert us["sufficient"] is False           # 3 candidates, floor 5
    assert us["reasons"] == ["small_dataset"]


def test_healthy_fmp_does_not_override_an_insufficient_dataset(tmp_path):
    """fmp_succeeded=True + fallback_used=False + 3 candidates must still fail."""
    summary = _build(tmp_path, constituent_resolution=_CONSTITUENTS,
                     screening_sufficiency=_SCREENING)
    assert summary["degraded_mode"] is False           # honest: nothing fell back
    assert summary["scanner"]["universe_sufficiency"]["sufficient"] is False


def test_stale_constituent_cache_is_visible_in_the_artifact(tmp_path):
    stale = {**_CONSTITUENTS, "source": "cache", "freshness": "stale",
             "age_days": 14.2, "degraded": True}
    s = _build(tmp_path, constituent_resolution=stale)["scanner"]
    assert s["constituent_resolution"]["freshness"] == "stale"
    assert s["constituent_resolution"]["degraded"] is True
    assert s["constituent_resolution"]["age_days"] == 14.2


def test_blocks_are_persisted_to_the_json_artifact(tmp_path):
    _build(tmp_path, constituent_resolution=_CONSTITUENTS, screening_sufficiency=_SCREENING)
    written = json.loads((tmp_path / "scraped_intel_run_summary.json").read_text())
    s = written["scanner"]
    assert s["constituent_resolution"]["count"] == 503
    assert s["screening_sufficiency"]["status"] == "healthy"


def test_existing_scanner_fields_survive(tmp_path):
    s = _build(tmp_path, constituent_resolution=_CONSTITUENTS)["scanner"]
    for key in ("fmp_attempted", "fmp_succeeded", "fallback_used", "watchlist_source",
                "symbols_processed", "symbol_count", "universe_sufficiency"):
        assert key in s, f"{key} disappeared"


def test_deterministic(tmp_path):
    a = _build(tmp_path, constituent_resolution=_CONSTITUENTS,
               screening_sufficiency=_SCREENING, timestamp="2026-08-03T12:00:00")
    b = _build(tmp_path, constituent_resolution=_CONSTITUENTS,
               screening_sufficiency=_SCREENING, timestamp="2026-08-03T12:00:00")
    assert a["scanner"] == b["scanner"]


def test_ranking_quality_is_published_when_supplied(tmp_path):
    rq = {"candidate_count": 55, "distinct_score_count": 55,
          "largest_tie_fraction": 0.036, "alphabetical_tie_tail_count": 0,
          "degenerate_ranking": False, "observe_only": True}
    s = _build(tmp_path, ranking_quality=rq)["scanner"]
    assert s["ranking_quality"]["degenerate_ranking"] is False
    assert s["ranking_quality"]["distinct_score_count"] == 55


def test_ranking_quality_absent_is_none_not_a_healthy_default(tmp_path):
    assert _build(tmp_path)["scanner"]["ranking_quality"] is None


_LIVENESS = {
    "observe_only": True, "status": "degraded",
    "factors": {"pe": {"input_coverage": 0.0, "score_nonzero_count": 0,
                       "score_variance": 0.0, "status": "inert"}},
    "filters": {"pe_bubble_guard": {"evaluable": 0, "rejections": 0, "status": "inert"}},
    "inert_components": ["pe", "pe_bubble_guard"], "suppresses_sleeve": False,
    "reasons": ["inert:pe,pe_bubble_guard"],
}


def test_factor_liveness_is_published_as_its_own_dimension(tmp_path):
    s = _build(tmp_path, factor_liveness=_LIVENESS)["scanner"]
    assert s["factor_liveness"]["status"] == "degraded"
    assert s["factor_liveness"]["factors"]["pe"]["status"] == "inert"


def test_factor_liveness_absent_is_none_not_healthy(tmp_path):
    assert _build(tmp_path)["scanner"]["factor_liveness"] is None


def test_screening_healthy_and_factors_degraded_coexist(tmp_path):
    """The exact live shape: 99.6% primary coverage AND an inert 15-point factor.
    These must remain separately readable — collapsing them would reproduce the
    misreading this dimension exists to prevent."""
    s = _build(tmp_path, screening_sufficiency=_SCREENING,
               factor_liveness=_LIVENESS)["scanner"]
    assert s["screening_sufficiency"]["status"] == "healthy"
    assert s["factor_liveness"]["status"] == "degraded"
    assert s["factor_liveness"]["suppresses_sleeve"] is False


def test_factor_liveness_persists_to_the_artifact(tmp_path):
    import json as _j
    _build(tmp_path, factor_liveness=_LIVENESS)
    w = _j.loads((tmp_path / "scraped_intel_run_summary.json").read_text())
    assert w["scanner"]["factor_liveness"]["inert_components"] == ["pe", "pe_bubble_guard"]


# ---------------------------------------------------------------------------
# Safe-mode could not reach the oversight surface (2026-08-07)
# ---------------------------------------------------------------------------
# main.py computes _scanner_safe_mode / _scanner_safe_mode_reasons and puts them
# on _scanner_meta, but build_run_summary had no such parameters and the call
# site passed neither -- so scraped_intel_run_summary.json carried no safe_mode
# at all. scanner_canary.py then read None and printed
#   "speculative sleeve suppressed: None / suppression reasons: none"
# on a run where the sleeve WAS suppressed for two reasons. A safety control
# that reads as ABSENT rather than ENGAGED is worse than no control: it grants
# an assurance nobody checked.

class TestSafeModeTransport:
    def test_safe_mode_reaches_the_artifact(self, tmp_path):
        s = _build(tmp_path, safe_mode=True,
                   safe_mode_reasons=["small_dataset", "screening_not_certified"])
        sc = s["scanner"]
        assert sc["safe_mode"] is True
        assert sc["safe_mode_reasons"] == ["small_dataset",
                                           "screening_not_certified"]

    def test_engaged_suppression_is_distinguishable_from_unknown(self, tmp_path):
        """False (checked, clear) must not look like None (never reported)."""
        cleared = _build(tmp_path, safe_mode=False, safe_mode_reasons=[])
        assert cleared["scanner"]["safe_mode"] is False
        assert cleared["scanner"]["safe_mode_reasons"] == []

    def test_omitted_stays_none_for_backward_compatibility(self, tmp_path):
        """Existing callers that pass neither must keep working."""
        s = _build(tmp_path)
        assert s["scanner"]["safe_mode"] is None
        assert s["scanner"]["safe_mode_reasons"] == []

    def test_reasons_are_copied_not_aliased(self, tmp_path):
        reasons = ["small_dataset"]
        s = _build(tmp_path, safe_mode=True, safe_mode_reasons=reasons)
        reasons.append("mutated_after_the_fact")
        assert s["scanner"]["safe_mode_reasons"] == ["small_dataset"]

    def test_canary_reports_engaged_not_none(self, tmp_path):
        """The end-to-end point of the fix: the canary must say ENGAGED."""
        from portfolio_automation.scanner_canary import build_scanner_canary
        _build(tmp_path, safe_mode=True, safe_mode_reasons=["small_dataset"])
        summary_path = tmp_path / "scraped_intel_run_summary.json"
        assert summary_path.exists()
        root = tmp_path / "root"
        (root / "outputs" / "latest").mkdir(parents=True)
        (root / "outputs" / "latest" / "scraped_intel_run_summary.json").write_text(
            summary_path.read_text(), encoding="utf-8")
        canary = build_scanner_canary(root)
        down = canary["downstream"]
        assert down["speculative_sleeve_suppressed"] is True
        assert "small_dataset" in down["suppression_reasons"]
