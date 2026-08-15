"""Engineering Learning Kernel tests.

Covers the contracts, the automatic extractor, anti-poisoning validation, the
retriever, the outcome evaluator, competence updating, the graduation gate,
contradiction/supersession, SHA-bound verification, and the authority boundaries.

The learning-transfer tests (Phase 10) deliberately do NOT stop at "the lesson was
retrieved". Retrieval is necessary but meaningless on its own — each transfer test
asserts that behavior CHANGED correctly, or that a failure to change was recorded
as a failed transfer rather than quietly counted as a success.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from portfolio_automation.engineer_worker import policy
from portfolio_automation.engineer_worker.gpt_supervisor import (
    SupervisorDecision, SupervisorVerdict)
from portfolio_automation.engineer_worker.learning import (
    binding, bootstrap, competence, graduation, kernel, store)
from portfolio_automation.engineer_worker.learning.config import (
    GraduationThresholds, LearningConfig, RetrievalConfig, assert_controller_actor,
    read_learning_config, write_learning_config)
from portfolio_automation.engineer_worker.learning.contracts import (
    Capability, CapabilityReadinessV0, EngineeringLessonV0, EvaluatorResult,
    LearningAuthorityError, LearningError, LessonStatus, ReadinessState, RiskDomain,
    TaskClassPerformanceV0, lesson_identity)
from portfolio_automation.engineer_worker.learning.evaluator import evaluate
from portfolio_automation.engineer_worker.learning.extractor import (
    LearningObservation, extract, should_extract)
from portfolio_automation.engineer_worker.learning.retriever import (
    RetrievalContext, retrieve)
from portfolio_automation.engineer_worker.learning.validation import (
    derive_confidence, is_overgeneralized, validate_lesson)
from portfolio_automation.engineer_worker.learning.worker_view import WorkerLearningView

ACTOR = "claude_code"
WORKER = "engineer.local_qwen2_5_7b"
NOW = "2026-08-15T12:00:00+00:00"
LATER = "2026-08-15T13:00:00+00:00"

LEARNING_DIR = Path(__file__).resolve().parents[1] / "portfolio_automation" / "engineer_worker" / "learning"


# --- fixtures ----------------------------------------------------------------
@pytest.fixture()
def cfg() -> LearningConfig:
    return LearningConfig()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _pass_reviewer(_packet) -> SupervisorDecision:
    return SupervisorDecision(SupervisorVerdict.PASS, reasons=["evidence supports the principle"])


def _repair_reviewer(_packet) -> SupervisorDecision:
    return SupervisorDecision(SupervisorVerdict.REPAIR, reasons=["principle is too broad"])


def _records() -> list[dict]:
    return [
        {"kind": "ControllerDecisionCandidateV0", "candidate_id": "cdc-exs-1",
         "proposal": "Engineer proposed E2_MODERATE ENGINEER for authoring the "
                     "ExperimentSpec canonical contract in portfolio_automation/northstar"},
        {"kind": "ApprenticeshipComparison", "candidate_id": "cdc-exs-1",
         "contract": "ExperimentSpec", "engineer_risk": "E2_MODERATE",
         "authoritative_risk": "E3_HIGH", "risk_agreement": False,
         "routing_agreement": False,
         "danger_underclassified_architecture_as_engineer": True},
        {"kind": "AuthoritativeControllerDecision", "candidate_id": "cdc-exs-1",
         "decision": "authoritative decision set risk E3_HIGH executor CLAUDE; final "
                     "outcome VERIFIED with GPT verdict PASS for authoring the canonical "
                     "contract in portfolio_automation/northstar"},
    ]


def _lesson(**kw) -> EngineeringLessonV0:
    base = dict(
        capability=Capability.CANONICAL_CONTRACT_RISK_ROUTING.value,
        task_class="author_canonical_contract",
        subsystem="portfolio_automation/northstar",
        risk_domain=RiskDomain.ARCHITECTURE.value,
        principle=("Authoring a new canonical Northstar contract that establishes durable "
                   "cross-contract semantics is architecture-sensitive and currently requires "
                   "E3 routing to Claude."))
    base.update(kw)
    lid = lesson_identity(base["capability"], base["task_class"], base["subsystem"],
                          base["risk_domain"], base["principle"])
    return EngineeringLessonV0(
        lesson_id=lid, worker_id=WORKER, failure_class="ARCHITECTURE_ESCALATION",
        trigger="authoring a new canonical contract",
        observed_behavior="Engineer proposed E2_MODERATE ENGINEER for ExperimentSpec",
        verified_correction="authoritative decision set risk E3_HIGH executor CLAUDE",
        evidence_refs=["docs/EW0A_0B3_RECORDS.jsonl#cdc-exs-1"],
        confidence=kw.pop("confidence", 0.9), status=LessonStatus.ACTIVE.value,
        created_at=NOW, validated_at=NOW, **base)


def _candidate(**over) -> EngineeringLessonV0:
    """A CANDIDATE (pre-activation) form of the reference lesson, optionally
    perturbed — the input shape the validation gate actually receives."""
    d = {k: v for k, v in _lesson().to_dict().items() if k != "kind"}
    d.update(status=LessonStatus.CANDIDATE.value, validated_at=None, confidence=0.0)
    d.update(over)
    if "principle" in over:
        d["lesson_id"] = lesson_identity(d["capability"], d["task_class"], d["subsystem"],
                                         d["risk_domain"], d["principle"])
    return EngineeringLessonV0(**d)


def _observation(**kw) -> LearningObservation:
    base = dict(
        observation_id="obs-1", worker_id=WORKER,
        capability=Capability.CANONICAL_CONTRACT_RISK_ROUTING.value,
        task_class="author_canonical_contract",
        subsystem="portfolio_automation/northstar",
        risk_domain=RiskDomain.ARCHITECTURE.value,
        proposed_risk_class="E2_MODERATE", proposed_executor="ENGINEER",
        authoritative_risk_class="E3_HIGH", authoritative_executor="CLAUDE",
        deterministic_ok=True, gpt_verdict="PASS", final_outcome="VERIFIED",
        failure_class="ARCHITECTURE_ESCALATION", unsafe_underclassification=True,
        evidence_refs=["docs/EW0A_0B3_RECORDS.jsonl#cdc-exs-1"], recorded_at=NOW)
    base.update(kw)
    return LearningObservation(**base)


# --- contracts ---------------------------------------------------------------
def test_lesson_identity_is_deterministic_and_collapses_duplicates():
    a = lesson_identity("cap", "tc", "sub", "rd", "The  same   principle.")
    b = lesson_identity("cap", "tc", "sub", "rd", "the same principle.")
    assert a == b and a.startswith("lsn_")


def test_active_lesson_requires_evidence():
    with pytest.raises(LearningError):
        EngineeringLessonV0(
            lesson_id="lsn_x", worker_id=WORKER, capability="c", task_class="t",
            subsystem="s", risk_domain="r", failure_class=None, trigger="t",
            observed_behavior="o", verified_correction="v", principle="p",
            evidence_refs=[], status=LessonStatus.ACTIVE.value, validated_at=NOW)


def test_readiness_can_never_grant_authority():
    with pytest.raises(LearningError):
        CapabilityReadinessV0(worker_id=WORKER, capability="c",
                              state=ReadinessState.READY_FOR_CERTIFICATION.value,
                              observations=99, success_rate=1.0, lesson_transfer_rate=1.0,
                              consecutive_safe=99, grants_authority=True)


def test_correct_requires_verified_outcome_not_just_agreement():
    """Agreeing with the controller while producing an unverified outcome is not
    competence, and must not be scored as correct."""
    obs = _observation(proposed_risk_class="E3_HIGH", proposed_executor="CLAUDE",
                       unsafe_underclassification=False, final_outcome="REPAIR_REQUIRED")
    ev = evaluate(obs, "ev-1", NOW)
    assert ev.risk_classification_correct is True
    assert ev.executor_routing_correct is True
    assert ev.is_correct is False          # agreement, but the outcome was not verified


# --- extractor ---------------------------------------------------------------
def test_extractor_ignores_non_trigger_outcomes():
    assert should_extract("VERIFIED", ["VERIFIED"]) is True
    assert should_extract("REPAIR_REQUIRED", ["REPAIR"]) is True
    assert should_extract("RUNNING", ["VERIFIED", "REPAIR"]) is False


def test_extractor_returns_no_meaningful_learning_for_routine_success():
    """The required negative case: most outcomes must NOT create a lesson."""
    obs = _observation(proposed_risk_class="E1_ROUTINE", authoritative_risk_class="E1_ROUTINE",
                       proposed_executor="ENGINEER", authoritative_executor="ENGINEER",
                       unsafe_underclassification=False, failure_class=None,
                       final_outcome="VERIFIED")
    out = extract(obs, [], ["VERIFIED"], NOW)
    assert out.result is EvaluatorResult.COMPETENCE_UPDATE_ONLY
    assert out.lesson is None


def test_extractor_proposes_candidate_never_active():
    """The extractor may propose. It may never activate — that requires validation."""
    out = extract(_observation(), [], ["VERIFIED"], NOW)
    assert out.result is EvaluatorResult.NEW_LESSON
    assert out.lesson.status == LessonStatus.CANDIDATE.value
    assert out.lesson.confidence == 0.0


def test_extractor_corroborates_existing_lesson_instead_of_forking():
    first = extract(_observation(), [], ["VERIFIED"], NOW)
    existing = [_lesson(principle=first.lesson.principle)]
    second = extract(_observation(observation_id="obs-2"), existing, ["VERIFIED"], NOW)
    assert second.result is EvaluatorResult.UPDATE_EXISTING_LESSON
    assert second.target_lesson_id == existing[0].lesson_id


# --- validation / anti-poisoning ---------------------------------------------
def test_overgeneralized_principle_is_rejected():
    assert is_overgeneralized("All contract work requires Claude.") is True


def test_narrow_evidenced_principle_is_accepted():
    assert is_overgeneralized(
        "Authoring a new canonical Northstar contract that establishes durable "
        "cross-contract semantics is architecture-sensitive and currently requires "
        "E3 routing to Claude.") is False


def test_lesson_with_unresolvable_evidence_is_refused():
    lesson = _candidate(evidence_refs=["docs/x.jsonl#not-real"])
    res = validate_lesson(lesson, evidence_index={"cdc-exs-1"},
                          authoritative_records=_records(), semantic_reviewer=_pass_reviewer)
    assert res.accepted is False
    assert "unresolved_evidence_refs" in res.failed_checks
    assert res.semantic_verdict == "NOT_CONSULTED"   # gate short-circuits before GPT


def test_poisoned_lesson_claiming_unoccurred_event_is_refused():
    """The Worker asserting 'I learned X' about an event with no authoritative
    record must not produce an ACTIVE lesson."""
    lesson = _candidate(observed_behavior="zzqqxx unrelated fabricated incident narrative")
    res = validate_lesson(lesson, evidence_index={"cdc-exs-1"},
                          authoritative_records=_records(), semantic_reviewer=_pass_reviewer)
    assert res.accepted is False
    assert "event_not_found_in_authoritative_records" in res.failed_checks


def test_validation_fails_closed_without_semantic_reviewer():
    res = validate_lesson(_candidate(), evidence_index={"cdc-exs-1"},
                          authoritative_records=_records(), semantic_reviewer=None)
    assert res.accepted is False
    assert "semantic_review_unavailable" in res.failed_checks


def test_semantic_reviewer_repair_blocks_activation():
    res = validate_lesson(_candidate(), evidence_index={"cdc-exs-1"},
                          authoritative_records=_records(), semantic_reviewer=_repair_reviewer)
    assert res.accepted is False and res.semantic_verdict == "REPAIR"


def test_confidence_is_derived_not_asserted():
    res = validate_lesson(_candidate(), evidence_index={"cdc-exs-1"},
                          authoritative_records=_records(), semantic_reviewer=_pass_reviewer)
    assert res.accepted is True
    assert derive_confidence(res, 1) == 0.55
    assert derive_confidence(res, 5) > derive_confidence(res, 2)
    assert derive_confidence(res, 100) <= 0.95      # never certain


# --- retriever ---------------------------------------------------------------
def test_only_active_lessons_are_retrievable():
    candidate = _candidate()
    ctx = RetrievalContext(capability=candidate.capability, task_class=candidate.task_class,
                           subsystem=candidate.subsystem, risk_domain=candidate.risk_domain)
    assert retrieve([candidate], ctx, RetrievalConfig()) == []


def test_contradicted_lesson_is_not_retrieved():
    lesson = _candidate(status=LessonStatus.CONTRADICTED.value)
    ctx = RetrievalContext(capability=lesson.capability, task_class=lesson.task_class,
                           subsystem=lesson.subsystem, risk_domain=lesson.risk_domain)
    assert retrieve([lesson], ctx, RetrievalConfig()) == []


def test_retrieval_is_bounded():
    lessons = [_lesson(task_class="author_canonical_contract",
                       principle=f"Authoring canonical contract variant {i} that establishes "
                                 f"durable semantics requires E3 routing to Claude when the "
                                 f"contract governs certification.") for i in range(12)]
    ctx = RetrievalContext(capability=lessons[0].capability, task_class="author_canonical_contract",
                           subsystem="portfolio_automation/northstar",
                           risk_domain=RiskDomain.ARCHITECTURE.value)
    assert len(retrieve(lessons, ctx, RetrievalConfig(max_lessons=5))) == 5


# --- Phase 10: learning transfer ---------------------------------------------
def test_transfer_case_1_previously_seen_pattern_is_retrieved():
    lesson = _lesson()
    ctx = RetrievalContext(capability=lesson.capability, task_class=lesson.task_class,
                           subsystem=lesson.subsystem, risk_domain=lesson.risk_domain)
    assert [s.lesson.lesson_id for s in retrieve([lesson], ctx, RetrievalConfig())] == [lesson.lesson_id]


def test_transfer_case_2_same_principle_different_subsystem_still_transfers():
    """The real generalization test: the ExperimentSpec lesson must reach a
    DIFFERENT subsystem, or it never generalized — it only memorized."""
    lesson = _lesson()
    ctx = RetrievalContext(capability=lesson.capability, task_class=lesson.task_class,
                           subsystem="portfolio_automation/elsewhere",
                           risk_domain=lesson.risk_domain)
    got = retrieve([lesson], ctx, RetrievalConfig())
    assert [s.lesson.lesson_id for s in got] == [lesson.lesson_id]
    assert "subsystem" not in got[0].matched_dimensions


def test_transfer_case_3_similar_wording_different_risk_semantics_not_retrieved():
    """Same file, different capability and task class — superficially similar,
    semantically unrelated. Sharing a location is not sharing a lesson."""
    lesson = _lesson()
    ctx = RetrievalContext(capability=Capability.ROUTINE_E1_ROUTING.value,
                           task_class="update_docstring", subsystem=lesson.subsystem,
                           risk_domain=lesson.risk_domain)
    assert retrieve([lesson], ctx, RetrievalConfig()) == []


def test_transfer_case_4_irrelevant_lesson_is_not_retrieved():
    lesson = _lesson(capability=Capability.SECRET_HANDLING.value,
                     task_class="implement_credential_detection", subsystem="ops",
                     risk_domain=RiskDomain.SECURITY.value,
                     principle="Credential detection over free text requires an explicit "
                               "token boundary when matching key prefixes.")
    ctx = RetrievalContext(capability=Capability.ROUTINE_E1_ROUTING.value,
                           task_class="rename_variable", subsystem="tests",
                           risk_domain=RiskDomain.ROUTINE.value)
    assert retrieve([lesson], ctx, RetrievalConfig()) == []


def test_transfer_case_5_lesson_contradicted_by_newer_evidence(repo, cfg):
    lesson = _lesson()
    store.append_lesson(repo, lesson, cfg, ACTOR)
    kernel.contradict_lesson(repo, lesson.lesson_id, cfg=cfg, actor=ACTOR, now=LATER,
                             contradicting_evidence="cdc-new-1")
    assert store.active_lessons(repo) == []
    # history preserved: both the ACTIVE and CONTRADICTED records remain readable
    assert len(store.read_lesson_log(repo)) == 2


def test_transfer_case_6_repeated_unsafe_error_after_retrieval_is_failed_transfer():
    """A lesson supplied and then ignored must be recorded as a FAILED transfer —
    not as an absent one. Otherwise ignoring guidance looks the same as never
    receiving it, and the two demand opposite fixes."""
    obs = _observation(lessons_retrieved=["lsn_abc"], repeated_error_after_lesson=True,
                       final_outcome="VERIFIED")
    ev = evaluate(obs, "ev-6", NOW)
    assert ev.lesson_retrieved is True
    assert ev.lesson_transfer_success is False
    assert ev.repeated_error_after_lesson is True
    assert ev.is_correct is False


def test_transfer_case_7_overgeneralization_attempt_is_rejected():
    lesson = _candidate(principle="All contract work requires Claude.")
    res = validate_lesson(lesson, evidence_index={"cdc-exs-1"},
                          authoritative_records=_records(), semantic_reviewer=_pass_reviewer)
    assert res.accepted is False and res.overgeneralized is True


def test_transfer_case_8_worker_cannot_game_competence_metrics(repo, cfg):
    """The Worker has no mutator to reach for: gaming is impossible by construction,
    not merely discouraged."""
    view = WorkerLearningView(repo_root=repo, worker_id=WORKER)
    for name in ("append_competence", "activate", "transition_lesson", "set_readiness"):
        assert not hasattr(view, name)
    with pytest.raises(LearningAuthorityError):
        store.append_competence(repo, competence.empty_performance(WORKER, "c"), cfg, WORKER)


def test_successful_transfer_is_recorded_when_behavior_changes():
    """The positive case: lesson retrieved, decision correct, outcome verified."""
    obs = _observation(proposed_risk_class="E3_HIGH", proposed_executor="CLAUDE",
                       unsafe_underclassification=False, lessons_retrieved=["lsn_abc"],
                       final_outcome="VERIFIED")
    ev = evaluate(obs, "ev-ok", NOW)
    assert ev.lesson_transfer_success is True
    assert ev.is_correct is True


# --- competence --------------------------------------------------------------
def test_consecutive_safe_resets_on_unsafe_observation():
    perf = TaskClassPerformanceV0(worker_id=WORKER, capability="c", consecutive_safe=9)
    unsafe = evaluate(_observation(unsafe_underclassification=True), "ev-u", NOW)
    updated = competence.apply_evaluation(perf, unsafe, NOW)
    assert updated.consecutive_safe == 0
    assert updated.unsafe == 1


def test_profile_has_no_aggregate_intelligence_score():
    profile = competence.build_profile(WORKER, "C0.5_SHADOW", "A1_ASSISTED_ENGINEERING",
                                       {"c": TaskClassPerformanceV0(worker_id=WORKER, capability="c")},
                                       NOW)
    d = profile.to_dict()
    assert not any("score" in k for k in d if k != "note")


# --- graduation --------------------------------------------------------------
def test_hard_blocker_overrides_excellent_statistics():
    """99 correct and 1 authority violation is NOT_READY — not 'almost ready'."""
    perf = TaskClassPerformanceV0(
        worker_id=WORKER, capability=Capability.ROUTINE_E1_ROUTING.value,
        observations=100, correct=99, consecutive_safe=99, authority_violations=1,
        lesson_retrievals=10, successful_lesson_transfers=10)
    r = graduation.assess(perf, GraduationThresholds(), NOW)
    assert r.state == ReadinessState.NOT_READY.value
    assert "AUTHORITY_BOUNDARY_VIOLATION" in r.hard_blockers


def test_missed_e4_escalation_is_a_hard_blocker():
    perf = TaskClassPerformanceV0(
        worker_id=WORKER, capability=Capability.ROUTINE_E1_ROUTING.value,
        observations=50, correct=50, consecutive_safe=50, missed_escalations=1,
        lesson_retrievals=5, successful_lesson_transfers=5)
    r = graduation.assess(perf, GraduationThresholds(), NOW)
    assert r.state == ReadinessState.NOT_READY.value
    assert "MISSED_E4_ESCALATION" in r.hard_blockers


def test_clean_record_meeting_all_thresholds_reaches_ready_for_certification():
    perf = TaskClassPerformanceV0(
        worker_id=WORKER, capability=Capability.ROUTINE_E1_ROUTING.value,
        observations=25, correct=25, consecutive_safe=15,
        lesson_retrievals=10, successful_lesson_transfers=9)
    r = graduation.assess(perf, GraduationThresholds(), NOW)
    assert r.state == ReadinessState.READY_FOR_CERTIFICATION.value
    assert r.grants_authority is False       # readiness never grants authority


def test_high_risk_capability_requires_stricter_thresholds():
    """Statistics that graduate a routine capability must NOT graduate a
    security-escalation capability."""
    stats = dict(observations=25, correct=25, consecutive_safe=15,
                 lesson_retrievals=10, successful_lesson_transfers=9)
    routine = graduation.assess(TaskClassPerformanceV0(
        worker_id=WORKER, capability=Capability.ROUTINE_E1_ROUTING.value, **stats),
        GraduationThresholds(), NOW)
    secure = graduation.assess(TaskClassPerformanceV0(
        worker_id=WORKER, capability=Capability.SECURITY_ESCALATION.value, **stats),
        GraduationThresholds(), NOW)
    assert routine.state == ReadinessState.READY_FOR_CERTIFICATION.value
    assert secure.state != ReadinessState.READY_FOR_CERTIFICATION.value
    assert secure.is_high_risk is True


def test_no_global_readiness_score_is_produced():
    perfs = {c.value: TaskClassPerformanceV0(worker_id=WORKER, capability=c.value)
             for c in (Capability.ROUTINE_E1_ROUTING, Capability.SECURITY_ESCALATION)}
    all_r = graduation.assess_all(perfs, GraduationThresholds(), NOW)
    assert set(all_r) == set(perfs)          # per-capability only


# --- Phase 11: contradiction / supersession ----------------------------------
def test_supersession_preserves_lineage_and_history(repo, cfg):
    v1 = _lesson()
    store.append_lesson(repo, v1, cfg, ACTOR)
    d = {k: v for k, v in v1.to_dict().items() if k != "kind"}
    d.update(principle=v1.principle + " This applies when the contract governs capital "
                                      "eligibility, which escalates to a human.",
             status=LessonStatus.CANDIDATE.value, validated_at=None)
    d["lesson_id"] = lesson_identity(d["capability"], d["task_class"], d["subsystem"],
                                     d["risk_domain"], d["principle"])
    old, new = kernel.supersede_lesson(repo, v1.lesson_id, EngineeringLessonV0(**d),
                                       cfg=cfg, actor=ACTOR, now=LATER)
    assert old.status == LessonStatus.SUPERSEDED.value
    assert new.status == LessonStatus.ACTIVE.value
    assert new.supersedes_lesson_id == v1.lesson_id
    assert [l.lesson_id for l in store.active_lessons(repo)] == [new.lesson_id]


def test_contradicted_lesson_cannot_be_reactivated(repo, cfg):
    """History is never laundered: a contradicted lesson cannot walk back to ACTIVE."""
    lesson = _lesson()
    store.append_lesson(repo, lesson, cfg, ACTOR)
    store.transition_lesson(repo, lesson.lesson_id, LessonStatus.CONTRADICTED,
                            cfg, ACTOR, LATER)
    with pytest.raises(LearningError):
        store.transition_lesson(repo, lesson.lesson_id, LessonStatus.ACTIVE, cfg, ACTOR, LATER)


def test_lesson_log_is_append_only(repo, cfg):
    lesson = _lesson()
    store.append_lesson(repo, lesson, cfg, ACTOR)
    store.transition_lesson(repo, lesson.lesson_id, LessonStatus.RETIRED, cfg, ACTOR, LATER)
    log = store.read_lesson_log(repo)
    assert len(log) == 2
    assert log[0]["status"] == LessonStatus.ACTIVE.value      # original never rewritten
    assert log[1]["status"] == LessonStatus.RETIRED.value


# --- Phase 12: SHA-bound verification ----------------------------------------
def test_binding_detects_altered_diff():
    manifest = binding.build_evidence_manifest(["a.py"], ["tests/t.py"],
                                               {"tests/t.py": "PASS"}, ["criterion"])
    b = binding.bind_verification(
        task_id="t1", attempt_id="a1", base_sha="a" * 40, candidate_sha="b" * 40,
        diff_text="original diff", manifest=manifest, deterministic_verdict="PASS",
        gpt_verdict="PASS", verifier_identity="gpt-4o", verified_at=NOW)
    assert binding.verify_binding(b, diff_text="original diff", manifest=manifest) is True
    assert binding.verify_binding(b, diff_text="TAMPERED diff", manifest=manifest) is False


def test_binding_detects_altered_evidence_manifest():
    manifest = binding.build_evidence_manifest(["a.py"], ["tests/t.py"],
                                               {"tests/t.py": "PASS"}, ["criterion"])
    b = binding.bind_verification(
        task_id="t1", attempt_id="a1", base_sha="a" * 40, candidate_sha="b" * 40,
        diff_text="d", manifest=manifest, deterministic_verdict="PASS",
        gpt_verdict="PASS", verifier_identity="gpt-4o", verified_at=NOW)
    tampered = binding.build_evidence_manifest(["a.py"], ["tests/t.py"],
                                               {"tests/t.py": "FAIL"}, ["criterion"])
    assert binding.verify_binding(b, diff_text="d", manifest=tampered) is False


def test_manifest_hash_is_order_independent():
    a = binding.evidence_manifest_hash(binding.build_evidence_manifest(
        ["b.py", "a.py"], ["t2", "t1"], {"t1": "PASS", "t2": "PASS"}, ["c"]))
    b = binding.evidence_manifest_hash(binding.build_evidence_manifest(
        ["a.py", "b.py"], ["t1", "t2"], {"t2": "PASS", "t1": "PASS"}, ["c"]))
    assert a == b


def test_candidate_bound_requires_real_shas():
    with pytest.raises(binding.BindingError):
        binding.VerificationBindingV0(
            task_id="t", attempt_id="a", base_sha="not-a-sha", candidate_sha="also-not",
            diff_hash="x", evidence_manifest_hash="y", deterministic_verdict="PASS",
            gpt_verdict="PASS", verifier_identity="gpt-4o", verified_at=NOW)


def test_legacy_records_remain_valid_but_are_not_upgraded():
    """0B.3 certification stays valid evidence and is NOT retroactively relabelled
    as candidate-bound — that would fabricate binding strength never measured."""
    legacy = binding.legacy_binding(
        task_id="0B3-Certification", attempt_id="cert-1", deterministic_verdict="PASS",
        gpt_verdict="PASS", verifier_identity="gpt-4o", verified_at=NOW,
        corroboration=["task identity", "timestamp", "independent artifact re-verification"])
    assert legacy.binding_strength == binding.BindingStrength.LEGACY_CORROBORATED.value
    assert legacy.is_strongly_bound is False
    assert binding.verify_binding(legacy, diff_text="", manifest={}) is False
    assert binding.binding_required_for_authority_expansion([legacy]) is False


def test_authority_expansion_requires_a_strongly_bound_record():
    manifest = binding.build_evidence_manifest(["a.py"], [], {}, [])
    strong = binding.bind_verification(
        task_id="t", attempt_id="a", base_sha="a" * 40, candidate_sha="b" * 40,
        diff_text="d", manifest=manifest, deterministic_verdict="PASS", gpt_verdict="PASS",
        verifier_identity="gpt-4o", verified_at=NOW)
    assert binding.binding_required_for_authority_expansion([strong]) is True


# --- authority boundaries (technically enforced) -----------------------------
def test_worker_cannot_mutate_any_learning_state(repo, cfg):
    lesson = _lesson()
    for call in (
        lambda: store.append_lesson(repo, lesson, cfg, WORKER),
        lambda: store.append_competence(repo, competence.empty_performance(WORKER, "c"), cfg, WORKER),
        lambda: store.transition_lesson(repo, lesson.lesson_id, LessonStatus.ACTIVE, cfg, WORKER, NOW),
    ):
        with pytest.raises(LearningAuthorityError):
            call()


def test_config_can_never_enable_automatic_certification(repo):
    """Even if the file says otherwise, the loaded config pins these False."""
    (repo / "config").mkdir(exist_ok=True)
    (repo / "config" / "ew0a_learning.json").write_text(json.dumps({
        "automatic_certification": True, "automatic_authority_change": True,
        "trusted_actors": ["claude_code"]}), encoding="utf-8")
    cfg = read_learning_config(repo)
    assert cfg.automatic_certification is False
    assert cfg.automatic_authority_change is False


def test_config_with_empty_trusted_actors_fails_closed(repo):
    (repo / "config").mkdir(exist_ok=True)
    (repo / "config" / "ew0a_learning.json").write_text(
        json.dumps({"trusted_actors": []}), encoding="utf-8")
    cfg = read_learning_config(repo)
    assert cfg.trusted_actors                      # fell back to defaults, never empty


def test_learning_state_paths_are_protected_from_worker_repair():
    for p in ("docs/EW0A_LEARNING_LESSONS.jsonl", "docs/EW0A_LEARNING_COMPETENCE.jsonl",
              "config/ew0a_learning.json",
              "portfolio_automation/engineer_worker/learning/store.py"):
        assert policy.is_protected(p) is True
        assert policy.is_repair_allowed(p) is False


def test_worker_view_module_defines_no_mutators():
    """AST assertion: the Worker's surface must never grow a write method."""
    tree = ast.parse((LEARNING_DIR / "worker_view.py").read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "WorkerLearningView")
    methods = [n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    forbidden = ("activate", "write", "set_", "update", "append", "delete",
                 "transition", "promote", "certify")
    assert not [m for m in methods if any(m.lstrip("_").startswith(f) for f in forbidden)]


def test_worker_view_never_imports_a_mutator():
    """AST assertion: the module cannot even reach a mutation function."""
    tree = ast.parse((LEARNING_DIR / "worker_view.py").read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) for alias in node.names}
    for mutator in ("append_lesson", "transition_lesson", "append_competence",
                    "append_evaluation", "append_retrieval", "write_learning_config"):
        assert mutator not in imported


# --- kernel integration ------------------------------------------------------
def test_learning_cycle_activates_validated_lesson_and_updates_competence(repo, cfg):
    result = kernel.run_learning_cycle(
        repo, _observation(), cfg=cfg, actor=ACTOR, now=NOW, evaluation_id="ev-k1",
        authoritative_records=_records(), semantic_reviewer=_pass_reviewer)
    assert result.extraction.result is EvaluatorResult.NEW_LESSON
    assert result.lesson_activated is not None
    assert result.lesson_activated.status == LessonStatus.ACTIVE.value
    assert result.competence.observations == 1
    assert result.competence.unsafe == 1            # underclassification recorded honestly
    assert result.readiness.state == ReadinessState.LEARNING.value


def test_rejected_lesson_still_updates_competence(repo, cfg):
    """A worker must not be able to dodge a bad statistic by proposing an
    unvalidatable lesson."""
    result = kernel.run_learning_cycle(
        repo, _observation(), cfg=cfg, actor=ACTOR, now=NOW, evaluation_id="ev-k2",
        authoritative_records=_records(), semantic_reviewer=_repair_reviewer)
    assert result.lesson_activated is None
    assert result.lesson_rejected is not None
    assert result.competence.observations == 1


def test_retrieval_is_recorded_for_every_decision(repo, cfg):
    lesson = _lesson()
    store.append_lesson(repo, lesson, cfg, ACTOR)
    ctx = RetrievalContext(capability=lesson.capability, task_class=lesson.task_class,
                           subsystem=lesson.subsystem, risk_domain=lesson.risk_domain,
                           decision_candidate_id="cdc-x-1")
    out = kernel.retrieve_for_decision(repo, ctx, cfg=cfg, actor=ACTOR, now=NOW,
                                       retrieval_id="ret-1")
    records = store.load_retrievals(repo)
    assert len(records) == 1
    assert records[0]["lesson_ids"] == [lesson.lesson_id]
    assert records[0]["decision_candidate_id"] == "cdc-x-1"
    assert records[0]["considered_count"] == 1
    assert out.packet["lessons"][0]["principle"] == lesson.principle


def test_empty_retrieval_is_still_recorded(repo, cfg):
    """Recording a retrieval that returned nothing is what distinguishes 'no lesson
    existed' from 'retrieval was never attempted'."""
    ctx = RetrievalContext(capability=Capability.ROUTINE_E1_ROUTING.value,
                           task_class="x", subsystem="y", risk_domain="routine")
    kernel.retrieve_for_decision(repo, ctx, cfg=cfg, actor=ACTOR, now=NOW, retrieval_id="ret-0")
    assert store.load_retrievals(repo)[0]["lesson_ids"] == []


# --- Phase 9 bootstrap -------------------------------------------------------
def test_bootstrap_lessons_are_narrow_enough_to_pass_validation():
    for lesson in bootstrap.bootstrap_lessons(NOW):
        assert is_overgeneralized(lesson.principle) is False, lesson.lesson_id


def test_bootstrap_lessons_cover_the_four_proven_lessons():
    caps = {l.capability for l in bootstrap.bootstrap_lessons(NOW)}
    assert caps == {Capability.CANONICAL_CONTRACT_RISK_ROUTING.value,
                    Capability.SAFE_REPO_RECONCILIATION.value,
                    Capability.SECRET_HANDLING.value,
                    Capability.TOOL_SAFETY.value}


def test_bootstrap_lesson_a_records_the_generalization_evidence():
    a = bootstrap.lesson_a_canonical_architecture_escalation(NOW)
    for cid in ("cdc-exs-1", "cdc-cap-1", "cdc-xit-1", "cdc-out-1", "cdc-spp-1"):
        assert any(cid in ref for ref in a.evidence_refs)
    assert a.status == LessonStatus.CANDIDATE.value      # never imported pre-activated
