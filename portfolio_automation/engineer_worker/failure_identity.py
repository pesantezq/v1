"""Regression baselines identified by test NODE ID, not by count.

WHY THIS EXISTS.

The crashed session's deterministic verification recorded ``baseline_failures:
15`` and ``new_relevant_failures: 0``. The first is a count; the second was a
human assertion derived from it. After the crash, proving the baseline had not
changed required checking out the base commit and re-running the fifteen tests
by hand -- forensic reconstruction, using evidence that lived outside the
record. Worse, count equality is not identity: a baseline of fifteen and a
candidate of fifteen can differ by a fixed test and a newly broken one, and the
arithmetic reports no regression.

So identity is stored, comparison is derived from it, and a missing baseline is
an explicit refusal rather than a zero.

WHY A FIXED TEST AND A DELETED TEST ARE DIFFERENT.

Under a count, deleting a failing test lowers the baseline and reads as an
improvement. ``removed_no_longer_collected`` separates "this test passes now"
from "this test is gone", because only the first is a fix.
"""
from __future__ import annotations

import hashlib
import platform
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Sequence

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER

SCHEMA_KIND = EXPERIMENTAL_MARKER
FAILURE_SET_SCHEMA_VERSION = "engineering.failure_set.v1"

#: pytest exit codes under which a run's failure set is meaningful at all.
#: 0 = all passed, 1 = tests failed. Anything else (2 = interrupted/collection
#: error, 3 = internal error, 4 = usage error, 5 = no tests collected) means the
#: suite did not actually run, and an empty failure list from such a run must
#: never read as a clean sheet.
_USABLE_EXIT_STATUS = frozenset({0, 1})


class Comparability(str, Enum):
    """Whether two failure sets may be compared at all."""

    COMPARABLE = "COMPARABLE"
    UNCOMPARABLE_SELECTION = "UNCOMPARABLE_SELECTION"
    UNCOMPARABLE_ENVIRONMENT = "UNCOMPARABLE_ENVIRONMENT"
    UNUSABLE_BASELINE_RUN = "UNUSABLE_BASELINE_RUN"
    UNUSABLE_CANDIDATE_RUN = "UNUSABLE_CANDIDATE_RUN"
    BASELINE_IDENTITY_UNAVAILABLE = "BASELINE_IDENTITY_UNAVAILABLE"


class RegressionStatus(str, Enum):
    """Deliberately three-valued.

    UNKNOWN exists so that 'we cannot tell' is representable. A two-valued type
    forces missing evidence to be encoded as NO_NEW_FAILURES, which is how
    absent evidence becomes a green result."""

    NO_NEW_FAILURES = "NO_NEW_FAILURES"
    NEW_FAILURES = "NEW_FAILURES"
    UNKNOWN = "UNKNOWN"


def env_fingerprint() -> dict[str, str]:
    """What makes two runs comparable. A baseline captured on a different
    interpreter or platform is not a baseline for this one."""
    return {"python_version": platform.python_version(),
            "platform": platform.system(),
            "implementation": platform.python_implementation()}


@dataclass(frozen=True)
class FailureSet:
    """Exact failing test identities from one run, plus what makes it comparable."""

    node_ids: tuple[str, ...]
    exitstatus: int
    selection_args: tuple[str, ...] = ()
    collect_errors: tuple[str, ...] = ()
    captured_at_sha: Optional[str] = None
    environment: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Stable order is part of the contract: a digest that depended on
        # discovery order would differ between two identical runs.
        object.__setattr__(self, "node_ids", tuple(sorted(self.node_ids)))
        object.__setattr__(self, "collect_errors", tuple(sorted(self.collect_errors)))

    @property
    def count(self) -> int:
        return len(self.node_ids)

    def usable(self) -> bool:
        """A run whose suite could not be imported has no failure set, even
        though its failure LIST is empty."""
        return self.exitstatus in _USABLE_EXIT_STATUS and not self.collect_errors

    def digest(self) -> str:
        # Node IDs legitimately contain ::, [, ], . and non-ASCII, so they are
        # joined with a newline and hashed as opaque strings -- never parsed.
        blob = "\n".join(self.node_ids).encode("utf-8")
        return "fset_" + hashlib.sha256(blob).hexdigest()[:32]

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": FAILURE_SET_SCHEMA_VERSION,
                "schema_kind": SCHEMA_KIND,
                "failure_node_ids": list(self.node_ids),
                "failure_set_digest": self.digest(),
                "failure_count": self.count,
                "pytest_exitstatus": self.exitstatus,
                "collect_errors": list(self.collect_errors),
                "selection_args": list(self.selection_args),
                "captured_at_sha": self.captured_at_sha,
                "environment": dict(self.environment),
                "usable": self.usable()}

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> "FailureSet":
        """Rebuild from a durable record ALONE -- no filesystem, no pytest."""
        return cls(
            node_ids=tuple(record.get("failure_node_ids") or ()),
            exitstatus=int(record.get("pytest_exitstatus", 1)),
            selection_args=tuple(record.get("selection_args") or ()),
            collect_errors=tuple(record.get("collect_errors") or ()),
            captured_at_sha=record.get("captured_at_sha"),
            environment=dict(record.get("environment") or {}))


@dataclass(frozen=True)
class FailureDelta:
    comparability: Comparability
    regression_status: RegressionStatus
    identical: tuple[str, ...] = ()
    fixed: tuple[str, ...] = ()
    removed_no_longer_collected: tuple[str, ...] = ()
    newly_failing: tuple[str, ...] = ()
    details: tuple[str, ...] = ()

    @property
    def new_relevant_failures(self) -> Optional[int]:
        """None when the comparison could not be made.

        Returning 0 for an impossible comparison is the single highest-value
        silent-pass route in this design, so the type refuses to express it."""
        if self.comparability is not Comparability.COMPARABLE:
            return None
        return len(self.newly_failing)

    def to_dict(self) -> dict[str, Any]:
        return {"comparability": self.comparability.value,
                "regression_status": self.regression_status.value,
                "identical": list(self.identical),
                "fixed": list(self.fixed),
                "removed_no_longer_collected": list(self.removed_no_longer_collected),
                "newly_failing": list(self.newly_failing),
                "new_relevant_failures": self.new_relevant_failures,
                "relevance_policy": "ALL_NEW_FAILURES_ARE_RELEVANT",
                "details": list(self.details)}


def _uncomparable(reason: Comparability, detail: str) -> FailureDelta:
    return FailureDelta(comparability=reason,
                        regression_status=RegressionStatus.UNKNOWN,
                        details=(detail,))


def compare_failure_sets(baseline: Optional[FailureSet], candidate: FailureSet,
                         *, candidate_collected: Optional[frozenset[str]] = None,
                         require_same_selection: bool = True) -> FailureDelta:
    """Derive the regression verdict from failure IDENTITIES.

    ``baseline is None`` means the durable baseline identity is unavailable.
    That is UNKNOWN, never "no new failures" -- the whole point of storing
    identities is that their absence must be visible."""
    if baseline is None:
        return _uncomparable(
            Comparability.BASELINE_IDENTITY_UNAVAILABLE,
            "no durable baseline node-id evidence; identity cannot be established "
            "and equality must not be inferred from counts")
    if not baseline.usable():
        return _uncomparable(
            Comparability.UNUSABLE_BASELINE_RUN,
            f"baseline exitstatus={baseline.exitstatus} "
            f"collect_errors={list(baseline.collect_errors)}")
    if not candidate.usable():
        return _uncomparable(
            Comparability.UNUSABLE_CANDIDATE_RUN,
            f"candidate exitstatus={candidate.exitstatus} "
            f"collect_errors={list(candidate.collect_errors)}")
    if require_same_selection and baseline.selection_args != candidate.selection_args:
        return _uncomparable(
            Comparability.UNCOMPARABLE_SELECTION,
            f"{list(baseline.selection_args)} != {list(candidate.selection_args)}")
    if baseline.environment and candidate.environment and \
            baseline.environment != candidate.environment:
        return _uncomparable(
            Comparability.UNCOMPARABLE_ENVIRONMENT,
            f"{baseline.environment} != {candidate.environment}")

    base, cand = set(baseline.node_ids), set(candidate.node_ids)
    gone = base - cand
    if candidate_collected is not None:
        # A test that no longer exists did not get fixed; it got deleted.
        fixed = tuple(sorted(n for n in gone if n in candidate_collected))
        deleted = tuple(sorted(n for n in gone if n not in candidate_collected))
    else:
        fixed, deleted = tuple(sorted(gone)), ()
    newly = tuple(sorted(cand - base))
    return FailureDelta(
        comparability=Comparability.COMPARABLE,
        regression_status=(RegressionStatus.NEW_FAILURES if newly
                           else RegressionStatus.NO_NEW_FAILURES),
        identical=tuple(sorted(base & cand)), fixed=fixed,
        removed_no_longer_collected=deleted, newly_failing=newly)
