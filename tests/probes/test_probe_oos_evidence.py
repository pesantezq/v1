"""E5 probes -- OOS evidence, single-block dominance, and selection bias.

Scenario 5  -- an OOS field claims true without sufficient folds.
Scenario 6  -- complete documentation coexists with invalid statistics.
Scenario 17 -- one observation/week controls a removal/pass-fail verdict
               (+ a sibling CLOSED by E3/WS16 `88281d6c`: concerns no longer
               auto-close by age regardless of remediation, F16.1).
Scenario 18 -- many tested tactics create a false leaderboard winner
               (selection bias -- STILL OPEN, no probe passes here, this is
               an honest-state documentation of the gap).
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest

from portfolio_automation import quant_watch_probes as QW
from portfolio_automation.portfolio_sim.oos_state import (
    MIN_FOLDS_FOR_SUFFICIENCY, OOSState, build_oos_evidence, classify_oos_state,
)
from portfolio_automation.portfolio_sim.strategy_lab_health import (
    _assess_legacy, _assess_strict, _dim_documentation_coverage, _dim_oos_validity,
    _oos_evidence_by_tactic, _roll_up,
)

from tests.probes.assertions import (
    assert_no_single_block_controls_result,
    assert_oos_evidence_supported,
)

NOW = datetime(2026, 7, 28, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Scenario 5 -- F2.1: `still_works_oos: null` must not read as "passed".
# (FIXED on main, 92176881.)
# ---------------------------------------------------------------------------


def test_untested_tactic_evidence_fails_the_supported_probe():
    """A tactic never run through walk_forward() at all (wf_entry=None,
    matching 25/26 of today's live leaderboard) must not be certifiable as
    OOS-supported."""
    evidence = build_oos_evidence("some_tactic", None)
    assert evidence["state"] == OOSState.OOS_NOT_TESTED.value
    with pytest.raises(AssertionError, match="absence of failure is not evidence"):
        assert_oos_evidence_supported(evidence, min_folds=MIN_FOLDS_FOR_SUFFICIENCY,
                                      context="tactic.oos_evidence (never tested)")


def test_insufficient_folds_evidence_fails_the_supported_probe():
    """Folds ran, but too few (2 < MIN_FOLDS_FOR_SUFFICIENCY=4) to be
    evidence rather than noise -- must not pass even though the raw
    aggregate numbers look favorable."""
    wf_entry = {"status": "ok", "splits": 2, "oos_mean_excess": 0.05, "oos_hit_rate": 0.7,
               "one_fold_controls_result": False}
    evidence = build_oos_evidence("thin_tactic", wf_entry)
    assert evidence["state"] == OOSState.OOS_INSUFFICIENT.value
    with pytest.raises(AssertionError, match="fold"):
        assert_oos_evidence_supported(evidence, min_folds=MIN_FOLDS_FOR_SUFFICIENCY,
                                      context="tactic.oos_evidence (2 folds only)")


def test_pre_fix_is_false_check_would_have_certified_the_untested_tactic():
    """Verify-by-construction: reproduce the EXACT pre-fix predicate
    (`still_works_oos is False`) against the same untested tactic. The
    pre-fix rule treats None as "did not fail," so it is silently absent from
    `failing_oos` -- the literal mechanism of F2.1. Confirms the modern
    structured-evidence probe rejects what the old boolean check would have
    let through as implicitly fine."""
    row = {"tactic_id": "some_tactic", "still_works_oos": None}  # never tested

    def _pre_fix_failing_oos(rows: list[dict]) -> list[str]:
        return [r["tactic_id"] for r in rows if r.get("still_works_oos") is False]

    assert _pre_fix_failing_oos([row]) == []  # the bug: reads as "nothing failed"

    evidence = build_oos_evidence(row["tactic_id"], None)
    with pytest.raises(AssertionError):
        assert_oos_evidence_supported(evidence, min_folds=MIN_FOLDS_FOR_SUFFICIENCY,
                                      context="tactic.oos_evidence")


def test_supported_tactic_with_enough_folds_passes():
    """Positive control: a genuinely well-tested, passing tactic must clear
    the probe."""
    wf_entry = {"status": "ok", "splits": 11, "oos_mean_excess": 0.110955,
               "oos_hit_rate": 0.6364, "one_fold_controls_result": False}
    evidence = build_oos_evidence("research_momentum_rotation", wf_entry)
    assert evidence["state"] == OOSState.OOS_SUPPORTED.value
    assert_oos_evidence_supported(evidence, min_folds=MIN_FOLDS_FOR_SUFFICIENCY,
                                  context="tactic.oos_evidence (well-tested)")


# ---------------------------------------------------------------------------
# Scenario 6 -- documentation complete + statistics invalid must not roll up
# GREEN (WS4 corollary, strategy_lab_health.py's own documented policy).
# (FIXED on main, 92176881.)
# ---------------------------------------------------------------------------


_ENVELOPE_TRUE = {"observe_only": True, "sandbox_only": True, "no_trade": True}


def _leaderboard(rows: list[dict]) -> dict:
    return {"status": "ok", "created_at": NOW.isoformat(), "leaderboard": rows, **_ENVELOPE_TRUE}


def test_complete_documentation_with_all_tactics_untested_does_not_roll_up_green(tmp_path):
    """25 fully-documented, never-OOS-tested tactics -- documentation_coverage
    is GREEN, but oos_validity/statistical_sufficiency must drag the overall
    roll-up to AMBER. This is the exact corollary the module's own docstring
    commits to (WS4 policy point 5): 'documentation complete + insufficient
    OOS evidence => not fully GREEN.'"""
    rows = [{"tactic_id": f"profile_{i}", "name": f"Profile {i}",
            "still_works_oos": None, "strategy_score": 1.0 - i * 0.01}
           for i in range(25)]
    lb = _leaderboard(rows)
    cat = {"coverage_complete": True, "undocumented": []}  # fully documented

    doc_dim = _dim_documentation_coverage(cat)
    assert doc_dim["status"] == "GREEN"

    oos_by_tactic = _oos_evidence_by_tactic(rows, {})
    oos_dim = _dim_oos_validity(rows, oos_by_tactic)
    assert oos_dim["status"] == "AMBER"

    overall = _roll_up({"documentation_coverage": doc_dim, "oos_validity": oos_dim})
    assert overall == "AMBER", (
        "complete documentation must not paper over the fact that nothing was "
        "actually OOS-validated")


def test_pre_fix_legacy_algorithm_called_this_exact_shape_green(tmp_path):
    """Verify-by-construction against the REAL pre-fix code path (kept
    verbatim in `_assess_legacy` specifically so this comparison is possible
    -- not a re-implementation). Same 25-untested-but-documented leaderboard:
    the old algorithm's bare-else GREEN fires because `failing_oos == []`
    and `coverage_complete is True`; the new strict path on the identical
    input is AMBER."""
    rows = [{"tactic_id": f"profile_{i}", "name": f"Profile {i}",
            "still_works_oos": None, "strategy_score": 1.0 - i * 0.01}
           for i in range(25)]
    lb = _leaderboard(rows)
    cat = {"coverage_complete": True, "undocumented": []}

    legacy = _assess_legacy(lb, cat, None, None, tmp_path, NOW)
    assert legacy["status"] == "GREEN"
    assert legacy["reasons"] == ["lab healthy: ran, populated, documented, no failing-OOS "
                                "tactic surfaced"]

    strict = _assess_strict(lb, cat, None, None, tmp_path, NOW, legacy)
    assert strict["status"] == "AMBER"
    assert any("no_credible_oos_test" in r for r in strict["blocking_reasons"])


# ---------------------------------------------------------------------------
# Scenario 17 -- no single fold/week controls a "supported" verdict.
# (FIXED on main -- ONE_FOLD_DOMINANCE_SHARE / one_fold_controls_result.)
# ---------------------------------------------------------------------------


def test_fold_dominated_result_is_not_certified_supported():
    """A tactic that otherwise looks like a clean OOS pass (positive mean
    excess, majority hit rate, enough folds) but where ONE fold's magnitude
    dominates the aggregate must classify as OOS_MIXED (fragile), not
    OOS_SUPPORTED -- no single observation should be allowed to control the
    verdict."""
    wf_entry = {"status": "ok", "splits": 6, "oos_mean_excess": 0.03, "oos_hit_rate": 0.6,
               "one_fold_controls_result": True}
    assert classify_oos_state(wf_entry) == OOSState.OOS_MIXED

    evidence = build_oos_evidence("fragile_tactic", wf_entry)
    with pytest.raises(AssertionError, match="not evidence of a validated result"):
        assert_oos_evidence_supported(evidence, min_folds=MIN_FOLDS_FOR_SUFFICIENCY,
                                      context="tactic.oos_evidence (fold-dominated)")


def test_pre_fix_naive_classifier_would_have_passed_the_fold_dominated_case():
    """Verify-by-construction: a naive classifier that only checks
    (oos_mean_excess > 0 and oos_hit_rate >= 0.5) -- ignoring fold
    dominance entirely -- certifies the SAME fixture as a clean pass. The
    real `classify_oos_state` (unchanged call, same input) correctly
    downgrades it to OOS_MIXED. Also exercises the general-purpose
    single-block-dominance helper directly against synthetic per-fold
    contributions with the same shape (one fold = 90% of the total)."""
    wf_entry = {"status": "ok", "splits": 6, "oos_mean_excess": 0.03, "oos_hit_rate": 0.6,
               "one_fold_controls_result": True}

    def _pre_fix_naive_classify(wf: dict) -> str:
        passes = wf["oos_mean_excess"] > 0 and wf["oos_hit_rate"] >= 0.5
        return "SUPPORTED" if passes else "FAILED"

    assert _pre_fix_naive_classify(wf_entry) == "SUPPORTED"  # the bug
    assert classify_oos_state(wf_entry) == OOSState.OOS_MIXED  # the fix, same input

    fold_contributions = [0.002, 0.001, -0.001, 0.0015, 0.0005, 0.27]  # one fold = ~90%+
    with pytest.raises(AssertionError, match="controlled by a single observation"):
        assert_no_single_block_controls_result(
            fold_contributions, context="tactic.per_fold_excess (fold-dominated)")


def test_evenly_distributed_folds_pass_the_dominance_probe():
    """Positive control: contributions spread across folds must not be
    flagged."""
    assert_no_single_block_controls_result(
        [0.02, 0.018, 0.021, 0.019, 0.017], context="tactic.per_fold_excess (even)")


def test_every_concern_detector_can_escalate_and_none_closes_by_age():
    """CLOSED by Phase E3/WS16 (`88281d6c`). F16.1 was that all three
    quant-watch detectors auto-resolved purely by age (`MAX_PROBE_AGE_DAYS`)
    regardless of remediation -- an unfixed concern silently vanished after 60
    days -- and that only `_eval_prior_gauge` could ever escalate. Both halves
    are now closed, so this probe pins the FIX rather than the gap: severity
    must be expressible by every detector, and age must not be a closure path
    in any of them."""
    for fn in (QW._eval_prior_gauge, QW._eval_neg_return, QW._eval_sector_drag):
        src = inspect.getsource(fn)
        assert "_escalated(" in src, (
            f"{fn.__name__} lost its escalation path -- F16.1 half-reopened: a "
            "severe concern from this detector can no longer be raised to RED")
        assert "MAX_PROBE_AGE_DAYS" not in src, (
            f"{fn.__name__} consults age again -- F16.1 reopened: a concern "
            "that ages out has not been remediated, it has been forgotten")


def test_age_survives_only_as_an_operator_visibility_marker():
    """The complement: `MAX_PROBE_AGE_DAYS` must still EXIST (an old
    unresolved concern is worth surfacing) but only as a `stale_unresolved`
    display flag -- deleting it outright would lose that signal, while letting
    it resolve anything would re-open F16.1."""
    assert QW.MAX_PROBE_AGE_DAYS > 0
    render_src = inspect.getsource(QW.render_status)
    assert "stale_unresolved" in render_src, (
        "age no longer surfaces long-unresolved concerns to the operator")


# ---------------------------------------------------------------------------
# Scenario 18 -- selection bias over many tested tactics creates a false
# leaderboard winner. STILL OPEN (F3.2/F3.3, C5 not implemented -- no
# selection-bias correction of any kind exists anywhere in the repo).
# This probe documents the honest current state; it is expected to keep
# passing (i.e. keep proving the gap is still open) until C5 ships.
# ---------------------------------------------------------------------------


def test_no_selection_bias_correction_exists_over_clustered_families_STILL_OPEN():
    """26 tactics drawn from 8 parameterized families (mirrors the real
    production shape per F3.2: 8 `profile_*` rows are one family via
    `_apply_tilts()`, 6 `shadow_*` rows are 2 baselines + 4 overlays). All 26
    share one historical price panel. Even when EVERY tactic reaches
    OOS_SUPPORTED (best case), `ranking_credibility` and
    `statistical_sufficiency` -- the two dimensions closest to a selection-
    bias check -- carry no notion of family clustering, effective-trial
    count, or any multiple-comparison correction (Holm/Bonferroni/deflated
    Sharpe/PBO). A rank-1 winner among 26 nominally-independent but really
    ~8-effective trials is therefore uncorrected top-of-N noise, and nothing
    in the health roll-up says so. This test pins that honest gap; it should
    keep passing until WS3/C5 ships a correction, at which point it should be
    rewritten to assert the correction fires instead."""
    families = 8
    variants_per_family = 26 // families + 1
    rows = []
    tid = 0
    for fam in range(families):
        for v in range(variants_per_family):
            if tid >= 26:
                break
            wf_entry = {"status": "ok", "splits": 5,
                       "oos_mean_excess": 0.01 + 0.001 * v, "oos_hit_rate": 0.55,
                       "one_fold_controls_result": False}
            rows.append({"tactic_id": f"fam{fam}_v{v}", "name": f"Grp{fam}Variant{v}",
                        "family": f"fam{fam}", "strategy_score": 1.0 - tid * 0.001,
                        "_wf": wf_entry})
            tid += 1
    rows = rows[:26]
    wf_results = {r["tactic_id"]: r.pop("_wf") for r in rows}

    oos_by_tactic = _oos_evidence_by_tactic(rows, wf_results)
    assert all(ev["state"] == OOSState.OOS_SUPPORTED.value for ev in oos_by_tactic.values()), (
        "fixture setup: every tactic should reach OOS_SUPPORTED for this to be the "
        "best-case scenario the health roll-up is asked to judge")

    from portfolio_automation.portfolio_sim.strategy_lab_health import (
        _dim_ranking_credibility, _dim_statistical_sufficiency,
    )
    ranking_dim = _dim_ranking_credibility(rows, oos_by_tactic)
    sufficiency_dim = _dim_statistical_sufficiency(rows, oos_by_tactic)

    # Honest gap: both GREEN today, and NEITHER dimension's evidence/reasons
    # mention family count, effective trials, or any correction method --
    # confirming no selection-bias control exists at this layer.
    assert ranking_dim["status"] == "GREEN"
    assert sufficiency_dim["status"] == "GREEN"
    corpus = " ".join(ranking_dim["evidence"] + ranking_dim["reasons"]
                      + sufficiency_dim["evidence"] + sufficiency_dim["reasons"]).lower()
    for term in ("family", "effective trial", "correction", "bonferroni", "holm",
                "deflated sharpe", "selection bias"):
        assert term not in corpus, (
            f"unexpected: {term!r} found in dimension evidence/reasons -- if a "
            "selection-bias correction has been added, update this probe to assert "
            "it fires correctly instead of asserting its absence")
