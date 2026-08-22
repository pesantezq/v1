"""G1 outcome taxonomy — WHICH outcomes may enter a supervisor accuracy figure.

WHY THIS EXISTS, AND WHY IT COMES FIRST.

The loop can end a task in a dozen ways. Dropping all of them into one
denominator produces a number that looks like supervisor accuracy and is not:

  * a protected-path breach is refused by ``deterministic_check`` BEFORE
    dispatch, so the supervisor was never asked. Counting it as a supervisor
    success would credit GPT for a guard it never saw. Counting it as a failure
    would blame GPT for a decision it never made. Both are wrong.
  * ``SUPERVISOR_UNAVAILABLE`` is an outage. Folding outages into semantic
    accuracy means a bad network makes the model look stupid, and a
    suspiciously reliable network makes it look sharp.
  * ``E4_HUMAN_REQUIRED`` is the system working exactly as designed. It is not
    a supervisor error that the loop declined to certify consequential work.

So the taxonomy is frozen BEFORE any metric is computed, and the denominator is
DERIVED from it rather than maintained by hand next to it. ``metrics`` asks this
module which population an outcome belongs to; there is no second list to drift.

The direction of the bias is deliberate. Every class whose participation is
uncertain is EXCLUDED, because an excluded case shrinks the denominator (making
a claim weaker and more honest) while a wrongly-included case inflates accuracy.

``experimental_noncanonical``.
"""
from __future__ import annotations

from enum import Enum

from portfolio_automation.engineer_worker.g1 import G1_NAMESPACE, G1_SCHEMA_KIND

TAXONOMY_SCHEMA_VERSION = f"{G1_NAMESPACE}.outcome_taxonomy.v1"


class Population(str, Enum):
    """What kind of observation an outcome is."""

    #: GPT received a valid packet and returned a decision. THE ONLY population
    #: that may appear in a semantic-accuracy denominator.
    SUPERVISOR_DECISION = "SUPERVISOR_DECISION"
    #: GPT was reached for, or reached, and could not produce a usable decision.
    #: Measured, reported, and kept out of accuracy.
    SUPERVISOR_OPERATIONAL_FAILURE = "SUPERVISOR_OPERATIONAL_FAILURE"
    #: A deterministic gate decided before the supervisor was consulted.
    PRE_SUPERVISOR_DETERMINISTIC = "PRE_SUPERVISOR_DETERMINISTIC"
    #: The worker, Claude, or the process itself failed.
    EXECUTOR_RUNTIME_FAILURE = "EXECUTOR_RUNTIME_FAILURE"
    #: The system correctly routed to a human.
    HUMAN_BOUND = "HUMAN_BOUND"


class OutcomeClass(str, Enum):
    """Every terminal outcome G1 knows how to classify.

    Values reuse the existing loop vocabulary (``VerificationVerdict``,
    ``FailureClass``, ``LoopStop``) rather than inventing parallel names, so a
    record can be classified from what the loop already wrote down."""

    # --- supervisor decisions -------------------------------------------
    PASS = "PASS"
    REPAIR = "REPAIR"
    ESCALATE = "ESCALATE"
    ABSTAIN = "ABSTAIN"

    # --- supervisor operational failures --------------------------------
    SUPERVISOR_UNAVAILABLE = "SUPERVISOR_UNAVAILABLE"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    TIMEOUT = "TIMEOUT"
    AUTH_FAILURE = "AUTH_FAILURE"
    TRANSPORT_FAILURE = "TRANSPORT_FAILURE"

    # --- pre-supervisor deterministic refusals --------------------------
    POLICY_VIOLATION = "POLICY_VIOLATION"
    ROADMAP_VIOLATION = "ROADMAP_VIOLATION"
    EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"
    STALE_CANDIDATE = "STALE_CANDIDATE"
    TEST_FAILURE = "TEST_FAILURE"

    # --- executor / runtime failures ------------------------------------
    WORKER_UNAVAILABLE = "WORKER_UNAVAILABLE"
    CLAUDE_UNAVAILABLE = "CLAUDE_UNAVAILABLE"
    CRASH_INDETERMINATE = "CRASH_INDETERMINATE"

    # --- human-bound ----------------------------------------------------
    E4_HUMAN_REQUIRED = "E4_HUMAN_REQUIRED"
    PROTECTED_HIGH_IMPACT_REVIEW = "PROTECTED_HIGH_IMPACT_REVIEW"


_POPULATION: dict[OutcomeClass, Population] = {
    OutcomeClass.PASS: Population.SUPERVISOR_DECISION,
    OutcomeClass.REPAIR: Population.SUPERVISOR_DECISION,
    OutcomeClass.ESCALATE: Population.SUPERVISOR_DECISION,
    OutcomeClass.ABSTAIN: Population.SUPERVISOR_DECISION,

    OutcomeClass.SUPERVISOR_UNAVAILABLE: Population.SUPERVISOR_OPERATIONAL_FAILURE,
    OutcomeClass.MALFORMED_RESPONSE: Population.SUPERVISOR_OPERATIONAL_FAILURE,
    OutcomeClass.TIMEOUT: Population.SUPERVISOR_OPERATIONAL_FAILURE,
    OutcomeClass.AUTH_FAILURE: Population.SUPERVISOR_OPERATIONAL_FAILURE,
    OutcomeClass.TRANSPORT_FAILURE: Population.SUPERVISOR_OPERATIONAL_FAILURE,

    OutcomeClass.POLICY_VIOLATION: Population.PRE_SUPERVISOR_DETERMINISTIC,
    OutcomeClass.ROADMAP_VIOLATION: Population.PRE_SUPERVISOR_DETERMINISTIC,
    OutcomeClass.EVIDENCE_INSUFFICIENT: Population.PRE_SUPERVISOR_DETERMINISTIC,
    OutcomeClass.STALE_CANDIDATE: Population.PRE_SUPERVISOR_DETERMINISTIC,
    OutcomeClass.TEST_FAILURE: Population.PRE_SUPERVISOR_DETERMINISTIC,

    OutcomeClass.WORKER_UNAVAILABLE: Population.EXECUTOR_RUNTIME_FAILURE,
    OutcomeClass.CLAUDE_UNAVAILABLE: Population.EXECUTOR_RUNTIME_FAILURE,
    OutcomeClass.CRASH_INDETERMINATE: Population.EXECUTOR_RUNTIME_FAILURE,

    OutcomeClass.E4_HUMAN_REQUIRED: Population.HUMAN_BOUND,
    OutcomeClass.PROTECTED_HIGH_IMPACT_REVIEW: Population.HUMAN_BOUND,
}

#: The single population admitted to semantic accuracy. Named once so the rule
#: has exactly one definition to audit.
ACCURACY_POPULATION = Population.SUPERVISOR_DECISION

#: The four verdicts a supervisor may legitimately return.
SUPERVISOR_VERDICTS = frozenset({
    OutcomeClass.PASS, OutcomeClass.REPAIR,
    OutcomeClass.ESCALATE, OutcomeClass.ABSTAIN,
})


class TaxonomyError(ValueError):
    """An outcome G1 has no classification for. Never silently bucketed."""


def population_of(outcome: OutcomeClass | str) -> Population:
    """Which population an outcome belongs to. Unknown outcomes RAISE.

    Defaulting an unrecognised outcome into any population is how a new loop
    state silently starts or stops counting toward accuracy months later."""
    try:
        key = outcome if isinstance(outcome, OutcomeClass) else OutcomeClass(outcome)
    except ValueError as exc:
        raise TaxonomyError(
            f"{outcome!r} is not a classified G1 outcome; add it to the taxonomy "
            "deliberately rather than letting it default into a population") from exc
    return _POPULATION[key]


def participates_in_accuracy(outcome: OutcomeClass | str) -> bool:
    """True only for genuine supervisor decisions."""
    return population_of(outcome) is ACCURACY_POPULATION


def taxonomy_manifest() -> dict:
    """Machine-readable statement of the frozen taxonomy.

    Emitted into every report so a reader can see which classes were counted
    without reading this file, and so a later change is visible as a diff."""
    return {
        "schema_version": TAXONOMY_SCHEMA_VERSION,
        "schema_kind": G1_SCHEMA_KIND,
        "accuracy_population": ACCURACY_POPULATION.value,
        "classes": [
            {"outcome": oc.value,
             "population": _POPULATION[oc].value,
             "in_accuracy_denominator": _POPULATION[oc] is ACCURACY_POPULATION}
            for oc in OutcomeClass
        ],
    }


def assert_taxonomy_total() -> None:
    """Every OutcomeClass has a population. Completeness is a build-time fact.

    A class with no mapping would raise a KeyError deep inside metric
    aggregation, at the worst possible moment."""
    missing = [oc.value for oc in OutcomeClass if oc not in _POPULATION]
    if missing:
        raise TaxonomyError(f"unclassified outcomes: {missing}")
