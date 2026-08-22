"""Deterministic evidence-sufficiency gate (EW-0B hardening).

WHY THIS EXISTS.

``deterministic_check`` validates the tests that RAN. It never asks whether any
ran. ``tests_ok`` starts True and only a failing entry can falsify it, so an
attempt carrying ``tests_run=[]`` clears the gate vacuously. ``scope_ok`` has the
same shape: ``all(_in_allowed(p) for p in [])`` is True, so an attempt that
changed nothing is "in scope". Both are correct universal quantifications over
an empty set, and both mean the cheapest way to satisfy a gate built from
``all(...)`` is to hand it nothing to check.

That is not hypothetical. It is the shortest path from a worker under pressure
to a PASS: claim completion, present no diff, run no tests, and let a supervisor
that was told "never PASS on missing tests" try to notice the absence in prose.
Asking a language model to detect an omission is strictly weaker than refusing
the omission deterministically.

WHAT THIS GATE IS NOT.

It does not judge whether tests are GOOD. Test quality is a semantic question
and it belongs to the independent supervisor, which reads the diff. This gate
answers only the deterministic, mechanically checkable half:

  * is there anything to judge at all (acceptance criteria, changed paths)?
  * does the claimed work actually appear in the diff?
  * were results reported for tests that were never run?

A supervisor asked to certify against absent evidence should never receive the
question. This gate is why it does not.

``experimental_noncanonical``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER

SCHEMA_KIND = EXPERIMENTAL_MARKER
EVIDENCE_SCHEMA_VERSION = "engineering.evidence_sufficiency.v0"


class EvidenceRefusal(str, Enum):
    """Why an attempt's evidence is too thin to put to an independent reviewer."""

    #: The task declares nothing to satisfy. A PASS would certify agreement with
    #: an empty requirement set, which is agreement about nothing.
    NO_ACCEPTANCE_CRITERIA = "NO_ACCEPTANCE_CRITERIA"
    #: Completion claimed, no file changed. The claim and the evidence disagree.
    NO_CHANGED_PATHS = "NO_CHANGED_PATHS"
    #: The task has an approved test surface and the attempt used none of it.
    NO_TESTS_RUN = "NO_TESTS_RUN"
    #: Paths were named but no diff accompanies them, so nothing can be read.
    NO_DIFF_EVIDENCE = "NO_DIFF_EVIDENCE"
    #: A path is claimed as changed but never appears in the diff that is meant
    #: to substantiate it.
    CHANGED_PATH_ABSENT_FROM_DIFF = "CHANGED_PATH_ABSENT_FROM_DIFF"
    #: A result is reported for a test the attempt never claims to have run.
    #: The reverse of a missing result, and the more dangerous direction: it is
    #: a PASS with no execution behind it.
    RESULT_WITHOUT_RUN = "RESULT_WITHOUT_RUN"


@dataclass(frozen=True)
class EvidenceAssessment:
    """Deterministic verdict on whether an attempt may be reviewed at all."""

    refusals: tuple[EvidenceRefusal, ...] = ()
    details: tuple[str, ...] = ()
    checks: dict[str, str] = field(default_factory=dict)

    @property
    def sufficient(self) -> bool:
        return not self.refusals

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": EVIDENCE_SCHEMA_VERSION, "schema_kind": SCHEMA_KIND,
                "evidence_sufficient": "YES" if self.sufficient else "NO",
                "refusals": [r.value for r in self.refusals],
                "details": list(self.details),
                "checks": dict(self.checks)}


def _diff_mentions(diff_text: str, path: str) -> bool:
    """Does the diff substantiate a claim about ``path``?

    A substring test over the whole diff, not a unified-diff parse. The gate has
    to hold for plain patches, ``git diff`` output and the hand-assembled diffs
    the escalation path produces, and a parser that understands only one of
    those would refuse valid evidence -- turning a safety gate into an outage.
    Over-acceptance here is bounded: the supervisor still reads the diff."""
    if not path:
        return False
    normalised = path.replace("\\", "/")
    return normalised in diff_text.replace("\\", "/")


def assess_evidence(task: Any, attempt: Any) -> EvidenceAssessment:
    """Decide whether ``attempt`` carries enough evidence to be judged.

    Collects EVERY refusal rather than returning on the first, so a repair does
    not have to rediscover the next deficiency one dispatch at a time."""
    refusals: list[EvidenceRefusal] = []
    details: list[str] = []
    checks: dict[str, str] = {}

    criteria = list(getattr(task, "acceptance_criteria", None) or [])
    checks["ACCEPTANCE_CRITERIA_PRESENT"] = "YES" if criteria else "NO"
    if not criteria:
        refusals.append(EvidenceRefusal.NO_ACCEPTANCE_CRITERIA)
        details.append(
            "the task declares no acceptance_criteria; a PASS would certify "
            "agreement with an empty requirement set")

    changed = list(getattr(attempt, "changed_paths", None) or [])
    checks["CHANGED_PATHS_PRESENT"] = "YES" if changed else "NO"
    if not changed:
        refusals.append(EvidenceRefusal.NO_CHANGED_PATHS)
        details.append(
            "the worker claim is accompanied by no changed path; the claim and "
            "the evidence disagree")

    allowed_tests = list(getattr(task, "allowed_tests", None) or [])
    tests_run = list(getattr(attempt, "tests_run", None) or [])
    checks["TESTS_RUN_PRESENT"] = "YES" if tests_run else "NO"
    if allowed_tests and not tests_run:
        refusals.append(EvidenceRefusal.NO_TESTS_RUN)
        details.append(
            f"the task approves {len(allowed_tests)} test target(s) and the "
            "attempt ran none; an empty test set satisfies every all() check "
            "vacuously")

    diff_text = getattr(attempt, "diff_text", "") or ""
    checks["DIFF_PRESENT"] = "YES" if diff_text.strip() else "NO"
    if changed and not diff_text.strip():
        refusals.append(EvidenceRefusal.NO_DIFF_EVIDENCE)
        details.append(
            f"{len(changed)} path(s) are claimed as changed but no diff "
            "accompanies them, so the change cannot be read")
    elif changed:
        absent = [p for p in changed if not _diff_mentions(diff_text, p)]
        if absent:
            refusals.append(EvidenceRefusal.CHANGED_PATH_ABSENT_FROM_DIFF)
            details.append(
                f"claimed as changed but never named in the diff: {sorted(absent)}")
        checks["CHANGED_PATHS_IN_DIFF"] = "NO" if absent else "YES"

    results = dict(getattr(attempt, "test_results", None) or {})
    orphans = sorted(set(results) - set(tests_run))
    checks["RESULTS_BACKED_BY_RUNS"] = "NO" if orphans else "YES"
    if orphans:
        refusals.append(EvidenceRefusal.RESULT_WITHOUT_RUN)
        details.append(
            f"results reported for tests the attempt never ran: {orphans}; a "
            "result with no execution behind it is a claim, not evidence")

    return EvidenceAssessment(refusals=tuple(refusals), details=tuple(details),
                              checks=checks)
