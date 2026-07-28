"""Tests for portfolio_automation.portfolio_sim.oos_state — the explicit OOS
state enum + evidence builder that replaces the historical bare
`still_works_oos: bool | None` field as the source of truth for Strategy Lab
tactics. See .superpowers/audit/ws-02-03-oos-selection.md."""
from __future__ import annotations

from portfolio_automation.portfolio_sim.oos_state import (
    OOSState, build_oos_evidence, classify_oos_state, legacy_still_works_oos,
)

_SUPPORTED = {
    "status": "ok", "splits": 11, "oos_mean_excess": 0.110955, "oos_hit_rate": 0.6364,
    "one_fold_controls_result": False, "train_months": 24, "test_months": 3,
    "is_oos_gap": 2.009588,
}

_FAILED = {
    "status": "ok", "splits": 11, "oos_mean_excess": -0.08, "oos_hit_rate": 0.27,
    "one_fold_controls_result": False, "train_months": 24, "test_months": 3,
}


def test_no_entry_maps_to_not_tested():
    # The 25/26-tactic production shape: no key in walk_forward_results.json at all.
    assert classify_oos_state(None) == OOSState.OOS_NOT_TESTED


def test_null_still_works_oos_row_with_no_wf_backing_maps_to_not_tested():
    # A row asserting still_works_oos: True/False/None with nothing behind it in
    # walk_forward_results.json must not be trusted — classification only ever
    # looks at the wf_entry, never at a row's own legacy boolean.
    assert classify_oos_state(None) == OOSState.OOS_NOT_TESTED
    assert legacy_still_works_oos(classify_oos_state(None)) is None


def test_data_blocked_states():
    assert classify_oos_state({"status": "no_params"}) == OOSState.OOS_DATA_BLOCKED
    assert classify_oos_state({"status": "insufficient_data", "splits": 0}) == OOSState.OOS_DATA_BLOCKED


def test_insufficient_folds():
    entry = {**_SUPPORTED, "splits": 2}  # below MIN_FOLDS_FOR_SUFFICIENCY
    assert classify_oos_state(entry) == OOSState.OOS_INSUFFICIENT


def test_missing_aggregate_fields_is_insufficient():
    entry = {"status": "ok", "splits": 11}  # no oos_mean_excess/oos_hit_rate
    assert classify_oos_state(entry) == OOSState.OOS_INSUFFICIENT


def test_supported_state():
    assert classify_oos_state(_SUPPORTED) == OOSState.OOS_SUPPORTED
    assert legacy_still_works_oos(OOSState.OOS_SUPPORTED) is True


def test_failed_state():
    assert classify_oos_state(_FAILED) == OOSState.OOS_FAILED
    assert legacy_still_works_oos(OOSState.OOS_FAILED) is False


def test_one_fold_dominance_downgrades_supported_to_mixed():
    entry = {**_SUPPORTED, "one_fold_controls_result": True}
    assert classify_oos_state(entry) == OOSState.OOS_MIXED
    assert legacy_still_works_oos(OOSState.OOS_MIXED) is None


def test_straddling_sign_and_hit_rate_is_mixed():
    # Positive mean excess but a minority hit rate: neither a clean pass nor fail.
    entry = {**_SUPPORTED, "oos_mean_excess": 0.05, "oos_hit_rate": 0.4}
    assert classify_oos_state(entry) == OOSState.OOS_MIXED


def test_build_oos_evidence_not_tested_has_no_fabricated_fields():
    ev = build_oos_evidence("untested_tactic", None)
    assert ev["state"] == OOSState.OOS_NOT_TESTED.value
    assert ev["folds"] is None
    assert ev["fold_construction"] is None
    assert ev["embargo_purge_rule"] is None
    assert ev["confidence_interval"] is None
    assert ev["one_fold_controls_result"] is None
    assert ev["legacy_still_works_oos"] is None
    assert ev["tax_note"] is None


def test_build_oos_evidence_supported_carries_real_evidence():
    ev = build_oos_evidence("research_momentum_rotation", _SUPPORTED)
    assert ev["state"] == OOSState.OOS_SUPPORTED.value
    assert ev["folds"] == 11
    assert ev["embargo_purge_rule"] == "none"  # a genuine finding, not an omission
    assert ev["confidence_interval"] is None   # genuinely absent — not fabricated
    assert ev["tax_note"] == "gross_until_cost_model"
    assert ev["legacy_still_works_oos"] is True
    assert ev["one_fold_controls_result"] is False
