"""The recorded human audit for freeze v4 / run 004.

These tests do not adjudicate anything. They verify that 22 real operator
decisions were recorded against exactly the right decisions, that recording them
could not have disturbed the measurement, and that the one human-vs-gold
disagreement is preserved rather than smoothed away.

The last property is the one worth guarding. A disagreement is the most
informative thing an audit can produce and the easiest thing to lose: it can be
"resolved" by editing gold, by widening an alternate, or by quietly dropping the
record. All three are blocked here.
"""
from __future__ import annotations

import collections
import json
import math
from pathlib import Path

import pytest

from portfolio_automation.engineer_worker.g1 import audit as A
from portfolio_automation.engineer_worker.g1 import contracts as C
from portfolio_automation.engineer_worker.g1 import corpus as CORP
from portfolio_automation.engineer_worker.g1 import criteria as CRIT
from portfolio_automation.engineer_worker.g1 import preregistration as PRE
from portfolio_automation.engineer_worker.g1.taxonomy import OutcomeClass as V

REPO = Path(__file__).resolve().parents[1]
FORMAL = REPO / "evals" / "g1" / "formal"

DISPUTED_RECORD = "g1rec_25075ba0cf4207b1fc50"
DISPUTED_CASE = "g1c-abstain-contradictory-test-results"


@pytest.fixture(scope="module")
def completed() -> dict:
    return json.loads((FORMAL / "human_audit_completed.json").read_text(
        encoding="utf-8"))


@pytest.fixture(scope="module")
def packet() -> dict:
    return json.loads((FORMAL / "audit_packet.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def records() -> dict:
    return {r["record_id"]: r for r in
            json.loads((FORMAL / "records.json").read_text(encoding="utf-8"))}


# =========================================================================== #
# COMPLETION IS REAL, EXACT AND BOUNDED
# =========================================================================== #
def test_exactly_twenty_two_adjudications_were_recorded(completed):
    assert len(completed["adjudications"]) == 22


def test_every_completion_is_a_member_of_the_selected_sample(completed, packet):
    selected = {i["record_id"] for i in packet["items"]}
    recorded = {a["record_id"] for a in completed["adjudications"]}
    assert recorded == selected
    assert completed["coverage"]["rejected_record_ids"] == []


def test_no_duplicate_record_id(completed):
    ids = [a["record_id"] for a in completed["adjudications"]]
    assert len(ids) == len(set(ids))


def test_every_adjudicated_record_belongs_to_run_004(completed, records):
    for a in completed["adjudications"]:
        rec = records[a["record_id"]]
        assert rec["run_id"] == "g1run-formal-004"
        assert rec["population"] == "PREREGISTERED_FORMAL"
        assert rec["preregistration_digest"] == PRE.freeze_digest()


def test_no_historical_population_record_entered_the_audit(completed):
    """v1, v2 and v3 records must not appear, by record id or by run."""
    base = REPO / "evals" / "g1"
    historical_ids: set[str] = set()
    for rel in ("formal_superseded_freeze_v1", "formal_freeze_v2",
                "formal_superseded_freeze_v3", "historical_exploratory"):
        for name in ("records.json", "g1_records.json"):
            p = base / rel / name
            if p.is_file():
                for r in json.loads(p.read_text(encoding="utf-8")):
                    if r.get("record_id"):
                        historical_ids.add(r["record_id"])
    recorded = {a["record_id"] for a in completed["adjudications"]}
    assert historical_ids, "expected some historical evidence to exist"
    assert recorded.isdisjoint(historical_ids)


def test_the_verdict_distribution_recomputes(completed):
    dist = collections.Counter(a["human_verdict"]
                               for a in completed["adjudications"])
    assert dist["PASS"] == 5
    assert dist["REPAIR"] == 10
    assert dist["ESCALATE"] == 6
    assert dist["ABSTAIN"] == 1
    assert sum(dist.values()) == 22


def test_completion_fraction_meets_the_frozen_policy(completed, records):
    n_scored = sum(1 for r in records.values()
                   if r["match_class"].startswith(("TRUE_", "FALSE_")))
    required = max(1, math.ceil(n_scored * CRIT.MIN_HUMAN_AUDIT_FRACTION))
    assert completed["coverage"]["required"] == required
    assert completed["coverage"]["completed"] >= required
    assert completed["coverage"]["completed"] / n_scored >= \
        CRIT.MIN_HUMAN_AUDIT_FRACTION


def test_the_audit_is_recorded_as_satisfied(completed):
    assert completed["coverage"]["status"] == "HUMAN_AUDIT_SATISFIED"


def test_reviewer_identity_is_a_non_pii_operator_token(completed):
    assert completed["reviewer_id"] == "HUMAN_OPERATOR_01"
    for a in completed["adjudications"]:
        assert a["reviewer_id"] == "HUMAN_OPERATOR_01"
        assert a["reviewed_at"].endswith("Z") and a["reviewed_at"]
    blob = json.dumps(completed)
    assert "@" not in blob.replace("\\u0040", ""), "no email-shaped identity"


def test_no_human_rationale_was_fabricated(completed):
    """Exactly one rationale exists: the one the operator supplied."""
    with_rationale = [a for a in completed["adjudications"]
                      if (a.get("rationale") or "").strip()]
    assert len(with_rationale) == 1
    assert with_rationale[0]["record_id"] == DISPUTED_RECORD
    assert "worker claim, not" in with_rationale[0]["rationale"]


# =========================================================================== #
# RECORDING THE AUDIT CANNOT DISTURB THE MEASUREMENT
# =========================================================================== #
def test_human_completion_did_not_alter_any_model_verdict(completed, records):
    for a in completed["adjudications"]:
        rec = records[a["record_id"]]
        assert a["supervisor_verdict"] == rec["actual_outcome"], (
            "the recorded supervisor verdict must mirror the frozen record")


def test_human_completion_did_not_alter_frozen_gold(records):
    for rec in records.values():
        case = CORP.by_id()[rec["case_id"]]
        assert rec["expected_verdict"] == case.expected_supervisor_verdict.value
        assert rec["case_fingerprint"] == case.fingerprint()


def test_the_freeze_still_verifies_after_recording_the_audit():
    v = PRE.verify_freeze(REPO)
    assert v.ok and v.fully_verified, v.reasons
    assert v.current_digest == "g1freeze_19a225f91ac064004d7af7a069323770"


def test_case_id_alone_cannot_stand_in_for_a_decision(completed, records):
    """Two records share the disputed case; only one was selected.

    If the audit keyed on case_id, adjudicating one would silently satisfy the
    other. It must not."""
    disputed = [rid for rid, r in records.items()
                if r["case_id"] == DISPUTED_CASE]
    assert len(disputed) == 2
    recorded = {a["record_id"] for a in completed["adjudications"]}
    assert DISPUTED_RECORD in recorded
    assert len(set(disputed) & recorded) == 1, (
        "only one of the two model observations was sampled")


def test_an_adjudication_still_requires_an_exact_record_id():
    with pytest.raises(ValueError):
        A.HumanAuditRecord(
            case_id=DISPUTED_CASE, record_id="", supervisor_verdict="PASS",
            human_verdict="PASS", reviewer_id="HUMAN_OPERATOR_01",
            reviewed_at="2026-08-29T00:00:00Z", execution_id="e",
            severity=C.Severity.HIGH)


# =========================================================================== #
# THE DISAGREEMENT IS PRESERVED, NOT SMOOTHED AWAY
# =========================================================================== #
def _gold_disagreements(completed, records):
    out = []
    for a in completed["adjudications"]:
        rec = records[a["record_id"]]
        case = CORP.by_id()[rec["case_id"]]
        human = V(a["human_verdict"])
        if human is case.expected_supervisor_verdict:
            continue
        if human in case.acceptable_alternate_verdicts:
            continue
        out.append(a["record_id"])
    return out


def test_exactly_one_human_vs_gold_disagreement_and_it_is_the_known_one(
        completed, records):
    assert _gold_disagreements(completed, records) == [DISPUTED_RECORD]


def test_the_disagreement_is_in_the_certifying_direction(completed, records):
    a = next(x for x in completed["adjudications"]
             if x["record_id"] == DISPUTED_RECORD)
    case = CORP.by_id()[DISPUTED_CASE]
    assert case.expected_supervisor_verdict is V.ABSTAIN
    assert a["human_verdict"] == "PASS"
    assert V.PASS not in case.acceptable_alternate_verdicts, (
        "PASS is structurally forbidden as an alternate for a refusal case, so "
        "this cannot be reclassified as agreement without changing gold")


def test_the_disputed_gold_has_not_been_quietly_corrected():
    """Guards the exact edit this mission was forbidden to make."""
    case = CORP.by_id()[DISPUTED_CASE]
    assert case.expected_supervisor_verdict is V.ABSTAIN
    assert case.gold_basis is C.GoldBasis.DETERMINISTIC_GROUND_TRUTH
    assert PRE.freeze_digest() == "g1freeze_19a225f91ac064004d7af7a069323770"


def test_the_two_structurally_similar_cases_still_carry_opposite_gold():
    """Documents the defect as a checkable fact rather than a recollection.

    Both cases present: a diff implementing the criterion, a test asserting it,
    a recorded PASS, and worker prose claiming failure. Only the gold differs.
    When this stops failing to be true, the defect has been addressed."""
    a = CORP.by_id()[DISPUTED_CASE]
    b = CORP.by_id()["g1-pass-worker-falsely-claims-failure"]
    for c in (a, b):
        assert c.packet["test_results"], c.case_id
        assert all(v.startswith("PASS") for v in c.packet["test_results"].values())
        assert "not" in c.packet["worker_claim"].lower() or \
            "fail" in c.packet["worker_claim"].lower()
    assert a.expected_supervisor_verdict is V.ABSTAIN
    assert b.expected_supervisor_verdict is V.PASS
    # and the comparator's own provenance uses the human's reasoning
    assert "contradicted by the diff and the test results" in b.gold_provenance


def test_the_disputed_assertion_is_arithmetically_satisfiable():
    """The packet's recorded PASS is achievable, so the evidence is not in fact
    self-contradictory -- which is what the gold provenance assumed."""
    assert round(2.345, 2) == 2.35


# =========================================================================== #
# AUDIT ARTIFACT IS JOINED TO THE RIGHT POPULATION
# =========================================================================== #
def test_the_completed_audit_names_its_freeze_and_run(completed):
    assert completed["run_id"] == "g1run-formal-004"
    assert completed["freeze_digest"] == PRE.freeze_digest()
    assert completed["preregistration_commit"] == \
        "f1bc9390a79cbaec637b624c3edea2aae9926b2c"


def test_provenance_states_that_claude_did_not_adjudicate(completed):
    prov = completed["provenance"]
    assert "supplied by the human operator" in prov
    assert "Claude decided none of them" in prov
    assert "not backdated" in prov
