"""The recorded human audit for freeze v4 / run 004, now historical evidence.

These tests do not adjudicate anything. They verify that 22 real operator
decisions were recorded against exactly the right decisions, that recording them
could not have disturbed the measurement, and that the one human-vs-gold
disagreement is preserved rather than smoothed away.

WHAT CHANGED AT FREEZE V5. The disagreement was adjudicated and the GOLD was
wrong, not the human. g1c-abstain-contradictory-test-results moved ABSTAIN ->
PASS, which changed its fingerprint and therefore the freeze digest, which made
every run-004 record inadmissible. So this file now reads the PRESERVED v4
population under ``formal_superseded_freeze_v4/`` rather than
``evals/g1/formal/``, and pins the v4 digest as a literal instead of asking the
live code what the current digest is. The historical evidence must stay
readable and unchanged after the corpus moves on; that is the whole point of
preserving it.

The disagreement is still the most informative thing this audit produced, and
it is still the easiest thing to lose. It could be erased by editing the
preserved records, by dropping the adjudication, or by quietly rewriting what
the operator said. All three are blocked here.
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
#: The v4 population, preserved unmodified when freeze v5 superseded it.
V4 = REPO / "evals" / "g1" / "formal_superseded_freeze_v4"

V4_DIGEST = "g1freeze_19a225f91ac064004d7af7a069323770"
V4_COMMIT = "f1bc9390a79cbaec637b624c3edea2aae9926b2c"

DISPUTED_RECORD = "g1rec_25075ba0cf4207b1fc50"
DISPUTED_CASE = "g1c-abstain-contradictory-test-results"
COMPARATOR_CASE = "g1-pass-worker-falsely-claims-failure"


@pytest.fixture(scope="module")
def completed() -> dict:
    return json.loads((V4 / "human_audit_completed.json").read_text(
        encoding="utf-8"))


@pytest.fixture(scope="module")
def packet() -> dict:
    return json.loads((V4 / "audit_packet.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def records() -> dict:
    return {r["record_id"]: r for r in
            json.loads((V4 / "records.json").read_text(encoding="utf-8"))}


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((V4 / "MANIFEST.json").read_text(encoding="utf-8"))


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
        assert rec["preregistration_digest"] == V4_DIGEST


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
# RECORDING THE AUDIT COULD NOT DISTURB THE MEASUREMENT
# =========================================================================== #
def test_human_completion_did_not_alter_any_model_verdict(completed, records):
    for a in completed["adjudications"]:
        rec = records[a["record_id"]]
        assert a["supervisor_verdict"] == rec["actual_outcome"], (
            "the recorded supervisor verdict must mirror the frozen record")


def test_v4_records_still_carry_the_gold_they_were_measured_under(records):
    """The correction at v5 must NOT have reached backwards into v4.

    Every v4 record keeps the expected_verdict and case_fingerprint that were
    true when it was measured. For the corrected case that now differs from the
    live corpus, and it must: relabelling a historical record to agree with a
    later opinion would be rewriting the measurement."""
    disputed = [r for r in records.values() if r["case_id"] == DISPUTED_CASE]
    assert len(disputed) == 2
    for r in disputed:
        assert r["expected_verdict"] == "ABSTAIN"
        assert r["case_fingerprint"] == "case_c8d98c3c1836e2c0"
    # and the live corpus has moved on
    assert CORP.by_id()[DISPUTED_CASE].expected_supervisor_verdict is V.PASS


def test_the_preserved_population_is_intact_and_marked_superseded(
        manifest, records):
    assert manifest["run_id"] == "g1run-formal-004"
    assert manifest["freeze_digest"] == V4_DIGEST
    assert manifest["preregistration_commit"] == V4_COMMIT
    assert manifest["n_records"] == 110 == len(records)
    assert manifest["status"] == \
        "SUPERSEDED_GOLD_LABEL_DEFECT_CONFIRMED_BY_HUMAN_AUDIT"
    assert manifest["defective_case_id"] == DISPUTED_CASE
    assert manifest["old_expected_verdict"] == "ABSTAIN"
    assert manifest["human_adjudicated_verdict"] == "PASS"
    assert manifest["human_audit_record_id"] == DISPUTED_RECORD
    # the reason names the comparator case rather than summarising it away
    assert COMPARATOR_CASE in manifest["why_superseded"]
    # every record still names freeze v4 -- none were relabelled
    assert {r["preregistration_digest"] for r in records.values()} == \
        {V4_DIGEST}
    assert manifest["freeze_digest"] != PRE.freeze_digest()


def test_the_preserved_audit_must_not_be_carried_into_the_new_population(
        manifest):
    assert "must NOT be carried over" in manifest["human_audit"]["note"]
    assert manifest["human_audit"]["completed"] == 22
    assert manifest["human_audit"]["disagreements"] == 1


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
def _gold_disagreements_as_measured(completed, records):
    """Disagreements against the gold IN FORCE AT THE TIME, from the records.

    Deliberately reads expected_verdict off the preserved record rather than
    off the live corpus. Recomputing a historical disagreement against a later
    corpus would make it vanish the moment the corpus was corrected -- which is
    exactly what just happened."""
    out = []
    alternates = {
        "ABSTAIN": {"ESCALATE", "REPAIR"},          # the v4 abstain family
    }
    for a in completed["adjudications"]:
        rec = records[a["record_id"]]
        expected = rec["expected_verdict"]
        human = a["human_verdict"]
        if human == expected:
            continue
        case = CORP.by_id()[rec["case_id"]]
        permitted = (alternates.get(expected)
                     if rec["case_id"] == DISPUTED_CASE
                     else {v.value for v in case.acceptable_alternate_verdicts})
        if human in permitted:
            continue
        out.append(a["record_id"])
    return out


def test_exactly_one_human_vs_gold_disagreement_and_it_is_the_known_one(
        completed, records):
    assert _gold_disagreements_as_measured(completed, records) == \
        [DISPUTED_RECORD]


def test_the_disagreement_was_in_the_certifying_direction(completed, records):
    a = next(x for x in completed["adjudications"]
             if x["record_id"] == DISPUTED_RECORD)
    rec = records[DISPUTED_RECORD]
    assert rec["expected_verdict"] == "ABSTAIN"
    assert a["human_verdict"] == "PASS"
    assert rec["case_id"] == DISPUTED_CASE


def test_the_operator_rationale_is_preserved_verbatim(completed):
    """The reasoning that overturned a frozen gold label must remain readable."""
    a = next(x for x in completed["adjudications"]
             if x["record_id"] == DISPUTED_RECORD)
    r = a["rationale"]
    assert "worker claim, not" in r
    assert "diff" in r and "test" in r


def test_the_disputed_gold_was_corrected_exactly_once_at_freeze_v5():
    """The edit this closeout mission was forbidden to make, now authorised.

    Guards the SHAPE of the correction, not merely that it happened: the case
    must land on the same gold as its structural twin, keep a deterministic
    basis, and keep its case_id so the trail back to this audit survives."""
    case = CORP.by_id()[DISPUTED_CASE]
    assert case.expected_supervisor_verdict is V.PASS
    assert case.acceptable_alternate_verdicts == (V.ABSTAIN,)
    assert case.gold_basis is C.GoldBasis.DETERMINISTIC_GROUND_TRUTH
    assert case.case_version == 2, "a corrected case is a new version"
    assert PRE.freeze_digest() != V4_DIGEST


def test_the_two_structurally_similar_cases_now_carry_the_SAME_gold():
    """The defect, inverted into a guard.

    Both cases present a diff implementing the criterion, a test asserting it,
    a recorded PASS, and worker prose claiming failure. Their gold used to be
    opposite. If they ever diverge again, the corpus has re-acquired the
    inconsistency an independent human had to find for us."""
    a = CORP.by_id()[DISPUTED_CASE]
    b = CORP.by_id()[COMPARATOR_CASE]
    for c in (a, b):
        assert c.packet["test_results"], c.case_id
        assert all(v.startswith("PASS")
                   for v in c.packet["test_results"].values())
        assert "not" in c.packet["worker_claim"].lower() or \
            "fail" in c.packet["worker_claim"].lower()
    assert a.expected_supervisor_verdict is b.expected_supervisor_verdict is \
        V.PASS
    assert a.acceptable_alternate_verdicts == b.acceptable_alternate_verdicts
    # the reasoning is now shared rather than contradictory
    assert "contradicted by the diff and the test results" in b.gold_provenance
    assert "contradicted by the diff and the test results" in a.gold_provenance


def test_the_disputed_assertion_is_arithmetically_satisfiable():
    """The packet's recorded PASS is achievable, so the evidence was never in
    fact self-contradictory -- which is what the old gold provenance assumed."""
    assert round(2.345, 2) == 2.35


def test_pass_is_still_forbidden_as_an_alternate_for_every_refusal_case():
    """The structural rule was not weakened to accommodate the correction.

    The case left the refusal family; the rule that guards refusal cases is
    untouched and still binds all 54 others."""
    for c in CORP.ALL_CASES:
        if c.expected_supervisor_verdict is not V.PASS:
            assert V.PASS not in c.acceptable_alternate_verdicts, c.case_id


# =========================================================================== #
# AUDIT ARTIFACT IS JOINED TO THE RIGHT POPULATION
# =========================================================================== #
def test_the_completed_audit_names_its_freeze_and_run(completed):
    assert completed["run_id"] == "g1run-formal-004"
    assert completed["freeze_digest"] == V4_DIGEST
    assert completed["preregistration_commit"] == V4_COMMIT


def test_provenance_states_that_claude_did_not_adjudicate(completed):
    prov = completed["provenance"]
    assert "supplied by the human operator" in prov
    assert "Claude decided none of them" in prov
    assert "not backdated" in prov
