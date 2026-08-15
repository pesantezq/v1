"""Learning Kernel contracts (Phase 1).

Engineering-organization records — NOT canonical Northstar investment contracts.

    EngineeringLessonV0        a reusable principle derived from verified evidence
    LessonRetrievalRecordV0    exactly which lessons were supplied to a decision
    OutcomeEvaluationV0        proposal vs authoritative vs verified outcome
    TaskClassPerformanceV0     per-capability competence statistics
    WorkerCompetenceProfileV0  a summary over TaskClassPerformanceV0 records
    CapabilityReadinessV0      graduation readiness (readiness != certification)

Identity is deterministic: a lesson's id is a sha256 over its semantic core, so the
same principle derived twice is the same lesson and cannot silently fork.

``experimental_noncanonical``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

from portfolio_automation.engineer_worker.learning import SCHEMA_KIND

LESSON_SCHEMA_VERSION = "engineering.lesson.v0"
RETRIEVAL_SCHEMA_VERSION = "engineering.lesson_retrieval.v0"
EVALUATION_SCHEMA_VERSION = "engineering.outcome_evaluation.v0"
PERFORMANCE_SCHEMA_VERSION = "engineering.task_class_performance.v0"
PROFILE_SCHEMA_VERSION = "engineering.worker_competence_profile.v0"
READINESS_SCHEMA_VERSION = "engineering.capability_readiness.v0"


class LearningError(ValueError):
    """Deterministic, fail-closed Learning Kernel error."""


class LearningAuthorityError(PermissionError):
    """Raised when a non-controller actor attempts to mutate learning state."""


# --- capability taxonomy -----------------------------------------------------
class Capability(str, Enum):
    """Capabilities are tracked SEPARATELY. There is deliberately no single
    generic 'worker intelligence' score — competence in one capability says
    nothing about another."""
    ROUTINE_E1_ROUTING = "routine_e1_routing"
    SAFE_REPO_RECONCILIATION = "safe_repo_reconciliation"
    CANONICAL_CONTRACT_RISK_ROUTING = "canonical_contract_risk_routing"
    BOUNDED_REPAIR_MANAGEMENT = "bounded_repair_management"
    ACCEPTANCE_CRITERIA_DESIGN = "acceptance_criteria_design"
    VERIFICATION_PLANNING = "verification_planning"
    SECURITY_ESCALATION = "security_escalation"
    CAPITAL_GOVERNANCE_ESCALATION = "capital_governance_escalation"
    TOOL_SAFETY = "tool_safety"
    SECRET_HANDLING = "secret_handling"


# Capabilities whose failure modes are catastrophic rather than merely costly.
# These carry stricter graduation thresholds (see graduation.py).
HIGH_RISK_CAPABILITIES = frozenset({
    Capability.SECURITY_ESCALATION,
    Capability.CAPITAL_GOVERNANCE_ESCALATION,
    Capability.CANONICAL_CONTRACT_RISK_ROUTING,
    Capability.SECRET_HANDLING,
})


class RiskDomain(str, Enum):
    ARCHITECTURE = "architecture"
    SECURITY = "security"
    GOVERNANCE = "governance"
    CAPITAL = "capital"
    REPOSITORY = "repository"
    TESTING = "testing"
    TOOLING = "tooling"
    ROUTINE = "routine"


class LessonStatus(str, Enum):
    CANDIDATE = "CANDIDATE"        # proposed; NOT yet usable for retrieval
    ACTIVE = "ACTIVE"              # evidence-validated; retrievable
    SUPERSEDED = "SUPERSEDED"      # replaced by a newer version
    CONTRADICTED = "CONTRADICTED"  # newer verified evidence contradicts it
    RETIRED = "RETIRED"            # deliberately withdrawn


# Only ACTIVE lessons may be retrieved into a decision context.
RETRIEVABLE_STATUSES = frozenset({LessonStatus.ACTIVE})


class EvaluatorResult(str, Enum):
    """What the automatic extractor concluded. NO_MEANINGFUL_LEARNING is required
    and expected — most outcomes should NOT create a lesson."""
    NEW_LESSON = "NEW_LESSON"
    UPDATE_EXISTING_LESSON = "UPDATE_EXISTING_LESSON"
    COMPETENCE_UPDATE_ONLY = "COMPETENCE_UPDATE_ONLY"
    NO_MEANINGFUL_LEARNING = "NO_MEANINGFUL_LEARNING"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def lesson_identity(capability: str, task_class: str, subsystem: str,
                    risk_domain: str, principle: str) -> str:
    """Deterministic lesson id over the semantic core. Two derivations of the same
    principle in the same context collapse to ONE lesson (no silent forking)."""
    core = json.dumps(
        {"capability": capability, "task_class": task_class, "subsystem": subsystem,
         "risk_domain": risk_domain, "principle": " ".join(principle.split()).lower()},
        ensure_ascii=True, sort_keys=True)
    return "lsn_" + _sha256(core)[:32]


@dataclass(frozen=True)
class EngineeringLessonV0:
    """A reusable principle derived from verified evidence.

    A lesson may change what future context a decision SEES. It may never change
    what authority a decision HAS."""
    lesson_id: str
    worker_id: str
    capability: str                       # Capability value
    task_class: str                       # e.g. "author_canonical_contract"
    subsystem: str                        # e.g. "portfolio_automation/northstar"
    risk_domain: str                      # RiskDomain value
    failure_class: str | None             # ew0a.FailureClass value, if any
    trigger: str                          # the recognizable situation
    observed_behavior: str                # what actually happened (evidence-backed)
    verified_correction: str              # what the authoritative outcome established
    principle: str                        # the transferable rule
    evidence_refs: list[str] = field(default_factory=list)
    confidence: float = 0.0               # 0..1, derived from evidence, never self-asserted
    status: str = LessonStatus.CANDIDATE.value
    created_at: str | None = None
    validated_at: str | None = None
    supersedes_lesson_id: str | None = None
    contradicts_lesson_id: str | None = None
    version: int = 1
    origin: str = "extractor"             # extractor | bootstrap | controller
    schema_version: str = LESSON_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND

    def __post_init__(self) -> None:
        if not self.lesson_id.startswith("lsn_"):
            raise LearningError(f"malformed lesson_id: {self.lesson_id!r}")
        if not 0.0 <= self.confidence <= 1.0:
            raise LearningError(f"confidence out of range: {self.confidence}")
        if self.status not in {s.value for s in LessonStatus}:
            raise LearningError(f"unknown lesson status: {self.status!r}")
        if self.status == LessonStatus.ACTIVE.value and not self.evidence_refs:
            # Structural anti-poisoning: an ACTIVE lesson ALWAYS carries evidence.
            raise LearningError("ACTIVE lesson requires at least one evidence ref")
        if self.status == LessonStatus.ACTIVE.value and not self.validated_at:
            raise LearningError("ACTIVE lesson requires validated_at")

    @property
    def is_retrievable(self) -> bool:
        return LessonStatus(self.status) in RETRIEVABLE_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "EngineeringLessonV0", **asdict(self)}


@dataclass(frozen=True)
class LessonRetrievalRecordV0:
    """What was supplied to a decision. Persisting this is what makes the
    difference between 'the lesson was never retrieved' and 'the lesson was
    retrieved and the Worker ignored it' measurable rather than guessed."""
    retrieval_id: str
    task_id: str | None
    decision_candidate_id: str | None
    lesson_ids: list[str]
    retrieved_at: str
    match_dimensions: list[str]
    rank: list[str]                       # lesson_ids in supplied rank order
    context_fingerprint: str
    considered_count: int = 0             # ACTIVE lessons examined before ranking
    schema_version: str = RETRIEVAL_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "LessonRetrievalRecordV0", **asdict(self)}


@dataclass(frozen=True)
class OutcomeEvaluationV0:
    """Compares Engineer proposal vs Claude authoritative decision vs deterministic
    verification vs GPT verdict vs final outcome.

    Agreement with Claude is NOT the sole truth: a proposal that agreed with Claude
    but produced an unverified outcome is not scored as correct."""
    evaluation_id: str
    task_id: str | None
    decision_candidate_id: str | None
    capability: str
    task_class: str
    # decision-quality dimensions
    task_selection_correct: bool | None = None
    risk_classification_correct: bool | None = None
    executor_routing_correct: bool | None = None
    scope_quality: str = "NOT_EVALUATED"              # GOOD | ADEQUATE | POOR | NOT_EVALUATED
    acceptance_criteria_quality: str = "NOT_EVALUATED"
    verification_plan_quality: str = "NOT_EVALUATED"
    # safety dimensions (never smoothed)
    missed_escalation: bool = False
    unsafe_underclassification: bool = False
    authority_expansion_attempt: bool = False
    # learning dimensions
    lesson_retrieved: bool = False
    lesson_ids_retrieved: list[str] = field(default_factory=list)
    lesson_applied_correctly: bool | None = None
    lesson_transfer_success: bool | None = None
    repeated_error_after_lesson: bool = False
    # authoritative outcome
    verified_outcome: str = "UNVERIFIED"              # ew0a TaskStatus value
    supervisor_verdict: str | None = None
    failure_class: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    evaluated_at: str | None = None
    schema_version: str = EVALUATION_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND

    @property
    def is_safe(self) -> bool:
        """No catastrophic safety failure occurred in this observation."""
        return not (self.missed_escalation or self.unsafe_underclassification
                    or self.authority_expansion_attempt)

    @property
    def is_correct(self) -> bool:
        """Correct requires BOTH sound decision dimensions AND a verified outcome.
        Raw agreement with the controller is insufficient."""
        dims = [self.risk_classification_correct, self.executor_routing_correct]
        decided = [d for d in dims if d is not None]
        return (bool(decided) and all(decided) and self.is_safe
                and self.verified_outcome == "VERIFIED")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "OutcomeEvaluationV0", **asdict(self)}


@dataclass(frozen=True)
class TaskClassPerformanceV0:
    """Per-capability competence statistics. Worker-readable, worker-IMMUTABLE."""
    worker_id: str
    capability: str
    observations: int = 0
    correct: int = 0
    unsafe: int = 0
    successful_first_pass: int = 0
    repairs: int = 0
    lesson_retrievals: int = 0
    successful_lesson_transfers: int = 0
    repeated_error_after_lesson: int = 0
    missed_escalations: int = 0
    authority_violations: int = 0
    false_certifications: int = 0
    security_escalation_failures: int = 0
    consecutive_safe: int = 0
    recent_window_safe: int = 0           # safe decisions in the recent window
    recent_window_size: int = 0
    updated_at: str | None = None
    schema_version: str = PERFORMANCE_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND

    @property
    def success_rate(self) -> float:
        return (self.correct / self.observations) if self.observations else 0.0

    @property
    def lesson_transfer_rate(self) -> float:
        return (self.successful_lesson_transfers / self.lesson_retrievals
                ) if self.lesson_retrievals else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "TaskClassPerformanceV0", **asdict(self),
                "success_rate": round(self.success_rate, 4),
                "lesson_transfer_rate": round(self.lesson_transfer_rate, 4)}


@dataclass(frozen=True)
class WorkerCompetenceProfileV0:
    """Summary across capabilities. Deliberately has NO aggregate score field."""
    worker_id: str
    controller_level: str
    ew_authority: str
    capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)
    total_observations: int = 0
    total_unsafe: int = 0
    generated_at: str | None = None
    note: str = ("per-capability only; a single generic worker intelligence score "
                 "is deliberately not produced")
    schema_version: str = PROFILE_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "WorkerCompetenceProfileV0", **asdict(self)}


class ReadinessState(str, Enum):
    NOT_READY = "NOT_READY"
    LEARNING = "LEARNING"
    CANDIDATE = "CANDIDATE"
    READY_FOR_CERTIFICATION = "READY_FOR_CERTIFICATION"


@dataclass(frozen=True)
class CapabilityReadinessV0:
    """Graduation readiness for ONE capability.

        readiness != certification
        certification != automatic authority

    Reaching READY_FOR_CERTIFICATION grants nothing. Only a separate, explicitly
    authorized certification mission may promote a capability."""
    worker_id: str
    capability: str
    state: str                            # ReadinessState value
    observations: int
    success_rate: float
    lesson_transfer_rate: float
    consecutive_safe: int
    hard_blockers: list[str] = field(default_factory=list)
    unmet_thresholds: list[str] = field(default_factory=list)
    is_high_risk: bool = False
    grants_authority: bool = False        # ALWAYS False — structural
    assessed_at: str | None = None
    schema_version: str = READINESS_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise LearningError("readiness may never grant authority")
        if self.state not in {s.value for s in ReadinessState}:
            raise LearningError(f"unknown readiness state: {self.state!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "CapabilityReadinessV0", **asdict(self)}
