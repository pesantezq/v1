"""Human spot-audit surface for G1.

WHAT WAS WRONG BEFORE THIS REPAIR.

Three defects, all of which inflated apparent coverage:

  * the sample and the completion check keyed on ``case_id`` alone. The corpus
    is measured under more than one model, so one case yields several scored
    decisions -- and an adjudication of gpt-4o's answer silently satisfied
    coverage for gpt-4o-mini's answer to the same question. Worse, an audit
    record for a case that was never selected counted anyway.
  * the sample was drawn from a DIFFERENT population than the accuracy
    denominator: it excluded the three EXCLUDED_* classes but still admitted
    supervisor outages and HUMAN_REVIEW_PENDING records. Outages are not
    semantic judgements; spending audit budget on them buys nothing and makes
    the fraction look met.
  * ``round()`` computed the target, so a configured 20% minimum could round
    DOWN -- 20% of 11 became 2. A minimum that rounds down is not a minimum.

Now: identity is ``record_id`` (which includes execution_id, config_id, run_id),
the population is ``contracts.is_scored`` -- the same predicate ``metrics``
uses -- and the target is ``ceil``.

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

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence

from portfolio_automation.engineer_worker.g1 import G1_NAMESPACE, G1_SCHEMA_KIND
from portfolio_automation.engineer_worker.g1.contracts import (
    EvaluationCaseV0, MatchClass, Severity, SupervisorEvaluationRecordV0,
    is_scored,
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


class AuditMembershipError(ValueError):
    """An audit record was submitted for a decision that was never selected.

    Rejected rather than ignored. Silently dropping it would make a submission
    that looks like progress produce none, and silently accepting it would let
    unrelated adjudications satisfy a coverage requirement."""


@dataclass(frozen=True)
class AuditItem:
    """One scored DECISION packaged for a human, with why it was selected.

    Keyed by ``record_id``, not ``case_id``: the same question answered by two
    models is two decisions, and each needs its own adjudication."""

    case_id: str
    record_id: str
    #: The configuration and run this decision came from, so a human can see
    #: which of several answers to the same case they are judging.
    execution_id_ref: str = ""
    config_id: str = ""
    run_id: str = ""
    served_model_version: str = ""
    priority: int = 0
    selection_reason: str = ""
    expected_verdict: str = ""
    supervisor_verdict: str = ""
    severity: str = ""
    split: str = ""
    protected_high_impact: bool = False
    supervisor_reasons: tuple[str, ...] = ()
    execution_id: str = ""
    #: The exact question the human is being asked to answer, so the audit does
    #: not silently become "do you like this code".
    question: str = (
        "Given ONLY the packet evidence, which verdict is correct: PASS, "
        "REPAIR, ESCALATE or ABSTAIN? Judge the diff and the tests, not the "
        "worker's prose.")

    def to_dict(self) -> dict[str, Any]:
        return {"case_id": self.case_id, "record_id": self.record_id,
                "execution_id_ref": self.execution_id_ref,
                "config_id": self.config_id, "run_id": self.run_id,
                "served_model_version": self.served_model_version,
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


def scored_population(records: Sequence[SupervisorEvaluationRecordV0]
                      ) -> list[SupervisorEvaluationRecordV0]:
    """EXACTLY the population ``compute_metrics`` scores.

    Shares ``contracts.is_scored`` with metrics rather than re-deriving the
    filter. Two filters that are meant to agree eventually do not, and the
    drift here spent audit budget on outages."""
    return [r for r in records if is_scored(r)]


def select_audit_sample(records: Sequence[SupervisorEvaluationRecordV0], *,
                        fraction: float = MIN_HUMAN_AUDIT_FRACTION
                        ) -> tuple[AuditItem, ...]:
    """Choose the audit sample deterministically, by decision identity.

    No randomness: a reproducible sample can be re-derived and checked months
    later, and random selection would make the audit itself unauditable.

    ``ceil``, not ``round``: a configured minimum that can round down is not a
    minimum. 20% of 11 scored decisions is 3, never 2."""
    scored = scored_population(records)
    if not scored:
        return ()
    target = min(len(scored), max(1, math.ceil(len(scored) * fraction)))
    # Sorted by (priority, record_id) -- record_id, not case_id, so the ordering
    # is total across decisions rather than colliding when one case appears
    # under several configurations.
    ranked = sorted(scored, key=lambda r: (_priority(r)[0], r.record_id()))
    out = []
    for r in ranked[:target]:
        prio, reason = _priority(r)
        out.append(AuditItem(
            case_id=r.case_id, record_id=r.record_id(),
            execution_id_ref=r.execution_id,
            config_id=(r.config.config_id() if r.config else ""),
            run_id=r.run_id,
            served_model_version=r.served_model_version,
            priority=prio, selection_reason=reason,
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
    #: The EXACT scored decision this adjudicates. Required: an audit that names
    #: only a case cannot say which model's answer it judged.
    record_id: str
    supervisor_verdict: str
    human_verdict: str
    reviewer_id: str
    reviewed_at: str
    execution_id: str
    severity: Severity
    rationale: str = ""

    def __post_init__(self) -> None:
        if not str(self.record_id).strip():
            raise ValueError(
                "record_id is required: an adjudication that names only a case "
                "cannot say which scored decision it judged, and one model's "
                "answer must never satisfy coverage for another's")
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
                "case_id": self.case_id, "record_id": self.record_id,
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
    #: Record ids, not case ids. The pending list must identify decisions.
    pending_record_ids: tuple[str, ...]
    pending_case_ids: tuple[str, ...]
    #: Submitted adjudications that named a decision outside the sample. Kept
    #: visible instead of dropped: a rejected submission is information.
    rejected_record_ids: tuple[str, ...]
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
                "pending_record_ids": list(self.pending_record_ids),
                "pending_case_ids": list(self.pending_case_ids),
                "rejected_record_ids": list(self.rejected_record_ids),
                "agreement_count": self.agreement_count,
                "disagreement_count": self.disagreement_count,
                "agreement_rate": self.agreement_rate,
                "weighted_disagreement": self.weighted_disagreement,
                "high_severity_disagreements":
                    list(self.high_severity_disagreements)}


def audit_coverage(sample: Sequence[AuditItem],
                   completed: Sequence[HumanAuditRecord],
                   *, n_scored: int, strict: bool = False) -> AuditCoverage:
    """Coverage arithmetic that reports the shortfall instead of hiding it.

    Only adjudications whose ``record_id`` is IN the selected sample count. An
    unrelated audit record -- one for a decision that was never selected, or for
    the same case under a different model -- is rejected, not counted. With
    ``strict=True`` it raises instead, for callers that would rather fail than
    read a rejection list."""
    selected = {i.record_id for i in sample}
    members = [r for r in completed if r.record_id in selected]
    rejected = tuple(sorted(r.record_id for r in completed
                            if r.record_id not in selected))
    if rejected and strict:
        raise AuditMembershipError(
            f"{len(rejected)} audit record(s) name decisions outside the "
            f"selected sample: {list(rejected)}")

    done = {r.record_id: r for r in members}
    pending_recs = tuple(i.record_id for i in sample if i.record_id not in done)
    pending_cases = tuple(i.case_id for i in sample if i.record_id not in done)
    agree = sum(1 for r in members if r.agreement is AuditAgreement.AGREE)
    disagree = sum(1 for r in members if r.agreement is AuditAgreement.DISAGREE)
    return AuditCoverage(
        n_scored=n_scored,
        required=len(sample),
        completed=len(done),
        pending_record_ids=pending_recs,
        pending_case_ids=pending_cases,
        rejected_record_ids=rejected,
        agreement_count=agree,
        disagreement_count=disagree,
        weighted_disagreement=sum(
            r.severity_weighted_disagreement for r in members),
        high_severity_disagreements=tuple(
            r.record_id for r in members
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
