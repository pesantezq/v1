"""Frozen result contract for the deterministic VS-002 result runner.

``experimental_noncanonical``. This module is PURE DATA + enums + the frozen
Student-t table. It performs no IO, no network, no package discovery, and
computes no experiment statistic — importing it has zero side effects. The
scientific computation lives in :mod:`result_runner`; the canonical frozen
scientific authority is ``evals/vertical_slice/VS-002_preregistration.json``.

The runner emits a :class:`VS002Result`. Its ``to_observations()`` renders a
strict-JSON dict suitable for embedding as the authority-screened
``observations`` payload of a canonical Northstar ``ExperimentResult`` (the same
pattern VS-001 uses). It carries no authority: ``grants_authority`` is always
False and ``vs002_executed`` reflects the real-execution semantics of the
artifact, never a claim made by this synthetic-certification mission.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Tuple

RESULT_SCHEMA_VERSION = "engineering.vs002_result.v1"
RESULT_SCHEMA_KIND = "experimental_noncanonical"
DEFAULT_RUNNER_ID = "vs002_evidence.result_runner"
RUNNER_VERSION = "v1"


# ── Frozen classification vocabularies (from binding_core.classification) ────
class CriterionOutcome(str, Enum):
    MET = "PREREGISTERED_CRITERIA_MET"
    NOT_MET = "PREREGISTERED_CRITERIA_NOT_MET"
    INCONCLUSIVE = "EVIDENCE_INCONCLUSIVE"


class SkillOutcome(str, Enum):
    PRESENT = "ECONOMIC_SKILL_EVIDENCE_PRESENT"
    NOT_ESTABLISHED = "ECONOMIC_SKILL_NOT_ESTABLISHED"


class H1Status(str, Enum):
    MET = "H1_MET"
    NOT_MET = "H1_NOT_MET"
    INCONCLUSIVE = "H1_INCONCLUSIVE"


class H2Status(str, Enum):
    MET = "H2_MET"
    NOT_MET = "H2_NOT_MET"
    INCONCLUSIVE = "H2_INCONCLUSIVE"


class NoActionStatus(str, Enum):
    BEAT = "NO_ACTION_BEAT"
    NOT_BEAT = "NO_ACTION_NOT_BEAT"
    INCONCLUSIVE = "NO_ACTION_INCONCLUSIVE"


# ── Frozen tabulated two-sided 95% Student-t critical values t(0.975, df) ────
# Deterministic table (NO scipy/statsmodels, NO normal approximation). The
# VS-002 package legally produces at most ~a few dozen independent cohorts, so
# df never exceeds this table in a real run; any df outside it FAILS CLOSED.
STUDENT_T_0975: dict[int, float] = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
    6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
    11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
    16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
    26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
    31: 2.040, 32: 2.037, 33: 2.035, 34: 2.032, 35: 2.030,
    36: 2.028, 37: 2.026, 38: 2.024, 39: 2.023, 40: 2.021,
}
STUDENT_T_MAX_DF = max(STUDENT_T_0975)


class StudentTTableError(ValueError):
    """Raised when a required df is outside the frozen tabulated range.

    Fail-closed: the runner never substitutes a normal approximation or an
    invented/interpolated critical value."""


def student_t_critical(df: int) -> float:
    """t(0.975, df) from the frozen table, or fail closed."""
    if not isinstance(df, int) or df < 1:
        raise StudentTTableError(f"degrees of freedom must be a positive int, got {df!r}")
    if df not in STUDENT_T_0975:
        raise StudentTTableError(
            f"df={df} is outside the tabulated Student-t range [1, {STUDENT_T_MAX_DF}]; "
            "no normal approximation or interpolation is permitted")
    return STUDENT_T_0975[df]


def _finite(x: float, name: str) -> float:
    """Reject NaN/Inf before it can reach a metric (G17)."""
    xf = float(x)
    if not math.isfinite(xf):
        raise ValueError(f"{name} is not finite: {x!r}")
    return xf


@dataclass(frozen=True)
class EvidenceIdentity:
    """The four frozen package identity strings the runner binds against.

    These are METADATA ONLY. The runner never opens, enumerates, or reads the
    real evidence package to obtain them; in synthetic certification they are
    supplied directly as strings."""
    package_id: str
    package_transport_digest: str
    source_production_sha: str
    evidence_schema_version: str

    def to_dict(self) -> dict[str, str]:
        return {
            "package_id": self.package_id,
            "package_transport_digest": self.package_transport_digest,
            "source_production_sha": self.source_production_sha,
            "evidence_schema_version": self.evidence_schema_version,
        }


@dataclass(frozen=True)
class Interval:
    """A two-sided 95% Student-t interval over independent cohort statistics."""
    n: int
    mean: float
    sample_sd: float
    se: float
    t_critical: float
    ci_low: float
    ci_high: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "mean": _finite(self.mean, "interval.mean"),
            "sample_sd": _finite(self.sample_sd, "interval.sample_sd"),
            "se": _finite(self.se, "interval.se"),
            "t_critical": _finite(self.t_critical, "interval.t_critical"),
            "ci_low": _finite(self.ci_low, "interval.ci_low"),
            "ci_high": _finite(self.ci_high, "interval.ci_high"),
        }


@dataclass(frozen=True)
class H1Result:
    status: H1Status
    cohort_count: int
    experiment_mean: Optional[float]
    interval: Optional[Interval]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "cohort_count": self.cohort_count,
            "experiment_mean_net_risk_adjusted_excess": (
                None if self.experiment_mean is None
                else _finite(self.experiment_mean, "h1.mean")),
            "interval": None if self.interval is None else self.interval.to_dict(),
        }


@dataclass(frozen=True)
class H2Result:
    status: H2Status
    valid_cohort_ic_count: int
    point_ic: Optional[float]
    reporting_interval: Optional[Interval]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "valid_cohort_ic_count": self.valid_cohort_ic_count,
            "point_ic": None if self.point_ic is None else _finite(self.point_ic, "h2.point_ic"),
            "reporting_interval": (
                None if self.reporting_interval is None
                else self.reporting_interval.to_dict()),
            "uncertainty_role": "REPORTING_ONLY",
        }


@dataclass(frozen=True)
class NoActionResult:
    status: NoActionStatus
    cohort_count: int
    mean_net_no_action_return: Optional[float]
    interval: Optional[Interval]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "cohort_count": self.cohort_count,
            "mean_net_no_action_return": (
                None if self.mean_net_no_action_return is None
                else _finite(self.mean_net_no_action_return, "no_action.mean")),
            "interval": None if self.interval is None else self.interval.to_dict(),
        }


@dataclass(frozen=True)
class PopulationSummary:
    base_population_rows: int
    scored_population_rows: int
    evaluated_signal_rows: int
    selected_cohort_dates: Tuple[str, ...]
    exclusion_counts: Mapping[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_population_rows": self.base_population_rows,
            "scored_population_rows": self.scored_population_rows,
            "evaluated_signal_rows": self.evaluated_signal_rows,
            "selected_cohort_dates": list(self.selected_cohort_dates),
            "selected_cohort_count": len(self.selected_cohort_dates),
            "exclusion_counts": dict(sorted(self.exclusion_counts.items())),
        }


@dataclass(frozen=True)
class VS002Result:
    """The narrow, versioned, experimental_noncanonical VS-002 result.

    Self-sufficient provenance: it binds the preregistration identity + freeze
    digest and the frozen evidence package identity, so a reader can verify what
    scientific contract and which package a result claims to answer — without
    the result ever having opened that package."""
    schema_version: str
    schema_kind: str
    runner_id: str
    runner_version: str
    generated_at: str
    preregistration_id: str
    preregistration_freeze_digest: str
    preregistration_schema_version: str
    preregistration_verified: bool
    evidence_binding: EvidenceIdentity
    population: PopulationSummary
    h1: H1Result
    h2: H2Result
    no_action: NoActionResult
    criterion_outcome: CriterionOutcome
    skill_outcome: SkillOutcome
    observe_only: bool = True
    grants_authority: bool = False
    vs002_executed: bool = False

    def to_observations(self) -> dict[str, Any]:
        """Strict-JSON, authority-key-safe payload (embeddable in a canonical
        ExperimentResult.observations)."""
        return {
            "schema_version": self.schema_version,
            "schema_kind": self.schema_kind,
            "runner_id": self.runner_id,
            "runner_version": self.runner_version,
            "generated_at": self.generated_at,
            "preregistration": {
                "preregistration_id": self.preregistration_id,
                "preregistration_freeze_digest": self.preregistration_freeze_digest,
                "schema_version": self.preregistration_schema_version,
                "verified": self.preregistration_verified,
            },
            "evidence_binding": self.evidence_binding.to_dict(),
            "population": self.population.to_dict(),
            "h1": self.h1.to_dict(),
            "h2": self.h2.to_dict(),
            "no_action": self.no_action.to_dict(),
            "classification": {
                "criterion_outcome": self.criterion_outcome.value,
                "skill_outcome": self.skill_outcome.value,
            },
            "observe_only": self.observe_only,
            "grants_authority": self.grants_authority,
            "vs002_executed": self.vs002_executed,
        }
