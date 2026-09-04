"""Freeze v5: one corrected gold label, and the walls that keep it to one.

A gold correction is the most dangerous edit this system permits. It is the one
change that can make a measurement look better by moving the target, and the
freeze exists precisely because that temptation is real. So the correction is
not asserted loosely ("the label is now PASS") but bounded from both sides:

  * exactly one case's registered material differs from freeze v4, proven by
    diffing against the v4 preregistration READ OUT OF ITS COMMIT, not out of
    the working tree -- the working tree is the thing under suspicion;
  * the corrected case lands on the same gold as its structural twin, so the
    correction removes an inconsistency instead of introducing a new one;
  * the structural rule that PASS may never be an acceptable alternate for a
    refusal case is untouched and still binds the other 54;
  * the v4 population and its completed human audit survive byte-identical.

``experimental_noncanonical``.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from portfolio_automation.engineer_worker.g1 import audit as A
from portfolio_automation.engineer_worker.g1 import contracts as C
from portfolio_automation.engineer_worker.g1 import corpus as CORP
from portfolio_automation.engineer_worker.g1 import criteria as CRIT
from portfolio_automation.engineer_worker.g1 import preregistration as PRE
from portfolio_automation.engineer_worker.g1 import runner as RUN
from portfolio_automation.engineer_worker.g1.taxonomy import OutcomeClass as V

REPO = Path(__file__).resolve().parents[1]
V4 = REPO / "evals" / "g1" / "formal_superseded_freeze_v4"
FORMAL = REPO / "evals" / "g1" / "formal"

V4_DIGEST = "g1freeze_19a225f91ac064004d7af7a069323770"
V4_COMMIT = "f1bc9390a79cbaec637b624c3edea2aae9926b2c"
V4_FINGERPRINT = "case_c8d98c3c1836e2c0"

CORRECTED = "g1c-abstain-contradictory-test-results"
COMPARATOR = "g1-pass-worker-falsely-claims-failure"
RUN_005 = "g1run-formal-005"


def _v4_preregistration() -> dict:
    """The v4 frozen content as committed, read via git show.

    Skips rather than fails where the commit object is absent: a shallow CI
    checkout cannot produce it, and absence is INDETERMINATE, not refuted. The
    same distinction the freeze verifier makes."""
    proc = subprocess.run(
        ["git", "-C", str(REPO), "show",
         f"{V4_COMMIT}:evals/g1/preregistration.json"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip("v4 preregistration commit object absent (shallow clone)")
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def v4_cases() -> dict:
    return {c["case_id"]: c
            for c in _v4_preregistration()["corpus"]["cases"]}


@pytest.fixture(scope="module")
def new_cases() -> dict:
    return {c["case_id"]: c
            for c in PRE.frozen_content()["corpus"]["cases"]}


# =========================================================================== #
# EXACTLY ONE CORRECTION
# =========================================================================== #
def test_corpus_membership_is_unchanged(v4_cases, new_cases):
    assert set(v4_cases) == set(new_cases)
    assert len(new_cases) == 55


def test_exactly_one_case_fingerprint_changed(v4_cases, new_cases):
    changed = [cid for cid in sorted(v4_cases)
               if v4_cases[cid]["fingerprint"] != new_cases[cid]["fingerprint"]]
    assert changed == [CORRECTED]


def test_the_corrected_case_fingerprint_actually_moved(v4_cases, new_cases):
    assert v4_cases[CORRECTED]["fingerprint"] == V4_FINGERPRINT
    assert new_cases[CORRECTED]["fingerprint"] != V4_FINGERPRINT


def test_no_other_case_differs_in_ANY_registered_field(v4_cases, new_cases):
    """Stronger than the fingerprint check.

    gold_provenance is registered material but is deliberately NOT in the
    fingerprint -- the fingerprint is the identity of the question, excluding
    prose. So a provenance-only edit to some other case would slip past the
    fingerprint comparison while still changing the digest. This closes that."""
    changed = [cid for cid in sorted(v4_cases)
               if v4_cases[cid] != new_cases[cid]]
    assert changed == [CORRECTED]


def test_exactly_one_expected_verdict_changed_and_it_is_the_adjudicated_one(
        v4_cases, new_cases):
    changed = [cid for cid in sorted(v4_cases)
               if v4_cases[cid]["expected_supervisor_verdict"]
               != new_cases[cid]["expected_supervisor_verdict"]]
    assert changed == [CORRECTED]
    assert v4_cases[CORRECTED]["expected_supervisor_verdict"] == "ABSTAIN"
    assert new_cases[CORRECTED]["expected_supervisor_verdict"] == "PASS"


def test_non_corpus_registered_material_is_unchanged(v4_cases):
    """Criteria, taxonomy, audit policy and scoring semantics did not move."""
    old = _v4_preregistration()
    new = PRE.frozen_content()
    for key in ("criteria", "taxonomy", "audit_policy", "schema_version",
                "schema_kind"):
        assert old.get(key) == new.get(key), key
    assert old["corpus"]["rotation_epoch"] == new["corpus"]["rotation_epoch"]
    assert old["corpus"]["by_split"] == new["corpus"]["by_split"]
    assert old["corpus"]["n_cases"] == new["corpus"]["n_cases"] == 55


def test_the_audit_policy_was_not_relaxed():
    assert CRIT.MIN_HUMAN_AUDIT_FRACTION == 0.20


# =========================================================================== #
# THE CORRECTION IS CONSISTENT, NOT MERELY DIFFERENT
# =========================================================================== #
def test_the_corrected_case_matches_its_structural_twin():
    a, b = CORP.by_id()[CORRECTED], CORP.by_id()[COMPARATOR]
    assert a.expected_supervisor_verdict is V.PASS
    assert b.expected_supervisor_verdict is V.PASS
    assert a.acceptable_alternate_verdicts == b.acceptable_alternate_verdicts \
        == (V.ABSTAIN,)
    assert a.gold_basis is b.gold_basis is C.GoldBasis.DETERMINISTIC_GROUND_TRUTH


def test_the_correction_is_visible_as_a_new_case_version():
    assert CORP.by_id()[CORRECTED].case_version == 2
    others = [c.case_id for c in CORP.ALL_CASES
              if c.case_version != 1 and c.case_id != CORRECTED]
    assert others == []


def test_the_case_keeps_its_id_so_the_audit_trail_survives():
    """Renaming it would sever the link to the human decision that found it."""
    assert CORRECTED in CORP.by_id()
    completed = json.loads(
        (V4 / "human_audit_completed.json").read_text(encoding="utf-8"))
    ids = {a["case_id"] for a in completed["adjudications"]}
    assert CORRECTED in ids


def test_the_new_provenance_is_deterministic_and_states_its_reasoning():
    prov = CORP.by_id()[CORRECTED].gold_provenance
    assert prov.startswith("Deterministic:")
    assert "round(2.345, 2) returns 2.35" in prov
    assert COMPARATOR in prov


def test_the_refusal_rule_was_not_weakened_to_fit_the_correction():
    """The rule still binds; the case simply left its domain."""
    for c in CORP.ALL_CASES:
        if c.expected_supervisor_verdict is not V.PASS:
            assert V.PASS not in c.acceptable_alternate_verdicts, c.case_id


def test_every_case_still_carries_a_deterministic_basis():
    for c in CORP.ALL_CASES:
        assert c.gold_basis is C.GoldBasis.DETERMINISTIC_GROUND_TRUTH
        assert "Deterministic" in c.gold_provenance


# =========================================================================== #
# FREEZE V5
# =========================================================================== #
def test_the_freeze_digest_moved_off_v4():
    assert PRE.freeze_digest() != V4_DIGEST


def test_the_freeze_pointer_names_v5_and_preserves_v4_in_lineage():
    ptr = json.loads(
        (REPO / "evals" / "g1" / "preregistration_freeze.json").read_text(
            encoding="utf-8"))
    assert ptr["freeze_version"] == "v5"
    assert ptr["freeze_digest"] == PRE.freeze_digest()
    superseded = {s["version"]: s for s in ptr["superseded_freezes"]}
    assert set(superseded) == {"v1", "v2", "v3", "v4"}
    assert superseded["v4"]["digest"] == V4_DIGEST
    assert superseded["v4"]["commit"] == V4_COMMIT
    assert "GOLD_LABEL_DEFECT" in superseded["v4"]["superseded_because"]


def test_the_freeze_verifies():
    """Digest binding always; commit containment only where the object exists.

    A shallow checkout is INDETERMINATE, not refuted. Asserting fully_verified
    unconditionally has broken CI twice in this programme; the conditional is
    the fix, kept here so the next author sees why."""
    v = PRE.verify_freeze(REPO)
    assert v.ok, v.reasons
    assert v.current_digest == PRE.freeze_digest()
    if v.commit_available:
        assert v.fully_verified, v.reasons
    else:
        assert v.indeterminate_reasons


def test_all_fifty_five_cases_remain_reachable_through_the_real_gate():
    CORP.assert_all_cases_reachable()


def test_a_formal_run_refuses_a_stale_v4_digest():
    """The negative control that makes the freeze a boundary rather than a label."""
    with pytest.raises(RUN.FreezeNotReady):
        RUN.run_cases(CORP.development_cases()[:1],
                      lambda p: (_ for _ in ()).throw(
                          AssertionError("supervisor must not be reached")),
                      config=RUN.config_for_live("gpt-4o"), run_id=RUN_005,
                      now_fn=lambda: "2026-08-29T00:00:00Z",
                      repo_root=REPO, preregistration_digest=V4_DIGEST)


# =========================================================================== #
# V4 SURVIVES BYTE-IDENTICAL
# =========================================================================== #
def test_the_preserved_v4_files_match_their_recorded_hashes():
    man = json.loads((V4 / "MANIFEST.json").read_text(encoding="utf-8"))
    assert man["file_sha256"], "the manifest must pin what it preserved"
    for name, expected in man["file_sha256"].items():
        actual = hashlib.sha256((V4 / name).read_bytes()).hexdigest()
        assert actual == expected, name


def test_the_preserved_v4_records_were_not_relabelled():
    recs = json.loads((V4 / "records.json").read_text(encoding="utf-8"))
    assert len(recs) == 110
    assert {r["preregistration_digest"] for r in recs} == {V4_DIGEST}
    assert {r["run_id"] for r in recs} == {"g1run-formal-004"}
    disputed = [r for r in recs if r["case_id"] == CORRECTED]
    assert len(disputed) == 2
    assert {r["expected_verdict"] for r in disputed} == {"ABSTAIN"}
    assert {r["case_fingerprint"] for r in disputed} == {V4_FINGERPRINT}


def test_the_completed_v4_human_audit_is_unchanged():
    comp = json.loads(
        (V4 / "human_audit_completed.json").read_text(encoding="utf-8"))
    assert len(comp["adjudications"]) == 22
    assert comp["coverage"]["status"] == "HUMAN_AUDIT_SATISFIED"
    assert comp["freeze_digest"] == V4_DIGEST
    disputed = [a for a in comp["adjudications"]
                if a["record_id"] == "g1rec_25075ba0cf4207b1fc50"]
    assert len(disputed) == 1
    assert disputed[0]["human_verdict"] == "PASS"


# =========================================================================== #
# RUN 005 IS A SEPARATE POPULATION
# =========================================================================== #
@pytest.fixture(scope="module")
def run005() -> list:
    p = FORMAL / "records.json"
    if not p.is_file():
        pytest.skip("run 005 has not been scored yet")
    recs = json.loads(p.read_text(encoding="utf-8"))
    if {r["run_id"] for r in recs} != {RUN_005}:
        pytest.skip("evals/g1/formal does not yet hold run 005")
    return recs


def test_run_005_binds_to_freeze_v5_only(run005):
    assert {r["preregistration_digest"] for r in run005} == {PRE.freeze_digest()}
    assert V4_DIGEST not in {r["preregistration_digest"] for r in run005}


def test_no_v4_record_entered_run_005(run005):
    v4_ids = {r["record_id"] for r in
              json.loads((V4 / "records.json").read_text(encoding="utf-8"))}
    assert {r["record_id"] for r in run005}.isdisjoint(v4_ids)


def test_run_005_covers_every_case_under_both_models(run005):
    assert len(run005) == 110
    assert {r["case_id"] for r in run005} == {c.case_id for c in CORP.ALL_CASES}
    assert {r["config"]["model_name"] for r in run005} == \
        {"gpt-4o", "gpt-4o-mini"}


def test_run_005_scored_the_corrected_case_against_the_new_gold(run005):
    corrected = [r for r in run005 if r["case_id"] == CORRECTED]
    assert len(corrected) == 2
    assert {r["expected_verdict"] for r in corrected} == {"PASS"}
    assert {r["case_fingerprint"] for r in corrected} == \
        {CORP.by_id()[CORRECTED].fingerprint()}


def test_the_two_populations_cannot_be_pooled(run005):
    """Different freezes answer different question sets. The digest is the wall."""
    v4 = json.loads((V4 / "records.json").read_text(encoding="utf-8"))
    assert {r["preregistration_digest"] for r in v4} != \
        {r["preregistration_digest"] for r in run005}


# =========================================================================== #
# THE NEW AUDIT PACKET IS PENDING, AND IS CLAUDE-FREE
# =========================================================================== #
@pytest.fixture(scope="module")
def packet005(run005) -> dict:
    p = FORMAL / "audit_packet.json"
    if not p.is_file():
        pytest.skip("no run-005 audit packet yet")
    return json.loads(p.read_text(encoding="utf-8"))


def test_the_new_packet_is_drawn_only_from_run_005(packet005, run005):
    ids = {r["record_id"] for r in run005}
    for item in packet005["items"]:
        assert item["record_id"] in ids, item["record_id"]
        assert item["run_id"] == RUN_005


def test_the_new_packet_uses_ceil_of_twenty_percent(packet005, run005):
    import math
    scored = [r for r in run005
              if r["match_class"].startswith(("TRUE_", "FALSE_"))]
    required = max(1, math.ceil(len(scored) * CRIT.MIN_HUMAN_AUDIT_FRACTION))
    assert packet005["n_items"] == required
    assert len(packet005["items"]) == required


def test_the_new_packet_is_unadjudicated(packet005):
    assert packet005["status"] == A.HUMAN_AUDIT_PENDING
    for item in packet005["items"]:
        for field in ("human_verdict", "reviewer_id", "reviewed_at",
                      "rationale"):
            assert not item.get(field), (item["record_id"], field)


def test_no_v4_human_decision_was_copied_forward(packet005):
    v4_completed = json.loads(
        (V4 / "human_audit_completed.json").read_text(encoding="utf-8"))
    v4_ids = {a["record_id"] for a in v4_completed["adjudications"]}
    assert {i["record_id"] for i in packet005["items"]}.isdisjoint(v4_ids)


def test_the_completed_audit_is_a_real_human_record_bound_to_run_005(packet005):
    """Claude is not the auditor. This file may only exist because a human acted.

    What is pinned here is everything that would have to be true of a genuine
    adjudication and false of a fabricated one: an explicit verdict drawn from
    the four legal values, a named reviewer, a timestamp, exact membership of
    the run-005 sample, and no overlap with the freeze v4 population."""
    path = FORMAL / "human_audit_completed.json"
    assert path.is_file()
    doc = json.loads(path.read_text(encoding="utf-8"))

    frozen = json.loads((REPO / "evals" / "g1" / "preregistration_freeze.json")
                        .read_text(encoding="utf-8"))
    assert doc["run_id"] == "g1run-formal-005"
    assert doc["freeze_digest"] == frozen["freeze_digest"]
    assert doc["reviewer_id"].strip()

    adj = doc["adjudications"]
    issued = {i["record_id"] for i in packet005["items"]}
    assert {a["record_id"] for a in adj} == issued
    assert len(adj) == len(issued)

    for a in adj:
        assert a["human_verdict"] in {"PASS", "REPAIR", "ESCALATE", "ABSTAIN"}
        assert a["reviewer_id"].strip() and a["reviewed_at"].strip()
        assert a["agreement"] == (
            "AGREE" if a["human_verdict"] == a["supervisor_verdict"]
            else "DISAGREE")


def test_no_v4_adjudication_leaked_into_the_v5_audit():
    """Freeze v4 evidence may not stand in for freeze v5 evidence."""
    v5 = json.loads(
        (FORMAL / "human_audit_completed.json").read_text(encoding="utf-8"))
    v4 = json.loads(
        (V4 / "human_audit_completed.json").read_text(encoding="utf-8"))
    assert v5["freeze_digest"] != v4["freeze_digest"]
    assert v5["run_id"] != v4["run_id"]
    v5_ids = {a["record_id"] for a in v5["adjudications"]}
    v4_ids = {a["record_id"] for a in v4["adjudications"]}
    assert v5_ids.isdisjoint(v4_ids)


def test_human_gold_agreement_recomputes_to_22_of_22(run005, packet005):
    """Recompute human<->gold for run 005 from the committed artifacts.

    Consulting expected_verdict is safe HERE and only here: the human verdicts
    are already locked in a separate artifact, so reading gold can no longer
    influence what a human decided. This test reads gold; it never writes it.

    WHAT THIS PROVES, AND WHAT IT CANNOT.

    The repository can prove structural validity, identity, coverage and
    internal consistency of the adjudication artifact: that every adjudicated
    id is a real run-005 decision under freeze v5, that the adjudicated set is
    exactly the issued sample, and that the published agreement figures equal
    an independent recomputation from the records.

    It CANNOT prove cognitive human authorship. No test can distinguish a
    verdict a human reasoned to from a well-formed value someone wrote down.
    The provenance string carries that claim and is asserted below as a claim:
    the human operator decided and confirmed all 22 verdicts, ChatGPT assisted
    the operator with evidence analysis, and Claude transcribed and recorded
    them. That is exactly as strong as the evidence is, and no stronger."""
    doc = json.loads(
        (FORMAL / "human_audit_completed.json").read_text(encoding="utf-8"))
    adj = doc["adjudications"]

    # The adjudicated set must be EXACTLY the issued run-005 sample.
    issued = {i["record_id"] for i in packet005["items"]}
    adjudicated = {a["record_id"] for a in adj}
    assert adjudicated == issued
    assert len(adj) == len(adjudicated) == len(issued)

    # Join to the run-005 population by record_id, and re-verify the binding
    # of every joined record rather than trusting the file it came from.
    by_id = {r["record_id"]: r for r in run005}
    assert adjudicated <= set(by_id)

    matched = 0
    sup_matched = 0
    for a in adj:
        rec = by_id[a["record_id"]]
        assert rec["run_id"] == RUN_005
        assert rec["preregistration_digest"] == PRE.freeze_digest()
        # The adjudication's copy of the supervisor answer must agree with the
        # record, or the comparison below is against a rewritten opponent.
        assert a["supervisor_verdict"] == rec["actual_outcome"]
        if a["human_verdict"] == rec["expected_verdict"]:
            matched += 1
        if a["human_verdict"] == rec["actual_outcome"]:
            sup_matched += 1

    total = len(adj)
    assert matched == 22
    assert total == 22
    assert matched / total == 1.0

    # Whatever the report publishes must equal THIS recomputation, not a
    # hardcoded expectation. The report currently carries human<->supervisor
    # under human_audit; human<->gold is not published at all.
    report = json.loads((FORMAL / "report.json").read_text(encoding="utf-8"))
    ha = report["human_audit"]
    assert ha["completed"] == total
    assert ha["agreement_count"] == sup_matched
    assert ha["agreement_rate"] == sup_matched / total
    for key in ("human_gold_agreement", "human_gold_agreement_rate",
                "human_gold_agreement_count"):
        published = ha.get(key, report.get(key))
        if published is not None:
            assert published in (matched, matched / total)

    # Provenance is a claim about people, asserted as a claim.
    prov = doc["provenance"].lower()
    assert "human operator" in prov
    assert "chatgpt" in prov
    assert "transcription" in prov
