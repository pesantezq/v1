"""G1-completion safeguards: population integrity and the formal scoring gate.

The paired controls for section-21 requirements A–Q that are not already covered
by tests/test_g1_measurement.py or tests/test_g1_preregistration.py.

The theme is one property: a preregistered result must be impossible to
manufacture by accident. Not by discipline — by construction.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from portfolio_automation.engineer_worker.g1 import audit as A
from portfolio_automation.engineer_worker.g1 import contracts as C
from portfolio_automation.engineer_worker.g1 import corpus as CORP
from portfolio_automation.engineer_worker.g1 import criteria as CRIT
from portfolio_automation.engineer_worker.g1 import metrics as M
from portfolio_automation.engineer_worker.g1 import preregistration as PRE
from portfolio_automation.engineer_worker.g1 import report as R
from portfolio_automation.engineer_worker.g1 import runner as RUN
from portfolio_automation.engineer_worker.g1 import taxonomy as T
from portfolio_automation.engineer_worker.gpt_supervisor import (
    SupervisorDecision, SupervisorVerdict)

V = T.OutcomeClass
REPO = Path(__file__).resolve().parents[1]
DIGEST = PRE.freeze_digest()

CFG_A = C.MeasurementConfig(model_provider="openai", model_name="model-a",
                            prompt_version="sysprompt-aaaa",
                            instruction_version="one-shot",
                            toolset_id="gpt_supervisor.review")
CFG_B = C.MeasurementConfig(model_provider="openai", model_name="model-b",
                            prompt_version="sysprompt-aaaa",
                            instruction_version="one-shot",
                            toolset_id="gpt_supervisor.review")


def _now():
    return "2026-08-22T15:00:00Z"


def _fixed(verdict):
    return lambda packet: SupervisorDecision(verdict, reasons=["fixed"])


def _rec(case, actual, *, cfg=CFG_A, run_id="run-x",
         population=C.RunPopulation.PREREGISTERED_FORMAL, digest=None):
    return C.SupervisorEvaluationRecordV0(
        case_id=case.case_id, case_fingerprint=case.fingerprint(),
        expected_verdict=case.expected_supervisor_verdict, actual_outcome=actual,
        match_class=C.classify(case, actual), severity=case.severity,
        split=case.split, gold_basis=case.gold_basis,
        execution_identity={"execution_id": f"exid_{cfg.model_name}",
                            "model_name": cfg.model_name,
                            "prompt_version": cfg.prompt_version},
        config=cfg, recorded_at=_now(), run_id=run_id, population=population,
        preregistration_digest=DIGEST if digest is None else digest,
        served_model_version=f"{cfg.model_name}-2026-01-01")


# =========================================================================== #
# A — the corpus can actually produce >= 100 fresh formal decisions
# =========================================================================== #
def test_the_corpus_supports_at_least_100_decisions_over_two_configurations():
    n_cases = len(CORP.ALL_CASES)
    assert n_cases >= 55, n_cases
    assert n_cases * 2 >= 110, "two configurations must leave margin above 100"
    # margin matters: 50x2 == exactly 100 leaves no room for one outage
    assert n_cases * 2 - 100 >= 10


def test_over_blocking_is_caught_by_false_fail_not_by_exact_accuracy():
    """A supervisor that refuses everything must be detectably wrong.

    This test originally asserted that blanket-REPAIR would depress EXACT
    accuracy below 75%. It does not: REPAIR is an acceptable alternate on 27 of
    55 cases (most ESCALATE and ABSTAIN cases), because refusing to make a
    change here is a defensible response even when escalation is the better
    route. So a blanket-REPAIR strategy scores ~78% exact.

    That is a real and reportable property of this corpus, not a bug to assert
    away: EXACT ACCURACY IS A WEAK DISCRIMINATOR AGAINST OVER-BLOCKING. The
    metric that catches it is false FAIL, whose denominator is exactly the cases
    that should have passed. This test pins the property that actually holds."""
    n_pass = sum(1 for c in CORP.ALL_CASES
                 if c.expected_supervisor_verdict is V.PASS)
    assert n_pass >= 10, n_pass

    refuse_all = [_rec(c, V.REPAIR) for c in CORP.ALL_CASES]
    m = M.compute_metrics(refuse_all, CORP.by_id())

    # over-blocking is caught, and caught completely
    assert m.false_fail_count == n_pass
    assert m.false_fail_rate.numerator == n_pass
    assert m.false_fail_rate.denominator == n_pass
    assert m.false_fail_rate.value == 1.0, (
        "every case that should have passed was refused")
    # ...and the safe-direction view correctly says nothing unsafe happened
    assert m.false_pass_count == 0

    # the documented weakness: exact accuracy stays high under blanket REPAIR
    assert m.exact_accuracy.value > 0.70, (
        "if this drops, the alternate-verdict policy changed and the "
        "documented limitation needs revisiting")


def test_passing_everything_is_also_punished():
    """The paired control: the corpus is not answerable by a single reflex."""
    pass_all = [_rec(c, V.PASS) for c in CORP.ALL_CASES]
    m = M.compute_metrics(pass_all, CORP.by_id())
    n_refusal = sum(1 for c in CORP.ALL_CASES
                    if c.expected_supervisor_verdict is not V.PASS)
    assert m.false_pass_count == n_refusal


def test_every_split_carries_more_than_one_verdict_class():
    for split in C.Split:
        verdicts = {c.expected_supervisor_verdict
                    for c in CORP.cases(split)}
        assert len(verdicts) > 1, f"{split.value} is answerable by one reflex"
        assert len(CORP.cases(split)) > 0


# =========================================================================== #
# B / Q — populations cannot be pooled, silently or otherwise
# =========================================================================== #
def test_mixed_populations_are_refused_by_metrics():
    case = CORP.ALL_CASES[0]
    formal = _rec(case, V.PASS)
    exploratory = _rec(case, V.PASS,
                       population=C.RunPopulation.EXPLORATORY_HISTORICAL,
                       digest="UNAVAILABLE_AT_RECORD_TIME")
    with pytest.raises(C.PopulationMismatch):
        M.compute_metrics([formal, exploratory])


def test_two_runs_of_the_same_freeze_are_still_two_populations():
    case = CORP.ALL_CASES[0]
    with pytest.raises(C.PopulationMismatch):
        M.compute_metrics([_rec(case, V.PASS, run_id="run-1"),
                           _rec(case, V.PASS, run_id="run-2")])


def test_a_report_cannot_silently_aggregate_populations():
    case = CORP.ALL_CASES[0]
    formal = _rec(case, V.PASS)
    other = _rec(case, V.PASS, run_id="run-other")
    m = M.compute_metrics([formal], CORP.by_id())
    cov = A.audit_coverage((), (), n_scored=m.n_scored)
    with pytest.raises(C.PopulationMismatch):
        R.build_report(metrics=m, coverage=cov, records=[formal, other],
                       configs=[CFG_A])


def test_a_homogeneous_population_is_accepted():
    """Paired control — the guard must not refuse legitimate input."""
    recs = [_rec(c, V.PASS) for c in CORP.development_cases()]
    m = M.compute_metrics(recs, CORP.by_id())
    assert m.n_total == len(recs)


# =========================================================================== #
# C — a record carrying the wrong freeze digest is not in this population
# =========================================================================== #
def test_a_record_from_a_different_freeze_is_excluded_from_the_population():
    case = CORP.ALL_CASES[0]
    mine = _rec(case, V.PASS)
    stale = _rec(case, V.PASS, digest="g1freeze_someotherfreeze")
    kept = M.formal_population([mine, stale], freeze_digest=DIGEST)
    assert [r.preregistration_digest for r in kept] == [DIGEST]
    assert stale not in kept


def test_exploratory_records_are_excluded_from_the_formal_population():
    case = CORP.ALL_CASES[0]
    hist = _rec(case, V.PASS,
                population=C.RunPopulation.EXPLORATORY_HISTORICAL,
                digest="UNAVAILABLE_AT_RECORD_TIME")
    assert M.formal_population([hist], freeze_digest=DIGEST) == []


def test_formal_population_can_be_narrowed_to_one_run():
    case = CORP.ALL_CASES[0]
    a = _rec(case, V.PASS, run_id="run-1")
    b = _rec(case, V.PASS, run_id="run-2")
    kept = M.formal_population([a, b], freeze_digest=DIGEST, run_id="run-1")
    assert [r.run_id for r in kept] == ["run-1"]


# =========================================================================== #
# D — formal scoring cannot begin before the freeze verifies
# =========================================================================== #
def test_a_formal_run_requires_a_repo_root_to_verify_against():
    with pytest.raises(ValueError, match="repo_root"):
        RUN.run_cases(CORP.development_cases()[:1], _fixed(SupervisorVerdict.PASS),
                      config=CFG_A, now_fn=_now, run_id="r",
                      preregistration_digest=DIGEST)


def test_a_formal_run_refuses_a_digest_that_does_not_match_the_tree():
    with pytest.raises(RUN.FreezeNotReady, match="the working tree registers"):
        RUN.run_cases(CORP.development_cases()[:1], _fixed(SupervisorVerdict.PASS),
                      config=CFG_A, now_fn=_now, run_id="r",
                      repo_root=REPO,
                      preregistration_digest="g1freeze_notthecurrentone")


def test_a_real_scored_run_demands_commit_level_containment_proof():
    """The stricter bar, required by the run script that spends real calls.

    A shallow checkout cannot prove containment. That is indeterminate, not
    refuted -- so it blocks a REAL scored run and does not block hermetic record
    construction. Both halves are asserted."""
    from portfolio_automation.engineer_worker.g1 import preregistration as _PRE
    v = _PRE.verify_freeze(REPO)
    if v.fully_verified:
        res = RUN.run_cases(CORP.development_cases()[:1],
                            _fixed(SupervisorVerdict.PASS), config=CFG_A,
                            now_fn=_now, run_id="r", repo_root=REPO,
                            preregistration_digest=DIGEST,
                            require_commit_proof=True)
        assert len(res.records) == 1
    else:
        with pytest.raises(RUN.FreezeNotReady, match="containment"):
            RUN.run_cases(CORP.development_cases()[:1],
                          _fixed(SupervisorVerdict.PASS), config=CFG_A,
                          now_fn=_now, run_id="r", repo_root=REPO,
                          preregistration_digest=DIGEST,
                          require_commit_proof=True)
        # ...and the same run without the strict flag still works
        res = RUN.run_cases(CORP.development_cases()[:1],
                            _fixed(SupervisorVerdict.PASS), config=CFG_A,
                            now_fn=_now, run_id="r", repo_root=REPO,
                            preregistration_digest=DIGEST)
        assert len(res.records) == 1


def test_a_formal_run_refuses_when_the_freeze_cannot_be_verified(tmp_path):
    """No pointer at all — the freeze is unverifiable, so nothing is scored."""
    called = {"n": 0}

    def spy(packet):
        called["n"] += 1
        return SupervisorDecision(SupervisorVerdict.PASS)

    with pytest.raises(RUN.FreezeNotReady):
        RUN.run_cases(CORP.development_cases()[:1], spy, config=CFG_A,
                      now_fn=_now, run_id="r", repo_root=tmp_path,
                      preregistration_digest=DIGEST)
    assert called["n"] == 0, "the supervisor must not be reached"


def test_a_formal_run_proceeds_when_the_freeze_verifies():
    """Paired control: the gate is a gate, not a wall."""
    res = RUN.run_cases(CORP.development_cases()[:2],
                        _fixed(SupervisorVerdict.PASS), config=CFG_A,
                        now_fn=_now, run_id="r", repo_root=REPO,
                        preregistration_digest=DIGEST)
    assert len(res.records) == 2
    assert all(r.preregistration_digest == DIGEST for r in res.records)


def test_an_exploratory_run_does_not_require_the_gate(tmp_path):
    """The gate exists for preregistered claims only."""
    res = RUN.run_cases(CORP.development_cases()[:1],
                        _fixed(SupervisorVerdict.PASS), config=CFG_A,
                        now_fn=_now, run_id="r",
                        population=C.RunPopulation.EXPLORATORY_HISTORICAL)
    assert len(res.records) == 1


# =========================================================================== #
# E / F / G / H — mutating registered material changes the digest
# =========================================================================== #
def _mutate_first_case(monkeypatch, **swaps):
    original = CORP.ALL_CASES
    first = original[0]
    kw = {f.name: getattr(first, f.name)
          for f in first.__dataclass_fields__.values()}
    kw.update(swaps)
    monkeypatch.setattr(CORP, "ALL_CASES",
                        (C.EvaluationCaseV0(**kw),) + original[1:])


def test_case_packet_mutation_changes_the_digest(monkeypatch):
    before = PRE.freeze_digest()
    _mutate_first_case(monkeypatch,
                       packet={**dict(CORP.ALL_CASES[0].packet),
                               "diff": "totally different diff"})
    assert PRE.freeze_digest() != before


def test_gold_label_mutation_changes_the_digest(monkeypatch):
    before = PRE.freeze_digest()
    first = CORP.ALL_CASES[0]
    other = (V.ESCALATE if first.expected_supervisor_verdict is not V.ESCALATE
             else V.ABSTAIN)
    _mutate_first_case(monkeypatch, expected_supervisor_verdict=other,
                       acceptable_alternate_verdicts=())
    assert PRE.freeze_digest() != before


def test_split_mutation_changes_the_digest(monkeypatch):
    before = PRE.freeze_digest()
    first = CORP.ALL_CASES[0]
    other = (C.Split.HELD_OUT if first.split is not C.Split.HELD_OUT
             else C.Split.DEVELOPMENT)
    _mutate_first_case(monkeypatch, split=other)
    assert PRE.freeze_digest() != before


def test_audit_policy_mutation_changes_the_digest(monkeypatch):
    before = PRE.freeze_digest()
    monkeypatch.setattr(CRIT, "MIN_HUMAN_AUDIT_FRACTION", 0.05)
    assert PRE.freeze_digest() != before


def test_sample_size_policy_mutation_changes_the_digest(monkeypatch):
    before = PRE.freeze_digest()
    monkeypatch.setattr(CRIT, "MIN_CELL_N_FOR_RATE", 3)
    assert PRE.freeze_digest() != before


def test_taxonomy_mutation_changes_the_digest(monkeypatch):
    before = PRE.freeze_digest()
    monkeypatch.setattr(T, "ACCURACY_POPULATION",
                        T.Population.SUPERVISOR_OPERATIONAL_FAILURE)
    assert PRE.freeze_digest() != before


def test_adding_a_case_changes_the_digest(monkeypatch):
    before = PRE.freeze_digest()
    monkeypatch.setattr(CORP, "ALL_CASES",
                        CORP.ALL_CASES + (CORP.ALL_CASES[0],))
    assert PRE.freeze_digest() != before


# =========================================================================== #
# I / J / K — audit identity, membership, ceiling (on the expanded scale)
# =========================================================================== #
def test_same_case_two_models_remains_two_audit_identities():
    case = CORP.ALL_CASES[0]
    a = _rec(case, V.PASS, cfg=CFG_A)
    b = _rec(case, V.PASS, cfg=CFG_B)
    assert a.record_id() != b.record_id()
    sample = A.select_audit_sample([a, b], fraction=1.0)
    assert {i.record_id for i in sample} == {a.record_id(), b.record_id()}
    cov = A.audit_coverage(sample, [], n_scored=2)
    assert cov.required == 2 and cov.completed == 0


def test_an_unrelated_adjudication_cannot_satisfy_coverage_at_scale():
    recs = [_rec(c, V.REPAIR) for c in CORP.ALL_CASES]
    sample = A.select_audit_sample(recs)
    stranger = A.HumanAuditRecord(
        case_id="nope", record_id="g1rec_unrelated", supervisor_verdict="PASS",
        human_verdict="PASS", reviewer_id="h", reviewed_at="t",
        execution_id="e", severity=C.Severity.HIGH)
    cov = A.audit_coverage(sample, [stranger], n_scored=len(recs))
    assert cov.completed == 0 and not cov.satisfied
    assert cov.rejected_record_ids == ("g1rec_unrelated",)


@pytest.mark.parametrize("n,expect", [(55, 11), (100, 20), (110, 22)])
def test_ceil_governs_the_audit_size_at_the_new_scale(n, expect):
    assert max(1, math.ceil(n * CRIT.MIN_HUMAN_AUDIT_FRACTION)) == expect


def test_the_audit_sample_covers_multiple_priority_bands_at_scale():
    recs = [_rec(c, c.expected_supervisor_verdict) for c in CORP.ALL_CASES]
    sample = A.select_audit_sample(recs)
    reasons = {i.selection_reason for i in sample}
    assert len(reasons) > 1, "one band must not starve all others"


# =========================================================================== #
# L / M / N / O — invariants that must survive the expansion
# =========================================================================== #
def test_held_out_isolation_still_enforced():
    with pytest.raises(CORP.SplitLeakError):
        RUN.run_cases(CORP.held_out_cases(), _fixed(SupervisorVerdict.PASS),
                      config=CFG_A, now_fn=_now, run_id="r", repo_root=REPO,
                      preregistration_digest=DIGEST)


def test_zero_denominator_remains_undefined():
    case = CORP.ALL_CASES[0]
    m = M.compute_metrics([_rec(case, V.POLICY_VIOLATION)])
    assert m.n_scored == 0
    assert m.false_pass_rate.defined is False
    assert m.false_pass_rate.value is None


def test_an_operational_failure_is_never_a_semantic_error():
    case = CORP.ALL_CASES[0]
    for outage in (V.SUPERVISOR_UNAVAILABLE, V.MALFORMED_RESPONSE, V.TIMEOUT,
                   V.AUTH_FAILURE, V.TRANSPORT_FAILURE):
        r = _rec(case, outage)
        assert r.match_class is C.MatchClass.SUPERVISOR_UNAVAILABLE
        m = M.compute_metrics([r])
        assert m.false_pass_count == 0 and m.false_fail_count == 0
        assert m.n_scored == 0


def test_pass_is_never_an_acceptable_alternate_for_a_refusal_case():
    for c in CORP.ALL_CASES:
        if c.expected_supervisor_verdict is not V.PASS:
            assert V.PASS not in c.acceptable_alternate_verdicts, c.case_id


def test_every_case_has_mechanically_stated_gold_provenance():
    for c in CORP.ALL_CASES:
        assert c.gold_basis is C.GoldBasis.DETERMINISTIC_GROUND_TRUTH
        prov = c.gold_provenance
        assert prov.strip()
        assert len(prov) > 80, (
            f"{c.case_id}: provenance too thin to be checkable")
        assert "Deterministic" in prov, c.case_id
