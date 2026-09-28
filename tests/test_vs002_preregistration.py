"""VS-002 FINAL preregistration freeze — result-blind contract tests.

These tests pin the frozen scientific rules recorded in
evals/vertical_slice/VS-002_preregistration.json. They NEVER execute against the
real VS-002 evidence package and compute NO experiment result; a small synthetic
digest mutation proves the freeze identity is load-bearing.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from portfolio_automation.northstar.canonical import canonical_dumps, content_hash

PREREG = Path("evals/vertical_slice/VS-002_preregistration.json")


@pytest.fixture(scope="module")
def art():
    return json.loads(PREREG.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def core(art):
    return art["binding_core"]


def test_status_not_executed(art):
    assert art["experiment_id"] == "VS-002"
    assert art["status"] == "PREREGISTERED_NOT_EXECUTED"
    assert art["vs002_executed"] is False
    assert art["schema_version"] == "engineering.vertical_slice.preregistration.v1"


def test_blocked_history_preserved():
    assert Path("evals/vertical_slice/VS-002_blocked.json").is_file()


def test_freeze_digest_matches_binding_core(art, core):
    assert content_hash(canonical_dumps(core)) == art["preregistration_freeze_digest"]


def test_freeze_digest_is_load_bearing(art, core):
    mutated = copy.deepcopy(core)
    mutated["H1"]["gate"] = "H1_MET iff experiment_mean > 0"   # drop lower-bound
    assert content_hash(canonical_dumps(mutated)) != art["preregistration_freeze_digest"]


# ── Ruling 1: H1 interval = two-sided 95% Student-t over cohort means ──────
def test_h1_interval_student_t_95_two_sided(core):
    iv = core["interval_method"]
    assert iv["type"] == "student_t"
    assert iv["confidence_level"] == 0.95
    assert iv["sided"] == "two-sided"
    assert iv["over"] == "independent cohort means"
    for bad in ("normal 1.96 approximation", "bootstrap", "HAC/Newey-West",
                "row-level pseudo-N"):
        assert bad in iv["forbidden"]
    assert iv["insufficient_rule"] == "n < 10 => EVIDENCE_INCONCLUSIVE"


# ── Ruling 2: H1 gate needs point>0 AND lower CI>0 ────────────────────────
def test_h1_gate_requires_point_and_lower_bound(core):
    g = core["H1"]["gate"]
    assert "experiment_mean > 0" in g and "H1_CI95.lower > 0" in g


# ── Ruling 3: H2 per-cohort IC, not pooled row IC ─────────────────────────
def test_h2_population_is_per_cohort(core):
    h2 = core["H2"]
    assert "for each greedy non-overlapping 7-day cohort" in h2["cohort_ic_population"]
    assert ">=3 valid pairs" in h2["cohort_ic_population"]
    assert "average ranks" in h2["cohort_ic_population"]
    assert h2["point_ic"].startswith("equal-weight arithmetic mean")
    assert ">= 10" in h2["independent_count"]
    assert h2["point_gate"] == "H2_MET iff H2_POINT_IC > 0"


# ── Ruling 4: H2 uncertainty reporting-only, never a gate ─────────────────
def test_h2_uncertainty_reporting_only(core):
    u = core["H2"]["uncertainty"]
    assert u["role"].startswith("REPORTING ONLY")
    assert "does NOT change the H2 gate" in u["role"]
    for bad in ("bootstrap", "Fisher-z", "jackknife", "permutation"):
        assert bad in u["forbidden"]


# ── Ruling 5: NO_ACTION lower-bound gate, no beta, rf=0 friction 0.10 ─────
def test_no_action_gate(core):
    na = core["no_action"]
    assert "mean_net_no_action_return > 0" in na["gate"]
    assert "NO_ACTION_CI95.lower > 0" in na["gate"]
    assert "does NOT use beta" in na["definition"]
    assert "- 0.10" in na["net_no_action_return_pct"]


# ── Ruling 6: exact-timestamp SPY join, no bar-derived fallback ───────────
def test_spy_forward_join_exact_no_fallback(core):
    j = core["forward_outcome"]["spy_forward_join"]
    assert "EXACT timestamp equality" in j["rule"]
    assert "signal_time==stock.signal_time" in j["rule"]
    for bad in ("join by calendar date only", "nearest SPY scan", "interpolate",
                "previous/next session", "compose from bars", "another source"):
        assert bad in j["forbidden_fallbacks"]
    assert "exclude" in j["on_no_exact_match"]


def test_bars_are_beta_only_forward_uses_recorded(core):
    assert core["risk_adjustment"]["bars_role"] == "PRE-SIGNAL BETA ESTIMATION ONLY"
    fo = core["forward_outcome"]
    assert "recorded outcome_return_7d" in fo["stock_outcome_source"]
    assert "recorded outcome_return_7d" in fo["spy_outcome_source"]


# ── row arithmetic units + beta window ────────────────────────────────────
def test_units_friction_rf(core):
    ra = core["row_arithmetic"]
    assert ra["friction_pp"] == 0.10
    assert ra["rf_7d_pp"] == 0.0
    assert core["units"]["recorded_outcome_return_7d"] == "percentage_points"
    assert core["units"]["friction"] == "percentage_points"
    assert core["rf_assumption"]["value_pp"] == 0.0
    assert core["rf_assumption"]["observed_evidence"] is False


def test_beta_window_min_obs(core):
    ra = core["risk_adjustment"]
    assert ra["estimation_window_sessions"] == 252
    assert ra["minimum_joint_observations"] == 60
    assert "STRICTLY before signal_time" in ra["window_boundary"]


# ── cohort rule + >=10 ────────────────────────────────────────────────────
def test_cohort_rule_and_min_cohorts(core):
    c = core["cohort_construction"]
    assert c["minimum_cohorts"] == 10
    assert "at least 7 calendar days after the previously selected" in c["rule"]
    assert "equal-weight" in c["aggregation"]
    assert "NOT weighted by row count" in c["aggregation"]


# ── final classification binding ──────────────────────────────────────────
def test_criterion_and_skill_binding(core):
    cl = core["classification"]
    assert cl["criterion_outcomes"] == [
        "PREREGISTERED_CRITERIA_MET", "PREREGISTERED_CRITERIA_NOT_MET",
        "EVIDENCE_INCONCLUSIVE"]
    assert "H1_MET AND H2_MET AND NO_ACTION_BEAT" in cl["criterion_binding"]
    assert ">= 10" in cl["criterion_binding"]
    assert cl["skill_outcomes"] == [
        "ECONOMIC_SKILL_EVIDENCE_PRESENT", "ECONOMIC_SKILL_NOT_ESTABLISHED"]
    assert cl["skill_binding"].startswith(
        "ECONOMIC_SKILL_EVIDENCE_PRESENT only when PREREGISTERED_CRITERIA_MET")


# ── package identity pinned ───────────────────────────────────────────────
def test_evidence_binding_pinned(core):
    eb = core["evidence_binding"]
    assert eb["package_id"] == "vs002evd_77469725f5592e6df33742b68a31ae1e"
    assert eb["source_production_sha"] == "7f2d4103326587ebce3c2a1898f200ac3e11d641"
    assert eb["package_transport_digest"] == \
        "4e1f5a6f432e6b1df7d062ab878922afa1439834f151c0bbfa214c289c216136"


def test_authority_grants_nothing(art):
    a = art["authority_statement"]
    for kw in ("no production", "portfolio", "model-promotion", "broker-write",
               "trading"):
        assert kw in a


def test_result_blind_no_computed_outcomes(art):
    """The preregistration must embed no computed VS-002 result."""
    blob = json.dumps(art).lower()
    for banned in ("h1_point_estimate", "observed_ic", "cohort_mean_values",
                   "result_metrics", "net_excess_values"):
        assert banned not in blob


# ── G3: witness doc drift corrected to running-max semantics ──────────────
def test_witness_doc_uses_running_max_semantics():
    doc = Path("docs/OUTPUT_ARTIFACT_CONTRACTS.md").read_text(encoding="utf-8")
    assert "running maximum of all prior interval lows" in doc
    assert "current_factor_upper <\n  previous_factor_lower" not in doc
