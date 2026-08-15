"""Lesson retriever (Phase 4).

Before each meaningful Engineer task or C0.5 controller proposal:

    classify context -> retrieve relevant ACTIVE lessons -> bounded packet ->
    record EXACTLY which lessons were supplied.

STRUCTURED retrieval only. No vector database: the match dimensions here
(capability / task_class / subsystem / risk_domain / failure_class) are the same
dimensions the authority model already routes on, so a structured match is both
explainable and auditable. Introduce embeddings only when evidence proves this
insufficient — an unexplainable retrieval is not auditable, and retrieval is part
of the evidence chain.

Only ACTIVE lessons are retrievable: CANDIDATE lessons are unvalidated, and
CONTRADICTED / SUPERSEDED / RETIRED lessons must never re-enter a decision.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from portfolio_automation.engineer_worker.learning.config import RetrievalConfig
from portfolio_automation.engineer_worker.learning.contracts import (
    EngineeringLessonV0, LessonRetrievalRecordV0)

# Dimension weights. Capability and task_class dominate: they are what the rule is
# ABOUT. Subsystem and risk_domain support cross-subsystem transfer at lower weight
# — a principle that only ever matches its original subsystem never generalizes.
_WEIGHTS: dict[str, float] = {
    "capability": 3.0,
    "task_class": 2.5,
    "risk_domain": 1.5,
    "failure_class": 1.25,
    "subsystem": 1.0,
}


@dataclass(frozen=True)
class RetrievalContext:
    """The classified context of the decision about to be made."""
    capability: str
    task_class: str
    subsystem: str
    risk_domain: str
    failure_class: str | None = None
    task_id: str | None = None
    decision_candidate_id: str | None = None

    def fingerprint(self) -> str:
        core = json.dumps({"capability": self.capability, "task_class": self.task_class,
                           "subsystem": self.subsystem, "risk_domain": self.risk_domain,
                           "failure_class": self.failure_class},
                          ensure_ascii=True, sort_keys=True)
        return "ctx_" + hashlib.sha256(core.encode("utf-8")).hexdigest()[:24]


@dataclass
class ScoredLesson:
    lesson: EngineeringLessonV0
    score: float
    matched_dimensions: list[str] = field(default_factory=list)


def score_lesson(lesson: EngineeringLessonV0, ctx: RetrievalContext,
                 cfg: RetrievalConfig) -> ScoredLesson:
    """Rank by specificity (how many dimensions match, weighted) then evidence
    confidence. A lesson that matches only the weakest dimension scores near zero
    and will not displace a directly relevant one."""
    matched: list[str] = []
    score = 0.0
    if cfg.match_capability and lesson.capability == ctx.capability:
        matched.append("capability"); score += _WEIGHTS["capability"]
    if cfg.match_task_class and lesson.task_class == ctx.task_class:
        matched.append("task_class"); score += _WEIGHTS["task_class"]
    if cfg.match_risk_domain and lesson.risk_domain == ctx.risk_domain:
        matched.append("risk_domain"); score += _WEIGHTS["risk_domain"]
    if (cfg.match_failure_class and ctx.failure_class
            and lesson.failure_class == ctx.failure_class):
        matched.append("failure_class"); score += _WEIGHTS["failure_class"]
    if cfg.match_subsystem and lesson.subsystem == ctx.subsystem:
        matched.append("subsystem"); score += _WEIGHTS["subsystem"]

    if score > 0.0:
        score += lesson.confidence * 1.5          # evidence confidence
        score += cfg.recency_weight * min(1.0, lesson.version / 5.0)
    return ScoredLesson(lesson=lesson, score=round(score, 4), matched_dimensions=matched)


# Dimensions that say what a lesson is ABOUT, as opposed to where it happened to
# occur. A lesson is relevant only if it matches at least one of them.
_TOPIC_DIMENSIONS = frozenset({"capability", "task_class"})


def is_relevant(scored: ScoredLesson) -> bool:
    """Relevance requires a TOPIC match (capability or task_class).

    Matching only subsystem/risk_domain is the classic false positive: two tasks in
    the same file with entirely different risk semantics look similar and are not.
    Sharing a location is not sharing a lesson."""
    return bool(_TOPIC_DIMENSIONS.intersection(scored.matched_dimensions))


def retrieve(active: list[EngineeringLessonV0], ctx: RetrievalContext,
             cfg: RetrievalConfig) -> list[ScoredLesson]:
    """Return at most ``cfg.max_lessons`` relevant ACTIVE lessons, best first.

    Never dumps the whole store into context: an oversized packet is functionally
    the same as no retrieval, because nothing in it is salient."""
    if not cfg.enabled:
        return []
    scored = [score_lesson(l, ctx, cfg) for l in active if l.is_retrievable]
    relevant = [s for s in scored if is_relevant(s)]
    relevant.sort(key=lambda s: (-s.score, s.lesson.lesson_id))
    return relevant[:max(0, cfg.max_lessons)]


def build_lesson_packet(scored: list[ScoredLesson], ctx: RetrievalContext) -> dict[str, Any]:
    """The bounded packet supplied to the decision. Carries the PRINCIPLE and its
    trigger — not the full incident history, which would crowd out the rule."""
    return {
        "context": {"capability": ctx.capability, "task_class": ctx.task_class,
                    "subsystem": ctx.subsystem, "risk_domain": ctx.risk_domain,
                    "failure_class": ctx.failure_class},
        "lessons": [
            {"lesson_id": s.lesson.lesson_id, "trigger": s.lesson.trigger,
             "principle": s.lesson.principle, "confidence": s.lesson.confidence,
             "matched_dimensions": s.matched_dimensions,
             "evidence_refs": s.lesson.evidence_refs[:5]}
            for s in scored
        ],
        "note": ("These lessons are CONTEXT, not authority. They may inform the "
                 "proposal; they never change the risk model, routing rules, or "
                 "any authority boundary."),
    }


def build_retrieval_record(scored: list[ScoredLesson], ctx: RetrievalContext,
                           retrieval_id: str, now: str, considered: int
                           ) -> LessonRetrievalRecordV0:
    """Persist exactly what was supplied.

    Without this record, 'the lesson existed but was not retrieved' and 'the lesson
    was retrieved and ignored' are indistinguishable — and they demand opposite
    fixes (retrieval tuning vs. worker behavior)."""
    dims = sorted({d for s in scored for d in s.matched_dimensions})
    return LessonRetrievalRecordV0(
        retrieval_id=retrieval_id, task_id=ctx.task_id,
        decision_candidate_id=ctx.decision_candidate_id,
        lesson_ids=[s.lesson.lesson_id for s in scored], retrieved_at=now,
        match_dimensions=dims, rank=[s.lesson.lesson_id for s in scored],
        context_fingerprint=ctx.fingerprint(), considered_count=considered)
