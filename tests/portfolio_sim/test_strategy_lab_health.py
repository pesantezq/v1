"""Tests for the strategy-lab health assessor.

WS4 (.superpowers/audit/ws-02-03-oos-selection.md) replaced the single
collapsed verdict with 9 independent dimensions + a fail-closed roll-up,
gated ON by default (STOCKBOT_STRATEGY_LAB_STRICT_HEALTH_DISABLED / the
config `strict_oos_rollup_enabled` flag / a kill-switch file can disable it
for exact-reproduction rollback). The headline fix: a tactic with
`still_works_oos: null` and no real walk-forward backing must classify as
OOS_NOT_TESTED and must NOT read as a passing verdict, even when
`failing_oos == []`.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from portfolio_automation.portfolio_sim.oos_state import OOSState
from portfolio_automation.portfolio_sim.strategy_lab_health import (
    KILL_SWITCH_ENV, _LEGACY_GREEN_REASON, _dim, assess_strategy_lab_health,
)

NOW = datetime(2026, 6, 12, tzinfo=timezone.utc)

_ENVELOPE_TRUE = {"observe_only": True, "sandbox_only": True, "no_trade": True}

# A real walk-forward entry that classifies as OOS_SUPPORTED under oos_state.py:
# splits=11 (>= MIN_FOLDS_FOR_SUFFICIENCY), oos_mean_excess>0, oos_hit_rate>=0.5,
# no single fold dominating.
_WF_SUPPORTED = {
    "status": "ok", "train_months": 24, "test_months": 3, "splits": 11,
    "is_mean_excess": 2.120543, "oos_mean_excess": 0.110955, "oos_hit_rate": 0.6364,
    "is_oos_gap": 2.009588, "overfit": 2.009588, "still_works_oos": True,
    "one_fold_controls_result": False,
}

# A real walk-forward entry that classifies as OOS_FAILED: negative mean OOS
# excess AND a minority hit rate, still with sufficient folds.
_WF_FAILED = {
    "status": "ok", "train_months": 24, "test_months": 3, "splits": 11,
    "is_mean_excess": 0.5, "oos_mean_excess": -0.08, "oos_hit_rate": 0.27,
    "is_oos_gap": 0.58, "overfit": 0.58, "still_works_oos": False,
    "one_fold_controls_result": False,
}


def _write(root, name, payload):
    d = root / "outputs" / "sandbox"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(payload))


def _disable_gate_env(monkeypatch):
    monkeypatch.setenv(KILL_SWITCH_ENV, "1")


def test_absent_is_amber(tmp_path):
    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["status"] == "AMBER"
    assert "leaderboard_absent" in r["reasons"][0]


def test_disabled_is_amber(tmp_path):
    _write(tmp_path, "strategy_leaderboard.json", {"status": "disabled", "leaderboard": []})
    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["status"] == "AMBER"
    assert any("disabled" in x for x in r["reasons"])


def test_fresh_but_empty_is_red(tmp_path):
    _write(tmp_path, "strategy_leaderboard.json",
           {"status": "ok", "leaderboard": [], "created_at": "2026-06-12T00:00:00Z"})
    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["status"] == "RED"


# --------------------------------------------------------------------------
# WS4 headline fix: failing_oos == [] with zero tested tactics must be AMBER,
# never GREEN. This is the regression test for the largest false-GREEN in the
# system (strategy_lab_health.py:121's `is False` check).
# --------------------------------------------------------------------------

def test_zero_tested_tactics_with_failing_oos_empty_yields_amber_not_green(tmp_path):
    # Mirrors the real production shape: 25 rows with still_works_oos: null and
    # zero rows with still_works_oos: false -> failing_oos == [] under the old
    # (still-present) legacy signal, yet nothing has ever been tested.
    rows = [
        {"tactic_id": f"untested_{i}", "name": f"Untested {i}", "strategy_score": 1.0 - i * 0.01,
         "still_works_oos": None, **_ENVELOPE_TRUE}
        for i in range(25)
    ]
    _write(tmp_path, "strategy_leaderboard.json", {
        "status": "ok", "created_at": "2026-06-12T00:00:00Z", "leaderboard": rows, **_ENVELOPE_TRUE})
    _write(tmp_path, "research_strategy_catalog.json", {"coverage_complete": True, "undocumented": []})
    _write(tmp_path, "walk_forward_results.json", {"results": {}})
    _write(tmp_path, "factor_exposure_report.json", {"factor_data_available": True})

    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["signals"]["failing_oos"] == []  # legacy signal still empty
    assert r["status"] == "AMBER"             # but the verdict must NOT be GREEN
    assert r["dimensions"]["oos_validity"]["status"] == "AMBER"
    assert any("no_credible_oos_test" in reason for reason in r["blocking_reasons"])
    assert r["signals"]["oos_state_counts"] == {OOSState.OOS_NOT_TESTED.value: 25}


def test_null_still_works_oos_maps_to_oos_not_tested_and_does_not_pass(tmp_path):
    rows = [{"tactic_id": "untested_one", "name": "Untested", "strategy_score": 1.0,
             "still_works_oos": None, **_ENVELOPE_TRUE}]
    _write(tmp_path, "strategy_leaderboard.json", {
        "status": "ok", "created_at": "2026-06-12T00:00:00Z", "leaderboard": rows, **_ENVELOPE_TRUE})
    _write(tmp_path, "research_strategy_catalog.json", {"coverage_complete": True, "undocumented": []})

    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["signals"]["oos_state_counts"] == {OOSState.OOS_NOT_TESTED.value: 1}
    assert r["dimensions"]["oos_validity"]["status"] != "GREEN"
    assert r["status"] != "GREEN"


def test_documentation_complete_plus_oos_insufficient_is_not_fully_green(tmp_path):
    # coverage_complete=true (documentation dimension GREEN) but no tactic has
    # any real walk-forward backing -> overall must still not be GREEN.
    rows = [{"tactic_id": "research_momentum_rotation", "name": "Momentum",
             "strategy_score": 1.2, "mean_excess_vs_spy": 0.05, "still_works_oos": True,
             **_ENVELOPE_TRUE}]
    _write(tmp_path, "strategy_leaderboard.json", {
        "status": "ok", "created_at": "2026-06-12T00:00:00Z", "leaderboard": rows, **_ENVELOPE_TRUE})
    _write(tmp_path, "research_strategy_catalog.json", {"coverage_complete": True, "undocumented": []})
    _write(tmp_path, "walk_forward_results.json", {"results": {}})  # no real backing
    _write(tmp_path, "factor_exposure_report.json", {"factor_data_available": True})

    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["dimensions"]["documentation_coverage"]["status"] == "GREEN"
    assert r["status"] != "GREEN"
    assert r["signals"]["top_tactic"] == "Momentum"


def test_failing_oos_tactic_is_amber_with_oos_failed_state(tmp_path):
    rows = [{"tactic_id": "research_momentum_rotation", "name": "M",
             "strategy_score": 0.1, "still_works_oos": False, **_ENVELOPE_TRUE}]
    _write(tmp_path, "strategy_leaderboard.json", {
        "status": "ok", "created_at": "2026-06-12T00:00:00Z", "leaderboard": rows, **_ENVELOPE_TRUE})
    _write(tmp_path, "research_strategy_catalog.json", {"coverage_complete": True, "undocumented": []})
    _write(tmp_path, "walk_forward_results.json",
           {"results": {"research_momentum_rotation": _WF_FAILED}})

    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["status"] == "AMBER"
    assert r["dimensions"]["oos_validity"]["status"] == "AMBER"
    assert any("OOS_FAILED" in reason for reason in r["blocking_reasons"])


def test_at_least_one_supported_tactic_can_reach_green(tmp_path):
    # Positive control: a single, well-tested, OOS_SUPPORTED tactic with full
    # documentation and no gaps should be able to reach GREEN.
    rows = [{"tactic_id": "research_momentum_rotation", "name": "Momentum",
             "strategy_score": 1.2, "mean_excess_vs_spy": 0.11, "still_works_oos": True,
             **_ENVELOPE_TRUE}]
    _write(tmp_path, "strategy_leaderboard.json", {
        "status": "ok", "created_at": "2026-06-12T00:00:00Z", "leaderboard": rows, **_ENVELOPE_TRUE})
    _write(tmp_path, "research_strategy_catalog.json", {"coverage_complete": True, "undocumented": []})
    _write(tmp_path, "walk_forward_results.json",
           {"results": {"research_momentum_rotation": _WF_SUPPORTED}})
    _write(tmp_path, "factor_exposure_report.json", {"factor_data_available": True})

    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["status"] == "GREEN"
    assert r["dimensions"]["oos_validity"]["status"] == "GREEN"
    assert r["dimensions"]["oos_validity"]["evidence"]  # non-empty positive evidence


# --------------------------------------------------------------------------
# WS14 (.superpowers/audit/ws-04-05-14-18-health.md): regime concentration
# must downgrade ranking_credibility/oos_validity — a strategy whose evidence
# is ~99% one regime must not read as generally validated.
# --------------------------------------------------------------------------

def _write_regime_performance(tmp_path, by_regime, resolved_signals):
    d = tmp_path / "outputs" / "regime"
    d.mkdir(parents=True, exist_ok=True)
    (d / "regime_performance.json").write_text(
        json.dumps({"resolved_signals": resolved_signals, "by_regime": by_regime}))


_CONCENTRATED_BY_REGIME = {
    "high_volatility": {"total_signals": 27, "effective_signals": 27, "avg_return_pct": 0.807,
                        "win_rate": 0.63, "share_of_evidence": 0.0119, "return_weighted_share": 0.0468},
    "neutral": {"total_signals": 2211, "effective_signals": 925, "avg_return_pct": 0.226,
               "win_rate": 0.519, "share_of_evidence": 0.9762, "return_weighted_share": 1.0722},
}


def test_regime_concentration_downgrades_green_to_amber_with_stated_reason(tmp_path):
    # Same positive-control fixture that reaches GREEN above, but now with a
    # concentrated + risk-off-unproven regime_performance.json alongside it —
    # ranking_credibility/oos_validity must downgrade, overall must not be GREEN.
    rows = [{"tactic_id": "research_momentum_rotation", "name": "Momentum",
             "strategy_score": 1.2, "mean_excess_vs_spy": 0.11, "still_works_oos": True,
             **_ENVELOPE_TRUE}]
    _write(tmp_path, "strategy_leaderboard.json", {
        "status": "ok", "created_at": "2026-06-12T00:00:00Z", "leaderboard": rows, **_ENVELOPE_TRUE})
    _write(tmp_path, "research_strategy_catalog.json", {"coverage_complete": True, "undocumented": []})
    _write(tmp_path, "walk_forward_results.json",
           {"results": {"research_momentum_rotation": _WF_SUPPORTED}})
    _write(tmp_path, "factor_exposure_report.json", {"factor_data_available": True})
    _write_regime_performance(tmp_path, _CONCENTRATED_BY_REGIME, 2238)

    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["status"] != "GREEN"
    assert r["dimensions"]["oos_validity"]["status"] == "AMBER"
    assert r["dimensions"]["ranking_credibility"]["status"] == "AMBER"
    assert any("regime_concentration" in reason for reason in r["dimensions"]["oos_validity"]["reasons"])
    assert any("regime_concentration" in reason for reason in r["dimensions"]["ranking_credibility"]["reasons"])
    assert any("regime_concentration" in reason for reason in r["blocking_reasons"])
    assert r["signals"]["regime_coverage"]["primary_state"] == "RISK_OFF_UNPROVEN"
    assert set(r["signals"]["regime_coverage"]["states"]) == {"REGIME_CONCENTRATED", "RISK_OFF_UNPROVEN"}


def test_regime_data_insufficient_does_not_downgrade(tmp_path):
    # No outputs/regime/regime_performance.json at all (as in the other
    # fixtures in this file) — REGIME_DATA_INSUFFICIENT must NOT downgrade
    # anything; absence of evidence is not evidence of concentration.
    rows = [{"tactic_id": "research_momentum_rotation", "name": "Momentum",
             "strategy_score": 1.2, "mean_excess_vs_spy": 0.11, "still_works_oos": True,
             **_ENVELOPE_TRUE}]
    _write(tmp_path, "strategy_leaderboard.json", {
        "status": "ok", "created_at": "2026-06-12T00:00:00Z", "leaderboard": rows, **_ENVELOPE_TRUE})
    _write(tmp_path, "research_strategy_catalog.json", {"coverage_complete": True, "undocumented": []})
    _write(tmp_path, "walk_forward_results.json",
           {"results": {"research_momentum_rotation": _WF_SUPPORTED}})
    _write(tmp_path, "factor_exposure_report.json", {"factor_data_available": True})

    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["status"] == "GREEN"
    assert r["signals"]["regime_coverage"]["primary_state"] == "REGIME_DATA_INSUFFICIENT"


def test_unreadable_regime_artifact_downgrades_and_does_not_buy_a_free_pass(tmp_path):
    # B4 correction. Two absences reach the consumer as REGIME_DATA_INSUFFICIENT
    # and must NOT be treated alike:
    #   - no artifact / too thin  -> no downgrade (test above); absence of
    #     evidence is not evidence of concentration
    #   - 2238 resolved signals present but the artifact's derived fields are
    #     absent (a pre-WS14 artifact, exactly what was on disk 2026-07-28)
    #     -> the evidence EXISTS and cannot be read. That is an instrumentation
    #     failure, and letting it pass as GREEN is how a stale artifact silently
    #     buys the credibility it never earned.
    rows = [{"tactic_id": "research_momentum_rotation", "name": "Momentum",
             "strategy_score": 1.2, "mean_excess_vs_spy": 0.11, "still_works_oos": True,
             **_ENVELOPE_TRUE}]
    _write(tmp_path, "strategy_leaderboard.json", {
        "status": "ok", "created_at": "2026-06-12T00:00:00Z", "leaderboard": rows, **_ENVELOPE_TRUE})
    _write(tmp_path, "research_strategy_catalog.json", {"coverage_complete": True, "undocumented": []})
    _write(tmp_path, "walk_forward_results.json",
           {"results": {"research_momentum_rotation": _WF_SUPPORTED}})
    _write(tmp_path, "factor_exposure_report.json", {"factor_data_available": True})
    _write_regime_performance(tmp_path, {
        "high_volatility": {"total_signals": 27, "win_rate": 0.63, "avg_return_pct": 0.807},
        "neutral": {"total_signals": 2211, "win_rate": 0.519, "avg_return_pct": 0.226},
    }, 2238)

    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["status"] != "GREEN"
    assert r["dimensions"]["oos_validity"]["status"] == "AMBER"
    assert r["dimensions"]["ranking_credibility"]["status"] == "AMBER"
    assert any("regime_coverage_unreadable" in x
               for x in r["dimensions"]["ranking_credibility"]["reasons"])
    assert r["signals"]["regime_coverage"]["insufficiency_kind"] == "missing_derived_fields"


def test_regime_concentration_appends_reason_to_already_downgraded_dimension(tmp_path):
    # oos_validity is already AMBER (no OOS_SUPPORTED tactic) for an unrelated
    # reason — the regime-concentration caveat must still be appended, not
    # silently dropped because the dimension wasn't GREEN.
    rows = [{"tactic_id": "untested_one", "name": "Untested", "strategy_score": 1.0,
             "still_works_oos": None, **_ENVELOPE_TRUE}]
    _write(tmp_path, "strategy_leaderboard.json", {
        "status": "ok", "created_at": "2026-06-12T00:00:00Z", "leaderboard": rows, **_ENVELOPE_TRUE})
    _write(tmp_path, "research_strategy_catalog.json", {"coverage_complete": True, "undocumented": []})
    _write_regime_performance(tmp_path, _CONCENTRATED_BY_REGIME, 2238)

    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["dimensions"]["oos_validity"]["status"] == "AMBER"
    reasons = r["dimensions"]["oos_validity"]["reasons"]
    assert any("no_credible_oos_test" in x for x in reasons)
    assert any("regime_concentration" in x for x in reasons)


# --------------------------------------------------------------------------
# Every GREEN dimension must carry positive evidence — enforced structurally.
# --------------------------------------------------------------------------

def test_green_dimension_without_evidence_is_impossible():
    downgraded = _dim("GREEN", [])
    assert downgraded["status"] == "AMBER"
    assert "no_positive_evidence_for_green" in downgraded["reasons"][0]

    kept = _dim("GREEN", ["some real evidence"])
    assert kept["status"] == "GREEN"
    assert kept["evidence"] == ["some real evidence"]


def test_every_green_dimension_in_a_real_verdict_has_evidence(tmp_path):
    rows = [{"tactic_id": "research_momentum_rotation", "name": "Momentum",
             "strategy_score": 1.2, "mean_excess_vs_spy": 0.11, "still_works_oos": True,
             **_ENVELOPE_TRUE}]
    _write(tmp_path, "strategy_leaderboard.json", {
        "status": "ok", "created_at": "2026-06-12T00:00:00Z", "leaderboard": rows, **_ENVELOPE_TRUE})
    _write(tmp_path, "research_strategy_catalog.json", {"coverage_complete": True, "undocumented": []})
    _write(tmp_path, "walk_forward_results.json",
           {"results": {"research_momentum_rotation": _WF_SUPPORTED}})
    _write(tmp_path, "factor_exposure_report.json", {"factor_data_available": True})

    r = assess_strategy_lab_health(tmp_path, now=NOW)
    for name, dim in r["dimensions"].items():
        if dim["status"] == "GREEN":
            assert dim["evidence"], f"{name} is GREEN with no evidence"


# --------------------------------------------------------------------------
# Backward compatibility: legacy top-level keys/sub-signals must still resolve.
# --------------------------------------------------------------------------

def test_legacy_consumer_keys_still_resolve(tmp_path):
    rows = [{"tactic_id": "research_momentum_rotation", "name": "Momentum",
             "strategy_score": 1.2, "mean_excess_vs_spy": 0.11, "still_works_oos": True,
             **_ENVELOPE_TRUE}]
    _write(tmp_path, "strategy_leaderboard.json", {
        "status": "ok", "created_at": "2026-06-12T00:00:00Z", "leaderboard": rows, **_ENVELOPE_TRUE})
    _write(tmp_path, "research_strategy_catalog.json", {"coverage_complete": True, "undocumented": []})
    _write(tmp_path, "walk_forward_results.json",
           {"results": {"research_momentum_rotation": _WF_SUPPORTED}})
    _write(tmp_path, "factor_exposure_report.json", {"factor_data_available": True})

    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert "status" in r and "reasons" in r and "signals" in r
    for key in ("lab_status", "tactic_count", "age_hours", "coverage_complete",
                "walk_forward_present", "failing_oos", "factor_data_available",
                "top_tactic", "top_score", "top_excess_vs_spy"):
        assert key in r["signals"], f"legacy signal '{key}' missing"
    assert r["signals"]["top_tactic"] == "Momentum"
    assert r["signals"]["walk_forward_present"] is True
    assert r["signals"]["failing_oos"] == []


# --------------------------------------------------------------------------
# Gate: disabled reproduces the previous (pre-WS4) verdict exactly.
# --------------------------------------------------------------------------

def test_gate_disabled_reproduces_legacy_green_verdict(tmp_path, monkeypatch):
    _disable_gate_env(monkeypatch)
    # Same fixture that is AMBER under strict rollup (test above) — under the
    # legacy algorithm this was the known-false GREEN the audit found.
    rows = [{"tactic_id": "research_momentum_rotation", "name": "Momentum",
             "strategy_score": 1.2, "mean_excess_vs_spy": 0.05, "still_works_oos": True}]
    _write(tmp_path, "strategy_leaderboard.json", {
        "status": "ok", "created_at": "2026-06-12T00:00:00Z", "leaderboard": rows})
    _write(tmp_path, "research_strategy_catalog.json", {"coverage_complete": True})
    _write(tmp_path, "walk_forward_results.json", {"results": {}})
    _write(tmp_path, "factor_exposure_report.json", {"factor_data_available": True})

    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["gate"]["strict_oos_rollup_enabled"] is False
    assert r["status"] == "GREEN"
    assert r["reasons"] == [_LEGACY_GREEN_REASON]
    assert "dimensions" not in r
    assert r["signals"]["top_tactic"] == "Momentum"


def test_gate_disabled_via_config_reproduces_legacy_amber_for_failing_oos(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "portfolio_sim": {"strategy_lab": {"health": {"strict_oos_rollup_enabled": False}}}}))
    rows = [{"tactic_id": "research_momentum_rotation", "name": "M",
             "strategy_score": 0.1, "still_works_oos": False}]
    _write(tmp_path, "strategy_leaderboard.json", {
        "status": "ok", "created_at": "2026-06-12T00:00:00Z", "leaderboard": rows})
    _write(tmp_path, "research_strategy_catalog.json", {"coverage_complete": True})

    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["gate"]["strict_oos_rollup_enabled"] is False
    assert r["gate"]["source"] == "config"
    assert r["status"] == "AMBER"
    assert any("still_works_oos=false" in x for x in r["reasons"])


def test_gate_enabled_by_default(tmp_path):
    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["gate"]["strict_oos_rollup_enabled"] is True
    assert r["gate"]["source"] == "default_on"


# ---------------------------------------------------------------------------
# Cross-catalog coverage seam (found 2026-08-03).
#
# documentation_coverage read research_strategy_catalog.json's own
# `coverage_complete`, and that flag is computed over the cards the catalog was
# HANDED — so a tactic surfaced in the leaderboard but absent from the catalog
# could never flip it. Live instance: crowd_signal_only / crowd_signal_plus_sentiment
# rank in the 26-row leaderboard while strategy_catalog.json carries only 16 cards,
# and BOTH gates reported complete. The rationale text for those two does exist in
# strategy_docs._RATIONALE, so the defect is that catalog documentation never
# reaches a reader of the catalog — not that the prose was never written.
# ---------------------------------------------------------------------------
def _lb(tmp_path, tactic_ids):
    rows = [{"tactic_id": t, "name": t.replace("_", " ").title(), "strategy_score": 1.0,
             "still_works_oos": None, **_ENVELOPE_TRUE} for t in tactic_ids]
    _write(tmp_path, "strategy_leaderboard.json", {
        "status": "ok", "created_at": "2026-06-12T00:00:00Z",
        "leaderboard": rows, **_ENVELOPE_TRUE})


def test_leaderboard_tactic_absent_from_every_catalog_is_flagged(tmp_path):
    """A surfaced tactic with no card and no _RATIONALE entry must not read GREEN."""
    _lb(tmp_path, ["research_dual_momentum", "totally_undocumented_tactic"])
    _write(tmp_path, "research_strategy_catalog.json", {
        "coverage_complete": True, "undocumented": [],
        "cards": [{"tactic_id": "research_dual_momentum", "academic_basis": "Antonacci"}]})

    r = assess_strategy_lab_health(tmp_path, now=NOW)
    dim = r["dimensions"]["documentation_coverage"]
    assert dim["status"] == "AMBER"
    assert any("totally_undocumented_tactic" in x for x in dim["reasons"])


def test_crowd_tactics_documented_only_in_strategy_docs_still_count(tmp_path):
    """The live case: no research-catalog card, but _RATIONALE covers them."""
    _lb(tmp_path, ["research_dual_momentum", "crowd_signal_only",
                   "crowd_signal_plus_sentiment"])
    _write(tmp_path, "research_strategy_catalog.json", {
        "coverage_complete": True, "undocumented": [],
        "cards": [{"tactic_id": "research_dual_momentum", "academic_basis": "Antonacci"}]})

    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["dimensions"]["documentation_coverage"]["status"] == "GREEN"


def test_research_catalog_own_undocumented_list_still_respected(tmp_path):
    _lb(tmp_path, ["research_dual_momentum"])
    _write(tmp_path, "research_strategy_catalog.json", {
        "coverage_complete": False, "undocumented": ["research_dual_momentum"],
        "cards": [{"tactic_id": "research_dual_momentum"}]})

    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["dimensions"]["documentation_coverage"]["status"] == "AMBER"


def test_card_without_academic_basis_or_rationale_is_flagged(tmp_path):
    """A card that exists but carries no documentation is not coverage."""
    _lb(tmp_path, ["research_made_up_thing"])
    _write(tmp_path, "research_strategy_catalog.json", {
        "coverage_complete": True, "undocumented": [],
        "cards": [{"tactic_id": "research_made_up_thing"}]})

    r = assess_strategy_lab_health(tmp_path, now=NOW)
    dim = r["dimensions"]["documentation_coverage"]
    assert dim["status"] == "AMBER"
    assert any("research_made_up_thing" in x for x in dim["reasons"])
