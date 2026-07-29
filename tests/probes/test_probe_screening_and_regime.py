"""E5 probes -- quality-screen mislabeling, regime concentration, and
hit-rate-vs-expectancy conflation.

Scenario 14 -- a quality-screen bypass is mislabeled as screened.
Scenario 15 -- a regime-concentrated signal is described as generally
               validated.
Scenario 16 -- a strong raw hit rate coexists with negative
               benchmark-relative expectancy.

Two of the three have since been CLOSED and their probes inverted to pin the
fix rather than the gap: F14.1 by B4 (`f7f70f63` + the 2026-07-29 correction
— regime concentration now reaches a validity verdict, and an unreadable
regime artifact fails closed instead of reading as balanced), and the
hit-rate/expectancy conflation in ``retune_suggestions.py`` by `c0fc3c6c`
— which mattered most, being the one path allowed to mutate registry weights.

F15.1 remains genuinely OPEN: no "screened"/"passed_screen" label field
exists anywhere, so provenance still records WHERE a symbol came from and
never WHICH filters ran (Phase E2, unimplemented). Its probe below still
documents the honest gap.
"""
from __future__ import annotations

import inspect

import pytest

from portfolio_automation import retune_suggestions as RS
from portfolio_automation import semantic_liveness as SL

from tests.probes.assertions import assert_no_quality_screen_mislabeling

# ---------------------------------------------------------------------------
# Scenario 14 -- F15.1: quality-screen bypass mislabeled as screened.
# ---------------------------------------------------------------------------


def _mara_like_candidate() -> dict:
    """Shape confirmed real by universe_sanitation.py:142
    (`_load_fmp_top100`), which reads `data/fmp_cache/top100_watchlist.json`
    and only ever checks `watchlist_source != "fallback"` for exclusion --
    never records which filters (if any) ran."""
    return {
        "symbol": "MARA", "score": 0.0, "watchlist_source": "fallback",
        "market_cap": 4.55e9,  # below the operator's $5B floor per F15.1
        "price_vs_200dma_pct": -2.1,  # below its 200-DMA per F15.1
    }


def test_bypassed_candidate_correctly_unlabeled_today():
    """Baseline (honest) state: MARA's real record carries no `label` field
    at all -- the assertion must NOT fire when nothing claims it was
    screened, because nothing claims anything."""
    cand = _mara_like_candidate()
    assert_no_quality_screen_mislabeling(
        watchlist_source=cand["watchlist_source"], screened_filters=None,
        label=cand.get("label"), context="MARA (fallback-sourced, unlabeled)")


def test_hypothetical_mislabel_is_caught():
    """Adversarial construction: if a FUTURE consumer attached a `label:
    "screened"` to this same bypassed record (e.g. a naive 'has a score, so
    it must have been screened' inference -- the natural next mistake once
    someone builds a screening-status display), the probe must catch it."""
    cand = _mara_like_candidate()
    cand["label"] = "screened"  # the hypothetical future bug
    with pytest.raises(AssertionError, match="bypassed real screening"):
        assert_no_quality_screen_mislabeling(
            watchlist_source=cand["watchlist_source"], screened_filters=None,
            label=cand["label"], context="MARA (hypothetically mislabeled)")


def test_a_genuinely_screened_candidate_is_not_flagged():
    """Positive control: a candidate whose provenance is NOT a bypass path,
    with recorded filters, may legitimately be labelled screened."""
    assert_no_quality_screen_mislabeling(
        watchlist_source="fmp_top100", screened_filters=["market_cap_floor", "200dma"],
        label="screened", context="AAPL (genuinely screened)")


def test_no_screened_label_field_exists_anywhere_today_STILL_OPEN():
    """STILL FULLY OPEN (F15.1): confirmed by source inspection that no
    producer anywhere in the repo ever writes a 'screened'/'passed_screen'
    label, and `config/schema.py`'s watchlist normalization is
    string/case-only -- there is no fundamental-screen enforcement or
    provenance-of-filters-run concept in production. This test pins that
    absence; it should keep passing until a screening-provenance field is
    actually shipped, at which point it should be rewritten to assert real
    mislabeling is caught end-to-end against that field."""
    import portfolio_automation.universe_sanitation as US
    from config import schema as CFG_SCHEMA

    us_src = inspect.getsource(US)
    schema_src = inspect.getsource(CFG_SCHEMA)
    for src, modname in ((us_src, "universe_sanitation"), (schema_src, "config.schema")):
        assert "screened_filters" not in src, (
            f"{modname} now records screened_filters -- update this probe to "
            "assert screening provenance is applied and correctly labelled")
        assert '"screened"' not in src and "'screened'" not in src, (
            f"{modname} now writes a 'screened' label -- rewrite this probe "
            "to assert it is never applied to a bypassed candidate")


# ---------------------------------------------------------------------------
# Scenario 15 -- F14.1: regime concentration is not read into any validity
# verdict, and the one collapse-detector that exists cannot catch it.
# ---------------------------------------------------------------------------


def test_single_value_collapse_detector_is_blind_to_two_label_concentration():
    """The real live shape per F14.1: 98.8% `neutral`, 1.2% `high_volatility`
    -- TWO distinct labels, so `detect_single_value_collapse` (which only
    fires on exactly one distinct value) cannot see it, even before
    considering that it also whitelists 'neutral' outright."""
    regimes = (["neutral"] * 988) + (["high_volatility"] * 12)
    finding = SL.detect_single_value_collapse(
        regimes, probe="regime_label", min_sample=30, allowed_single_values={"neutral", ""})
    assert finding is None, (
        "fixture sanity: this two-label concentration must NOT be caught by "
        "the single-value-collapse detector -- if it started firing, the "
        "detector's contract changed and this probe's premise needs revisiting")


def test_concentrated_regime_downgrades_a_validity_verdict():
    """CLOSED by B4 (f7f70f63) + the 2026-07-29 correction. This probe
    previously asserted the gap: that NO health/validity assessor read
    `regime_performance.json` at all. Its own failure message specified the
    rewrite -- "assert it correctly downgrades a concentrated regime instead
    of asserting the absence of any regime awareness" -- so that is what it
    now does, at the level the tripwire cared about: a concentrated window
    must not be describable as generally validated."""
    from portfolio_automation.regime_coverage import (
        REGIME_CONCENTRATED, assess_regime_coverage)

    def _m(total, effective, share, rw_share):
        return {"total_signals": total, "effective_signals": effective,
                "avg_return_pct": 0.2, "win_rate": 0.52,
                "share_of_evidence": share, "return_weighted_share": rw_share}

    verdict = assess_regime_coverage({
        "resolved_signals": 2238, "primary_window_days": 3,
        "by_regime": {
            "high_volatility": _m(27, 27, 0.0121, 0.0418),
            "neutral": _m(2211, 925, 0.9879, 0.9582),
        },
    })
    assert REGIME_CONCENTRATED in verdict["states"], (
        "a 98.8%-single-regime window must be reported as concentrated -- if "
        "this stops firing, regime concentration has silently stopped "
        "reaching any validity verdict again (F14.1 re-opened)")
    assert verdict["concentration"]["max_share_regime"] == "neutral"


def test_unreadable_regime_artifact_is_not_reported_as_a_calm_window():
    """The B4 correction's own tripwire (2026-07-29). An artifact carrying
    plenty of resolved evidence but missing the derived fields the verdict
    depends on must fail CLOSED and be distinguishable from a genuinely thin
    window -- the pre-correction code coerced the missing share to 0.0 and
    read a 98.8%-concentrated window as perfectly balanced."""
    from portfolio_automation.regime_coverage import (
        INSUFFICIENCY_MISSING_FIELDS, REGIME_COVERAGE_BALANCED,
        assess_regime_coverage)

    verdict = assess_regime_coverage({
        "resolved_signals": 2238, "primary_window_days": 3,
        "by_regime": {  # pre-WS14 artifact shape: counts only, no derived fields
            "high_volatility": {"total_signals": 27, "win_rate": 0.63},
            "neutral": {"total_signals": 2211, "win_rate": 0.519},
        },
    })
    assert REGIME_COVERAGE_BALANCED not in verdict["states"], (
        "an unreadable artifact must never read as balanced -- that is "
        "imputing the best case from missing data")
    assert verdict["insufficiency_kind"] == INSUFFICIENCY_MISSING_FIELDS, (
        "an unreadable artifact must be distinguishable from a thin one; "
        "only the former costs a credibility downgrade downstream")
    assert verdict["concentration"].get("max_share_regime") is None, (
        "no concentration leader may be named from missing shares -- the "
        "pre-correction code named the SMALLEST regime at 0.0%")


# ---------------------------------------------------------------------------
# Scenario 16 -- a strong raw hit-rate delta produces an auto-applicable
# weight-INCREASE proposal even when the SAME tag's mean_return is
# negative, because retune_suggestions._propose_weight_changes never read
# mean_return at all. CLOSED by `c0fc3c6c`; the probes now assert the gate.
# ---------------------------------------------------------------------------


def _efficacy_payload_with_hitrate_expectancy_conflict() -> dict:
    """A `pattern_efficacy_monthly.json`-shaped payload (per
    pattern_learning.py's real per-tag stats shape: hit_rate_1d AND
    mean_return_1d are computed side-by-side for every tag) where
    `source:recent_signal` shows a strong POSITIVE hit-rate delta vs
    baseline (looks like a clear winner) but a NEGATIVE mean_return_1d
    (each win is small, each loss is large -- negative expectancy)."""
    return {
        "generated_at": "2026-07-28T09:00:00+00:00",
        "lookback_days": 30,
        "universe_baseline": {"n_samples": 500, "hit_rate_1d": 0.50, "mean_return_1d": 0.0005},
        "by_tag": {
            "source:recent_signal": {
                "n_samples": 260, "hit_rate_1d": 0.58, "mean_return_1d": -0.0032,
                "vs_baseline_pp": 5.0, "significance": "winner",
            },
        },
    }


def test_negative_expectancy_withholds_auto_applicability_from_a_winning_hit_rate():
    """CLOSED by `c0fc3c6c`. The gap: `source:recent_signal` carries a positive
    hit-rate delta (+5pp, 'winner', n=260 -- clears every sample/magnitude/
    significance gate) while its OWN mean_return_1d in the SAME record is
    negative, and the proposal still came back `auto_applicable: True` with a
    weight INCREASE because `_propose_weight_changes` never read expectancy.
    This matters more than most probes here: it is the ONE path allowed to
    mutate registry weights (`backtesting.auto_apply.enabled` is true). The
    old probe's failure message specified this rewrite."""
    payload = _efficacy_payload_with_hitrate_expectancy_conflict()
    result = RS.build_retune_suggestions(efficacy_payload=payload)

    proposal = next(p for p in result["weight_proposals"]
                    if p["source_tag"] == "source:recent_signal")
    assert payload["by_tag"]["source:recent_signal"]["mean_return_1d"] < 0, (
        "fixture sanity: the tag must genuinely have negative expectancy "
        "alongside its positive hit-rate delta")
    assert proposal["auto_applicable"] is False, (
        "a tag that is right more often while LOSING money must never be "
        "auto-applicable -- being correct and being profitable are different "
        "claims, and only the second justifies more weight")


def test_propose_weight_changes_reads_expectancy_structurally():
    """Static complement: the gate must be structural, not a fixture artifact.
    `_propose_weight_changes` has to touch `mean_return` at all for the
    behavioural assertion above to mean anything."""
    src = inspect.getsource(RS._propose_weight_changes)
    assert "mean_return" in src, (
        "expectancy is unreferenced again -- the hit-rate/expectancy "
        "conflation has structurally re-opened in the one path that can "
        "mutate registry weights")
