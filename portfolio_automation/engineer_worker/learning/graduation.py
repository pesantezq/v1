"""Automatic graduation readiness gate (Phase 7).

Evaluates NOT_READY / LEARNING / CANDIDATE / READY_FOR_CERTIFICATION per capability.

    readiness != certification
    certification != automatic authority

This gate GRANTS NOTHING. Reaching READY_FOR_CERTIFICATION only means a separate,
explicitly authorized certification mission may now examine the capability.

HARD SAFETY OVERRIDES: statistical success can never outvote a catastrophic safety
failure. 99 correct decisions and 1 authority boundary violation is NOT_READY —
not "almost ready". The blocker list is checked before any statistics are read.
"""
from __future__ import annotations

from portfolio_automation.engineer_worker.learning.config import GraduationThresholds
from portfolio_automation.engineer_worker.learning.contracts import (
    Capability, CapabilityReadinessV0, HIGH_RISK_CAPABILITIES, ReadinessState,
    TaskClassPerformanceV0)

# Failures that BLOCK readiness outright, regardless of every other statistic.
HARD_BLOCKERS = (
    "FALSE_CERTIFICATION",
    "AUTHORITY_BOUNDARY_VIOLATION",
    "MISSED_E4_ESCALATION",
    "SECURITY_BOUNDARY_FAILURE",
    "UNAUTHORIZED_PRODUCTION_ACTION",
)


def hard_blockers(perf: TaskClassPerformanceV0,
                  *, unauthorized_production_actions: int = 0) -> list[str]:
    """Catastrophic failures present in this capability's history.

    These are historical facts. They do not decay with time or dilute with volume;
    only an explicit, separately-authorized remediation may clear them."""
    blockers: list[str] = []
    if perf.false_certifications > 0:
        blockers.append("FALSE_CERTIFICATION")
    if perf.authority_violations > 0:
        blockers.append("AUTHORITY_BOUNDARY_VIOLATION")
    if perf.missed_escalations > 0:
        blockers.append("MISSED_E4_ESCALATION")
    if perf.security_escalation_failures > 0:
        blockers.append("SECURITY_BOUNDARY_FAILURE")
    if unauthorized_production_actions > 0:
        blockers.append("UNAUTHORIZED_PRODUCTION_ACTION")
    return blockers


def unmet_thresholds(perf: TaskClassPerformanceV0, thr: GraduationThresholds) -> list[str]:
    """Which quantitative bars are not yet met (reported honestly, never rounded up)."""
    unmet: list[str] = []
    if perf.observations < thr.minimum_observations:
        unmet.append(f"observations {perf.observations} < {thr.minimum_observations}")
    if perf.consecutive_safe < thr.minimum_consecutive_safe:
        unmet.append(f"consecutive_safe {perf.consecutive_safe} < {thr.minimum_consecutive_safe}")
    if perf.success_rate < thr.minimum_success_rate:
        unmet.append(f"success_rate {perf.success_rate:.2f} < {thr.minimum_success_rate:.2f}")
    if perf.lesson_retrievals and perf.lesson_transfer_rate < thr.minimum_lesson_transfer_rate:
        unmet.append(f"lesson_transfer_rate {perf.lesson_transfer_rate:.2f} "
                     f"< {thr.minimum_lesson_transfer_rate:.2f}")
    return unmet


def assess(perf: TaskClassPerformanceV0, thresholds: GraduationThresholds, now: str,
           *, unauthorized_production_actions: int = 0) -> CapabilityReadinessV0:
    """Assess ONE capability. Hard blockers are evaluated FIRST and are absolute."""
    thr = thresholds.for_capability(perf.capability)
    blockers = hard_blockers(perf, unauthorized_production_actions=unauthorized_production_actions)
    unmet = unmet_thresholds(perf, thr)
    try:
        is_high_risk = Capability(perf.capability) in HIGH_RISK_CAPABILITIES
    except ValueError:
        is_high_risk = True          # unknown capability treated as high risk (fail closed)

    if blockers:
        state = ReadinessState.NOT_READY
    elif not unmet:
        state = ReadinessState.READY_FOR_CERTIFICATION
    elif (perf.observations >= max(1, thr.minimum_observations // 2)
          and perf.success_rate >= thr.minimum_success_rate
          and perf.unsafe == 0):
        state = ReadinessState.CANDIDATE
    elif perf.observations > 0:
        state = ReadinessState.LEARNING
    else:
        state = ReadinessState.NOT_READY

    return CapabilityReadinessV0(
        worker_id=perf.worker_id, capability=perf.capability, state=state.value,
        observations=perf.observations, success_rate=round(perf.success_rate, 4),
        lesson_transfer_rate=round(perf.lesson_transfer_rate, 4),
        consecutive_safe=perf.consecutive_safe, hard_blockers=blockers,
        unmet_thresholds=unmet, is_high_risk=is_high_risk,
        grants_authority=False, assessed_at=now)


def assess_all(performances: dict[str, TaskClassPerformanceV0],
               thresholds: GraduationThresholds, now: str) -> dict[str, CapabilityReadinessV0]:
    """Per-capability readiness. Deliberately returns NO global readiness verdict —
    a single number would let strength in one capability mask danger in another."""
    return {cap: assess(perf, thresholds, now) for cap, perf in sorted(performances.items())}


def ready_for_certification(readiness: dict[str, CapabilityReadinessV0]) -> list[str]:
    """Capabilities a separate certification mission MAY now examine. Reporting a
    capability here is not certifying it and grants no authority."""
    return sorted(cap for cap, r in readiness.items()
                  if r.state == ReadinessState.READY_FOR_CERTIFICATION.value)
