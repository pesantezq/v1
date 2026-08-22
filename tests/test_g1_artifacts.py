"""The committed FORMAL G1 artifacts must stay internally consistent.

These tests re-derive, using today's code, what the committed report claims.
If the taxonomy, a denominator, the audit sampler or the status logic changes,
this fails — which is correct, because the stored numbers would no longer mean
what they say.

They do not re-run the supervisor. Live results are not reproducible and this
suite must stay hermetic.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.engineer_worker.g1 import audit as A
from portfolio_automation.engineer_worker.g1 import contracts as C
from portfolio_automation.engineer_worker.g1 import corpus as CORP
from portfolio_automation.engineer_worker.g1 import criteria as CRIT
from portfolio_automation.engineer_worker.g1 import metrics as M
from portfolio_automation.engineer_worker.g1 import preregistration as PRE
from portfolio_automation.engineer_worker.g1 import report as R
from portfolio_automation.engineer_worker.g1 import taxonomy as T

REPO = Path(__file__).resolve().parents[1]
FORMAL = REPO / "evals" / "g1" / "formal"


@pytest.fixture(scope="module")
def report() -> dict:
    return json.loads((FORMAL / "report.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def raw_records() -> list[dict]:
    return json.loads((FORMAL / "records.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def records(raw_records) -> list[C.SupervisorEvaluationRecordV0]:
    out = []
    for d in raw_records:
        cfg = d["config"]
        out.append(C.SupervisorEvaluationRecordV0(
            case_id=d["case_id"], case_fingerprint=d["case_fingerprint"],
            expected_verdict=T.OutcomeClass(d["expected_verdict"]),
            actual_outcome=T.OutcomeClass(d["actual_outcome"]),
            match_class=C.MatchClass(d["match_class"]),
            severity=C.Severity(d["severity"]), split=C.Split(d["split"]),
            gold_basis=C.GoldBasis(d["gold_basis"]),
            execution_identity=d["execution_identity"],
            config=C.MeasurementConfig(
                model_provider=cfg["model_provider"],
                model_name=cfg["model_name"],
                prompt_version=cfg["prompt_version"],
                instruction_version=cfg["instruction_version"],
                toolset_id=cfg["toolset_id"]),
            candidate_sha=d["candidate_sha"],
            served_model_version=d["served_model_version"],
            supervisor_reasons=tuple(d["supervisor_reasons"]),
            supervisor_error=d["supervisor_error"],
            latency_ms=d["latency_ms"], recorded_at=d["recorded_at"],
            protected_high_impact=d["protected_high_impact"],
            run_id=d["run_id"],
            population=C.RunPopulation(d["population"]),
            preregistration_digest=d["preregistration_digest"]))
    return out


def test_the_formal_artifacts_exist():
    for name in ("report.json", "records.json", "per_model.json",
                 "audit_packet.json"):
        assert (FORMAL / name).is_file(), name


# =========================================================================== #
# PREREGISTRATION BINDING
# =========================================================================== #
def test_the_formal_run_is_bound_to_a_verified_freeze(report):
    """The binding must hold in any checkout.

    The commit-level proof needs the freeze commit's object, which a shallow
    CI clone does not carry. That is indeterminate, not refuted -- so this
    asserts the digest binding unconditionally and the commit proof only where
    it can be attempted."""
    pre = report["preregistration"]
    v = PRE.verify_freeze(REPO)
    assert v.ok, v.reasons
    assert pre["freeze_digest"] == v.current_digest
    assert pre["preregistration_commit"] == v.recorded_commit
    assert pre["population"] == "PREREGISTERED_FORMAL"
    if v.commit_available:
        assert v.fully_verified, v.reasons
    else:
        assert v.indeterminate_reasons


def test_every_record_carries_the_verified_freeze_digest(records):
    expected = PRE.freeze_digest()
    for r in records:
        assert r.population is C.RunPopulation.PREREGISTERED_FORMAL
        assert r.preregistration_digest == expected, (
            f"{r.case_id} was measured against a different freeze")


def test_the_formal_run_has_exactly_one_run_id(records):
    assert len({r.run_id for r in records}) == 1
    assert next(iter({r.run_id for r in records})).startswith("g1run-formal-")


def test_no_exploratory_record_leaked_into_the_formal_population(records):
    assert all(r.population is C.RunPopulation.PREREGISTERED_FORMAL
               for r in records)


def test_the_superseded_and_historical_populations_are_kept_separate():
    hist = REPO / "evals" / "g1" / "historical_exploratory"
    sup = REPO / "evals" / "g1" / "formal_superseded_freeze_v1"
    assert (hist / "MANIFEST.json").is_file()
    assert (sup / "MANIFEST.json").is_file()
    supman = json.loads((sup / "MANIFEST.json").read_text(encoding="utf-8"))
    assert supman["status"] == "SUPERSEDED_BY_AUDIT_POLICY_REFREEZE"
    assert supman["not_combinable_with"] == "evals/g1/formal/"
    histman = json.loads((hist / "MANIFEST.json").read_text(encoding="utf-8"))
    assert histman["population"] == "EXPLORATORY_HISTORICAL"


# =========================================================================== #
# CONFIGURATION JOIN  (the defect that broke attribution)
# =========================================================================== #
def test_every_report_configuration_joins_back_to_records(report, records):
    reported = {c["config_id"] for c in report["configurations"]}
    from_records = {r.config.config_id() for r in records}
    assert reported == from_records, (
        f"report lists {reported - from_records} that match no record, and "
        f"records carry {from_records - reported} that the report omits")
    assert report["configuration_record_join_verified"] is True


def test_configuration_identity_excludes_the_served_build(records):
    """A config whose id changes after it runs cannot join to its own records."""
    assert "served_model_version" not in C.MeasurementConfig.__dataclass_fields__
    for r in records:
        assert r.config.config_id().startswith("g1cfg_")
        assert r.served_model_version.startswith(r.config.model_name)
        assert r.served_model_version != r.config.model_name, (
            "the served build should be more specific than the requested name")


def test_more_than_one_configuration_actually_ran(report, records):
    assert len({c["config_id"] for c in report["configurations"]}) >= 2
    assert len({r.config.model_name for r in records}) >= 2


# =========================================================================== #
# REPRODUCIBILITY
# =========================================================================== #
def test_every_stored_record_reclassifies_identically(records):
    cases = CORP.by_id()
    for r in records:
        case = cases.get(r.case_id)
        assert case is not None, f"{r.case_id} is no longer in the corpus"
        assert C.classify(case, r.actual_outcome) is r.match_class, r.case_id


def test_stored_records_still_match_their_case_fingerprints(records):
    cases = CORP.by_id()
    for r in records:
        assert cases[r.case_id].fingerprint() == r.case_fingerprint


def test_the_reported_metrics_are_reproducible(records, report):
    got = M.compute_metrics(records, CORP.by_id()).to_dict()
    want = report["metrics"]
    for key in ("n_total", "n_scored", "n_excluded", "false_pass_count",
                "false_fail_count", "by_match_class", "n_by_execution_id",
                "false_pass_rate", "exact_accuracy"):
        assert got[key] == want[key], key


def test_the_reported_status_is_still_what_the_evidence_implies(records, report):
    m = M.compute_metrics(records, CORP.by_id())
    sample = A.select_audit_sample(records)
    cov = A.audit_coverage(sample, [], n_scored=m.n_scored)
    assert R.measurement_status(m, cov).status == report["status"]["status"]


def test_no_excluded_outcome_is_inside_the_scored_denominator(records, report):
    derived = sum(1 for r in records if C.is_scored(r))
    assert report["metrics"]["n_scored"] == derived


# =========================================================================== #
# AUDIT SAMPLE IDENTITY  (the defect that inflated coverage)
# =========================================================================== #
@pytest.fixture(scope="module")
def packet() -> dict:
    return json.loads((FORMAL / "audit_packet.json").read_text(encoding="utf-8"))


def test_the_audit_packet_is_keyed_on_record_identity(packet):
    ids = [i["record_id"] for i in packet["items"]]
    assert all(i.startswith("g1rec_") for i in ids)
    assert len(set(ids)) == len(ids), "duplicate record ids in the sample"


def test_the_audit_sample_matches_what_the_sampler_derives_now(records, packet):
    sample = A.select_audit_sample(records)
    assert {i.record_id for i in sample} == {i["record_id"] for i in packet["items"]}


def test_the_sample_size_is_a_true_minimum(records, packet):
    import math
    n_scored = sum(1 for r in records if C.is_scored(r))
    expect = max(1, math.ceil(n_scored * CRIT.MIN_HUMAN_AUDIT_FRACTION))
    assert len(packet["items"]) == expect
    assert len(packet["items"]) >= n_scored * CRIT.MIN_HUMAN_AUDIT_FRACTION


def test_the_sample_is_drawn_only_from_scored_decisions(records, packet):
    scored_ids = {r.record_id() for r in records if C.is_scored(r)}
    for item in packet["items"]:
        assert item["record_id"] in scored_ids


def test_a_case_measured_twice_can_appear_twice_in_the_sample(records, packet):
    """The whole point of record-level identity: two models, two decisions.

    Not asserted to be non-empty for every possible corpus, but if any case
    does repeat, its entries must be distinct decisions."""
    by_case: dict[str, list[str]] = {}
    for item in packet["items"]:
        by_case.setdefault(item["case_id"], []).append(item["record_id"])
    for case_id, ids in by_case.items():
        assert len(set(ids)) == len(ids), case_id
        if len(ids) > 1:
            served = {i["served_model_version"] for i in packet["items"]
                      if i["case_id"] == case_id}
            assert len(served) == len(ids), (
                f"{case_id} appears {len(ids)} times but names {len(served)} "
                "served builds — the entries are not distinct decisions")


def test_the_packet_identifies_which_configuration_each_decision_came_from(packet):
    for item in packet["items"]:
        assert item["config_id"].startswith("g1cfg_")
        assert item["served_model_version"]
        assert item["run_id"].startswith("g1run-formal-")


def test_the_audit_is_pending_and_nothing_is_prefilled(report, packet):
    a = report["human_audit"]
    assert a["status"] == A.HUMAN_AUDIT_PENDING
    assert a["completed"] == 0 and a["required"] > 0
    assert a["agreement_rate"] is None
    assert a["rejected_record_ids"] == []
    assert packet["status"] == A.HUMAN_AUDIT_PENDING
    assert "human_verdict" not in json.dumps(packet)
    for item in packet["items"]:
        assert item["packet"] is not None


def test_pending_ids_are_record_ids_not_case_ids(report, packet):
    pending = set(report["human_audit"]["pending_record_ids"])
    assert pending == {i["record_id"] for i in packet["items"]}


# =========================================================================== #
# HYGIENE
# =========================================================================== #
def test_every_false_pass_is_listed_individually(report):
    m = report["metrics"]
    assert len(m["false_pass_cases"]) == m["false_pass_count"]
    for c in m["false_pass_cases"]:
        assert c["case_id"] and c["severity"] and c["expected"] and c["actual"]
        assert c["record_id"].startswith("g1rec_")


def test_no_credential_shaped_material_in_any_g1_artifact():
    import re
    pat = re.compile(r"sk-[A-Za-z0-9_\-]{16,}|AKIA[0-9A-Z]{16}|"
                     r"BEGIN [A-Z ]*PRIVATE KEY|ew0a_openai_key")
    for p in (REPO / "evals" / "g1").rglob("*.json"):
        assert not pat.search(p.read_text(encoding="utf-8")), p.name


def test_no_authority_claim_in_any_g1_artifact():
    for p in (REPO / "evals" / "g1").rglob("*.json"):
        blob = p.read_text(encoding="utf-8")
        for forbidden in ("AUTHORITY_PROMOTED", "C1_ENABLED", "AUTONOMY_GRANTED"):
            assert forbidden not in blob, f"{p.name} claims {forbidden}"
