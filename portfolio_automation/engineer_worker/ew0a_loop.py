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
from portfolio_automation.engineer_worker.durable_certification import (
    CertificationUnavailable, ReviewContext)
from portfolio_automation.engineer_worker.roadmap_guard import (
    RoadmapAuthorization, RoadmapViolation, assert_mission_authorized,
    assert_roadmap_authoritative)

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
    ROADMAP_VIOLATION = "STOP:roadmap_violation"
    WORKER_OUTAGE = "STOP:worker_unavailable"


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


def _invoke_worker(fn: Callable[..., Any], *args: Any) -> tuple[Any, str | None]:
    """Call a worker function without letting its failure become the loop's.

    An unhandled exception out of ``engineer_fn`` propagates through
    ``run_task``, out of ``run_mission``, and takes down the mission -- past the
    outcome append, so the run leaves no durable record of what happened. A
    malformed return is the same hazard more quietly: a non-AttemptEvidence
    object reaches ``certify_attempt``, where ``getattr`` defaults would let it
    look like an attempt that simply changed nothing.

    Both are classified here as WORKER_FAILURE. Neither can produce a
    certification: this returns no evidence, and evidence is what the gate
    consumes."""
    try:
        result = fn(*args)
    except Exception as exc:                      # noqa: BLE001 - deliberate
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(result, AttemptEvidence):
        return None, (f"worker returned {type(result).__name__}, not "
                      "AttemptEvidence; a malformed result is not evidence")
    return result, None


def _lineage_entry(attempt: Any, verification: Any, *, executor: str,
                   worker_error: str | None = None) -> dict[str, Any]:
    """One machine-readable row of what this attempt was and how it ended."""
    return {
        "attempt_id": getattr(attempt, "attempt_id", None),
        "executor": executor,
        "escalated_from_attempt_id": getattr(attempt, "escalated_from_attempt_id", None),
        "candidate_sha": getattr(verification, "candidate_sha", None),
        "verdict": getattr(getattr(verification, "verdict", None), "value", None),
        "supervisor_verdict": getattr(verification, "supervisor_verdict", None),
        "failure_class": (FailureClass.WORKER_FAILURE.value if worker_error
                          else getattr(verification, "failure_class", None)),
        "worker_error": worker_error,
        "evidence_refs": list(getattr(verification, "evidence_refs", []) or []),
    }


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
    # --- EW-0B (all defaulted; existing constructions keep working) ---------
    #: One entry per attempt, in order. Carries executor, candidate SHA, the
    #: verdict and any escalation link, so the apprenticeship record can answer
    #: "what was attempted, what failed, what was repaired" without prose.
    attempt_lineage: list[dict[str, Any]] = field(default_factory=list)
    #: Screened execution identity of the LAST supervisor decision.
    execution_identity: dict[str, Any] | None = None
    candidate_sha: str | None = None
    #: Attempts lost to a worker exception / malformed output. Counted rather
    #: than folded into engineer_attempts: an attempt that never produced
    #: evidence is a different fact from one that produced failing evidence.
    worker_failures: int = 0
    worker_unavailable: bool = False


def run_task(task: EngineeringTaskV0, level: EngineerAuthorityLevel, policy: RuntimePolicy,
             engineer_fn: EngineerFn, claude_fn: ClaudeFn, supervisor: SupervisorFn,
             now_fn: Callable[[], str], vid: Callable[[], str],
             *, certification: ReviewContext,
             roadmap: RoadmapAuthorization) -> TaskRunResult:
    """Run one task through routing -> bounded engineer attempts -> escalation ->
    independent verification. Never certifies without deterministic PASS + GPT
    PASS, and never reaches the supervisor except through durable certification.

    ``certification`` is required and has no default. A parameter that can be
    omitted is a parameter that will be omitted, and the omission would restore
    the ephemeral path this exists to close."""
    if not certification.durable:
        raise CertificationUnavailable(
            "the A1 operating loop refuses a non-durable certification context; "
            "work stays unverified rather than certified on weaker evidence")
    # The mission boundary compares two values the SAME caller supplies, so on
    # its own it proves only self-consistency. ``roadmap`` is resolved from the
    # protected roadmap record, which the worker cannot write -- that is what
    # makes the boundary constrain anything. Required and undefaulted for the
    # same reason ``certification`` is: an omittable guard gets omitted.
    assert_mission_authorized(roadmap, policy.mission_id)
    assert_mission_authorized(roadmap, task.mission_id)
    route = route_task(task, level)
    if route is Route.HUMAN_REQUIRED:
        return TaskRunResult(task.task_id, route.value, TaskStatus.ESCALATION_REQUIRED.value,
                             "HUMAN_REQUIRED", 0, True, 0, True, False, None, "E4_HUMAN_REQUIRED")

    eng_attempts = 0
    last_v: EngineeringVerificationV0 | None = None
    supervisor_outage = False
    lineage: list[dict[str, Any]] = []
    worker_failures = 0
    last_worker_error: str | None = None
    limit = min(task.max_attempts, policy.engineer_attempts_per_task)

    if route is Route.ENGINEER:
        while eng_attempts < limit:
            eng_attempts += 1
            attempt, worker_error = _invoke_worker(engineer_fn, task, eng_attempts)
            if worker_error is not None:
                worker_failures += 1
                last_worker_error = worker_error
                lineage.append(_lineage_entry(None, None, executor=Route.ENGINEER.value,
                                              worker_error=worker_error))
                continue          # bounded retry; never a certification
            v = certify_attempt(task, attempt, supervisor, now_fn, vid(),
                                certification=certification)
            last_v = v
            lineage.append(_lineage_entry(attempt, v, executor=Route.ENGINEER.value))
            if v.verdict is VerificationVerdict.PASS:
                return _ok(task, route, v, eng_attempts, False, 0, False,
                           lineage=lineage, worker_failures=worker_failures)
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
            return _stop(task, route, last_v, eng_attempts, "SUPERVISOR_UNAVAILABLE",
                         supervisor_outage=True, lineage=lineage,
                         worker_failures=worker_failures)
        if last_v and last_v.verdict is VerificationVerdict.ABSTAIN:
            return _res(task, route, last_v, eng_attempts, False, 0, False,
                        TaskStatus.ABSTAINED, "ABSTAIN", lineage=lineage,
                        worker_failures=worker_failures)
        if worker_failures >= eng_attempts and eng_attempts > 0:
            # Every engineer attempt died before producing evidence. Escalating
            # a task whose failure is the worker itself hands Claude no
            # evidence to repair from, so this stops for a human instead of
            # spending an escalation budget on an empty packet.
            return TaskRunResult(
                task.task_id, route.value, TaskStatus.ESCALATION_REQUIRED.value,
                "WORKER_UNAVAILABLE", eng_attempts, False, 0, False, False, None,
                FailureClass.WORKER_FAILURE.value, attempt_lineage=lineage,
                worker_failures=worker_failures, worker_unavailable=True)
        # exhausted E2 or explicit ESCALATE -> Claude (if enabled)

    # --- Claude escalation (E3 route, or exhausted/escalated engineer) --------
    if not policy.auto_claude_escalation_for_e3_or_exhausted_e2:
        return _res(task, route, last_v, eng_attempts, False, 0, False,
                    TaskStatus.ESCALATION_REQUIRED, "ESCALATE", lineage=lineage,
                    worker_failures=worker_failures)
    claude_attempts = 0
    climit = policy.claude_attempts_per_escalation
    while claude_attempts < climit:
        claude_attempts += 1
        c_attempt, worker_error = _invoke_worker(claude_fn, task, last_v)
        if worker_error is not None:
            worker_failures += 1
            last_worker_error = worker_error
            lineage.append(_lineage_entry(None, None, executor=Route.CLAUDE.value,
                                          worker_error=worker_error))
            continue
        # Claude does NOT bypass GPT, and does not bypass durable certification.
        v = certify_attempt(task, c_attempt, supervisor, now_fn, vid(),
                            certification=certification)
        last_v = v
        lineage.append(_lineage_entry(c_attempt, v, executor=Route.CLAUDE.value))
        if v.verdict is VerificationVerdict.PASS:
            return _ok(task, Route.CLAUDE, v, eng_attempts, True, claude_attempts, False,
                       lineage=lineage, worker_failures=worker_failures)
        if v.verdict is VerificationVerdict.SUPERVISOR_UNAVAILABLE and policy.gpt_supervisor_required:
            return _stop(task, Route.CLAUDE, last_v, eng_attempts, "SUPERVISOR_UNAVAILABLE",
                         escalated=True, claude_attempts=claude_attempts,
                         supervisor_outage=True, lineage=lineage,
                         worker_failures=worker_failures)
    # both engineer and claude failed
    return TaskRunResult(task.task_id, Route.CLAUDE.value, TaskStatus.ESCALATION_REQUIRED.value,
                         (last_v.verdict.value if last_v else "FAIL"), eng_attempts, True,
                         claude_attempts, False, False,
                         last_v.to_dict() if last_v else None,
                         (last_v.failure_class if last_v
                          else (FailureClass.WORKER_FAILURE.value if last_worker_error
                                else None)),
                         attempt_lineage=lineage,
                         execution_identity=(last_v.execution_identity if last_v else None),
                         candidate_sha=(last_v.candidate_sha if last_v else None),
                         worker_failures=worker_failures,
                         worker_unavailable=bool(last_worker_error and last_v is None))


def _ok(task, route, v, eng_a, escalated, claude_a, human, *, lineage=None,
        worker_failures=0):
    return TaskRunResult(task.task_id, route.value, TaskStatus.VERIFIED.value, v.verdict.value,
                         eng_a, escalated, claude_a, human, False, v.to_dict(), v.failure_class,
                         attempt_lineage=list(lineage or []),
                         execution_identity=v.execution_identity,
                         candidate_sha=v.candidate_sha, worker_failures=worker_failures)

def _res(task, route, v, eng_a, escalated, claude_a, human, status, verdict, *,
         lineage=None, worker_failures=0):
    return TaskRunResult(task.task_id, route.value, status.value, verdict, eng_a, escalated,
                         claude_a, human, False, v.to_dict() if v else None,
                         v.failure_class if v else None,
                         attempt_lineage=list(lineage or []),
                         execution_identity=(v.execution_identity if v else None),
                         candidate_sha=(v.candidate_sha if v else None),
                         worker_failures=worker_failures)

def _stop(task, route, v, eng_a, verdict, *, escalated=False, claude_attempts=0,
          supervisor_outage=False, lineage=None, worker_failures=0):
    return TaskRunResult(task.task_id, route.value, TaskStatus.VERIFYING.value, verdict, eng_a,
                         escalated, claude_attempts, False, supervisor_outage,
                         v.to_dict() if v else None, v.failure_class if v else None,
                         attempt_lineage=list(lineage or []),
                         execution_identity=(v.execution_identity if v else None),
                         candidate_sha=(v.candidate_sha if v else None),
                         worker_failures=worker_failures)


def run_authorized_mission(repo_root: str | Path, level: EngineerAuthorityLevel,
                           task_queue: list[EngineeringTaskV0],
                           engineer_fn: EngineerFn, claude_fn: ClaudeFn,
                           supervisor: SupervisorFn, now_fn: Callable[[], str],
                           vid: Callable[[], str], outcome_log: str | None = None,
                           *, certification: ReviewContext) -> "MissionReport":
    """THE PRODUCTION ENTRY POINT. Resolves its own authorization from disk.

    ``run_mission`` takes a RoadmapAuthorization, which is right for a harness
    and wrong as the only door: a caller able to construct
    ``RoadmapAuthorization.for_mission(x)`` can authorize x, and a guard whose
    input the applicant manufactures is the applicant authorizing itself.

    Both sides are resolved here from protected on-disk state -- the roadmap
    record for WHICH mission is authorized, the runtime policy for what the
    loop is configured to run -- and neither is accepted from the caller. The
    synthetic path is not removed, only made unreachable from here."""
    root = Path(repo_root)
    roadmap = RoadmapAuthorization.read(root)
    assert_roadmap_authoritative(roadmap)
    policy = read_runtime_policy(root)
    if policy is None:
        raise RoadmapViolation(
            f"the runtime policy at {DEFAULT_RUNTIME_REL} is absent or malformed; "
            "refusing to dispatch under a configuration that cannot be read")
    # Cross-check, not self-check: the policy is one protected file and the
    # roadmap is another, so agreement here is two independent records agreeing
    # rather than one caller agreeing with itself.
    assert_mission_authorized(roadmap, policy.mission_id)
    return run_mission(policy, task_queue, level, engineer_fn, claude_fn,
                       supervisor, now_fn, vid, outcome_log,
                       certification=certification, roadmap=roadmap)


@dataclass
class MissionReport:
    mission_id: str
    tasks_run: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    verified: int = 0
    escalated: int = 0
    human_required: int = 0
    supervisor_outage: bool = False
    worker_outage: bool = False
    roadmap_violation: bool = False


def run_mission(policy: RuntimePolicy, task_queue: list[EngineeringTaskV0],
                level: EngineerAuthorityLevel, engineer_fn: EngineerFn, claude_fn: ClaudeFn,
                supervisor: SupervisorFn, now_fn: Callable[[], str], vid: Callable[[], str],
                outcome_log: str | None = None, *,
                certification: ReviewContext,
                roadmap: RoadmapAuthorization) -> MissionReport:
    """Process tasks WITHIN the authorized mission only. Auto-selects the next task
    after each completion; STOPS at the mission boundary, on human-required, on an
    authority violation, on supervisor outage, or when the checkpoint budget is
    exhausted. It NEVER starts a task from another mission or another phase."""
    if not certification.durable:
        raise CertificationUnavailable(
            "the A1 operating loop refuses a non-durable certification context")
    rep = MissionReport(mission_id=policy.mission_id)
    processed = 0
    try:
        # Resolved BEFORE any task runs. Refusing the whole mission is the
        # correct granularity: an unauthorized mission is not a queue with some
        # bad entries, it is work that should not have been dispatched at all.
        assert_mission_authorized(roadmap, policy.mission_id)
    except RoadmapViolation as exc:
        rep.stop_reason = f"{LoopStop.ROADMAP_VIOLATION.value} ({exc})"
        rep.roadmap_violation = True
        return rep
    for task in task_queue:
        # mission boundary: refuse any task not in the authorized mission
        if task.mission_id != policy.mission_id:
            rep.stop_reason = LoopStop.MISSION_COMPLETE.value + f" (out-of-mission task {task.task_id} refused)"
            break
        if processed >= policy.max_tasks_without_checkpoint:
            rep.stop_reason = LoopStop.CHECKPOINT_BUDGET.value
            break
        try:
            r = run_task(task, level, policy, engineer_fn, claude_fn, supervisor, now_fn,
                         vid, certification=certification, roadmap=roadmap)
        except RoadmapViolation as exc:
            rep.stop_reason = f"{LoopStop.ROADMAP_VIOLATION.value} ({exc})"
            rep.roadmap_violation = True
            break
        rep.tasks_run.append(asdict(r))
        processed += 1
        if outcome_log:
            append_outcome(outcome_log, OutcomeRecord(
                task_id=task.task_id, title=task.title, risk_class=task.risk_class.value,
                executor=r.route, attempt_count=r.engineer_attempts + r.claude_attempts,
                failure_classes=[r.failure_class] if r.failure_class else [], escalated=r.escalated,
                supervisor_verdict=(r.verification or {}).get("supervisor_verdict"),
                final_status=r.final_status, tests_run=[], policy_violation=(r.failure_class == "POLICY_VIOLATION"),
                human_intervention=r.human_required, disposition=r.verdict, recorded_at=now_fn(),
                # EW-0B: the apprenticeship record is now attributable. Without
                # these, an outcome cannot say which candidate or which model /
                # prompt / toolset produced it, and G1 would be measuring an
                # unlabelled population.
                mission_id=policy.mission_id, candidate_sha=r.candidate_sha,
                execution_id=(r.execution_identity or {}).get("execution_id"),
                execution_identity=r.execution_identity,
                attempt_lineage=list(r.attempt_lineage)))
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
        if r.worker_unavailable:
            rep.worker_outage = True
            rep.stop_reason = LoopStop.WORKER_OUTAGE.value
            break
        if r.escalated and r.final_status != TaskStatus.VERIFIED.value:
            rep.escalated += 1
            rep.stop_reason = LoopStop.BOTH_FAILED.value
            break
    else:
        rep.stop_reason = LoopStop.MISSION_COMPLETE.value
    return rep
