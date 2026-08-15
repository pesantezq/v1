"""EW-0A bounded, GPT-supervised autonomous engineering loop.

Wires the certified EW-0A machinery into the operating loop the operator will
authorize for Northstar engineering:

  mission -> select next task WITHIN mission -> risk route (E1/E2 Engineer, E3
  Claude, E4 Human) -> candidate -> deterministic verification -> INDEPENDENT GPT
  verification -> PASS/REPAIR/ESCALATE/ABSTAIN -> bounded repair -> escalation ->
  next task -> mission-boundary STOP.

Bounded by construction: mission-scoped next-task only (never starts another
phase), attempt-limited repair, Claude escalation returns to independent
verification, E4/human-required stops, and the disabled authorities
(merge/deploy/production/promotion/capital) are denied via ew0a_authority. GPT
supervisor is REQUIRED — SUPERVISOR_UNAVAILABLE pauses (never certifies).

``experimental_noncanonical``.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER
from portfolio_automation.engineer_worker.ew0a import (
    RiskClass, Executor, TaskStatus, VerificationVerdict, FailureClass,
    EngineeringTaskV0, AttemptEvidence, EngineeringVerificationV0, certify_attempt,
    OutcomeRecord, append_outcome)
from portfolio_automation.engineer_worker.ew0a_authority import (
    EngineerAuthorityLevel, AuthorityError, admit_engineer_task, assert_operation_allowed)
from portfolio_automation.engineer_worker.gpt_supervisor import SupervisorDecision, SupervisorVerdict

SCHEMA_KIND = EXPERIMENTAL_MARKER
RUNTIME_SCHEMA_VERSION = "engineering.runtime_policy.v0"
DEFAULT_RUNTIME_REL = "config/ew0a_runtime.json"


def regression_delta(baseline_failures: list[str] | set[str],
                     candidate_failures: list[str] | set[str]) -> dict[str, list[str]]:
    """Exact-node-ID regression comparison (Phase 13). Returns the NEW failures a
    candidate introduced and any baseline failures it fixed. A candidate with ANY
    new relevant failure must NOT be certified; a disappearing baseline failure is
    an improvement but never masks a newly introduced one."""
    base, cand = set(baseline_failures), set(candidate_failures)
    return {"new_relevant_failures": sorted(cand - base), "fixed": sorted(base - cand)}


class LoopStop(str, Enum):
    MISSION_COMPLETE = "STOP_FOR_MISSION_REVIEW:mission_complete"
    CHECKPOINT_BUDGET = "STOP_FOR_MISSION_REVIEW:checkpoint_budget"
    HUMAN_REQUIRED = "STOP:human_required"
    AUTHORITY_VIOLATION = "STOP:authority_violation"
    SUPERVISOR_OUTAGE = "STOP:supervisor_outage"
    BOTH_FAILED = "STOP:engineer_and_claude_failed"


@dataclass
class RuntimePolicy:
    """Conservative first-phase supervised-autonomous runtime policy. The AUTHORITY
    booleans mirror the EW-0A/A1 model (they are validated against it, not a parallel
    system). Persisted (trusted, protected) at config/ew0a_runtime.json."""
    mission_id: str
    engineering_mode: str = "SUPERVISED_AUTONOMOUS"
    authority: str = EngineerAuthorityLevel.A1_ASSISTED_ENGINEERING.value
    gpt_supervisor_required: bool = True
    auto_next_task_within_mission: bool = True
    auto_repair_within_attempt_limit: bool = True
    auto_claude_escalation_for_e3_or_exhausted_e2: bool = True
    # disabled authorities (must stay false)
    auto_merge: bool = False
    auto_deploy: bool = False
    auto_production_mutation: bool = False
    auto_authority_promotion: bool = False
    auto_capital_action: bool = False
    # conservative window
    max_concurrent_tasks: int = 1
    max_tasks_without_checkpoint: int = 10
    engineer_attempts_per_task: int = 2
    claude_attempts_per_escalation: int = 2
    schema_version: str = RUNTIME_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND

    def disabled_authorities_ok(self) -> bool:
        return not (self.auto_merge or self.auto_deploy or self.auto_production_mutation
                    or self.auto_authority_promotion or self.auto_capital_action)


def read_runtime_policy(repo_root: str | Path, rel: str = DEFAULT_RUNTIME_REL) -> RuntimePolicy | None:
    p = Path(repo_root) / rel
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return RuntimePolicy(**{k: v for k, v in d.items() if k in RuntimePolicy.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        return None


def write_runtime_policy(repo_root: str | Path, policy: RuntimePolicy, rel: str = DEFAULT_RUNTIME_REL) -> None:
    p = Path(repo_root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(policy), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


# --- routing -----------------------------------------------------------------
class Route(str, Enum):
    ENGINEER = "ENGINEER"
    CLAUDE = "CLAUDE"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"


def route_task(task: EngineeringTaskV0, level: EngineerAuthorityLevel) -> Route:
    """Deterministic risk routing. E1/E2 -> Engineer (only at A1); E3 -> Claude;
    E4 -> Human-required (autonomous completion insufficient)."""
    if task.risk_class in (RiskClass.E1_ROUTINE, RiskClass.E2_MODERATE):
        admit_engineer_task(level, task.risk_class)   # raises if not A1 (fail closed)
        return Route.ENGINEER
    if task.risk_class is RiskClass.E3_HIGH:
        return Route.CLAUDE
    return Route.HUMAN_REQUIRED   # E4


# Injected fns: produce an AttemptEvidence for a task. supervisor: packet->decision.
EngineerFn = Callable[[EngineeringTaskV0, int], AttemptEvidence]
ClaudeFn = Callable[[EngineeringTaskV0, EngineeringVerificationV0], AttemptEvidence]
SupervisorFn = Callable[[dict[str, Any]], SupervisorDecision]


@dataclass
class TaskRunResult:
    task_id: str
    route: str
    final_status: str
    verdict: str
    engineer_attempts: int
    escalated: bool
    claude_attempts: int
    human_required: bool
    supervisor_outage: bool
    verification: dict[str, Any] | None
    failure_class: str | None


def run_task(task: EngineeringTaskV0, level: EngineerAuthorityLevel, policy: RuntimePolicy,
             engineer_fn: EngineerFn, claude_fn: ClaudeFn, supervisor: SupervisorFn,
             now_fn: Callable[[], str], vid: Callable[[], str]) -> TaskRunResult:
    """Run one task through routing -> bounded engineer attempts -> escalation ->
    independent verification. Never certifies without deterministic PASS + GPT PASS."""
    route = route_task(task, level)
    if route is Route.HUMAN_REQUIRED:
        return TaskRunResult(task.task_id, route.value, TaskStatus.ESCALATION_REQUIRED.value,
                             "HUMAN_REQUIRED", 0, True, 0, True, False, None, "E4_HUMAN_REQUIRED")

    eng_attempts = 0
    last_v: EngineeringVerificationV0 | None = None
    supervisor_outage = False
    limit = min(task.max_attempts, policy.engineer_attempts_per_task)

    if route is Route.ENGINEER:
        while eng_attempts < limit:
            eng_attempts += 1
            attempt = engineer_fn(task, eng_attempts)
            v = certify_attempt(task, attempt, supervisor, now_fn, vid())
            last_v = v
            if v.verdict is VerificationVerdict.PASS:
                return _ok(task, route, v, eng_attempts, False, 0, False)
            if v.verdict is VerificationVerdict.SUPERVISOR_UNAVAILABLE and policy.gpt_supervisor_required:
                supervisor_outage = True
                break                                   # pause; never certify without GPT
            if v.verdict in (VerificationVerdict.ESCALATE, VerificationVerdict.ABSTAIN):
                break
            # REPAIR/FAIL -> bounded retry (auto_repair) or exhaust
            if v.verdict is VerificationVerdict.FAIL and v.failure_class == FailureClass.POLICY_VIOLATION.value:
                break                                   # policy violation: stop, no retry
        # engineer exhausted / escalate / abstain
        if supervisor_outage:
            return _stop(task, route, last_v, eng_attempts, "SUPERVISOR_UNAVAILABLE", supervisor_outage=True)
        if last_v and last_v.verdict is VerificationVerdict.ABSTAIN:
            return _res(task, route, last_v, eng_attempts, False, 0, False,
                        TaskStatus.ABSTAINED, "ABSTAIN")
        # exhausted E2 or explicit ESCALATE -> Claude (if enabled)

    # --- Claude escalation (E3 route, or exhausted/escalated engineer) --------
    if not policy.auto_claude_escalation_for_e3_or_exhausted_e2:
        return _res(task, route, last_v, eng_attempts, False, 0, False,
                    TaskStatus.ESCALATION_REQUIRED, "ESCALATE")
    claude_attempts = 0
    climit = policy.claude_attempts_per_escalation
    while claude_attempts < climit:
        claude_attempts += 1
        c_attempt = claude_fn(task, last_v)
        v = certify_attempt(task, c_attempt, supervisor, now_fn, vid())  # Claude does NOT bypass GPT
        last_v = v
        if v.verdict is VerificationVerdict.PASS:
            return _ok(task, Route.CLAUDE, v, eng_attempts, True, claude_attempts, False)
        if v.verdict is VerificationVerdict.SUPERVISOR_UNAVAILABLE and policy.gpt_supervisor_required:
            return _stop(task, Route.CLAUDE, last_v, eng_attempts, "SUPERVISOR_UNAVAILABLE",
                         escalated=True, claude_attempts=claude_attempts, supervisor_outage=True)
    # both engineer and claude failed
    return TaskRunResult(task.task_id, Route.CLAUDE.value, TaskStatus.ESCALATION_REQUIRED.value,
                         (last_v.verdict.value if last_v else "FAIL"), eng_attempts, True,
                         claude_attempts, False, False,
                         last_v.to_dict() if last_v else None,
                         last_v.failure_class if last_v else None)


def _ok(task, route, v, eng_a, escalated, claude_a, human):
    return TaskRunResult(task.task_id, route.value, TaskStatus.VERIFIED.value, v.verdict.value,
                         eng_a, escalated, claude_a, human, False, v.to_dict(), v.failure_class)

def _res(task, route, v, eng_a, escalated, claude_a, human, status, verdict):
    return TaskRunResult(task.task_id, route.value, status.value, verdict, eng_a, escalated,
                         claude_a, human, False, v.to_dict() if v else None,
                         v.failure_class if v else None)

def _stop(task, route, v, eng_a, verdict, *, escalated=False, claude_attempts=0, supervisor_outage=False):
    return TaskRunResult(task.task_id, route.value, TaskStatus.VERIFYING.value, verdict, eng_a,
                         escalated, claude_attempts, False, supervisor_outage,
                         v.to_dict() if v else None, v.failure_class if v else None)


@dataclass
class MissionReport:
    mission_id: str
    tasks_run: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    verified: int = 0
    escalated: int = 0
    human_required: int = 0
    supervisor_outage: bool = False


def run_mission(policy: RuntimePolicy, task_queue: list[EngineeringTaskV0],
                level: EngineerAuthorityLevel, engineer_fn: EngineerFn, claude_fn: ClaudeFn,
                supervisor: SupervisorFn, now_fn: Callable[[], str], vid: Callable[[], str],
                outcome_log: str | None = None) -> MissionReport:
    """Process tasks WITHIN the authorized mission only. Auto-selects the next task
    after each completion; STOPS at the mission boundary, on human-required, on an
    authority violation, on supervisor outage, or when the checkpoint budget is
    exhausted. It NEVER starts a task from another mission or another phase."""
    rep = MissionReport(mission_id=policy.mission_id)
    processed = 0
    for task in task_queue:
        # mission boundary: refuse any task not in the authorized mission
        if task.mission_id != policy.mission_id:
            rep.stop_reason = LoopStop.MISSION_COMPLETE.value + f" (out-of-mission task {task.task_id} refused)"
            break
        if processed >= policy.max_tasks_without_checkpoint:
            rep.stop_reason = LoopStop.CHECKPOINT_BUDGET.value
            break
        r = run_task(task, level, policy, engineer_fn, claude_fn, supervisor, now_fn, vid)
        rep.tasks_run.append(asdict(r))
        processed += 1
        if outcome_log:
            append_outcome(outcome_log, OutcomeRecord(
                task_id=task.task_id, title=task.title, risk_class=task.risk_class.value,
                executor=r.route, attempt_count=r.engineer_attempts + r.claude_attempts,
                failure_classes=[r.failure_class] if r.failure_class else [], escalated=r.escalated,
                supervisor_verdict=(r.verification or {}).get("supervisor_verdict"),
                final_status=r.final_status, tests_run=[], policy_violation=(r.failure_class == "POLICY_VIOLATION"),
                human_intervention=r.human_required, disposition=r.verdict, recorded_at=now_fn()))
        if r.final_status == TaskStatus.VERIFIED.value:
            rep.verified += 1
        if r.human_required:
            rep.human_required += 1
            rep.stop_reason = LoopStop.HUMAN_REQUIRED.value
            break                                   # human decision stops the affected branch
        if r.supervisor_outage:
            rep.supervisor_outage = True
            rep.stop_reason = LoopStop.SUPERVISOR_OUTAGE.value
            break
        if r.escalated and r.final_status != TaskStatus.VERIFIED.value:
            rep.escalated += 1
            rep.stop_reason = LoopStop.BOTH_FAILED.value
            break
    else:
        rep.stop_reason = LoopStop.MISSION_COMPLETE.value
    return rep
