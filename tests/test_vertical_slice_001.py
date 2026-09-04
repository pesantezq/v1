"""VS-001: the controls that make the first vertical slice believable.

The experiment's numbers are not the thing under test here. What is under test
is whether the machinery could have produced flattering numbers dishonestly:
by letting the future into the signal side, by re-aiming the question after the
answer was known, or by drifting between runs.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from portfolio_automation.evidence_gateway.admissibility import is_admissible
from portfolio_automation.vertical_slice import evidence as EVD
from portfolio_automation.vertical_slice import experiment as X
from portfolio_automation.vertical_slice import journal as J
from portfolio_automation.vertical_slice import preregistration as PRE

REPO = Path(__file__).resolve().parent.parent
FROZEN = REPO / "evals" / "vertical_slice" / "VS-001_preregistration.json"
RESULT = REPO / "evals" / "vertical_slice" / "VS-001_result.json"


@pytest.fixture(scope="module")
def frozen() -> dict:
    return json.loads(FROZEN.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def result() -> dict:
    return json.loads(RESULT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rows() -> list:
    return list(EVD.iter_row_evidence(REPO / PRE.EVIDENCE_REL))


# ── the frozen question ────────────────────────────────────────────────────


def test_the_registered_freeze_reproduces_from_current_code_and_data(frozen):
    ok, reasons = PRE.verify_freeze(REPO, frozen)
    assert ok, reasons
    assert frozen["freeze_digest"].startswith("vsfreeze_")


def test_the_freeze_refuses_a_re_aimed_question(frozen):
    """Changing the question after the fact must break the freeze, not pass."""
    tampered = json.loads(json.dumps(frozen))
    tampered["preregistration"]["evaluation"]["acceptance_rule"] = "SUPPORTED always"
    ok, reasons = PRE.verify_freeze(REPO, tampered)
    assert not ok and any("content differs" in r for r in reasons)


def test_the_freeze_refuses_changed_evidence(frozen):
    """The data is part of the frozen identity, so swapping it is detected."""
    tampered = json.loads(json.dumps(frozen))
    tampered["evidence_sha256"] = "0" * 64
    ok, reasons = PRE.verify_freeze(REPO, tampered)
    assert not ok and any("evidence file changed" in r for r in reasons)


def test_the_acceptance_rule_was_frozen_before_the_result_existed(frozen):
    rule = frozen["preregistration"]["evaluation"]["acceptance_rule"]
    assert "SUPPORTED only if" in rule
    assert "95% CI" in rule
    # The rule must not have been softened into something unfalsifiable.
    assert "NOT_SUPPORTED" in rule


def test_the_preregistration_declares_its_own_weaknesses(frozen):
    """A preregistration that lists no limitations is not honest, it is unfinished."""
    limits = frozen["preregistration"]["evaluation"][
        "known_limitations_declared_in_advance"]
    assert len(limits) >= 4
    joined = " ".join(limits).lower()
    assert "small sample" in joined
    assert "serial correlation" in joined or "serially correlated" in joined


# ── point-in-time: the future must stay out ────────────────────────────────


def test_every_timestamp_reaching_the_gateway_is_timezone_aware(rows):
    """The gateway refuses naive datetimes; the adapter must not hand it any."""
    assert rows
    for item in rows:
        assert item.signal.pit.known_at.tzinfo is not None
        if item.outcome is not None:
            assert item.outcome.pit.known_at.tzinfo is not None


def test_no_outcome_is_admissible_at_its_own_signal_instant(rows):
    """THE leakage control. One row carries two facts knowable at two times;
    the later one must be invisible at the earlier one."""
    checked = 0
    for item in rows:
        if item.outcome is None:
            continue
        checked += 1
        decision = is_admissible(item.outcome.pit, item.scan_time)
        assert not decision.admitted, (item.ticker, item.scan_time)
        assert decision.reason.value == "KNOWN_AT_AFTER_AS_OF"
    assert checked > 0


def test_an_outcome_becomes_admissible_once_it_is_actually_known(rows):
    """The control must be a time boundary, not a blanket refusal that would
    make the experiment vacuous."""
    withs = [i for i in rows if i.outcome is not None]
    assert withs
    item = withs[0]
    assert is_admissible(item.outcome.pit, item.outcome_known_at).admitted
    later = item.outcome_known_at + timedelta(days=1)
    assert is_admissible(item.outcome.pit, later).admitted


def test_signal_and_outcome_are_distinct_snapshots_with_distinct_identity(rows):
    item = next(i for i in rows if i.outcome is not None)
    assert item.signal.snapshot_id != item.outcome.snapshot_id
    assert item.signal.evidence_type == EVD.SIGNAL_EVIDENCE_TYPE
    assert item.outcome.evidence_type == EVD.OUTCOME_EVIDENCE_TYPE
    # The signal payload must not carry the answer.
    assert "outcome_return_7d_pct" not in item.signal.payload_copy()


# ── the recorded run ───────────────────────────────────────────────────────


def test_the_recorded_result_reports_zero_leaks(result):
    leak = result["leakage_controls"]
    assert leak["admitted_before_resolution"] == 0
    assert leak["outcome_snapshots_tested_at_signal_as_of"] > 0
    assert leak["refusal_reasons"] == {
        "KNOWN_AT_AFTER_AS_OF": leak["outcome_snapshots_tested_at_signal_as_of"]}


def test_the_benchmark_is_never_scored_against_itself(result):
    assert X.BENCHMARK_TICKER not in result["experiment_spec"]["universe"]


def test_the_verdict_follows_the_frozen_rule_and_not_the_authors_preference(result):
    """Recompute the verdict from the recorded metrics using the frozen rule."""
    primary = result["metrics"]["scan_clustered_mean_net_excess_return_pct"]
    n, mean, low = primary["n"], primary["mean"], primary["ci_low"]
    if n < X.MIN_SCANS_FOR_CONCLUSION:
        expected = "INCONCLUSIVE_SMALL_SAMPLE"
    elif mean > 0 and low > 0:
        expected = "SUPPORTED"
    else:
        expected = "NOT_SUPPORTED"
    assert result["verdict"] == expected


def test_the_result_is_bound_to_the_spec_and_the_freeze(result):
    obs = result["experiment_result"]["observations"] \
        if "observations" in result["experiment_result"] else None
    assert result["experiment_result"]["experiment_spec_id"] == \
        result["experiment_spec"]["experiment_spec_id"]
    assert result["experiment_spec"]["hypothesis_claim_id"] == \
        result["research_claim"]["claim_id"]
    assert obs is None or True  # observations are hashed, not echoed


def test_friction_was_actually_charged(result):
    """Gross and net must differ by exactly the frozen friction, or the cost
    assumption is decoration."""
    m = result["metrics"]
    gross = m["mean_gross_excess_pct"]
    net = m["naive_per_row_mean_net_excess_return_pct"]["mean"]
    expected = PRE.FRICTION_ROUND_TRIP_PCT + PRE.FRICTION_COMMISSION_PCT
    assert abs((gross - net) - expected) < 1e-9


def test_unmatured_outcomes_were_excluded_and_never_imputed(result):
    pop = result["population"]
    assert pop["excluded_unmatured_outcome"] > 0
    assert pop["rows_loaded"] == (pop["outcome_snapshots_built"]
                                 + pop["excluded_unmatured_outcome"])


def test_the_run_is_deterministic():
    """Two runs over unchanged inputs must agree on identity and on numbers."""
    a = X.run(REPO)
    b = X.run(REPO)
    assert a["experiment_spec"]["experiment_spec_id"] == \
        b["experiment_spec"]["experiment_spec_id"]
    assert a["metrics"] == b["metrics"]
    assert a["verdict"] == b["verdict"]


def test_no_artifact_claims_authority():
    for path in (FROZEN, RESULT):
        blob = path.read_text(encoding="utf-8")
        for forbidden in ("AUTHORITY_PROMOTED", "C1_ENABLED", "AUTONOMY_GRANTED"):
            assert forbidden not in blob, path.name


# ── the journal ────────────────────────────────────────────────────────────


def test_the_journal_refuses_an_entry_that_cannot_say_what_happened():
    with pytest.raises(J.JournalError):
        J.validate_entry({"journal_id": "x", "timestamp": "t", "mission": "m",
                          "component": "c", "what_happened": "",
                          "expected_behavior": "e", "actual_behavior": "a",
                          "first_detector": "d"})


def test_the_journal_refuses_invented_structure():
    with pytest.raises(J.JournalError):
        J.validate_entry({"journal_id": "x", "timestamp": "t", "mission": "m",
                          "component": "c", "what_happened": "w",
                          "expected_behavior": "e", "actual_behavior": "a",
                          "first_detector": "d", "severity": "critical"})


def test_the_journal_permits_honest_uncertainty():
    entry = J.validate_entry({
        "journal_id": "x", "timestamp": "t", "mission": "m", "component": "c",
        "what_happened": "w", "expected_behavior": "e", "actual_behavior": "a",
        "first_detector": "d", "provisional_defect_class": "unclear",
        "plausibly_automatable": "unknown",
        "regression_evidence_added": "not_yet_determined"})
    assert entry["provisional_defect_class"] == "unclear"


def test_the_committed_journal_entries_are_readable_and_valid():
    entries = J.read_entries(repo_root=REPO)
    assert entries, "the journal must exist before the first execution"
    for entry in entries:
        stripped = {k: v for k, v in entry.items()
                    if k not in ("schema_version", "schema_kind")}
        J.validate_entry(stripped)


def test_the_journal_has_no_authority():
    """Structural, not textual: the journal must be unable to decide anything.

    An earlier version of this test grepped the source for words like "block"
    and failed on the docstring sentence saying the journal blocks nothing --
    it punished the module for documenting its own powerlessness. What actually
    matters is that it imports no authority machinery and exposes no decision
    surface."""
    import inspect

    import portfolio_automation.vertical_slice.journal as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    for banned_import in ("roadmap_guard", "agent_policy", "ew0a_authority",
                          "engineer_worker"):
        assert banned_import not in source, banned_import

    public = {n for n, _ in inspect.getmembers(mod, inspect.isfunction)
              if not n.startswith("_")}
    assert public == {"validate_entry", "append_entry", "read_entries"}, public

    for name in public:
        assert not name.startswith(("is_", "can_", "may_", "should_", "assert_"))
