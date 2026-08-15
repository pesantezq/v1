"""Automatic lesson extractor (Phase 2).

Runs automatically after meaningful outcomes (VERIFIED / REPAIR / ESCALATE /
ABSTAIN / POLICY_VIOLATION / HUMAN_DECISION) and decides one of:

    NEW_LESSON | UPDATE_EXISTING_LESSON | COMPETENCE_UPDATE_ONLY | NO_MEANINGFUL_LEARNING

``NO_MEANINGFUL_LEARNING`` is the common case by design. A memory that records
everything retrieves nothing useful — routine successes update competence and
leave the lesson store alone.

Extraction reads AUTHORITATIVE evidence only: the task contract, the worker /
controller proposal, the authoritative decision, the deterministic result, the GPT
verdict, the final outcome, the failure classification, and any human decision. A
Worker assertion "I learned X" never produces a lesson by itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from portfolio_automation.engineer_worker.learning.contracts import (
    Capability, EngineeringLessonV0, EvaluatorResult, LessonStatus, RiskDomain,
    lesson_identity)

# Outcomes worth examining at all.
MEANINGFUL_OUTCOMES = frozenset({
    "VERIFIED", "REPAIR", "REPAIR_REQUIRED", "ESCALATE", "ESCALATION_REQUIRED",
    "ABSTAIN", "ABSTAINED", "POLICY_VIOLATION", "FAILED_VALIDATION", "HUMAN_DECISION"})

# Failure classes that carry a transferable engineering principle when corrected.
_LESSON_WORTHY_FAILURES = frozenset({
    "ARCHITECTURE_ESCALATION", "SECURITY_ESCALATION", "POLICY_VIOLATION",
    "AMBIGUOUS_REQUIREMENT", "VERIFICATION_FAILURE"})


@dataclass
class LearningObservation:
    """One authoritative observation the extractor may reason over.

    Every field is either an authoritative record or an explicitly-labelled worker
    CLAIM. The extractor never treats a claim as evidence."""
    observation_id: str
    worker_id: str
    capability: str
    task_class: str
    subsystem: str
    risk_domain: str
    # proposal (worker / C0.5 shadow controller) — a CLAIM, not authority
    proposed_risk_class: str | None = None
    proposed_executor: str | None = None
    proposed_reasoning: str = ""
    # authoritative decision (controller)
    authoritative_risk_class: str | None = None
    authoritative_executor: str | None = None
    authoritative_note: str = ""
    # verification chain
    deterministic_ok: bool | None = None
    gpt_verdict: str | None = None
    final_outcome: str = "UNVERIFIED"
    failure_class: str | None = None
    human_decision: str | None = None
    # safety signals (authoritative, never smoothed)
    missed_escalation: bool = False
    unsafe_underclassification: bool = False
    authority_expansion_attempt: bool = False
    # learning context
    lessons_retrieved: list[str] = field(default_factory=list)
    repeated_error_after_lesson: bool = False
    first_pass: bool = True
    attempt_count: int = 1
    evidence_refs: list[str] = field(default_factory=list)
    recorded_at: str | None = None

    @property
    def risk_agreement(self) -> bool | None:
        if self.proposed_risk_class is None or self.authoritative_risk_class is None:
            return None
        return self.proposed_risk_class == self.authoritative_risk_class

    @property
    def routing_agreement(self) -> bool | None:
        if self.proposed_executor is None or self.authoritative_executor is None:
            return None
        return self.proposed_executor == self.authoritative_executor

    @property
    def is_unsafe(self) -> bool:
        return (self.missed_escalation or self.unsafe_underclassification
                or self.authority_expansion_attempt)


@dataclass
class ExtractionOutcome:
    result: EvaluatorResult
    lesson: EngineeringLessonV0 | None = None
    target_lesson_id: str | None = None
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"result": self.result.value,
                "lesson_id": self.lesson.lesson_id if self.lesson else self.target_lesson_id,
                "rationale": self.rationale}


def should_extract(final_outcome: str, auto_extract_after: tuple[str, ...] | list[str]) -> bool:
    """Whether the configured triggers fire for this outcome."""
    o = (final_outcome or "").upper()
    triggers = {t.upper() for t in auto_extract_after}
    if o in triggers:
        return True
    # REPAIR_REQUIRED/ESCALATION_REQUIRED/ABSTAINED are the task-status spellings
    # of the REPAIR/ESCALATE/ABSTAIN verdicts.
    alias = {"REPAIR_REQUIRED": "REPAIR", "ESCALATION_REQUIRED": "ESCALATE",
             "ABSTAINED": "ABSTAIN", "FAILED_VALIDATION": "POLICY_VIOLATION"}
    return alias.get(o, "") in triggers


def _principle_for(obs: LearningObservation) -> str:
    """Compose a NARROW, transferable principle bound to the evidenced context.

    Two disciplines are applied here, both learned from independent review:

    * SCOPE — the task class and subsystem are named, so the rule cannot drift into
      a blanket policy (the same bar ``validation.is_overgeneralized`` enforces).
    * VOICE — the principle is DESCRIPTIVE ("the authoritative classification
      recorded was X"), never prescriptive ("X requires routing to Y"). A lesson is
      evidence about past authoritative decisions. Prescriptive wording reads as a
      lesson asserting authority, which violates the kernel's core invariant:
      learning may change future context, never authority."""
    if obs.unsafe_underclassification or obs.missed_escalation:
        return (f"When a {obs.task_class} task in {obs.subsystem} establishes durable "
                f"{obs.risk_domain} semantics, it was proposed as "
                f"{obs.proposed_risk_class or 'a lower risk class'} while the "
                f"authoritative classification recorded was "
                f"{obs.authoritative_risk_class or 'a higher class'} routed to "
                f"{obs.authoritative_executor or 'the controller'}, so the risk of this "
                f"task class has been observed to exceed its surface appearance.")
    if obs.failure_class == "POLICY_VIOLATION":
        return (f"When performing {obs.task_class} work in {obs.subsystem}, a "
                f"{obs.risk_domain} policy boundary applied that was not visible from the "
                f"task text alone, and the violation was detected only after the fact, so "
                f"the boundary is worth confirming before acting in this task class.")
    if obs.failure_class == "SECURITY_ESCALATION":
        return (f"When a {obs.task_class} change touched {obs.subsystem}, a security "
                f"boundary was recorded as escalation-worthy where the change affected "
                f"credential, secret, or authority handling, even where the immediate "
                f"test suite passed.")
    return (f"When {obs.task_class} work in {obs.subsystem} establishes durable "
            f"{obs.risk_domain} semantics, the verified path recorded was the "
            f"{obs.authoritative_risk_class or obs.proposed_risk_class or 'assigned'} "
            f"classification routed to "
            f"{obs.authoritative_executor or 'the assigned executor'}.")


def _trigger_for(obs: LearningObservation) -> str:
    return (f"{obs.task_class} in {obs.subsystem} "
            f"(risk_domain={obs.risk_domain}, failure_class={obs.failure_class or 'none'})")


def _observed_for(obs: LearningObservation) -> str:
    if obs.is_unsafe:
        return (f"Proposal classified the task as {obs.proposed_risk_class} routed to "
                f"{obs.proposed_executor}; the authoritative classification was "
                f"{obs.authoritative_risk_class} routed to {obs.authoritative_executor}. "
                f"{obs.proposed_reasoning}".strip())
    return (f"Proposal: risk={obs.proposed_risk_class}, executor={obs.proposed_executor}; "
            f"authoritative: risk={obs.authoritative_risk_class}, "
            f"executor={obs.authoritative_executor}; outcome={obs.final_outcome}.")


def _correction_for(obs: LearningObservation) -> str:
    return (f"Authoritative decision set risk={obs.authoritative_risk_class} and "
            f"executor={obs.authoritative_executor}; final outcome {obs.final_outcome}"
            + (f" with GPT verdict {obs.gpt_verdict}" if obs.gpt_verdict else "")
            + (f"; {obs.authoritative_note}" if obs.authoritative_note else "") + ".")


def extract(obs: LearningObservation, existing: list[EngineeringLessonV0],
            auto_extract_after: tuple[str, ...] | list[str], now: str
            ) -> ExtractionOutcome:
    """Decide what, if anything, to learn from ONE authoritative observation."""
    if not should_extract(obs.final_outcome, auto_extract_after):
        return ExtractionOutcome(EvaluatorResult.NO_MEANINGFUL_LEARNING,
                                 rationale=f"outcome {obs.final_outcome} is not an extraction trigger")

    lesson_worthy = (
        obs.is_unsafe
        or (obs.failure_class in _LESSON_WORTHY_FAILURES)
        or (obs.risk_agreement is False)
        or (obs.routing_agreement is False)
        or bool(obs.human_decision))

    if not lesson_worthy:
        # A clean routine success is real evidence about competence, but it is not
        # a new principle. Recording it as a lesson would dilute retrieval.
        return ExtractionOutcome(
            EvaluatorResult.COMPETENCE_UPDATE_ONLY,
            rationale=("outcome carries no novel principle; competence statistics "
                       "updated without polluting the lesson store"))

    principle = _principle_for(obs)
    lid = lesson_identity(obs.capability, obs.task_class, obs.subsystem,
                          obs.risk_domain, principle)

    match = next((l for l in existing if l.lesson_id == lid), None)
    if match is not None:
        return ExtractionOutcome(
            EvaluatorResult.UPDATE_EXISTING_LESSON, target_lesson_id=lid,
            rationale=("observation corroborates an existing lesson; confidence is "
                       "updated rather than forking a duplicate principle"))

    candidate = EngineeringLessonV0(
        lesson_id=lid, worker_id=obs.worker_id, capability=obs.capability,
        task_class=obs.task_class, subsystem=obs.subsystem, risk_domain=obs.risk_domain,
        failure_class=obs.failure_class, trigger=_trigger_for(obs),
        observed_behavior=_observed_for(obs), verified_correction=_correction_for(obs),
        principle=principle, evidence_refs=list(obs.evidence_refs), confidence=0.0,
        status=LessonStatus.CANDIDATE.value, created_at=now, origin="extractor")
    return ExtractionOutcome(EvaluatorResult.NEW_LESSON, lesson=candidate,
                             rationale="novel evidenced principle proposed as a CANDIDATE "
                                       "(activation requires independent validation)")
