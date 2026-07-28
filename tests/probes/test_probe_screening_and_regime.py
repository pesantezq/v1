"""E5 probes -- quality-screen mislabeling, regime concentration, and
hit-rate-vs-expectancy conflation.

Scenario 14 -- a quality-screen bypass is mislabeled as screened.
Scenario 15 -- a regime-concentrated signal is described as generally
               validated.
Scenario 16 -- a strong raw hit rate coexists with negative
               benchmark-relative expectancy.

All three document STILL-OPEN gaps (F15.1, F14.1, and a hit-rate/expectancy
conflation in ``retune_suggestions.py`` respectively). None appear in the
reliability-program's implemented-changes table. Each probe demonstrates the
shared assertion catching the shape when it occurs, and documents that the
production data needed to run it for real (a "screened" label field; a
regime-aware validity check; an expectancy-aware weight-proposal gate) does
not exist yet.
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


def test_no_module_reads_regime_performance_into_a_validity_verdict_STILL_OPEN():
    """STILL OPEN (F14.1): grep the two real health/validity assessors most
    likely to own this (`strategy_lab_health.py`, `quant_watch_probes.py`)
    and confirm neither reads `regime_performance.json` at all -- regime
    concentration feeds no GREEN/AMBER/RED verdict anywhere today. The two
    real readers that DO exist (`decision_context_capture.py`,
    `daily_input_snapshot.py`) are descriptive/freshness-only, not validity
    checks; this test does not need to inspect them to make its point."""
    from portfolio_automation.portfolio_sim import strategy_lab_health as SLH
    from portfolio_automation import quant_watch_probes as QW

    for module in (SLH, QW):
        src = inspect.getsource(module)
        assert "regime_performance" not in src and "regime" not in src.lower(), (
            f"{module.__name__} now references regime data -- if a "
            "regime-aware validity dimension has been added, rewrite this "
            "probe to assert it correctly downgrades a concentrated regime "
            "instead of asserting the absence of any regime awareness")


# ---------------------------------------------------------------------------
# Scenario 16 -- a strong raw hit-rate delta produces an auto-applicable
# weight-INCREASE proposal even when the SAME tag's mean_return is
# negative -- retune_suggestions._propose_weight_changes never reads
# mean_return at all. STILL OPEN.
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


def test_retune_suggestion_proposes_weight_increase_despite_negative_expectancy():
    """Real production function, real-shaped input. `source:recent_signal`
    carries a positive hit-rate delta (+5pp, 'winner', n=260 -- comfortably
    clears the auto-apply sample/magnitude/significance gates) but its OWN
    mean_return_1d in the SAME record is negative. The proposal still comes
    back `auto_applicable: True` with a weight INCREASE, because
    `_propose_weight_changes` never reads `mean_return_1d` at all."""
    payload = _efficacy_payload_with_hitrate_expectancy_conflict()
    result = RS.build_retune_suggestions(efficacy_payload=payload)

    hit_rate_proposal = next(p for p in result["weight_proposals"]
                             if p["source_tag"] == "source:recent_signal")
    assert hit_rate_proposal["delta"] > 0, "hit-rate delta alone drives an INCREASE"
    assert hit_rate_proposal["auto_applicable"] is True

    tag_stats = payload["by_tag"]["source:recent_signal"]
    assert tag_stats["mean_return_1d"] < 0, (
        "fixture sanity: the tag genuinely has negative expectancy alongside "
        "its positive hit-rate delta")
    assert "mean_return" not in hit_rate_proposal["rationale"].lower(), (
        "the proposal's own rationale text never mentions expectancy -- "
        "confirming it was never considered, not merely omitted from display")


def test_propose_weight_changes_source_never_reads_mean_return_STILL_OPEN():
    """STILL OPEN: static confirmation that `_propose_weight_changes` has no
    code path touching `mean_return` at all -- the gap demonstrated above is
    not a fixture artifact, it is structural. If this test starts failing
    (mean_return referenced), rewrite the test above to assert the gate
    correctly WITHHOLDS auto-applicability for negative-expectancy tags
    instead of asserting today's honest gap."""
    src = inspect.getsource(RS._propose_weight_changes)
    assert "mean_return" not in src
