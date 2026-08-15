"""Lesson validation / anti-poisoning (Phase 3).

A lesson candidate becomes ACTIVE only after passing, in this order:

  1. evidence refs EXIST (resolvable against authoritative records);
  2. the reported event ACTUALLY OCCURRED (not merely claimed);
  3. the proposed correction MATCHES the authoritative outcome;
  4. the principle is NOT overgeneralized;
  5. independent GPT semantic review, where semantic judgment is required.

Mirrors ``ew0a.certify_attempt``: the deterministic gate runs FIRST and
short-circuits, so a poisoned or fabricated candidate never reaches (or spends)
the independent verifier. A Worker statement of the form "I learned X" is never
sufficient — validation reads authoritative evidence, not self-report.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from portfolio_automation.engineer_worker.gpt_supervisor import (
    SupervisorDecision, SupervisorVerdict)
from portfolio_automation.engineer_worker.learning.contracts import (
    EngineeringLessonV0, LessonStatus)

# Universal quantifiers that turn a narrow, transferable rule into an unsafe
# blanket policy ("All contract work requires Claude.").
_UNIVERSAL = re.compile(
    r"(?<![A-Za-z])(all|every|any|always|never|everything|anything|"
    r"whenever|invariably|universally)(?![A-Za-z])", re.IGNORECASE)

# Clauses that NARROW a principle back to a defensible scope.
_NARROWING = re.compile(
    r"(?<![A-Za-z])(when|where|if|unless|that establishes|specific|currently|"
    r"in the .{1,40} subsystem|for .{1,40} tasks|which|whose|during|involving|"
    r"only if|only when|scoped|bounded)(?![A-Za-z])", re.IGNORECASE)

_MIN_PRINCIPLE_CHARS = 40
_MAX_PRINCIPLE_CHARS = 600


@dataclass
class ValidationResult:
    """Why a candidate was accepted or refused. Refusals are informative — a
    rejected lesson is evidence about the extractor, not just a dead end."""
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    failed_checks: list[str] = field(default_factory=list)
    evidence_verified: list[str] = field(default_factory=list)
    unresolved_evidence: list[str] = field(default_factory=list)
    overgeneralized: bool = False
    semantic_verdict: str | None = None       # PASS | REPAIR | ESCALATE | ABSTAIN | NOT_CONSULTED
    semantic_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted, "reasons": self.reasons,
            "failed_checks": self.failed_checks,
            "evidence_verified": self.evidence_verified,
            "unresolved_evidence": self.unresolved_evidence,
            "overgeneralized": self.overgeneralized,
            "semantic_verdict": self.semantic_verdict,
            "semantic_reasons": self.semantic_reasons,
        }


def check_evidence_refs(lesson: EngineeringLessonV0,
                        evidence_index: set[str]) -> tuple[list[str], list[str]]:
    """Resolve each evidence ref against authoritative identifiers.

    A ref may be a bare identifier (``cdc-spp-1``) or a qualified locator
    (``docs/EW0A_0B3_RECORDS.jsonl#cdc-spp-1``); the fragment is what must resolve."""
    verified, unresolved = [], []
    for ref in lesson.evidence_refs:
        token = ref.split("#", 1)[1] if "#" in ref else ref
        token = token.split(":", 1)[1] if token.startswith(("task:", "attempt:", "record:")) else token
        (verified if token in evidence_index else unresolved).append(ref)
    return verified, unresolved


def check_event_occurred(lesson: EngineeringLessonV0,
                         authoritative_records: list[dict[str, Any]]) -> bool:
    """Verify the reported event actually appears in authoritative records.

    Guards the poisoning case where a candidate cites a real record id but
    describes an event that record does not contain."""
    tokens = {t for t in re.split(r"[^A-Za-z0-9_.\-]+", lesson.observed_behavior.lower()) if len(t) > 3}
    if not tokens:
        return False
    for rec in authoritative_records:
        blob = " ".join(str(v) for v in rec.values()).lower()
        if any(t in blob for t in tokens):
            return True
    return False


def check_correction_matches(lesson: EngineeringLessonV0,
                             authoritative_records: list[dict[str, Any]]) -> bool:
    """The verified_correction must be supported by an authoritative outcome, not
    by the Worker's account of it."""
    tokens = {t for t in re.split(r"[^A-Za-z0-9_.\-]+", lesson.verified_correction.lower())
              if len(t) > 3}
    if not tokens:
        return False
    for rec in authoritative_records:
        blob = " ".join(str(v) for v in rec.values()).lower()
        hits = sum(1 for t in tokens if t in blob)
        if hits >= max(2, len(tokens) // 5):
            return True
    return False


def is_overgeneralized(principle: str) -> bool:
    """Deterministic overgeneralization heuristic.

    Rejects  'All contract work requires Claude.'
    Accepts  'Authoring a new canonical Northstar contract that establishes durable
              cross-contract semantics is architecture-sensitive and currently
              requires E3 routing to Claude.'

    A universal quantifier is permitted only when the sentence also carries a
    narrowing clause that bounds where the rule applies."""
    text = " ".join(principle.split())
    if len(text) < _MIN_PRINCIPLE_CHARS or len(text) > _MAX_PRINCIPLE_CHARS:
        return True
    if _UNIVERSAL.search(text) and not _NARROWING.search(text):
        return True
    return False


LESSON_REVIEW_SYSTEM = (
    "You are an INDEPENDENT reviewer of a proposed engineering LESSON for a local "
    "AI engineering lab. You did not produce the lesson and must not trust its "
    "self-description. Decide ONLY from the supplied evidence whether the lesson "
    "is (a) supported by the cited authoritative outcome, (b) narrow enough to be "
    "safe, and (c) broad enough to transfer to a genuinely different task. "
    "Return ONE JSON object with keys: verdict (one of PASS, REPAIR, ESCALATE, "
    "ABSTAIN), reasons (array of strings), unresolved_requirements (array of "
    "strings), evidence_checked (array of strings). "
    "PASS only if the principle follows from the evidence AND is neither a blanket "
    "policy nor a restatement of one specific incident. REPAIR if it should be "
    "narrowed or reworded. ESCALATE if it asserts authority, certification, or "
    "policy change. ABSTAIN if the evidence is insufficient to judge. JSON only."
)


def build_lesson_review_packet(lesson: EngineeringLessonV0,
                               evidence_excerpts: list[dict[str, Any]]) -> dict[str, Any]:
    """Bounded review packet. Carries NO secrets and no hidden worker reasoning —
    only the candidate principle and the authoritative evidence behind it."""
    return {
        "candidate_lesson": {
            "capability": lesson.capability, "task_class": lesson.task_class,
            "subsystem": lesson.subsystem, "risk_domain": lesson.risk_domain,
            "failure_class": lesson.failure_class, "trigger": lesson.trigger,
            "observed_behavior": lesson.observed_behavior,
            "verified_correction": lesson.verified_correction,
            "principle": lesson.principle,
        },
        "evidence_refs": lesson.evidence_refs,
        "authoritative_evidence": evidence_excerpts[:20],
        "review_question": ("Is this principle supported by the evidence, narrow "
                            "enough to be safe, and broad enough to transfer?"),
    }


SemanticReviewer = Callable[[dict[str, Any]], SupervisorDecision]


def validate_lesson(lesson: EngineeringLessonV0, *, evidence_index: set[str],
                    authoritative_records: list[dict[str, Any]],
                    require_evidence: bool = True,
                    semantic_reviewer: SemanticReviewer | None = None) -> ValidationResult:
    """Run the full anti-poisoning gate. Fail-closed at every step.

    The deterministic checks run first and short-circuit: a candidate with
    unresolvable evidence never reaches the independent semantic reviewer."""
    res = ValidationResult(accepted=False)

    # 1) evidence refs resolve
    verified, unresolved = check_evidence_refs(lesson, evidence_index)
    res.evidence_verified, res.unresolved_evidence = verified, unresolved
    if require_evidence and not verified:
        res.failed_checks.append("no_resolvable_evidence")
        res.reasons.append("lesson cites no evidence that resolves to an authoritative record")
    if unresolved:
        res.failed_checks.append("unresolved_evidence_refs")
        res.reasons.append(f"unresolvable evidence refs: {unresolved}")

    # 2) the reported event actually occurred
    if not check_event_occurred(lesson, authoritative_records):
        res.failed_checks.append("event_not_found_in_authoritative_records")
        res.reasons.append("observed_behavior is not corroborated by authoritative records")

    # 3) the correction matches authoritative evidence
    if not check_correction_matches(lesson, authoritative_records):
        res.failed_checks.append("correction_not_supported")
        res.reasons.append("verified_correction is not supported by an authoritative outcome")

    # 4) overgeneralization
    if is_overgeneralized(lesson.principle):
        res.overgeneralized = True
        res.failed_checks.append("overgeneralized_principle")
        res.reasons.append("principle is a blanket policy; narrow it to its evidenced scope")

    if res.failed_checks:
        res.semantic_verdict = "NOT_CONSULTED"   # deterministic gate short-circuits
        return res

    # 5) independent semantic review
    if semantic_reviewer is None:
        res.semantic_verdict = "NOT_CONSULTED"
        res.failed_checks.append("semantic_review_unavailable")
        res.reasons.append("independent semantic review required but unavailable; failing closed")
        return res

    decision = semantic_reviewer(build_lesson_review_packet(lesson, authoritative_records))
    res.semantic_verdict = decision.verdict.value
    res.semantic_reasons = list(decision.reasons)
    if decision.verdict is not SupervisorVerdict.PASS:
        res.failed_checks.append(f"semantic_review_{decision.verdict.value.lower()}")
        res.reasons.append(f"independent reviewer returned {decision.verdict.value}")
        return res

    res.accepted = True
    res.reasons.append("evidence resolved, event corroborated, correction supported, "
                       "scope bounded, independent review PASS")
    return res


def consensus_reviewer(reviewer: SemanticReviewer, *, samples: int = 3,
                       required_passes: int = 2, transport_retries: int = 3,
                       backoff_fn: Callable[[int], None] | None = None) -> SemanticReviewer:
    """Wrap a semantic reviewer in a majority vote.

    An LLM judge is not deterministic: the same candidate can return PASS on one
    sample and REPAIR on the next. A single sample therefore makes activation a
    coin-flip, and — worse — invites re-running until the desired verdict appears,
    which is precisely the validator-gaming this kernel exists to prevent.

    The vote fixes both: it is applied UNIFORMLY to every candidate before any
    verdict is seen, so it cannot be selectively deployed on a lesson someone wants
    to keep. Fail-closed: a tie or a transport failure is not a PASS, and any
    ESCALATE (the reviewer judging that the lesson asserts authority) vetoes
    outright regardless of the other votes."""
    def _sample(packet: dict[str, Any]) -> SupervisorDecision:
        """One vote, retrying ONLY transport unavailability.

        A SUPERVISOR_UNAVAILABLE carries no judgment — it means the reviewer was
        never reached (rate limit, timeout, link failure), so retrying it asks the
        question for the first time. Retrying a REPAIR or ESCALATE would instead be
        re-rolling an answer already given, which is the gaming this gate forbids.
        Exhausted retries stay UNAVAILABLE and never become a PASS."""
        decision = reviewer(packet)
        for attempt in range(transport_retries):
            if decision.verdict is not SupervisorVerdict.SUPERVISOR_UNAVAILABLE:
                return decision
            if backoff_fn is not None:
                backoff_fn(attempt)
            decision = reviewer(packet)
        return decision

    def _vote(packet: dict[str, Any]) -> SupervisorDecision:
        decisions = [_sample(packet) for _ in range(max(1, samples))]
        verdicts = [d.verdict for d in decisions]
        reasons = [f"vote {i + 1}: {d.verdict.value}" for i, d in enumerate(decisions)]
        reasons += [r for d in decisions for r in d.reasons[:2]]

        if SupervisorVerdict.ESCALATE in verdicts:
            return SupervisorDecision(SupervisorVerdict.ESCALATE, reasons=reasons)
        passes = sum(1 for v in verdicts if v is SupervisorVerdict.PASS)
        if passes >= required_passes:
            return SupervisorDecision(SupervisorVerdict.PASS, reasons=reasons)
        # Report the dominant non-pass verdict so the refusal is actionable.
        non_pass = [v for v in verdicts if v is not SupervisorVerdict.PASS]
        dominant = max(set(non_pass), key=non_pass.count) if non_pass else SupervisorVerdict.ABSTAIN
        return SupervisorDecision(dominant, reasons=reasons + [
            f"consensus not reached: {passes}/{len(verdicts)} PASS, "
            f"{required_passes} required"])
    return _vote


def derive_confidence(result: ValidationResult, corroborating_observations: int) -> float:
    """Confidence is DERIVED from evidence, never self-asserted by the Worker.

    Base credit for a validated candidate, plus credit for each independent
    corroborating observation, capped below certainty — a lesson can always be
    contradicted by later evidence."""
    if not result.accepted:
        return 0.0
    base = 0.55
    corroboration = min(0.35, 0.10 * max(0, corroborating_observations - 1))
    return round(min(0.95, base + corroboration), 4)


def activation_status(result: ValidationResult) -> LessonStatus:
    return LessonStatus.ACTIVE if result.accepted else LessonStatus.CANDIDATE
