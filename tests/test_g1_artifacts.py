"""The committed G1 artifacts must still be internally consistent.

WHY THIS EXISTS.

``evals/g1/*.json`` is the measured result. Once committed it is evidence, and
evidence that can drift from the code that produced it is worse than none: a
report claiming a taxonomy or a criteria set the repository no longer has would
misrepresent what was counted.

So these tests re-derive from the committed records what the committed report
claims, using today's code. If the taxonomy changes, a denominator changes, or
the status logic changes, this fails — which is the correct outcome, because the
stored numbers would no longer mean what they say.

They do NOT re-run the supervisor. Live results are not reproducible and this
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
from portfolio_automation.engineer_worker.g1 import report as R
from portfolio_automation.engineer_worker.g1 import taxonomy as T

EVALS = Path(__file__).resolve().parents[1] / "evals" / "g1"


@pytest.fixture(scope="module")
def report() -> dict:
    return json.loads((EVALS / "g1_report.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def raw_records() -> list[dict]:
    return json.loads((EVALS / "g1_records.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def records(raw_records) -> list[C.SupervisorEvaluationRecordV0]:
    out = []
    for d in raw_records:
        out.append(C.SupervisorEvaluationRecordV0(
            case_id=d["case_id"], case_fingerprint=d["case_fingerprint"],
            expected_verdict=T.OutcomeClass(d["expected_verdict"]),
            actual_outcome=T.OutcomeClass(d["actual_outcome"]),
            match_class=C.MatchClass(d["match_class"]),
            severity=C.Severity(d["severity"]), split=C.Split(d["split"]),
            gold_basis=C.GoldBasis(d["gold_basis"]),
            execution_identity=d["execution_identity"],
            candidate_sha=d["candidate_sha"],
            served_model_version=d.get("served_model_version", "UNAVAILABLE_AT_RECORD_TIME"),
            supervisor_reasons=tuple(d["supervisor_reasons"]),
            supervisor_error=d["supervisor_error"],
            latency_ms=d["latency_ms"], recorded_at=d["recorded_at"],
            protected_high_impact=d["protected_high_impact"]))
    return out


def test_the_artifacts_exist():
    for name in ("g1_report.json", "g1_records.json", "g1_per_model.json",
                 "g1_audit_packet.json", "g1_corpus_manifest.json"):
        assert (EVALS / name).is_file(), name


def test_every_stored_record_reclassifies_identically(records):
    """Re-derive the match class from the case and the actual outcome.

    If classification logic changed, the stored numbers would silently start
    meaning something else."""
    cases = CORP.by_id()
    for r in records:
        case = cases.get(r.case_id)
        assert case is not None, f"{r.case_id} is no longer in the corpus"
        assert C.classify(case, r.actual_outcome) is r.match_class, r.case_id


def test_stored_records_still_match_their_case_fingerprints(records):
    """A case edited after measurement would invalidate its own result."""
    cases = CORP.by_id()
    for r in records:
        assert cases[r.case_id].fingerprint() == r.case_fingerprint, (
            f"{r.case_id} was edited after it was measured")


def test_the_reported_metrics_are_reproducible_from_the_records(records, report):
    recomputed = M.compute_metrics(records, CORP.by_id()).to_dict()
    stored = report["metrics"]
    for key in ("n_total", "n_scored", "n_excluded", "false_pass_count",
                "false_fail_count", "by_match_class", "n_by_execution_id"):
        assert recomputed[key] == stored[key], key
    assert recomputed["false_pass_rate"] == stored["false_pass_rate"]
    assert recomputed["exact_accuracy"] == stored["exact_accuracy"]


def test_the_reported_status_is_still_what_the_evidence_implies(records, report):
    m = M.compute_metrics(records, CORP.by_id())
    sample = A.select_audit_sample(records)
    cov = A.audit_coverage(sample, [], n_scored=m.n_scored)
    assert R.measurement_status(m, cov).status == report["status"]["status"]


def test_no_excluded_outcome_is_inside_the_scored_denominator(records, report):
    """AC2, checked against the artifact rather than only the code."""
    scored = report["metrics"]["n_scored"]
    derived = sum(1 for r in records
                  if T.participates_in_accuracy(r.actual_outcome)
                  and r.match_class is not C.MatchClass.HUMAN_REVIEW_PENDING)
    assert scored == derived


def test_every_record_is_attributable(records):
    """AC3: an unattributable measurement cannot be grouped by configuration."""
    for r in records:
        ident = r.execution_identity
        assert ident["schema_version"] == "engineering.execution_identity.v1"
        assert r.execution_id.startswith("exid_")
        assert ident["model_provider"] and ident["model_name"]
        assert ident["prompt_version"].startswith("sysprompt-")
        assert ident["toolset_id"] == "gpt_supervisor.review"


def test_served_build_is_recorded_and_is_not_the_requested_name(records):
    """The requested model and the build that answered are different facts."""
    for r in records:
        assert r.served_model_version != "UNAVAILABLE_AT_RECORD_TIME"
        assert r.served_model_version.startswith(
            r.execution_identity["model_name"])
        # the pre-call identity still declines to guess it
        assert r.execution_identity["model_version"] == "UNAVAILABLE_AT_RECORD_TIME"


def test_more_than_one_configuration_was_actually_measured(report, records):
    """Section H may only compare configurations that really ran."""
    configs = {c["config_id"] for c in report["configurations"]}
    assert len(configs) >= 2
    models = {r.execution_identity["model_name"] for r in records}
    assert len(models) >= 2


def test_all_three_splits_were_scored(report):
    s = report["sample_size"]
    assert s["n_development"] > 0 and s["n_held_out"] > 0 and s["n_rotating"] > 0


def test_the_report_carries_the_frozen_criteria_and_taxonomy(report):
    assert report["criteria"]["frozen_at_candidate"] == \
        CRIT.CRITERIA_FROZEN_AT_CANDIDATE
    assert report["taxonomy"] == T.taxonomy_manifest()
    assert report["corpus"]["rotation_epoch"] == CORP.ROTATION_EPOCH


def test_the_human_audit_is_recorded_as_pending_not_as_done(report):
    a = report["human_audit"]
    assert a["status"] == A.HUMAN_AUDIT_PENDING
    assert a["completed"] == 0
    assert a["required"] > 0
    assert a["agreement_rate"] is None, "no agreement may be claimed with 0 audits"


def test_the_audit_packet_has_no_prefilled_human_verdicts():
    pkt = json.loads((EVALS / "g1_audit_packet.json").read_text(encoding="utf-8"))
    assert pkt["status"] == A.HUMAN_AUDIT_PENDING
    blob = json.dumps(pkt)
    assert "human_verdict" not in blob, "a prefilled label is a fabricated label"
    for item in pkt["items"]:
        assert item["packet"] is not None


def test_every_false_pass_is_listed_individually(report):
    m = report["metrics"]
    assert len(m["false_pass_cases"]) == m["false_pass_count"]
    for c in m["false_pass_cases"]:
        assert c["case_id"] and c["severity"] and c["expected"] and c["actual"]


def test_the_artifacts_carry_no_credential_shaped_material():
    import re
    pat = re.compile(r"sk-[A-Za-z0-9_\-]{16,}|AKIA[0-9A-Z]{16}|"
                     r"BEGIN [A-Z ]*PRIVATE KEY|ew0a_openai_key")
    for p in EVALS.glob("*.json"):
        assert not pat.search(p.read_text(encoding="utf-8")), p.name


def test_no_authority_claim_appears_in_the_artifacts():
    for p in EVALS.glob("*.json"):
        blob = p.read_text(encoding="utf-8")
        for forbidden in ("AUTHORITY_PROMOTED", "C1_ENABLED", "AUTONOMY_GRANTED"):
            assert forbidden not in blob, f"{p.name} claims {forbidden}"
