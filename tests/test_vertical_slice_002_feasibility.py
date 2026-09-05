"""VS-002: the blocker is measured on every run, not asserted once.

"There is not enough data" decays badly as a claim. Someone backfills a price
archive, the sentence in the report still reads true, and the blocker is never
revisited. So these tests recompute it from the repository. The day real price
history lands, they fail — and that failure is the notification that VS-002 can
proceed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.vertical_slice import feasibility as F

REPO = Path(__file__).resolve().parent.parent
BLOCKED = REPO / "evals" / "vertical_slice" / "VS-002_blocked.json"


@pytest.fixture(scope="module")
def measured() -> F.Feasibility:
    return F.assess(REPO)


@pytest.fixture(scope="module")
def artifact() -> dict:
    return json.loads(BLOCKED.read_text(encoding="utf-8"))


def test_risk_adjustment_is_still_not_credible(measured):
    """The load-bearing claim. If this fails, re-read the blockers: the data
    situation changed and VS-002 may now be executable."""
    assert not measured.risk_adjustment_credible
    assert len(measured.blockers) >= 1


def test_the_artifact_matches_a_fresh_measurement(measured, artifact):
    """The committed artifact must not drift from what the repo actually says."""
    assert artifact["measured_feasibility"] == measured.to_dict()
    assert artifact["verdict"] == "VERTICAL_SLICE_BLOCKED_INSUFFICIENT_RISK_DATA"


def test_there_are_too_few_independent_cohorts(measured):
    """VS-001 reported 20 clusters. Non-overlapping, there are 2."""
    assert measured.non_overlapping_cohorts < F.MIN_INDEPENDENT_COHORTS
    assert measured.non_overlapping_cohorts == 2


def test_the_scored_window_is_far_shorter_than_the_file_suggests(measured):
    """The file spans ~19.5 days; the population that actually has matured 7-day
    outcomes spans ~9.5. VS-001's report said 20-day, which described the file
    rather than the evidence — recorded as DIJ-0007."""
    assert measured.full_file_span_days > 19
    assert measured.scored_span_days < 10
    assert measured.scored_calendar_dates == 11


def test_several_scans_are_near_duplicates_of_each_other(measured):
    """Four scans within 32 minutes share one identical outcome window. They are
    not 'four observations' under any sampling story."""
    clusters = measured.same_day_scan_clusters
    assert clusters, "expected same-day scan clusters"
    tightest = min(c["spread_minutes"] for c in clusters.values())
    assert tightest < 60


def test_risk_estimation_inputs_fail_the_repos_own_gates(measured):
    """Not a threshold invented for this argument — the gates this codebase
    already applies to correlation and volatility."""
    assert measured.joint_return_observations_min < F.MIN_OBS_VOLATILITY
    assert measured.joint_return_observations_min < F.MIN_OBS_CORRELATION


def test_observation_intervals_are_not_comparable(measured):
    """Regressing a mixture of 1-minute and 31-hour returns biases beta toward
    zero regardless of sample size."""
    assert measured.scan_gap_hours_min < 0.1
    assert measured.scan_gap_hours_max > 24
    assert measured.sub_two_hour_gaps > 0


def test_no_durable_price_history_exists(measured):
    """The single hard blocker. portfolio_sim/prices.py documents this archive;
    nothing has ever produced it."""
    assert measured.price_archive_present is False
    assert measured.factor_file_present is False


def test_the_feasibility_probe_computes_no_outcome_statistic():
    """The probe must not be able to answer the VS-002 question early.

    Computing a risk-adjusted result before the question is frozen is the
    contamination this mission exists to avoid, so the capability is absent by
    construction rather than by discipline.

    Structural, not textual. A first version of this test banned the substring
    "outcome_return" and failed because the probe legitimately READS that column
    name to count matured rows — the same brittle-control mistake recorded as
    DIJ-0005, repeated. What matters is that the probe never converts an outcome
    value to a number, and that its public surface is a single measurement
    function."""
    import inspect

    source = Path(F.__file__).read_text(encoding="utf-8")

    # Reading the column name to filter is fine. Parsing its VALUE is not.
    assert 'float(r["outcome_return_7d"' not in source
    assert "float(row[\"outcome_return_7d\"" not in source

    public = {n for n, _ in inspect.getmembers(F, inspect.isfunction)
              if not n.startswith("_") and inspect.getmodule(_) is F}
    assert public == {"assess"}, public

    # Every numeric field the probe returns must be a COUNT or an interval,
    # never a performance statistic.
    # Token equality, not substring: a substring ban on "ic" matches "price",
    # which is the third variant of the DIJ-0005 mistake in this file alone.
    tokens = {t for f in F.Feasibility.__dataclass_fields__ for t in f.split("_")}
    assert tokens.isdisjoint({"excess", "alpha", "sharpe", "beta", "ic"}), tokens


def test_the_blocked_artifact_records_a_real_design_not_a_stub(artifact):
    """A blocked mission still has to prove WHAT was blocked."""
    design = artifact["attempted_design"]
    assert design["status"] == "NOT_EXECUTED_INSUFFICIENT_EVIDENCE"
    assert design["hypotheses"]["H1_portfolio_skill"]
    assert design["hypotheses"]["H2_ranking_skill"]
    # H1 and H2 must be separable — the VS-001 conflation must not recur.
    assert "never collapsed" in design["hypotheses"]["separation_rule"]
    # Every rejected risk model must carry a reason.
    rejected = design["intended_risk_adjustment"]["rejected_alternatives"]
    assert len(rejected) >= 4
    assert all(v.startswith("REJECTED") for v in rejected.values())


def test_acceptance_semantics_separate_criterion_from_conclusion(artifact):
    """VS-001's single word SUPPORTED invited the conflation the operator
    caught. The successor vocabulary must keep them apart."""
    sem = artifact["attempted_design"]["acceptance_semantics"]
    assert "PREREGISTERED_CRITERIA_MET" in sem["criterion_outcomes"]
    assert "ECONOMIC_SKILL_NOT_ESTABLISHED" in sem["skill_outcomes"]
    assert "SUPPORTED" not in sem["criterion_outcomes"]
    assert "H1 AND H2" in sem["binding_rule"]


def test_friction_is_labelled_an_assumption_not_a_measurement(artifact):
    friction = artifact["attempted_design"]["intended_friction"]
    assert friction["status"] == "ASSUMPTION, NOT MEASUREMENT"
    justification = friction["justification_for_reuse"]
    # It must say WHY it remains reasonable, not merely repeat the number.
    assert "NOT reused because it was" in justification
    assert "no measured execution cost" in justification


def test_the_evidence_contract_requires_adjusted_prices(artifact):
    """A split inside a 252-day beta window destroys the estimate, so corporate
    actions become REQUIRED here even though VS-001 could defer them."""
    contract = artifact["minimum_evidence_contract"]
    assert "adj_close" in contract["required_fields_per_bar"]
    assert "REQUIRED" in contract["required_fields_per_bar"]["adj_close"]
    assert "REQUIRED" in contract["corporate_actions"]
    assert "known_at" in contract["required_fields_per_bar"]


def test_the_artifact_grants_no_authority(artifact):
    assert "grants nothing" in artifact["authority_statement"]
    blob = json.dumps(artifact)
    for forbidden in ("AUTHORITY_PROMOTED", "C1_ENABLED", "AUTONOMY_GRANTED"):
        assert forbidden not in blob
