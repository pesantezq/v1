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
from functools import lru_cache
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


# ── Deterministic tabulated two-sided 95% Student-t critical values t(0.975, df) ─
# The frozen interval rule mandates deterministic TABULATED Student-t 0.975
# critical values with NO scipy/statsmodels and NO normal-1.96 approximation.
#
# The VS-002 independent-cohort count has NO authoritative finite maximum in
# source: cohorts are greedy non-overlapping 7-day groups over the signal-
# evidence window, and that window's length is not frozen by the preregistration
# or the evidence contracts. This mission may not inspect the real package to
# bound it. So instead of assuming a small df (the earlier "a few dozen cohorts"
# claim was never established from source), the table is GENERATED over a
# generous, explicit, REPRODUCIBLE domain by the pure-python quantile method
# below, and the runner FAILS CLOSED above that domain rather than approximating.
# STUDENT_T_MAX_DF is table coverage, NOT a claimed scientific maximum, and no
# real-package contents informed its value.

STUDENT_T_MAX_DF = 1000  # covers df up to 1000, i.e. >= 1001 independent
#                          non-overlapping 7-day cohorts (~19 years of them) —
#                          far beyond any realistic VS-002 signal window.


def _regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b), Numerical-Recipes ``betai`` via the
    Lentz continued fraction. Pure-python, deterministic, no dependency."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(ln_beta + a * math.log(x) + b * math.log(1.0 - x))

    def _betacf(a: float, b: float, x: float) -> float:
        tiny = 1e-300
        qab = a + b
        qap = a + 1.0
        qam = a - 1.0
        c = 1.0
        d = 1.0 - qab * x / qap
        if abs(d) < tiny:
            d = tiny
        d = 1.0 / d
        h = d
        for m in range(1, 300):
            m2 = 2 * m
            aa = m * (b - m) * x / ((qam + m2) * (a + m2))
            d = 1.0 + aa * d
            if abs(d) < tiny:
                d = tiny
            c = 1.0 + aa / c
            if abs(c) < tiny:
                c = tiny
            d = 1.0 / d
            h *= d * c
            aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
            d = 1.0 + aa * d
            if abs(d) < tiny:
                d = tiny
            c = 1.0 + aa / c
            if abs(c) < tiny:
                c = tiny
            d = 1.0 / d
            delta = d * c
            h *= delta
            if abs(delta - 1.0) < 1e-15:
                break
        return h

    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _student_t_0975_quantile(df: int) -> float:
    """Upper 0.975 critical value of Student's t with ``df`` degrees of freedom.

    Uses the upper-tail identity P(T > t) = 0.5 * I_{df/(df+t^2)}(df/2, 1/2):
    solves I_x(df/2, 1/2) = 0.05 for x by bisection, then t = sqrt(df*(1-x)/x).
    Deterministic; no scipy/statsmodels."""
    a = df / 2.0
    b = 0.5
    lo, hi = 1e-300, 1.0 - 1e-16
    for _ in range(300):
        mid = 0.5 * (lo + hi)
        if _regularized_incomplete_beta(a, b, mid) > 0.05:
            hi = mid
        else:
            lo = mid
    x = 0.5 * (lo + hi)
    return math.sqrt(df * (1.0 - x) / x)


@lru_cache(maxsize=None)
def _tabulated_t_0975(df: int) -> float:
    """One deterministic tabulated t(0.975, df) value, stored to 6 decimals and
    MEMOIZED — the cache is the table. Computed by the reproducible method above
    (never scipy/statsmodels, never a normal approximation). Only the handful of
    df actually used by a run are ever materialized, so import stays instant,
    while the value for any df is fixed, reviewable, and reproducible."""
    return round(_student_t_0975_quantile(df), 6)


class StudentTTableError(ValueError):
    """Raised when a required df is outside the tabulated domain.

    Fail-closed: the runner never substitutes a normal approximation or an
    invented/interpolated critical value; extending the domain is a reviewable
    ruling, not a silent fallback."""


def student_t_critical(df: int) -> float:
    """t(0.975, df) from the deterministic tabulated domain, or fail closed."""
    if not isinstance(df, int) or df < 1:
        raise StudentTTableError(f"degrees of freedom must be a positive int, got {df!r}")
    if df > STUDENT_T_MAX_DF:
        raise StudentTTableError(
            f"df={df} is outside the tabulated Student-t domain [1, {STUDENT_T_MAX_DF}]; "
            "no normal approximation or interpolation is permitted — extending the domain "
            "requires an explicit operator/scientific ruling")
    return _tabulated_t_0975(df)


def student_t_table() -> dict[int, float]:
    """Materialize the full tabulated domain [1, STUDENT_T_MAX_DF] as a plain
    dict. A reviewable static representation of the table; used by the
    reproducibility test, never at import time."""
    return {df: _tabulated_t_0975(df) for df in range(1, STUDENT_T_MAX_DF + 1)}


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
