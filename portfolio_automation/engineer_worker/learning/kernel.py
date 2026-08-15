"""The Learning Kernel pipeline (Phase 8) — integration with the C0.5 shadow controller.

Runs automatically around every significant controller decision:

    state before decision -> retrieve relevant lessons -> Engineer C0.5 proposal ->
    persist candidate -> Claude authoritative decision -> execute -> GPT verification
    -> OutcomeEvaluator -> LessonExtractor -> CompetenceUpdater -> GraduationGate

This is NOT a second orchestration framework. ``ew0a_loop`` still owns task
execution, routing, and certification; the kernel wraps it with the two learning
touchpoints — retrieval BEFORE a decision and evaluation AFTER a verified outcome.

The Engineer remains non-authoritative throughout. No C1 dispatch authority is
created here, and no code path in this module can change authority state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from portfolio_automation.engineer_worker.learning import competence as competence_mod
from portfolio_automation.engineer_worker.learning import graduation, store
from portfolio_automation.engineer_worker.learning.config import (
    LearningConfig, assert_controller_actor, read_learning_config)
from portfolio_automation.engineer_worker.learning.contracts import (
    CapabilityReadinessV0, EngineeringLessonV0, EvaluatorResult, LessonStatus,
    OutcomeEvaluationV0, TaskClassPerformanceV0, WorkerCompetenceProfileV0)
from portfolio_automation.engineer_worker.learning.evaluator import evaluate
from portfolio_automation.engineer_worker.learning.extractor import (
    ExtractionOutcome, LearningObservation, extract)
from portfolio_automation.engineer_worker.learning.retriever import (
    RetrievalContext, ScoredLesson, build_lesson_packet, build_retrieval_record, retrieve)
from portfolio_automation.engineer_worker.learning.validation import (
    SemanticReviewer, derive_confidence, validate_lesson)


@dataclass
class RetrievalOutput:
    packet: dict[str, Any]
    scored: list[ScoredLesson]
    record_id: str
    lesson_ids: list[str] = field(default_factory=list)


@dataclass
class LearningCycleResult:
    """Everything one observation produced. Reported honestly — a rejected lesson
    and a no-learning verdict are results, not omissions."""
    evaluation: OutcomeEvaluationV0
    extraction: ExtractionOutcome
    lesson_activated: EngineeringLessonV0 | None = None
    lesson_rejected: dict[str, Any] | None = None
    competence: TaskClassPerformanceV0 | None = None
    readiness: CapabilityReadinessV0 | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation": self.evaluation.to_dict(),
            "extraction": self.extraction.to_dict(),
            "lesson_activated": (self.lesson_activated.lesson_id
                                 if self.lesson_activated else None),
            "lesson_rejected": self.lesson_rejected,
            "competence": self.competence.to_dict() if self.competence else None,
            "readiness": self.readiness.to_dict() if self.readiness else None,
        }


def retrieve_for_decision(repo_root: str | Path, ctx: RetrievalContext, *,
                          cfg: LearningConfig, actor: str, now: str, retrieval_id: str
                          ) -> RetrievalOutput:
    """Step 1: supply bounded, relevant context BEFORE a decision is made, and
    record exactly what was supplied."""
    assert_controller_actor(cfg, actor)
    active = store.active_lessons(repo_root)
    scored = retrieve(active, ctx, cfg.retrieval)
    record = build_retrieval_record(scored, ctx, retrieval_id, now, considered=len(active))
    store.append_retrieval(repo_root, record, cfg, actor)
    return RetrievalOutput(packet=build_lesson_packet(scored, ctx), scored=scored,
                           record_id=retrieval_id, lesson_ids=list(record.lesson_ids))


def run_learning_cycle(repo_root: str | Path, obs: LearningObservation, *,
                       cfg: LearningConfig | None = None,
                       actor: str, now: str, evaluation_id: str,
                       authoritative_records: list[dict[str, Any]] | None = None,
                       semantic_reviewer: SemanticReviewer | None = None,
                       scope_items: list[str] | None = None,
                       acceptance_items: list[str] | None = None,
                       verification_items: list[str] | None = None,
                       authority_violation: bool = False,
                       false_certification: bool = False,
                       security_escalation_failure: bool = False,
                       ) -> LearningCycleResult:
    """Steps 2-5: evaluate the outcome, extract a lesson if warranted, validate it
    independently, update competence, and re-assess readiness.

    Ordering is deliberate: competence is updated from the EVALUATION, not from the
    lesson. A rejected lesson candidate must still update competence, or a worker
    could avoid a bad statistic by proposing an unvalidatable lesson."""
    root = Path(repo_root)
    cfg = cfg or read_learning_config(root)
    assert_controller_actor(cfg, actor)
    records = authoritative_records if authoritative_records is not None else []

    # --- evaluate -----------------------------------------------------------
    ev = evaluate(obs, evaluation_id, now, scope_items=scope_items,
                  acceptance_items=acceptance_items, verification_items=verification_items)
    store.append_evaluation(root, ev, cfg, actor)

    # --- extract ------------------------------------------------------------
    existing = store.load_lessons(root)
    outcome = extract(obs, existing, cfg.auto_extract_after, now)

    activated: EngineeringLessonV0 | None = None
    rejected: dict[str, Any] | None = None

    if outcome.result is EvaluatorResult.NEW_LESSON and outcome.lesson is not None:
        idx = store.evidence_index(records)
        result = validate_lesson(outcome.lesson, evidence_index=idx,
                                 authoritative_records=records,
                                 require_evidence=cfg.require_evidence,
                                 semantic_reviewer=semantic_reviewer)
        # The candidate is persisted either way: a rejected candidate is evidence
        # about the extractor and must not vanish from the audit trail.
        store.append_lesson(root, outcome.lesson, cfg, actor)
        if result.accepted:
            confident = _with_confidence(outcome.lesson, derive_confidence(result, 1))
            store.append_lesson(root, confident, cfg, actor)
            activated = store.transition_lesson(root, confident.lesson_id,
                                                LessonStatus.ACTIVE, cfg, actor, now)
        else:
            rejected = result.to_dict()

    elif outcome.result is EvaluatorResult.UPDATE_EXISTING_LESSON and outcome.target_lesson_id:
        current = store.get_lesson(root, outcome.target_lesson_id)
        if current is not None and current.is_retrievable:
            corroborated = _corroborate(current, obs.evidence_refs, now)
            store.append_lesson(root, corroborated, cfg, actor)
            activated = corroborated

    # --- competence ---------------------------------------------------------
    perf = None
    readiness = None
    if cfg.competence_update_after_outcome:
        current = store.load_competence(root).get(
            obs.capability, competence_mod.empty_performance(obs.worker_id, obs.capability))
        perf = competence_mod.apply_evaluation(
            current, ev, now, recent_window_size=cfg.recent_window_size,
            authority_violation=authority_violation, false_certification=false_certification,
            security_escalation_failure=security_escalation_failure,
            first_pass=obs.first_pass, repaired=obs.attempt_count > 1)
        store.append_competence(root, perf, cfg, actor)
        if cfg.auto_assess_readiness:
            readiness = graduation.assess(perf, cfg.thresholds, now)

    return LearningCycleResult(evaluation=ev, extraction=outcome, lesson_activated=activated,
                               lesson_rejected=rejected, competence=perf, readiness=readiness)


def _with_confidence(lesson: EngineeringLessonV0, confidence: float) -> EngineeringLessonV0:
    from dataclasses import replace
    return replace(lesson, confidence=confidence)


def _corroborate(lesson: EngineeringLessonV0, new_refs: list[str], now: str
                 ) -> EngineeringLessonV0:
    """Corroboration raises confidence and records the additional evidence WITHOUT
    rewriting the principle — an existing rule strengthened by a new observation is
    the same rule."""
    from dataclasses import replace
    refs = list(dict.fromkeys([*lesson.evidence_refs, *new_refs]))
    bump = min(0.95, round(lesson.confidence + 0.10, 4))
    return replace(lesson, evidence_refs=refs, confidence=bump,
                   version=lesson.version + 1, validated_at=now)


def build_competence_profile(repo_root: str | Path, worker_id: str, *,
                             controller_level: str, ew_authority: str, now: str
                             ) -> WorkerCompetenceProfileV0:
    return competence_mod.build_profile(worker_id, controller_level, ew_authority,
                                        store.load_competence(repo_root), now)


def assess_all_readiness(repo_root: str | Path, *, cfg: LearningConfig | None = None,
                         now: str) -> dict[str, CapabilityReadinessV0]:
    root = Path(repo_root)
    cfg = cfg or read_learning_config(root)
    return graduation.assess_all(store.load_competence(root), cfg.thresholds, now)


def contradict_lesson(repo_root: str | Path, lesson_id: str, *, cfg: LearningConfig,
                      actor: str, now: str, contradicting_evidence: str
                      ) -> EngineeringLessonV0:
    """Phase 11: newer verified evidence contradicts an ACTIVE lesson.

    Recorded as a transition, never as a rewrite — the original lesson and the
    evidence that produced it stay in the log."""
    return store.transition_lesson(repo_root, lesson_id, LessonStatus.CONTRADICTED,
                                   cfg, actor, now,
                                   contradicts_lesson_id=contradicting_evidence)


def supersede_lesson(repo_root: str | Path, old_lesson_id: str,
                     new_lesson: EngineeringLessonV0, *, cfg: LearningConfig,
                     actor: str, now: str) -> tuple[EngineeringLessonV0, EngineeringLessonV0]:
    """Phase 11: replace lesson v1 with v2, preserving lineage in both directions.

    Retrieval will prefer the new lesson (only ACTIVE lessons are retrievable) while
    the superseded one remains readable for audit."""
    from dataclasses import replace
    linked = replace(new_lesson, supersedes_lesson_id=old_lesson_id)
    store.append_lesson(repo_root, linked, cfg, actor)
    activated = store.transition_lesson(repo_root, linked.lesson_id, LessonStatus.ACTIVE,
                                        cfg, actor, now)
    superseded = store.transition_lesson(repo_root, old_lesson_id, LessonStatus.SUPERSEDED,
                                         cfg, actor, now)
    return superseded, activated
