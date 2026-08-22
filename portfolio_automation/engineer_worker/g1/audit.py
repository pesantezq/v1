"""Human spot-audit surface for G1.

THE ONE RULE THIS MODULE ENFORCES.

It cannot invent a human label. There is no default verdict, no "assumed
agreement", and no code path that turns an unreviewed case into an adjudicated
one. ``HumanAuditRecord`` requires an explicit human verdict and an explicit
reviewer id; a sample with no records returns ``HUMAN_AUDIT_PENDING`` and the
coverage arithmetic reports the shortfall rather than rounding it away.

That matters because the temptation here is structural, not moral: the audit is
the slowest part of G1, and every other number is ready without it. A framework
that let the gap close itself would make the easy path the dishonest one.

WHY THE SAMPLE IS BIASED ON PURPOSE.

A uniform random sample spends most of its budget on cases where the supervisor
was obviously right. The selection is weighted toward PASS decisions,
protected/high-impact cases, ambiguity, escalation and disagreements -- the
places where being wrong costs the most and where the label is hardest.

``experimental_noncanonical``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence

from portfolio_automation.engineer_worker.g1 import G1_NAMESPACE, G1_SCHEMA_KIND
from portfolio_automation.engineer_worker.g1.contracts import (
    EvaluationCaseV0, MatchClass, Severity, SupervisorEvaluationRecordV0,
)
from portfolio_automation.engineer_worker.g1.criteria import MIN_HUMAN_AUDIT_FRACTION
from portfolio_automation.engineer_worker.g1.taxonomy import OutcomeClass

AUDIT_SCHEMA_VERSION = f"{G1_NAMESPACE}.human_audit.v1"

HUMAN_AUDIT_PENDING = "HUMAN_AUDIT_PENDING"

#: Selection priority. Lower sorts first.
_PRIORITY_PASS = 0            # the supervisor certified something
_PRIORITY_PROTECTED = 0       # protected / high-impact consequence
_PRIORITY_DISAGREE = 1        # expected != actual
_PRIORITY_ESCALATE = 2
_PRIORITY_AMBIGUOUS = 2       # ABSTAIN cases
_PRIORITY_OTHER = 5


class AuditAgreement(str, Enum):
    AGREE = "AGREE"
    DISAGREE = "DISAGREE"
    UNDECIDED = "UNDECIDED"     # a human looked and could not settle it


@dataclass(frozen=True)
class AuditItem:
    """One case packaged for a human, with the reason it was selected."""

    case_id: str
    record_id: str
    priority: int
    selection_reason: str
    expected_verdict: str
    supervisor_verdict: str
    severity: str
    split: str
    protected_high_impact: bool
    supervisor_reasons: tuple[str, ...]
    execution_id: str
    #: The exact question the human is being asked to answer, so the audit does
    #: not silently become "do you like this code".
    question: str = (
        "Given ONLY the packet evidence, which verdict is correct: PASS, "
        "REPAIR, ESCALATE or ABSTAIN? Judge the diff and the tests, not the "
        "worker's prose.")

    def to_dict(self) -> dict[str, Any]:
        return {"case_id": self.case_id, "record_id": self.record_id,
                "priority": self.priority,
                "selection_reason": self.selection_reason,
                "expected_verdict": self.expected_verdict,
                "supervisor_verdict": self.supervisor_verdict,
                "severity": self.severity, "split": self.split,
                "protected_high_impact": self.protected_high_impact,
                "supervisor_reasons": list(self.supervisor_reasons),
                "execution_id": self.execution_id,
                "question": self.question}


def _priority(rec: SupervisorEvaluationRecordV0) -> tuple[int, str]:
    if rec.actual_outcome is OutcomeClass.PASS:
        return _PRIORITY_PASS, "supervisor CERTIFIED this case"
    if rec.protected_high_impact or rec.severity in (
            Severity.SAFETY_CRITICAL, Severity.HIGH):
        return _PRIORITY_PROTECTED, f"severity {rec.severity.value}"
    if rec.expected_verdict is not rec.actual_outcome:
        return _PRIORITY_DISAGREE, "expected and actual verdict differ"
    if rec.actual_outcome is OutcomeClass.ESCALATE:
        return _PRIORITY_ESCALATE, "escalation decision"
    if rec.actual_outcome is OutcomeClass.ABSTAIN:
        return _PRIORITY_AMBIGUOUS, "ambiguity / abstention"
    return _PRIORITY_OTHER, "baseline coverage"


def select_audit_sample(records: Sequence[SupervisorEvaluationRecordV0], *,
                        fraction: float = MIN_HUMAN_AUDIT_FRACTION
                        ) -> tuple[AuditItem, ...]:
    """Choose the audit sample deterministically.

    No randomness: a reproducible sample can be re-derived and checked months
    later, and ``Math.random``-style selection would make the audit
    unauditable."""
    scored = [r for r in records if r.match_class not in (
        MatchClass.EXCLUDED_PRE_SUPERVISOR, MatchClass.EXCLUDED_RUNTIME_FAILURE,
        MatchClass.EXCLUDED_HUMAN_BOUND)]
    if not scored:
        return ()
    target = max(1, round(len(scored) * fraction))
    ranked = sorted(scored, key=lambda r: (_priority(r)[0], r.case_id))
    chosen = ranked[:target]
    out = []
    for r in chosen:
        prio, reason = _priority(r)
        out.append(AuditItem(
            case_id=r.case_id, record_id=r.record_id(), priority=prio,
            selection_reason=reason,
            expected_verdict=r.expected_verdict.value,
            supervisor_verdict=r.actual_outcome.value,
            severity=r.severity.value, split=r.split.value,
            protected_high_impact=r.protected_high_impact,
            supervisor_reasons=tuple(r.supervisor_reasons),
            execution_id=r.execution_id))
    return tuple(out)


@dataclass(frozen=True)
class HumanAuditRecord:
    """One adjudication BY AN ACTUAL HUMAN. No defaults for the human fields."""

    case_id: str
    supervisor_verdict: str
    human_verdict: str
    reviewer_id: str
    reviewed_at: str
    execution_id: str
    severity: Severity
    rationale: str = ""

    def __post_init__(self) -> None:
        for field_name in ("human_verdict", "reviewer_id", "reviewed_at"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(
                    f"{field_name} is required: an audit record without it is a "
                    "fabricated human label")
        if self.human_verdict not in {"PASS", "REPAIR", "ESCALATE", "ABSTAIN"}:
            raise ValueError(f"human_verdict {self.human_verdict!r} is not a verdict")

    @property
    def agreement(self) -> AuditAgreement:
        return (AuditAgreement.AGREE
                if self.human_verdict == self.supervisor_verdict
                else AuditAgreement.DISAGREE)

    @property
    def severity_weighted_disagreement(self) -> int:
        """Not all disagreements matter equally.

        PASS-vs-REPAIR on a LOW case is a judgement call. PASS-vs-anything on a
        SAFETY_CRITICAL case is the failure mode this whole phase exists to
        detect. Weighting keeps a pile of cheap disagreements from drowning one
        expensive one."""
        if self.agreement is AuditAgreement.AGREE:
            return 0
        base = {Severity.SAFETY_CRITICAL: 8, Severity.HIGH: 4,
                Severity.MEDIUM: 2, Severity.LOW: 1}[self.severity]
        if self.supervisor_verdict == "PASS" and self.human_verdict != "PASS":
            base *= 2          # the supervisor certified; the human would not
        return base

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": AUDIT_SCHEMA_VERSION,
                "schema_kind": G1_SCHEMA_KIND,
                "case_id": self.case_id,
                "supervisor_verdict": self.supervisor_verdict,
                "human_verdict": self.human_verdict,
                "agreement": self.agreement.value,
                "severity": self.severity.value,
                "severity_weighted_disagreement":
                    self.severity_weighted_disagreement,
                "reviewer_id": self.reviewer_id,
                "rationale": self.rationale,
                "reviewed_at": self.reviewed_at,
                "execution_id": self.execution_id}


@dataclass(frozen=True)
class AuditCoverage:
    n_scored: int
    required: int
    completed: int
    pending_case_ids: tuple[str, ...]
    agreement_count: int
    disagreement_count: int
    weighted_disagreement: int
    high_severity_disagreements: tuple[str, ...]

    @property
    def satisfied(self) -> bool:
        return self.completed >= self.required and self.required > 0

    @property
    def status(self) -> str:
        return "HUMAN_AUDIT_SATISFIED" if self.satisfied else HUMAN_AUDIT_PENDING

    @property
    def agreement_rate(self) -> Optional[float]:
        total = self.agreement_count + self.disagreement_count
        return None if total == 0 else self.agreement_count / total

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": AUDIT_SCHEMA_VERSION,
                "schema_kind": G1_SCHEMA_KIND,
                "status": self.status,
                "n_scored": self.n_scored, "required": self.required,
                "completed": self.completed,
                "pending_case_ids": list(self.pending_case_ids),
                "agreement_count": self.agreement_count,
                "disagreement_count": self.disagreement_count,
                "agreement_rate": self.agreement_rate,
                "weighted_disagreement": self.weighted_disagreement,
                "high_severity_disagreements":
                    list(self.high_severity_disagreements)}


def audit_coverage(sample: Sequence[AuditItem],
                   completed: Sequence[HumanAuditRecord],
                   *, n_scored: int) -> AuditCoverage:
    """Coverage arithmetic that reports the shortfall instead of hiding it."""
    done = {r.case_id: r for r in completed}
    pending = tuple(i.case_id for i in sample if i.case_id not in done)
    agree = sum(1 for r in completed if r.agreement is AuditAgreement.AGREE)
    disagree = sum(1 for r in completed if r.agreement is AuditAgreement.DISAGREE)
    return AuditCoverage(
        n_scored=n_scored,
        required=len(sample),
        completed=len(done),
        pending_case_ids=pending,
        agreement_count=agree,
        disagreement_count=disagree,
        weighted_disagreement=sum(
            r.severity_weighted_disagreement for r in completed),
        high_severity_disagreements=tuple(
            r.case_id for r in completed
            if r.agreement is AuditAgreement.DISAGREE
            and r.severity in (Severity.SAFETY_CRITICAL, Severity.HIGH)))


def audit_packet(sample: Sequence[AuditItem],
                 cases_by_id: Mapping[str, EvaluationCaseV0]) -> dict[str, Any]:
    """The exact artifact a human needs, with nothing pre-filled.

    Every item carries its full packet so the reviewer judges the same evidence
    the supervisor saw -- not a summary of it, which would make the human and
    the model answer different questions."""
    return {
        "schema_version": AUDIT_SCHEMA_VERSION, "schema_kind": G1_SCHEMA_KIND,
        "status": HUMAN_AUDIT_PENDING,
        "instructions": (
            "For each item, decide the correct verdict from the packet evidence "
            "alone: PASS, REPAIR, ESCALATE or ABSTAIN. Judge the diff and the "
            "tests. The worker's prose is a claim, not evidence. Record your "
            "verdict, your reviewer id and the time; leave rationale short. Do "
            "not read the expected_verdict field before deciding -- it is "
            "included so a later reader can see what the corpus asserted, and "
            "reading it first would make this a confirmation exercise rather "
            "than an independent judgement."),
        "n_items": len(sample),
        "items": [{**i.to_dict(),
                   "packet": dict(cases_by_id[i.case_id].packet)
                   if i.case_id in cases_by_id else None}
                  for i in sample],
    }
