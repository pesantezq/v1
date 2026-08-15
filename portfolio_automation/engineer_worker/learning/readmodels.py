"""Worker Control Center — LEARNING read-model projections (Phase 13).

Extends the controller-owned projection layer with the learning surfaces:

    RecentLessonsSummary · CapabilityCompetenceSummary · LessonTransferSummary
    · GraduationReadinessSummary

Same contract as ``ew0a_readmodels``: trusted, NON-AUTHORITATIVE, read-only by
construction, no secrets, and ``PENDING_BACKEND`` rather than a fabricated value
wherever no authoritative record exists.

No projection here may alter lessons, competence, readiness policy, or authority —
this module imports only read accessors and defines no mutator. The GUI consumes
these; it never writes, and there is no action endpoint anywhere in this path.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

from portfolio_automation.engineer_worker.learning import SCHEMA_KIND
from portfolio_automation.engineer_worker.learning.config import read_learning_config
from portfolio_automation.engineer_worker.learning.contracts import LessonStatus
from portfolio_automation.engineer_worker.learning.graduation import assess_all
from portfolio_automation.engineer_worker.learning.store import (
    load_competence, load_evaluations, load_lessons, load_retrievals)

LEARNING_READMODEL_SCHEMA_VERSION = "engineering.learning_readmodel.v0"
PENDING_BACKEND = "PENDING_BACKEND"


def _base(kind: str) -> dict[str, Any]:
    return {"schema_version": LEARNING_READMODEL_SCHEMA_VERSION,
            "schema_kind": SCHEMA_KIND, "read_model": kind}


@dataclass(frozen=True)
class RecentLessonsSummary:
    """Lesson store state. CANDIDATE and CONTRADICTED counts are shown, not hidden:
    a rejected lesson is evidence about the extractor's calibration."""
    active_count: int
    candidate_count: int
    superseded_count: int
    contradicted_count: int
    retired_count: int
    recent: list[dict[str, Any]] = field(default_factory=list)
    security_classification: str = "operational"

    def to_dict(self) -> dict[str, Any]:
        return {**_base("RecentLessonsSummary"), **asdict(self)}


@dataclass(frozen=True)
class CapabilityCompetenceSummary:
    """Per-capability competence. Deliberately carries no aggregate score."""
    worker_id: str
    capabilities: list[dict[str, Any]] = field(default_factory=list)
    total_observations: int = 0
    total_unsafe: int = 0
    note: str = "per-capability only; no aggregate worker intelligence score exists"

    def to_dict(self) -> dict[str, Any]:
        return {**_base("CapabilityCompetenceSummary"), **asdict(self)}


@dataclass(frozen=True)
class LessonTransferSummary:
    """Whether learning actually changed behavior.

    ``retrieved_but_ignored`` is the honest counterpart to ``successful_transfers``:
    without it, a lesson that was supplied and disregarded is indistinguishable from
    one that was never supplied."""
    retrievals: int
    lessons_supplied: int
    successful_transfers: int
    failed_transfers: int
    retrieved_but_ignored: int
    repeated_error_after_lesson: int
    transfer_rate: float | str

    def to_dict(self) -> dict[str, Any]:
        return {**_base("LessonTransferSummary"), **asdict(self)}


@dataclass(frozen=True)
class GraduationReadinessSummary:
    """Per-capability readiness. Displaying READY_FOR_CERTIFICATION grants nothing."""
    capabilities: list[dict[str, Any]] = field(default_factory=list)
    ready_for_certification: list[str] = field(default_factory=list)
    c1_enabled: bool = False
    readiness_is_not_certification: bool = True
    certification_is_not_authority: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {**_base("GraduationReadinessSummary"), **asdict(self)}


def build_recent_lessons_summary(repo_root: str | Path, limit: int = 10
                                 ) -> RecentLessonsSummary:
    lessons = load_lessons(repo_root)

    def count(status: LessonStatus) -> int:
        return sum(1 for l in lessons if l.status == status.value)

    recent = sorted(lessons, key=lambda l: (l.validated_at or l.created_at or "", l.lesson_id),
                    reverse=True)[:limit]
    return RecentLessonsSummary(
        active_count=count(LessonStatus.ACTIVE),
        candidate_count=count(LessonStatus.CANDIDATE),
        superseded_count=count(LessonStatus.SUPERSEDED),
        contradicted_count=count(LessonStatus.CONTRADICTED),
        retired_count=count(LessonStatus.RETIRED),
        recent=[{"lesson_id": l.lesson_id, "capability": l.capability,
                 "task_class": l.task_class, "subsystem": l.subsystem,
                 "status": l.status, "confidence": l.confidence,
                 "evidence_refs": len(l.evidence_refs), "principle": l.principle}
                for l in recent])


def build_capability_competence_summary(repo_root: str | Path, worker_id: str
                                        ) -> CapabilityCompetenceSummary:
    perfs = load_competence(repo_root)
    rows = [p.to_dict() for cap, p in sorted(perfs.items())]
    return CapabilityCompetenceSummary(
        worker_id=worker_id, capabilities=rows,
        total_observations=sum(p.observations for p in perfs.values()),
        total_unsafe=sum(p.unsafe for p in perfs.values()))


def build_lesson_transfer_summary(repo_root: str | Path) -> LessonTransferSummary:
    retrievals = load_retrievals(repo_root)
    evaluations = load_evaluations(repo_root)
    supplied = sum(len(r.get("lesson_ids") or []) for r in retrievals)
    with_lesson = [e for e in evaluations if e.get("lesson_retrieved")]
    success = sum(1 for e in with_lesson if e.get("lesson_transfer_success") is True)
    failed = sum(1 for e in with_lesson if e.get("lesson_transfer_success") is False)
    ignored = sum(1 for e in with_lesson if e.get("lesson_applied_correctly") is False)
    repeated = sum(1 for e in evaluations if e.get("repeated_error_after_lesson"))
    rate: float | str = (round(success / len(with_lesson), 4) if with_lesson
                         else PENDING_BACKEND)
    return LessonTransferSummary(
        retrievals=len(retrievals), lessons_supplied=supplied,
        successful_transfers=success, failed_transfers=failed,
        retrieved_but_ignored=ignored, repeated_error_after_lesson=repeated,
        transfer_rate=rate)


def build_graduation_readiness_summary(repo_root: str | Path, now: str
                                       ) -> GraduationReadinessSummary:
    cfg = read_learning_config(repo_root)
    readiness = assess_all(load_competence(repo_root), cfg.thresholds, now)
    rows = [r.to_dict() for r in readiness.values()]
    ready = sorted(cap for cap, r in readiness.items()
                   if r.state == "READY_FOR_CERTIFICATION")
    return GraduationReadinessSummary(
        capabilities=rows, ready_for_certification=ready,
        c1_enabled=False)          # structural: C1 is disabled


def build_learning_dashboard(repo_root: str | Path, worker_id: str, now: str
                             ) -> dict[str, Any]:
    """Assemble the learning half of the Worker Control Center dashboard."""
    root = Path(repo_root)
    return {
        **_base("LearningDashboard"),
        "recent_lessons": build_recent_lessons_summary(root).to_dict(),
        "capability_competence": build_capability_competence_summary(root, worker_id).to_dict(),
        "lesson_transfer": build_lesson_transfer_summary(root).to_dict(),
        "graduation_readiness": build_graduation_readiness_summary(root, now).to_dict(),
    }
