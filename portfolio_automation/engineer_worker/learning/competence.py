"""Automatic competence updater (Phase 6).

Competence is tracked PER CAPABILITY. There is deliberately no single generic
"worker intelligence" score: a worker that routes routine tasks well may still be
dangerous at security escalation, and one aggregate number would hide exactly the
signal that matters.

The Worker may READ its profile. It may never edit it — every mutator here runs
through the trusted-actor gate in ``store``/``config``, and the competence log is
outside the Worker's repair scope.
"""
from __future__ import annotations

from dataclasses import replace

from portfolio_automation.engineer_worker.learning.contracts import (
    Capability, OutcomeEvaluationV0, TaskClassPerformanceV0, WorkerCompetenceProfileV0)


def empty_performance(worker_id: str, capability: str) -> TaskClassPerformanceV0:
    return TaskClassPerformanceV0(worker_id=worker_id, capability=capability)


def apply_evaluation(perf: TaskClassPerformanceV0, ev: OutcomeEvaluationV0, now: str,
                     *, recent_window_size: int = 20,
                     authority_violation: bool = False,
                     false_certification: bool = False,
                     security_escalation_failure: bool = False,
                     first_pass: bool = True, repaired: bool = False
                     ) -> TaskClassPerformanceV0:
    """Fold ONE evaluation into a capability's statistics.

    ``consecutive_safe`` resets to zero on ANY unsafe observation. That reset is
    what makes the graduation gate a real bar: a worker cannot accumulate a long
    safe streak, commit one authority violation, and keep the streak."""
    safe = ev.is_safe and not authority_violation and not false_certification
    correct = ev.is_correct

    observations = perf.observations + 1
    recent_window = min(observations, recent_window_size)

    return replace(
        perf,
        observations=observations,
        correct=perf.correct + (1 if correct else 0),
        unsafe=perf.unsafe + (0 if safe else 1),
        successful_first_pass=perf.successful_first_pass + (1 if (correct and first_pass) else 0),
        repairs=perf.repairs + (1 if repaired else 0),
        lesson_retrievals=perf.lesson_retrievals + (1 if ev.lesson_retrieved else 0),
        successful_lesson_transfers=perf.successful_lesson_transfers
        + (1 if ev.lesson_transfer_success else 0),
        repeated_error_after_lesson=perf.repeated_error_after_lesson
        + (1 if ev.repeated_error_after_lesson else 0),
        missed_escalations=perf.missed_escalations + (1 if ev.missed_escalation else 0),
        authority_violations=perf.authority_violations
        + (1 if (authority_violation or ev.authority_expansion_attempt) else 0),
        false_certifications=perf.false_certifications + (1 if false_certification else 0),
        security_escalation_failures=perf.security_escalation_failures
        + (1 if security_escalation_failure else 0),
        consecutive_safe=(perf.consecutive_safe + 1) if safe else 0,
        recent_window_safe=min(recent_window,
                               (perf.recent_window_safe + 1) if safe else 0),
        recent_window_size=recent_window,
        updated_at=now)


def build_profile(worker_id: str, controller_level: str, ew_authority: str,
                  performances: dict[str, TaskClassPerformanceV0], now: str
                  ) -> WorkerCompetenceProfileV0:
    """Summarize across capabilities WITHOUT collapsing them into one score."""
    caps = {cap: perf.to_dict() for cap, perf in sorted(performances.items())}
    return WorkerCompetenceProfileV0(
        worker_id=worker_id, controller_level=controller_level, ew_authority=ew_authority,
        capabilities=caps,
        total_observations=sum(p.observations for p in performances.values()),
        total_unsafe=sum(p.unsafe for p in performances.values()),
        generated_at=now)


def known_capabilities() -> list[str]:
    return [c.value for c in Capability]
