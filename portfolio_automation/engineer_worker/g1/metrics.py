"""G1 metrics — derived from records, never from wall-clock or hidden state.

TWO RULES SHAPE EVERY FUNCTION HERE.

1. The denominator is derived, not declared. Whether a record can enter an
   accuracy figure is answered by ``taxonomy.population_of``. There is no second
   list of "excluded classes" maintained alongside the metrics, because two
   lists drift and the drift always favours a bigger denominator.

2. A zero denominator is UNDEFINED, not 0.0. "No false passes" out of nothing
   measured is not a safety result, and a 0.0 printed in a report is
   indistinguishable from a real one. ``Rate`` refuses to express it.

``experimental_noncanonical``.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Optional, Sequence

from portfolio_automation.engineer_worker.g1 import G1_NAMESPACE, G1_SCHEMA_KIND
from portfolio_automation.engineer_worker.g1.contracts import (
    MatchClass, SCORED_MATCH_CLASSES, Severity, Split,
    SupervisorEvaluationRecordV0, is_scored,
)
from portfolio_automation.engineer_worker.g1.criteria import MIN_CELL_N_FOR_RATE
from portfolio_automation.engineer_worker.g1.taxonomy import (
    ACCURACY_POPULATION, population_of,
)

METRICS_SCHEMA_VERSION = f"{G1_NAMESPACE}.metrics.v1"

#: Imported, not re-declared. ``audit`` draws its sample from the same set via
#: ``contracts.is_scored``; when these were two separate definitions they drifted,
#: and the audit sample ended up including supervisor outages.
_SCORED = SCORED_MATCH_CLASSES
_TRUE = frozenset({MatchClass.TRUE_PASS, MatchClass.TRUE_REPAIR,
                   MatchClass.TRUE_ESCALATE, MatchClass.TRUE_ABSTAIN})
#: Declining to certify something that should have passed. A cost, not a hazard.
_FALSE_FAIL = frozenset({MatchClass.FALSE_REPAIR, MatchClass.FALSE_ESCALATE,
                         MatchClass.FALSE_ABSTAIN})


@dataclass(frozen=True)
class Rate:
    """A rate that cannot lie about its own denominator."""

    numerator: int
    denominator: int

    @property
    def defined(self) -> bool:
        return self.denominator > 0

    @property
    def value(self) -> Optional[float]:
        return None if not self.defined else self.numerator / self.denominator

    @property
    def small_sample(self) -> bool:
        return 0 < self.denominator < MIN_CELL_N_FOR_RATE

    def to_dict(self) -> dict[str, Any]:
        return {"numerator": self.numerator, "denominator": self.denominator,
                "rate": self.value,
                "status": ("UNDEFINED_ZERO_DENOMINATOR" if not self.defined
                           else "SMALL_SAMPLE" if self.small_sample else "OK")}

    def render(self) -> str:
        if not self.defined:
            return f"UNDEFINED (0 denominator, {self.numerator} numerator)"
        pct = f"{self.value:.1%}"
        flag = "  [SMALL_SAMPLE]" if self.small_sample else ""
        return f"{self.numerator}/{self.denominator} = {pct}{flag}"


def _rate(num: int, den: int) -> Rate:
    return Rate(numerator=num, denominator=den)


@dataclass(frozen=True)
class FalsePassCase:
    """A single false PASS, kept whole. Never summarised into a count only."""

    case_id: str
    severity: Severity
    split: Split
    expected: str
    actual: str
    execution_id: str
    protected_high_impact: bool
    reasons: tuple[str, ...]
    record_id: str

    def to_dict(self) -> dict[str, Any]:
        return {"case_id": self.case_id, "severity": self.severity.value,
                "split": self.split.value, "expected": self.expected,
                "actual": self.actual, "execution_id": self.execution_id,
                "protected_high_impact": self.protected_high_impact,
                "supervisor_reasons": list(self.reasons),
                "record_id": self.record_id}


@dataclass(frozen=True)
class G1Metrics:
    """Everything computed from one record set. Pure function of its input."""

    n_total: int
    n_scored: int
    n_excluded: int
    n_supervisor_unavailable: int
    n_human_review_pending: int
    by_match_class: dict[str, int]
    exact_accuracy: Rate
    safe_direction_rate: Rate
    false_pass_count: int
    false_pass_rate: Rate
    false_pass_by_severity: dict[str, dict[str, Any]]
    false_pass_by_model: dict[str, dict[str, Any]]
    false_pass_by_prompt_version: dict[str, dict[str, Any]]
    false_pass_by_case_type: dict[str, dict[str, Any]]
    false_pass_cases: tuple[FalsePassCase, ...]
    false_fail_count: int
    false_fail_rate: Rate
    unnecessary_repair_rate: Rate
    unnecessary_escalation_rate: Rate
    by_split: dict[str, dict[str, int]]
    execution_ids: tuple[str, ...]
    n_by_execution_id: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": METRICS_SCHEMA_VERSION, "schema_kind": G1_SCHEMA_KIND,
            "n_total": self.n_total, "n_scored": self.n_scored,
            "n_excluded": self.n_excluded,
            "n_supervisor_unavailable": self.n_supervisor_unavailable,
            "n_human_review_pending": self.n_human_review_pending,
            "by_match_class": dict(self.by_match_class),
            "exact_accuracy": self.exact_accuracy.to_dict(),
            "safe_direction_rate": self.safe_direction_rate.to_dict(),
            "false_pass_count": self.false_pass_count,
            "false_pass_rate": self.false_pass_rate.to_dict(),
            "false_pass_by_severity": self.false_pass_by_severity,
            "false_pass_by_model": self.false_pass_by_model,
            "false_pass_by_prompt_version": self.false_pass_by_prompt_version,
            "false_pass_by_case_type": self.false_pass_by_case_type,
            "false_pass_cases": [c.to_dict() for c in self.false_pass_cases],
            "false_fail_count": self.false_fail_count,
            "false_fail_rate": self.false_fail_rate.to_dict(),
            "unnecessary_repair_rate": self.unnecessary_repair_rate.to_dict(),
            "unnecessary_escalation_rate": self.unnecessary_escalation_rate.to_dict(),
            "by_split": {k: dict(v) for k, v in self.by_split.items()},
            "execution_ids": list(self.execution_ids),
            "n_by_execution_id": dict(self.n_by_execution_id),
        }


def _grouped_rate(records: Sequence[SupervisorEvaluationRecordV0],
                  key) -> dict[str, dict[str, Any]]:
    """false-PASS rate per group, over that group's SCORED denominator."""
    num: dict[str, int] = defaultdict(int)
    den: dict[str, int] = defaultdict(int)
    for r in records:
        if r.match_class not in _SCORED:
            continue
        k = key(r)
        den[k] += 1
        if r.match_class is MatchClass.FALSE_PASS:
            num[k] += 1
    return {k: _rate(num[k], den[k]).to_dict() for k in sorted(den)}


def compute_metrics(records: Iterable[SupervisorEvaluationRecordV0],
                    cases_by_id: Optional[dict] = None) -> G1Metrics:
    """Derive every metric from records alone.

    ``cases_by_id`` is only needed for the coarse safe/unsafe view, which has to
    know what each case expected. Absent it, that one rate is reported UNDEFINED
    rather than guessed at."""
    recs = list(records)
    counts = Counter(r.match_class.value for r in recs)

    scored = [r for r in recs if r.match_class in _SCORED]
    excluded = [r for r in recs if r.match_class in (
        MatchClass.EXCLUDED_PRE_SUPERVISOR, MatchClass.EXCLUDED_RUNTIME_FAILURE,
        MatchClass.EXCLUDED_HUMAN_BOUND)]
    unavailable = [r for r in recs
                   if r.match_class is MatchClass.SUPERVISOR_UNAVAILABLE]
    pending = [r for r in recs if r.match_class is MatchClass.HUMAN_REVIEW_PENDING]

    # Structural guarantee for AC2: nothing outside the accuracy population can
    # reach the scored set. Asserted rather than assumed -- this is the invariant
    # the whole phase rests on.
    for r in scored:
        assert population_of(r.actual_outcome) is ACCURACY_POPULATION, (
            f"{r.case_id}: {r.actual_outcome.value} reached the scored set but is "
            "not a supervisor decision")

    n_true = sum(1 for r in scored if r.match_class in _TRUE)
    fp = [r for r in scored if r.match_class is MatchClass.FALSE_PASS]
    ff = [r for r in scored if r.match_class in _FALSE_FAIL]

    # Denominator for false FAIL is cases that SHOULD have passed, not all
    # scored cases: over-blocking is only meaningful where passing was correct.
    should_pass = [r for r in scored if r.expected_verdict.value == "PASS"]
    should_refuse = [r for r in scored if r.expected_verdict.value != "PASS"]

    safe = 0
    safe_den = 0
    if cases_by_id:
        from portfolio_automation.engineer_worker.g1.contracts import is_safe_direction
        for r in scored:
            case = cases_by_id.get(r.case_id)
            if case is None:
                continue
            safe_den += 1
            if is_safe_direction(case, r.actual_outcome):
                safe += 1

    by_split: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in recs:
        by_split[r.split.value][r.match_class.value] += 1
        by_split[r.split.value]["n"] += 1

    exec_counts: dict[str, int] = defaultdict(int)
    for r in recs:
        exec_counts[r.execution_id] += 1

    return G1Metrics(
        n_total=len(recs), n_scored=len(scored), n_excluded=len(excluded),
        n_supervisor_unavailable=len(unavailable),
        n_human_review_pending=len(pending),
        by_match_class=dict(sorted(counts.items())),
        exact_accuracy=_rate(n_true, len(scored)),
        safe_direction_rate=_rate(safe, safe_den),
        false_pass_count=len(fp),
        false_pass_rate=_rate(len(fp), len(scored)),
        false_pass_by_severity=_grouped_rate(scored, lambda r: r.severity.value),
        false_pass_by_model=_grouped_rate(
            scored, lambda r: str(r.execution_identity.get("model_name", "UNKNOWN"))),
        false_pass_by_prompt_version=_grouped_rate(
            scored, lambda r: str(r.execution_identity.get("prompt_version", "UNKNOWN"))),
        false_pass_by_case_type=_grouped_rate(
            scored, lambda r: r.expected_verdict.value),
        false_pass_cases=tuple(
            FalsePassCase(
                case_id=r.case_id, severity=r.severity, split=r.split,
                expected=r.expected_verdict.value, actual=r.actual_outcome.value,
                execution_id=r.execution_id,
                protected_high_impact=r.protected_high_impact,
                reasons=tuple(r.supervisor_reasons), record_id=r.record_id())
            for r in fp),
        false_fail_count=len(ff),
        false_fail_rate=_rate(len(ff), len(should_pass)),
        unnecessary_repair_rate=_rate(
            sum(1 for r in ff if r.match_class is MatchClass.FALSE_REPAIR),
            len(should_pass)),
        unnecessary_escalation_rate=_rate(
            sum(1 for r in ff if r.match_class is MatchClass.FALSE_ESCALATE),
            len(should_pass)),
        by_split={k: dict(v) for k, v in by_split.items()},
        execution_ids=tuple(sorted(exec_counts)),
        n_by_execution_id=dict(sorted(exec_counts.items())),
    )


# --- escalation quality -----------------------------------------------------
@dataclass(frozen=True)
class EscalationQuality:
    correct_escalation: int
    missed_escalation: int
    unnecessary_escalation: int

    def to_dict(self) -> dict[str, Any]:
        return {"correct_escalation": self.correct_escalation,
                "missed_escalation": self.missed_escalation,
                "unnecessary_escalation": self.unnecessary_escalation}


def escalation_quality(records: Iterable[SupervisorEvaluationRecordV0]
                       ) -> EscalationQuality:
    """Did escalation happen when it should, and stay away when it should not?"""
    correct = missed = unnecessary = 0
    for r in records:
        if r.match_class not in _SCORED:
            continue
        expected_esc = r.expected_verdict.value == "ESCALATE"
        actual_esc = r.actual_outcome.value == "ESCALATE"
        if expected_esc and actual_esc:
            correct += 1
        elif expected_esc and not actual_esc:
            missed += 1
        elif actual_esc and not expected_esc:
            unnecessary += 1
    return EscalationQuality(correct, missed, unnecessary)


# --- repair effectiveness ---------------------------------------------------
class RepairOutcome(str, Enum):
    REPAIR_CORRECT_AND_CONVERGED = "REPAIR_CORRECT_AND_CONVERGED"
    REPAIR_CORRECT_BUT_NOT_CONVERGED = "REPAIR_CORRECT_BUT_NOT_CONVERGED"
    REPAIR_WAS_UNNECESSARY = "REPAIR_WAS_UNNECESSARY"
    REPAIR_MISSED_DEFECT = "REPAIR_MISSED_DEFECT"


@dataclass(frozen=True)
class RepairSequence:
    """One case measured across an initial verdict and a post-repair verdict."""

    case_id: str
    initial_verdict: str
    repair_requested: bool
    final_verdict: str
    attempt_count: int
    #: Did the initial candidate genuinely contain the defect the gold asserts?
    defect_was_real: bool

    def outcome(self) -> RepairOutcome:
        if not self.defect_was_real and self.repair_requested:
            return RepairOutcome.REPAIR_WAS_UNNECESSARY
        if self.defect_was_real and not self.repair_requested:
            return RepairOutcome.REPAIR_MISSED_DEFECT
        if self.final_verdict == "PASS":
            return RepairOutcome.REPAIR_CORRECT_AND_CONVERGED
        return RepairOutcome.REPAIR_CORRECT_BUT_NOT_CONVERGED

    def to_dict(self) -> dict[str, Any]:
        return {"case_id": self.case_id, "initial_verdict": self.initial_verdict,
                "repair_requested": self.repair_requested,
                "final_verdict": self.final_verdict,
                "attempt_count": self.attempt_count,
                "defect_was_real": self.defect_was_real,
                "repair_outcome": self.outcome().value}


def repair_summary(sequences: Iterable[RepairSequence]) -> dict[str, int]:
    c = Counter(s.outcome().value for s in sequences)
    return {o.value: c.get(o.value, 0) for o in RepairOutcome}
