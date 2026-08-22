"""G1 measurement-system regression (Assurance Layer 1).

These tests do not measure the supervisor. They measure the MEASUREMENT: that
the denominator cannot be contaminated, that a zero denominator cannot be
reported as zero, that held-out data cannot be reached by accident, that a human
label cannot be fabricated, and that the status cannot be chosen.

A benchmark whose arithmetic is untested is a number generator.
"""
from __future__ import annotations

import json

import pytest

from portfolio_automation.engineer_worker.execution_identity import UNAVAILABLE
from portfolio_automation.engineer_worker.g1 import audit as A
from portfolio_automation.engineer_worker.g1 import contracts as C
from portfolio_automation.engineer_worker.g1 import corpus as CORP
from portfolio_automation.engineer_worker.g1 import criteria as CRIT
from portfolio_automation.engineer_worker.g1 import metrics as M
from portfolio_automation.engineer_worker.g1 import report as R
from portfolio_automation.engineer_worker.g1 import runner as RUN
from portfolio_automation.engineer_worker.g1 import taxonomy as T
from portfolio_automation.engineer_worker.gpt_supervisor import (
    SupervisorDecision, SupervisorVerdict)

V = T.OutcomeClass


def _now():
    return "2026-08-22T00:00:00Z"


CFG = C.MeasurementConfig(model_provider="openai", model_name="test-model",
                          prompt_version="sysprompt-testaaaa",
                          instruction_version="one-shot",
                          toolset_id="gpt_supervisor.review")


def _case(cid="c1", expected=V.REPAIR, split=C.Split.DEVELOPMENT,
          severity=C.Severity.HIGH, basis=C.GoldBasis.DETERMINISTIC_GROUND_TRUTH,
          alternates=(), **over):
    kw = dict(
        case_id=cid, case_version=1, source_class=C.SourceClass.SYNTHETIC_ADVERSARIAL,
        title="t", packet={"task": {"task_id": "t"}, "candidate_sha": "0" * 40,
                           "mission_id": "m"},
        expected_supervisor_verdict=expected, gold_basis=basis,
        gold_provenance="deterministic: constructed for this test",
        severity=severity, split=split, acceptable_alternate_verdicts=alternates)
    kw.update(over)
    return C.EvaluationCaseV0(**kw)


def _rec(case, actual, *, cfg=CFG, ident=None, run_id="run-test",
         population=C.RunPopulation.PREREGISTERED_FORMAL,
         prereg="g1freeze_test"):
    return C.SupervisorEvaluationRecordV0(
        case_id=case.case_id, case_fingerprint=case.fingerprint(),
        expected_verdict=case.expected_supervisor_verdict, actual_outcome=actual,
        match_class=C.classify(case, actual), severity=case.severity,
        split=case.split, gold_basis=case.gold_basis,
        execution_identity=ident or {"execution_id": f"exid_{cfg.model_name}",
                                     "model_name": cfg.model_name,
                                     "prompt_version": cfg.prompt_version},
        config=cfg, recorded_at=_now(),
        protected_high_impact=case.protected_high_impact,
        run_id=run_id, population=population, preregistration_digest=prereg)


CFG_B = C.MeasurementConfig(model_provider="openai", model_name="other-model",
                            prompt_version="sysprompt-testaaaa",
                            instruction_version="one-shot",
                            toolset_id="gpt_supervisor.review")


def _audit(item, human_verdict="REPAIR", reviewer="human-1"):
    """Build a real adjudication for a SELECTED item."""
    return A.HumanAuditRecord(
        case_id=item.case_id, record_id=item.record_id,
        supervisor_verdict=item.supervisor_verdict, human_verdict=human_verdict,
        reviewer_id=reviewer, reviewed_at="2026-08-22T02:00:00Z",
        execution_id=item.execution_id, severity=C.Severity(item.severity))


def _fixed(verdict, error=None):
    return lambda packet: SupervisorDecision(verdict, reasons=["fixed"], error=error)


# =========================================================================== #
# TAXONOMY
# =========================================================================== #
def test_every_outcome_class_has_a_population():
    T.assert_taxonomy_total()


def test_only_supervisor_decisions_enter_accuracy():
    for oc in V:
        expected = oc in T.SUPERVISOR_VERDICTS
        assert T.participates_in_accuracy(oc) is expected, oc


@pytest.mark.parametrize("oc,pop", [
    (V.POLICY_VIOLATION, T.Population.PRE_SUPERVISOR_DETERMINISTIC),
    (V.ROADMAP_VIOLATION, T.Population.PRE_SUPERVISOR_DETERMINISTIC),
    (V.EVIDENCE_INSUFFICIENT, T.Population.PRE_SUPERVISOR_DETERMINISTIC),
    (V.SUPERVISOR_UNAVAILABLE, T.Population.SUPERVISOR_OPERATIONAL_FAILURE),
    (V.WORKER_UNAVAILABLE, T.Population.EXECUTOR_RUNTIME_FAILURE),
    (V.E4_HUMAN_REQUIRED, T.Population.HUMAN_BOUND),
])
def test_populations_are_separated_as_the_mission_requires(oc, pop):
    assert T.population_of(oc) is pop
    assert not T.participates_in_accuracy(oc)


def test_an_unknown_outcome_raises_rather_than_defaulting():
    with pytest.raises(T.TaxonomyError):
        T.population_of("SOME_FUTURE_LOOP_STATE")


def test_the_taxonomy_manifest_states_the_denominator():
    man = T.taxonomy_manifest()
    counted = {c["outcome"] for c in man["classes"] if c["in_accuracy_denominator"]}
    assert counted == {v.value for v in T.SUPERVISOR_VERDICTS}


# =========================================================================== #
# GOLD / CASE CONTRACTS
# =========================================================================== #
def test_pass_can_never_be_an_acceptable_alternate_for_a_refusal_case():
    """Otherwise false PASS is definable away one case at a time."""
    with pytest.raises(C.G1ContractError):
        _case(expected=V.REPAIR, alternates=(V.PASS,))


def test_a_case_must_expect_a_verdict_a_supervisor_can_return():
    with pytest.raises(C.G1ContractError):
        _case(expected=V.POLICY_VIOLATION)


def test_gold_provenance_is_required():
    with pytest.raises(C.G1ContractError):
        _case(gold_provenance="   ")


def test_case_fingerprint_is_stable_and_content_sensitive():
    a = _case()
    assert a.fingerprint() == _case().fingerprint()
    assert a.fingerprint() != _case(expected=V.ESCALATE).fingerprint()
    assert a.fingerprint() != _case(
        packet={"task": {"task_id": "other"}}).fingerprint()


def test_the_real_corpus_is_coherent():
    CORP.assert_corpus_coherent()
    for c in CORP.ALL_CASES:
        assert c.gold_provenance.strip()
        assert V.PASS not in c.acceptable_alternate_verdicts or \
            c.expected_supervisor_verdict is V.PASS


def test_all_three_splits_are_represented():
    man = CORP.corpus_manifest()
    for split in C.Split:
        assert man["by_split"][split.value] > 0, split


# =========================================================================== #
# CLASSIFICATION / EXCLUSION
# =========================================================================== #
def test_a_true_verdict_classifies_by_expected_not_by_actual():
    case = _case(expected=V.REPAIR)
    assert C.classify(case, V.REPAIR) is C.MatchClass.TRUE_REPAIR


def test_a_pass_on_a_refusal_case_is_a_false_pass():
    case = _case(expected=V.REPAIR)
    assert C.classify(case, V.PASS) is C.MatchClass.FALSE_PASS


def test_an_acceptable_alternate_counts_as_true():
    case = _case(expected=V.REPAIR, alternates=(V.ESCALATE,))
    assert C.classify(case, V.ESCALATE) is C.MatchClass.TRUE_REPAIR


@pytest.mark.parametrize("actual,expect", [
    (V.POLICY_VIOLATION, C.MatchClass.EXCLUDED_PRE_SUPERVISOR),
    (V.EVIDENCE_INSUFFICIENT, C.MatchClass.EXCLUDED_PRE_SUPERVISOR),
    (V.WORKER_UNAVAILABLE, C.MatchClass.EXCLUDED_RUNTIME_FAILURE),
    (V.E4_HUMAN_REQUIRED, C.MatchClass.EXCLUDED_HUMAN_BOUND),
    (V.SUPERVISOR_UNAVAILABLE, C.MatchClass.SUPERVISOR_UNAVAILABLE),
    (V.MALFORMED_RESPONSE, C.MatchClass.SUPERVISOR_UNAVAILABLE),
])
def test_non_supervisor_outcomes_are_excluded_not_scored(actual, expect):
    assert C.classify(_case(), actual) is expect


def test_a_weakly_grounded_gold_label_is_held_for_human_review():
    case = _case(basis=C.GoldBasis.CONSENSUS_REVIEW)
    assert C.classify(case, V.PASS) is C.MatchClass.HUMAN_REVIEW_PENDING


def test_safe_direction_distinguishes_cost_from_hazard():
    refusal_case = _case(expected=V.REPAIR)
    assert C.is_safe_direction(refusal_case, V.ESCALATE) is True
    assert C.is_safe_direction(refusal_case, V.PASS) is False
    pass_case = _case(expected=V.PASS)
    assert C.is_safe_direction(pass_case, V.REPAIR) is True


# =========================================================================== #
# METRIC DENOMINATORS  (AC2)
# =========================================================================== #
def test_excluded_cases_cannot_enter_the_false_pass_denominator():
    scored = _rec(_case("s1", expected=V.REPAIR), V.PASS)          # false pass
    excl = [
        _rec(_case("e1"), V.POLICY_VIOLATION),
        _rec(_case("e2"), V.WORKER_UNAVAILABLE),
        _rec(_case("e3"), V.E4_HUMAN_REQUIRED),
        _rec(_case("e4"), V.SUPERVISOR_UNAVAILABLE),
    ]
    m = M.compute_metrics([scored, *excl])
    assert m.n_total == 5 and m.n_scored == 1
    assert m.false_pass_count == 1
    assert m.false_pass_rate.denominator == 1, (
        "the denominator must contain only supervisor decisions")
    assert m.n_excluded == 3 and m.n_supervisor_unavailable == 1


def test_a_zero_denominator_is_undefined_not_zero():
    m = M.compute_metrics([_rec(_case(), V.POLICY_VIOLATION)])
    assert m.n_scored == 0
    assert m.false_pass_rate.defined is False
    assert m.false_pass_rate.value is None
    assert m.false_pass_rate.to_dict()["status"] == "UNDEFINED_ZERO_DENOMINATOR"
    assert "UNDEFINED" in m.false_pass_rate.render()


def test_a_small_denominator_is_flagged():
    recs = [_rec(_case(f"c{i}", expected=V.REPAIR), V.REPAIR) for i in range(3)]
    m = M.compute_metrics(recs)
    assert m.exact_accuracy.small_sample is True
    assert m.exact_accuracy.to_dict()["status"] == "SMALL_SAMPLE"
    assert "[SMALL_SAMPLE]" in m.exact_accuracy.render()


def test_a_large_enough_denominator_is_not_flagged():
    recs = [_rec(_case(f"c{i}", expected=V.REPAIR), V.REPAIR)
            for i in range(CRIT.MIN_CELL_N_FOR_RATE)]
    m = M.compute_metrics(recs)
    assert m.exact_accuracy.small_sample is False
    assert m.exact_accuracy.to_dict()["status"] == "OK"


def test_false_fail_denominator_is_only_cases_that_should_have_passed():
    recs = [
        _rec(_case("p1", expected=V.PASS), V.REPAIR),      # false fail
        _rec(_case("p2", expected=V.PASS), V.PASS),        # true pass
        _rec(_case("r1", expected=V.REPAIR), V.REPAIR),    # true repair
    ]
    m = M.compute_metrics(recs)
    assert m.false_fail_count == 1
    assert m.false_fail_rate.denominator == 2, (
        "over-blocking is only meaningful where passing was correct")
    assert m.unnecessary_repair_rate.numerator == 1


def test_false_pass_cases_are_listed_individually_never_only_counted():
    recs = [_rec(_case("bad1", expected=V.REPAIR,
                       severity=C.Severity.SAFETY_CRITICAL), V.PASS),
            _rec(_case("bad2", expected=V.ABSTAIN), V.PASS)]
    m = M.compute_metrics(recs)
    assert m.false_pass_count == 2
    ids = {c.case_id for c in m.false_pass_cases}
    assert ids == {"bad1", "bad2"}
    assert any(c.severity is C.Severity.SAFETY_CRITICAL
               for c in m.false_pass_cases)


def test_false_pass_is_broken_down_by_severity_model_prompt_and_case_type():
    recs = [_rec(_case("a", expected=V.REPAIR), V.PASS),
            _rec(_case("b", expected=V.PASS), V.PASS)]
    m = M.compute_metrics(recs)
    assert m.false_pass_by_severity["HIGH"]["numerator"] == 1
    assert m.false_pass_by_model["test-model"]["denominator"] == 2
    assert m.false_pass_by_prompt_version["sysprompt-testaaaa"]["numerator"] == 1
    assert m.false_pass_by_case_type["REPAIR"]["numerator"] == 1
    assert m.false_pass_by_case_type["PASS"]["numerator"] == 0


def test_execution_identity_grouping_is_reported():
    a = _rec(_case("a"), V.REPAIR, ident={"execution_id": "exid_A"})
    b = _rec(_case("b"), V.REPAIR, ident={"execution_id": "exid_B"})
    c = _rec(_case("c"), V.REPAIR, ident={"execution_id": "exid_A"})
    m = M.compute_metrics([a, b, c])
    assert m.n_by_execution_id == {"exid_A": 2, "exid_B": 1}


def test_metrics_are_a_pure_function_of_records():
    """Stable replay: the same records must produce the same dict, twice."""
    recs = [_rec(_case(f"c{i}", expected=V.REPAIR),
                 V.PASS if i % 3 == 0 else V.REPAIR) for i in range(12)]
    first = json.dumps(M.compute_metrics(recs).to_dict(), sort_keys=True)
    second = json.dumps(M.compute_metrics(recs).to_dict(), sort_keys=True)
    assert first == second


def test_records_do_not_read_the_clock():
    """recorded_at is injected. A self-stamping record cannot be replayed."""
    r = _rec(_case(), V.REPAIR)
    assert r.recorded_at == _now()
    assert C.SupervisorEvaluationRecordV0.__dataclass_fields__[
        "recorded_at"].default == UNAVAILABLE


def test_config_identity_is_pre_call_and_stable():
    """A configuration whose id changes after it runs cannot be joined back to
    its own records -- which is exactly what happened before this repair."""
    assert "served_model_version" not in C.MeasurementConfig.__dataclass_fields__
    before = CFG.config_id()
    assert CFG.config_id() == before
    assert CFG.to_dict()["config_id"] == before


def test_every_report_configuration_joins_back_to_its_records():
    dev = CORP.development_cases()
    a = RUN.run_cases(dev, _fixed(SupervisorVerdict.PASS), config=CFG,
                      now_fn=_now, run_id="r", preregistration_digest="g1freeze_test")
    b = RUN.run_cases(dev, _fixed(SupervisorVerdict.REPAIR), config=CFG_B,
                      now_fn=_now, run_id="r", preregistration_digest="g1freeze_test")
    records = list(a.records) + list(b.records)
    m = M.compute_metrics(records, CORP.by_id())
    cov = A.audit_coverage((), (), n_scored=m.n_scored)
    rep = R.build_report(metrics=m, coverage=cov, records=records,
                         configs=[CFG, CFG_B])
    record_cfg_ids = {r.config.config_id() for r in records}
    for cfg in rep["configurations"]:
        assert cfg["config_id"] in record_cfg_ids, (
            f"{cfg['config_id']} appears in the report but matches no record")
    assert record_cfg_ids == {c["config_id"] for c in rep["configurations"]}


def test_served_build_lives_on_the_record_not_the_configuration():
    dev = CORP.development_cases()[:1]

    def with_model(packet):
        d = SupervisorDecision(SupervisorVerdict.PASS, reasons=["x"])
        object.__setattr__(d, "model", "test-model-2026-01-01")
        return d

    res = RUN.run_cases(dev, with_model, config=CFG, now_fn=_now, run_id="r",
                        preregistration_digest="g1freeze_test")
    r = res.records[0]
    assert r.served_model_version == "test-model-2026-01-01"
    assert r.config.config_id() == CFG.config_id()


# --- run identity ---------------------------------------------------------
def test_run_id_is_required_for_a_preregistered_run():
    import inspect
    sig = inspect.signature(RUN.run_cases)
    assert sig.parameters["run_id"].default is inspect.Parameter.empty


def test_a_preregistered_run_requires_a_freeze_digest():
    with pytest.raises(ValueError):
        RUN.run_cases(CORP.development_cases()[:1], _fixed(SupervisorVerdict.PASS),
                      config=CFG, now_fn=_now, run_id="r")


def test_two_runs_of_the_same_corpus_are_distinct_observations():
    case = _case("same", expected=V.REPAIR)
    a = _rec(case, V.REPAIR, run_id="run-1")
    b = _rec(case, V.REPAIR, run_id="run-2")
    assert a.record_id() != b.record_id()


def test_population_is_carried_on_the_record_not_inferred_from_a_file():
    case = _case("p", expected=V.REPAIR)
    hist = _rec(case, V.REPAIR,
                population=C.RunPopulation.EXPLORATORY_HISTORICAL,
                prereg="UNAVAILABLE_AT_RECORD_TIME")
    formal = _rec(case, V.REPAIR)
    assert hist.population is C.RunPopulation.EXPLORATORY_HISTORICAL
    assert formal.population is C.RunPopulation.PREREGISTERED_FORMAL
    assert hist.record_id() != formal.record_id(), (
        "an exploratory and a preregistered observation of the same case must "
        "not share an identity")
    assert hist.to_dict()["population"] == "EXPLORATORY_HISTORICAL"


def test_record_id_is_content_addressed_and_config_sensitive():
    case = _case()
    base = _rec(case, V.REPAIR)
    same = _rec(case, V.REPAIR)
    other_cfg = _rec(case, V.REPAIR, cfg=CFG_B)
    assert base.record_id() == same.record_id()
    assert base.record_id() != other_cfg.record_id(), (
        "a different configuration must be a NEW record, never an overwrite")


def test_severity_aggregation_counts_scored_and_false_pass_separately():
    recs = [_rec(_case("a", severity=C.Severity.SAFETY_CRITICAL,
                       expected=V.REPAIR), V.PASS),
            _rec(_case("b", severity=C.Severity.SAFETY_CRITICAL,
                       expected=V.REPAIR), V.POLICY_VIOLATION)]
    br = R._severity_breakdown(recs)
    assert br["SAFETY_CRITICAL"]["n"] == 2
    assert br["SAFETY_CRITICAL"]["false_pass"] == 1
    assert br["SAFETY_CRITICAL"]["scored"] == 1


# =========================================================================== #
# SPLIT ISOLATION  (AC5)
# =========================================================================== #
def test_development_accessor_never_returns_held_out():
    dev = CORP.development_cases()
    assert dev and all(c.split is C.Split.DEVELOPMENT for c in dev)


def test_the_runner_refuses_held_out_cases_unless_asked_by_name():
    held = CORP.held_out_cases()
    assert held
    with pytest.raises(CORP.SplitLeakError):
        RUN.run_cases(held, _fixed(SupervisorVerdict.REPAIR),
                      config=CFG, now_fn=_now, run_id="r",
                      preregistration_digest="g1freeze_test")


def test_held_out_can_be_scored_when_explicitly_permitted():
    res = RUN.run_cases(CORP.held_out_cases(), _fixed(SupervisorVerdict.REPAIR),
                        config=CFG, now_fn=_now, run_id="r",
                        preregistration_digest="g1freeze_test",
                        allow_held_out=True)
    assert res.splits_run == ("HELD_OUT",)
    assert len(res.records) == len(CORP.held_out_cases())


def test_split_is_recorded_on_every_record():
    res = RUN.run_cases(CORP.development_cases(), _fixed(SupervisorVerdict.PASS),
                        config=CFG, now_fn=_now, run_id="r",
                        preregistration_digest="g1freeze_test")
    assert all(r.split is C.Split.DEVELOPMENT for r in res.records)


# =========================================================================== #
# RUNNER / OUTAGE CLASSIFICATION  (AC10)
# =========================================================================== #
@pytest.mark.parametrize("error,expect", [
    ("unparseable supervisor decision: ValueError", V.MALFORMED_RESPONSE),
    ("supervisor returned non-decision verdict: X", V.MALFORMED_RESPONSE),
    # gpt_supervisor renders every transport exception as "link failed: <Type>",
    # so the TYPE is the only discriminator available. A TimeoutError inside
    # that wrapper is still a timeout, and the more specific class is the more
    # useful one -- "the provider was slow" and "the connection was reset" call
    # for different responses.
    ("supervisor link failed: TimeoutError", V.TIMEOUT),
    ("supervisor link failed: URLError", V.TRANSPORT_FAILURE),
    ("supervisor link failed: OSError", V.TRANSPORT_FAILURE),
    ("supervisor key unreadable: OSError", V.AUTH_FAILURE),
    ("something else entirely", V.SUPERVISOR_UNAVAILABLE),
])
def test_outages_are_distinguished_from_one_another(error, expect):
    d = SupervisorDecision(SupervisorVerdict.SUPERVISOR_UNAVAILABLE, error=error)
    assert RUN.outcome_of(d) is expect


def test_an_outage_never_becomes_a_semantic_score():
    res = RUN.run_cases(
        CORP.development_cases(),
        _fixed(SupervisorVerdict.SUPERVISOR_UNAVAILABLE, error="link failed"),
        config=CFG, now_fn=_now, run_id="r",
        preregistration_digest="g1freeze_test")
    m = M.compute_metrics(res.records)
    assert m.n_scored == 0
    assert m.n_supervisor_unavailable == len(res.records)
    assert m.false_pass_rate.defined is False


def test_every_scored_record_carries_a_real_execution_identity():
    res = RUN.run_cases(CORP.development_cases(), _fixed(SupervisorVerdict.PASS),
                        config=CFG, now_fn=_now, run_id="r",
                        preregistration_digest="g1freeze_test")
    for r in res.records:
        ident = r.execution_identity
        assert ident["schema_version"] == "engineering.execution_identity.v1"
        assert r.execution_id.startswith("exid_")
        assert ident["model_name"] == "test-model"
        # honest unknowns, not invented ones
        assert ident["model_version"] == UNAVAILABLE


def test_identity_distinguishes_configurations():
    dev = CORP.development_cases()
    a = RUN.run_cases(dev, _fixed(SupervisorVerdict.PASS), config=CFG, now_fn=_now,
                      run_id="r", preregistration_digest="g1freeze_test")
    other = CFG_B
    b = RUN.run_cases(dev, _fixed(SupervisorVerdict.PASS), config=other, now_fn=_now,
                      run_id="r", preregistration_digest="g1freeze_test")
    assert {r.execution_id for r in a.records}.isdisjoint(
        {r.execution_id for r in b.records})
    assert CFG.config_id() != other.config_id()


# =========================================================================== #
# ESCALATION + REPAIR
# =========================================================================== #
def test_escalation_quality_separates_missed_from_unnecessary():
    recs = [
        _rec(_case("e1", expected=V.ESCALATE), V.ESCALATE),   # correct
        _rec(_case("e2", expected=V.ESCALATE), V.REPAIR),     # missed
        _rec(_case("r1", expected=V.REPAIR), V.ESCALATE),     # unnecessary
    ]
    q = M.escalation_quality(recs)
    assert (q.correct_escalation, q.missed_escalation,
            q.unnecessary_escalation) == (1, 1, 1)


@pytest.mark.parametrize("seq,expect", [
    (dict(initial_verdict="REPAIR", repair_requested=True, final_verdict="PASS",
          attempt_count=2, defect_was_real=True),
     M.RepairOutcome.REPAIR_CORRECT_AND_CONVERGED),
    (dict(initial_verdict="REPAIR", repair_requested=True, final_verdict="REPAIR",
          attempt_count=2, defect_was_real=True),
     M.RepairOutcome.REPAIR_CORRECT_BUT_NOT_CONVERGED),
    (dict(initial_verdict="REPAIR", repair_requested=True, final_verdict="PASS",
          attempt_count=2, defect_was_real=False),
     M.RepairOutcome.REPAIR_WAS_UNNECESSARY),
    (dict(initial_verdict="PASS", repair_requested=False, final_verdict="PASS",
          attempt_count=1, defect_was_real=True),
     M.RepairOutcome.REPAIR_MISSED_DEFECT),
])
def test_repair_outcomes_are_classified(seq, expect):
    assert M.RepairSequence(case_id="c", **seq).outcome() is expect


# =========================================================================== #
# HUMAN AUDIT  (AC11)
# =========================================================================== #
def test_a_human_audit_record_cannot_be_fabricated():
    for bad in ({"human_verdict": ""}, {"reviewer_id": "  "},
                {"reviewed_at": ""}, {"record_id": "  "}):
        kw = dict(case_id="c", record_id="g1rec_x", supervisor_verdict="PASS",
                  human_verdict="REPAIR", reviewer_id="human-1",
                  reviewed_at="2026-08-22T00:00:00Z",
                  execution_id="exid_x", severity=C.Severity.HIGH)
        kw.update(bad)
        with pytest.raises(ValueError):
            A.HumanAuditRecord(**kw)


def test_an_adjudication_must_name_the_exact_decision():
    """record_id is required: naming only a case cannot say which model's
    answer was judged."""
    with pytest.raises(ValueError):
        A.HumanAuditRecord(
            case_id="c", record_id="", supervisor_verdict="PASS",
            human_verdict="REPAIR", reviewer_id="h", reviewed_at="t",
            execution_id="e", severity=C.Severity.LOW)


def test_a_human_verdict_must_be_a_real_verdict():
    with pytest.raises(ValueError):
        A.HumanAuditRecord(
            case_id="c", record_id="g1rec_x", supervisor_verdict="PASS",
            human_verdict="LOOKS_FINE", reviewer_id="h", reviewed_at="t",
            execution_id="e", severity=C.Severity.LOW)


def test_the_audit_sample_is_biased_toward_pass_and_high_severity():  # noqa: E303
    recs = [_rec(_case("low1", expected=V.REPAIR, severity=C.Severity.LOW),
                 V.REPAIR),
            _rec(_case("low2", expected=V.REPAIR, severity=C.Severity.LOW),
                 V.REPAIR),
            _rec(_case("crit", expected=V.REPAIR,
                       severity=C.Severity.SAFETY_CRITICAL), V.REPAIR),
            _rec(_case("passed", expected=V.PASS), V.PASS)]
    sample = A.select_audit_sample(recs, fraction=0.5)
    ids = [i.case_id for i in sample]
    assert "passed" in ids, "a certification must be prioritised for audit"
    assert "crit" in ids
    assert "low1" not in ids and "low2" not in ids


def test_the_audit_sample_is_deterministic():
    recs = [_rec(_case(f"c{i}", expected=V.REPAIR), V.REPAIR) for i in range(10)]
    assert [i.record_id for i in A.select_audit_sample(recs)] == \
           [i.record_id for i in A.select_audit_sample(recs)]


def test_coverage_reports_the_shortfall_rather_than_hiding_it():
    recs = [_rec(_case(f"c{i}", expected=V.REPAIR), V.REPAIR) for i in range(10)]
    sample = A.select_audit_sample(recs)
    cov = A.audit_coverage(sample, [], n_scored=len(recs))
    assert cov.satisfied is False
    assert cov.status == A.HUMAN_AUDIT_PENDING
    assert len(cov.pending_record_ids) == cov.required
    assert cov.agreement_rate is None


def test_coverage_is_satisfied_only_by_real_adjudications():
    recs = [_rec(_case(f"c{i}", expected=V.REPAIR), V.REPAIR) for i in range(5)]
    sample = A.select_audit_sample(recs)
    cov = A.audit_coverage(sample, [_audit(i) for i in sample],
                           n_scored=len(recs))
    assert cov.satisfied and cov.status == "HUMAN_AUDIT_SATISFIED"
    assert cov.agreement_rate == 1.0
    assert cov.rejected_record_ids == ()


# --- ADVERSARIAL: the defects this repair closes -------------------------
def test_the_same_case_under_two_models_is_two_separate_decisions():
    """The central defect. One case, two configurations, two adjudications."""
    case = _case("shared", expected=V.REPAIR)
    a = _rec(case, V.PASS, cfg=CFG)          # gpt-4o-ish
    b = _rec(case, V.REPAIR, cfg=CFG_B)      # the other model
    assert a.record_id() != b.record_id(), (
        "the same case under two configurations must not collapse to one id")

    sample = A.select_audit_sample([a, b], fraction=1.0)
    assert len(sample) == 2
    assert {i.record_id for i in sample} == {a.record_id(), b.record_id()}

    # adjudicating ONE of them must not satisfy coverage for the other
    cov = A.audit_coverage(sample, [_audit(sample[0])], n_scored=2)
    assert cov.completed == 1 and cov.required == 2
    assert not cov.satisfied
    assert sample[1].record_id in cov.pending_record_ids


def test_an_unrelated_adjudication_cannot_satisfy_coverage():
    """An audit record for a decision that was never selected is REJECTED."""
    recs = [_rec(_case(f"c{i}", expected=V.REPAIR), V.REPAIR) for i in range(10)]
    sample = A.select_audit_sample(recs)
    stranger = A.HumanAuditRecord(
        case_id="not-in-sample", record_id="g1rec_totally_unrelated",
        supervisor_verdict="PASS", human_verdict="PASS", reviewer_id="h",
        reviewed_at="t", execution_id="e", severity=C.Severity.HIGH)
    cov = A.audit_coverage(sample, [stranger], n_scored=len(recs))
    assert cov.completed == 0, "an unselected decision must not count"
    assert not cov.satisfied
    assert cov.rejected_record_ids == ("g1rec_totally_unrelated",)


def test_strict_coverage_raises_on_a_non_member_adjudication():
    recs = [_rec(_case("c", expected=V.REPAIR), V.REPAIR)]
    sample = A.select_audit_sample(recs)
    stranger = A.HumanAuditRecord(
        case_id="x", record_id="g1rec_nope", supervisor_verdict="PASS",
        human_verdict="PASS", reviewer_id="h", reviewed_at="t",
        execution_id="e", severity=C.Severity.LOW)
    with pytest.raises(A.AuditMembershipError):
        A.audit_coverage(sample, [stranger], n_scored=1, strict=True)


def test_duplicate_adjudications_of_one_decision_count_once():
    recs = [_rec(_case(f"c{i}", expected=V.REPAIR), V.REPAIR) for i in range(10)]
    sample = A.select_audit_sample(recs)
    twice = [_audit(sample[0]), _audit(sample[0], reviewer="human-2")]
    cov = A.audit_coverage(sample, twice, n_scored=len(recs))
    assert cov.completed == 1


def test_the_audit_population_is_exactly_the_scored_population():
    """Outages and pending records must not consume audit budget."""
    scored = [_rec(_case(f"s{i}", expected=V.REPAIR), V.REPAIR) for i in range(10)]
    noise = [
        _rec(_case("out"), V.SUPERVISOR_UNAVAILABLE),
        _rec(_case("pol"), V.POLICY_VIOLATION),
        _rec(_case("wrk"), V.WORKER_UNAVAILABLE),
        _rec(_case("hum"), V.E4_HUMAN_REQUIRED),
        _rec(_case("pend", basis=C.GoldBasis.CONSENSUS_REVIEW), V.PASS),
    ]
    everything = scored + noise
    m = M.compute_metrics(everything)
    sample = A.select_audit_sample(everything)
    selected_ids = {i.record_id for i in sample}
    assert m.n_scored == 10
    for r in noise:
        assert r.record_id() not in selected_ids, r.case_id
    # the sample is drawn from exactly n_scored, so ceil applies to 10 not 15
    assert cov_required(m.n_scored) == len(sample)


def cov_required(n_scored: int) -> int:
    import math
    return max(1, math.ceil(n_scored * CRIT.MIN_HUMAN_AUDIT_FRACTION))


@pytest.mark.parametrize("n_scored,expect", [
    (1, 1), (4, 1), (5, 1), (6, 2), (10, 2), (11, 3), (17, 4), (34, 7),
])
def test_the_audit_fraction_is_a_true_minimum(n_scored, expect):
    """ceil, not round: 20% of 11 is 3, never 2."""
    recs = [_rec(_case(f"c{i}", expected=V.REPAIR), V.REPAIR)
            for i in range(n_scored)]
    sample = A.select_audit_sample(recs)
    assert len(sample) == expect
    assert len(sample) >= n_scored * CRIT.MIN_HUMAN_AUDIT_FRACTION


def test_disagreement_weighting_ranks_a_certification_above_a_routing_dispute():
    cheap = A.HumanAuditRecord(
        case_id="a", record_id="g1rec_a", supervisor_verdict="REPAIR",
        human_verdict="ESCALATE", reviewer_id="h", reviewed_at="t",
        execution_id="e", severity=C.Severity.LOW)
    expensive = A.HumanAuditRecord(
        case_id="b", record_id="g1rec_b", supervisor_verdict="PASS",
        human_verdict="REPAIR", reviewer_id="h", reviewed_at="t",
        execution_id="e", severity=C.Severity.SAFETY_CRITICAL)
    assert expensive.severity_weighted_disagreement > \
        cheap.severity_weighted_disagreement * 4


def test_the_audit_packet_carries_the_same_evidence_the_supervisor_saw():
    recs = [_rec(c, V.PASS) for c in CORP.development_cases()]
    sample = A.select_audit_sample(recs, fraction=1.0)  # noqa: E501
    pkt = A.audit_packet(sample, CORP.by_id())
    assert pkt["status"] == A.HUMAN_AUDIT_PENDING
    for item in pkt["items"]:
        assert item["packet"] == dict(CORP.by_id()[item["case_id"]].packet)


# =========================================================================== #
# STATUS DERIVATION  (cannot be chosen)
# =========================================================================== #
def test_no_records_is_blocked():
    m = M.compute_metrics([])
    cov = A.audit_coverage((), (), n_scored=0)
    d = R.measurement_status(m, cov)
    assert d.status == R.STATUS_BLOCKED
    assert R.BLOCKED_BY_SUPERVISOR_ACCESS in d.blockers


def test_all_outages_is_blocked_not_inconclusive():
    recs = [_rec(_case(f"c{i}"), V.SUPERVISOR_UNAVAILABLE) for i in range(5)]
    m = M.compute_metrics(recs)
    cov = A.audit_coverage((), (), n_scored=0)
    assert R.measurement_status(m, cov).status == R.STATUS_BLOCKED


def test_unmet_audit_forces_inconclusive_even_with_perfect_scores():
    """The pressure point: every raw number is ideal and the answer is still not
    COMPLETE."""
    recs = [_rec(_case(f"c{i}", expected=V.REPAIR), V.REPAIR)
            for i in range(200)]
    m = M.compute_metrics(recs)
    assert m.false_pass_count == 0 and m.exact_accuracy.value == 1.0
    sample = A.select_audit_sample(recs)
    cov = A.audit_coverage(sample, [], n_scored=m.n_scored)
    d = R.measurement_status(m, cov)
    assert d.status == R.STATUS_INCONCLUSIVE
    assert any("human audit incomplete" in b for b in d.blockers)


def test_small_scored_sample_forces_inconclusive():
    recs = [_rec(_case(f"c{i}", expected=V.REPAIR), V.REPAIR) for i in range(5)]
    m = M.compute_metrics(recs)
    sample = A.select_audit_sample(recs)
    done = [_audit(i, human_verdict=i.supervisor_verdict) for i in sample]
    cov = A.audit_coverage(sample, done, n_scored=m.n_scored)
    d = R.measurement_status(m, cov)
    assert d.status == R.STATUS_INCONCLUSIVE
    assert any("scored decisions" in b for b in d.blockers)


def test_complete_requires_both_audit_and_sample():
    n = CRIT.RECOMMENDED_THRESHOLD_FOR_HUMAN_APPROVAL["min_scored_decisions"]
    recs = [_rec(_case(f"c{i}", expected=V.REPAIR), V.REPAIR) for i in range(n)]
    m = M.compute_metrics(recs)
    sample = A.select_audit_sample(recs)
    done = [_audit(i, human_verdict=i.supervisor_verdict) for i in sample]
    cov = A.audit_coverage(sample, done, n_scored=m.n_scored)
    assert R.measurement_status(m, cov).status == R.STATUS_COMPLETE


def test_the_status_cannot_be_passed_in():
    """There is no parameter for it, by design."""
    import inspect
    sig = inspect.signature(R.measurement_status)
    assert list(sig.parameters) == ["metrics", "coverage"]
    sig2 = inspect.signature(R.build_report)
    assert "status" not in sig2.parameters


def test_no_authority_language_is_emitted():
    """G1 measures. It must not be able to say it granted anything."""
    recs = [_rec(_case(f"c{i}", expected=V.REPAIR), V.REPAIR) for i in range(3)]
    m = M.compute_metrics(recs)
    cov = A.audit_coverage(A.select_audit_sample(recs), [], n_scored=m.n_scored)
    rep = R.build_report(metrics=m, coverage=cov, records=recs, configs=[CFG])
    blob = json.dumps(rep)
    for forbidden in ("AUTHORITY_PROMOTED", "C1_ENABLED", "AUTONOMY_GRANTED"):
        assert forbidden not in blob
    assert rep["status"]["status"] in (
        R.STATUS_COMPLETE, R.STATUS_INCONCLUSIVE, R.STATUS_BLOCKED)


def test_the_report_embeds_the_frozen_criteria_and_taxonomy():
    recs = [_rec(_case("c", expected=V.REPAIR), V.REPAIR)]
    m = M.compute_metrics(recs)
    cov = A.audit_coverage((), (), n_scored=m.n_scored)
    rep = R.build_report(metrics=m, coverage=cov, records=recs, configs=[CFG])
    assert "frozen_at_candidate" not in rep["criteria"], (
        "the freeze anchor belongs in the preregistration pointer, not here")
    assert rep["taxonomy"]["accuracy_population"] == "SUPERVISOR_DECISION"
    assert rep["criteria"]["min_human_audit_fraction"] == 0.20
    # a recommendation, never an applied gate
    assert "recommended_threshold_for_human_approval" in rep["criteria"]


def test_render_text_reports_undefined_rather_than_zero():
    m = M.compute_metrics([_rec(_case(), V.POLICY_VIOLATION)])
    cov = A.audit_coverage((), (), n_scored=0)
    rep = R.build_report(metrics=m, coverage=cov,
                         records=[_rec(_case(), V.POLICY_VIOLATION)],
                         configs=[CFG])
    text = R.render_text(rep)
    assert "UNDEFINED" in text
    assert "0.0%" not in text


def test_no_false_pass_is_stated_explicitly_not_omitted():
    recs = [_rec(_case("c", expected=V.REPAIR), V.REPAIR)]
    m = M.compute_metrics(recs)
    cov = A.audit_coverage((), (), n_scored=m.n_scored)
    text = R.render_text(R.build_report(metrics=m, coverage=cov, records=recs,
                                        configs=[CFG]))
    assert "FALSE PASS CASES: none" in text
