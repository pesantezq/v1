"""EW-0A Safe Engineering Operations — task/risk/verification/escalation model
plus the deterministic certification orchestrator.

CORE INVARIANT (anti-self-certification): the component performing work NEVER
declares its own work successful. An attempt becomes ``VERIFIED`` only when BOTH
   (a) deterministic checks pass (scope / protected-path / policy / tests), AND
   (b) the INDEPENDENT GPT supervisor returns ``PASS``.
Any other combination yields REPAIR_REQUIRED / ESCALATION_REQUIRED / ABSTAINED /
FAILED_VALIDATION / (supervisor) SUPERVISOR_UNAVAILABLE — never success. A failed
or unavailable supervisor can never produce a certification.

``experimental_noncanonical``. Does not define canonical Northstar contracts.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER
from portfolio_automation.engineer_worker import policy
from portfolio_automation.engineer_worker.durable_certification import (
    CertificationUnavailable, ReviewContext, dispatch_durably,
)
from portfolio_automation.engineer_worker.evidence_sufficiency import assess_evidence
from portfolio_automation.engineer_worker.gpt_supervisor import (
    SupervisorDecision, SupervisorVerdict)


def _candidate_sha(candidate: Any) -> str | None:
    """The commit a binding describes, or None when it cannot say."""
    return getattr(candidate, "head_at_binding", None)

SCHEMA_KIND = EXPERIMENTAL_MARKER
TASK_SCHEMA_VERSION = "engineering.task.v0"
VERIFICATION_SCHEMA_VERSION = "engineering.ew0a_verification.v0"
OUTCOME_SCHEMA_VERSION = "engineering.outcome.v0"


class EW0AError(ValueError):
    """Deterministic, fail-closed EW-0A error."""


# --- risk / executor model ---------------------------------------------------
class RiskClass(str, Enum):
    E1_ROUTINE = "E1_ROUTINE"
    E2_MODERATE = "E2_MODERATE"
    E3_HIGH = "E3_HIGH"                 # architecture / security / governance / broad refactor
    E4_CONSEQUENTIAL = "E4_CONSEQUENTIAL"  # production / capital / irreversible


class Executor(str, Enum):
    ENGINEER = "ENGINEER"                 # local Engineer Worker
    ENGINEER_STRICT = "ENGINEER_STRICT"   # Engineer Worker, stricter verification
    CLAUDE = "CLAUDE"                     # senior / escalation
    CLAUDE_HUMAN = "CLAUDE_HUMAN"         # Claude + human authority


# Default executor per risk class. Assignment is controlled OUTSIDE the worker;
# the worker can neither choose its executor nor lower its risk class.
_RISK_DEFAULT_EXECUTOR: dict[RiskClass, Executor] = {
    RiskClass.E1_ROUTINE: Executor.ENGINEER,
    RiskClass.E2_MODERATE: Executor.ENGINEER_STRICT,
    RiskClass.E3_HIGH: Executor.CLAUDE,
    RiskClass.E4_CONSEQUENTIAL: Executor.CLAUDE_HUMAN,
}

_ENGINEER_EXECUTORS = frozenset({Executor.ENGINEER, Executor.ENGINEER_STRICT})
_WORKER_ALLOWED_RISK = frozenset({RiskClass.E1_ROUTINE, RiskClass.E2_MODERATE})


def default_executor(risk: RiskClass) -> Executor:
    return _RISK_DEFAULT_EXECUTOR[risk]


def worker_may_execute(risk: RiskClass) -> bool:
    """The Engineer Worker may only execute E1/E2. E3/E4 route to Claude(/human)."""
    return risk in _WORKER_ALLOWED_RISK


def assign_executor(risk: RiskClass, requested_executor: Executor | None = None) -> Executor:
    """Deterministically assign the executor. A request to run the ENGINEER on an
    E3/E4 task is REFUSED (fail closed) — the worker cannot self-elevate scope."""
    if requested_executor in _ENGINEER_EXECUTORS and not worker_may_execute(risk):
        raise EW0AError(f"Engineer executor refused for {risk.value}: routes to Claude(/human)")
    return requested_executor or default_executor(risk)


# --- authoritative task status (only the orchestrator sets these) ------------
class TaskStatus(str, Enum):
    CREATED = "CREATED"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    IMPLEMENTATION_COMPLETE = "IMPLEMENTATION_COMPLETE"  # worker claim ONLY (not authoritative success)
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"                # terminal success (deterministic + GPT PASS)
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    ESCALATION_REQUIRED = "ESCALATION_REQUIRED"
    ABSTAINED = "ABSTAINED"
    FAILED_VALIDATION = "FAILED_VALIDATION"
    INTERRUPTED = "INTERRUPTED"
    CANCELLED = "CANCELLED"


_TERMINAL = frozenset({TaskStatus.VERIFIED, TaskStatus.FAILED_VALIDATION,
                       TaskStatus.ABSTAINED, TaskStatus.CANCELLED})


# --- typed failure model -----------------------------------------------------
class FailureClass(str, Enum):
    IMPLEMENTATION_BUG = "IMPLEMENTATION_BUG"
    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
    TEST_FAILURE = "TEST_FAILURE"
    ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    AMBIGUOUS_REQUIREMENT = "AMBIGUOUS_REQUIREMENT"
    ARCHITECTURE_ESCALATION = "ARCHITECTURE_ESCALATION"
    SECURITY_ESCALATION = "SECURITY_ESCALATION"
    WORKER_FAILURE = "WORKER_FAILURE"
    SANDBOX_FAILURE = "SANDBOX_FAILURE"
    # EW-0B: the attempt cleared the deterministic gate but carries too little
    # evidence to be judged at all (no criteria, no diff, no test execution,
    # results with no run behind them). Distinct from TEST_FAILURE, which means
    # a test ran and failed -- here nothing ran, which is the weaker evidence.
    EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"
    TIMEOUT = "TIMEOUT"
    INTERRUPTED = "INTERRUPTED"


class NextAction(str, Enum):
    RETRY_ENGINEER = "RETRY_ENGINEER"       # bounded retry allowed
    REMAIN_UNVERIFIED = "REMAIN_UNVERIFIED"
    ABSTAIN = "ABSTAIN"
    ESCALATE_CLAUDE = "ESCALATE_CLAUDE"
    ESCALATE_HUMAN = "ESCALATE_HUMAN"
    STOP_NO_RETRY = "STOP_NO_RETRY"


_FAILURE_ACTION: dict[FailureClass, NextAction] = {
    FailureClass.IMPLEMENTATION_BUG: NextAction.RETRY_ENGINEER,
    FailureClass.VERIFICATION_FAILURE: NextAction.REMAIN_UNVERIFIED,
    FailureClass.TEST_FAILURE: NextAction.RETRY_ENGINEER,
    FailureClass.ENVIRONMENT_FAILURE: NextAction.REMAIN_UNVERIFIED,
    FailureClass.POLICY_VIOLATION: NextAction.STOP_NO_RETRY,     # no automatic retry
    FailureClass.AMBIGUOUS_REQUIREMENT: NextAction.ABSTAIN,
    FailureClass.ARCHITECTURE_ESCALATION: NextAction.ESCALATE_CLAUDE,
    FailureClass.SECURITY_ESCALATION: NextAction.ESCALATE_HUMAN,
    FailureClass.WORKER_FAILURE: NextAction.RETRY_ENGINEER,
    FailureClass.SANDBOX_FAILURE: NextAction.REMAIN_UNVERIFIED,
    FailureClass.EVIDENCE_INSUFFICIENT: NextAction.RETRY_ENGINEER,
    FailureClass.TIMEOUT: NextAction.REMAIN_UNVERIFIED,
    FailureClass.INTERRUPTED: NextAction.REMAIN_UNVERIFIED,      # INTERRUPTED is never SUCCESS
}


def action_for_failure(fc: FailureClass) -> NextAction:
    return _FAILURE_ACTION[fc]


# --- task contract -----------------------------------------------------------
@dataclass
class EngineeringTaskV0:
    task_id: str
    title: str
    goal: str
    risk_class: RiskClass
    executor: Executor
    mission_id: str | None = None       # the mission this task belongs to (scope boundary)
    base_sha: str | None = None
    allowed_paths: list[str] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    forbidden_operations: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    verification_steps: list[str] = field(default_factory=list)
    allowed_tests: list[str] = field(default_factory=list)
    max_attempts: int = 2
    timeout_seconds: int = 300
    escalation_conditions: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.CREATED
    schema_version: str = TASK_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["risk_class"] = self.risk_class.value
        d["executor"] = self.executor.value
        d["status"] = self.status.value
        return d


@dataclass
class AttemptEvidence:
    """What a worker attempt produced. NONE of these fields grant success; they
    are evidence the deterministic orchestrator + independent supervisor judge."""
    attempt_id: str
    executor: Executor
    worker_claim: str                       # e.g. "IMPLEMENTATION_COMPLETE" — a CLAIM, not authority
    changed_paths: list[str] = field(default_factory=list)
    diff_text: str = ""
    tests_run: list[str] = field(default_factory=list)
    test_results: dict[str, str] = field(default_factory=dict)
    py_compile_ok: bool | None = None
    canonical_repo_touched: bool = False    # MUST be False — work happens in a disposable copy
    abstained: bool = False
    abstain_reason: str | None = None
    notes: str = ""
    #: EW-0B. Proof that THIS attempt's evidence describes a committed
    #: candidate. A repair that actually changes code produces a NEW commit, so
    #: the binding taken for attempt 1 no longer describes attempt 2 -- reusing
    #: it makes every genuine repair refuse at dispatch (HEAD moved), which is
    #: safe but means the repair-to-certification path can never complete.
    #: When None the context-level binding is used, preserving prior behaviour.
    candidate_binding: Any = None
    #: EW-0B escalation lineage. When Claude repairs an engineer attempt, this
    #: names the attempt it was handed, so a later audit can read the chain
    #: worker -> REPAIR -> Claude -> independent review without inferring it
    #: from ordering. Additive: absent on every historical record.
    escalated_from_attempt_id: str | None = None


# --- deterministic verification record ---------------------------------------
class VerificationVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    REPAIR = "REPAIR"
    ESCALATE = "ESCALATE"
    ABSTAIN = "ABSTAIN"
    SUPERVISOR_UNAVAILABLE = "SUPERVISOR_UNAVAILABLE"


@dataclass
class EngineeringVerificationV0:
    verification_id: str
    task_id: str
    attempt_id: str
    verdict: VerificationVerdict
    deterministic_ok: bool
    protected_path_ok: bool
    scope_ok: bool
    policy_ok: bool
    tests_ok: bool
    canonical_repo_untouched: bool
    supervisor_verdict: str | None = None
    supervisor_reasons: list[str] = field(default_factory=list)
    unresolved_requirements: list[str] = field(default_factory=list)
    failure_class: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    verified_at: str | None = None
    #: EW-0B. The execution configuration behind the review, so an audit can
    #: group verdicts by model/prompt/toolset without reconstructing it.
    execution_identity: dict[str, Any] | None = None
    #: EW-0B. The deterministic evidence-sufficiency result, recorded whether
    #: it refused or not -- a check that only leaves a trace when it fires
    #: cannot later be distinguished from a check that never ran.
    evidence_assessment: dict[str, Any] | None = None
    #: EW-0B. The candidate this verdict is about. A verdict that does not name
    #: its candidate can be misread as applying to a later one.
    candidate_sha: str | None = None
    schema_version: str = VERIFICATION_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND

    def __post_init__(self) -> None:
        # A PASS with no evidence references is a certification that cannot
        # exhibit what it certified. Without this the dataclass could simply be
        # constructed with verdict=PASS and status_for_verdict would hand back
        # VERIFIED, which is a route to certification that never touches the
        # durable path at all.
        if self.verdict is VerificationVerdict.PASS and not self.evidence_refs:
            raise ValueError(
                "a PASS must carry evidence_refs binding it to a persisted "
                "review packet; certification without a preimage is not "
                "certification")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        return d


# --- deterministic gate (runs BEFORE the supervisor; fail-closed) ------------
def deterministic_check(task: EngineeringTaskV0, attempt: AttemptEvidence
                        ) -> tuple[bool, bool, bool, bool, list[str], FailureClass | None]:
    """Trusted deterministic checks. Returns
    (protected_ok, scope_ok, policy_ok, tests_ok, unresolved, failure_class)."""
    unresolved: list[str] = []
    # 1) canonical repo must be untouched (work lives in a disposable copy)
    if attempt.canonical_repo_touched:
        return (False, False, False, False, ["canonical repo was modified"],
                FailureClass.POLICY_VIOLATION)
    # 2) protected-path guard: no changed path may be protected
    protected_ok = not any(policy.is_protected(p) for p in attempt.changed_paths)
    # 3) scope guard: every changed path must be within the task's allowed_paths
    def _in_allowed(p: str) -> bool:
        pp = p.replace("\\", "/")
        return any(pp == a or pp.startswith(a.rstrip("/") + "/") or pp.startswith(a)
                   for a in task.allowed_paths)
    scope_ok = bool(task.allowed_paths) and all(_in_allowed(p) for p in attempt.changed_paths)
    if not scope_ok:
        unresolved.append("one or more changed paths are outside allowed_paths")
    # 4) policy: requested tests must be in the task's allowlist
    policy_ok = True
    for t in attempt.tests_run:
        try:
            policy.check_test_allowed(task.allowed_tests, t)
        except policy.PolicyError:
            policy_ok = False
            unresolved.append(f"test not allowlisted: {t}")
    # 5) tests: every REQUESTED test must have actually run and PASSED
    tests_ok = True
    for t in attempt.tests_run:
        res = attempt.test_results.get(t, "")
        if not res.upper().startswith("PASS"):
            tests_ok = False
            unresolved.append(f"test not passing: {t} -> {res or 'no result'}")
    if attempt.py_compile_ok is False:
        tests_ok = False
        unresolved.append("py_compile failed")

    fc: FailureClass | None = None
    if not protected_ok:
        fc = FailureClass.POLICY_VIOLATION
    elif not scope_ok or not policy_ok:
        fc = FailureClass.POLICY_VIOLATION
    elif not tests_ok:
        fc = FailureClass.TEST_FAILURE
    return protected_ok, scope_ok, policy_ok, tests_ok, unresolved, fc


# --- supervisor packet + escalation packet builders --------------------------
def build_supervisor_packet(task: EngineeringTaskV0, attempt: AttemptEvidence,
                            det: dict[str, Any],
                            evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Bounded evidence for the INDEPENDENT GPT supervisor. Contains NO secrets,
    NO connection facts, NO credentials — just task + attempt evidence."""
    return {
        "task": {"task_id": task.task_id, "title": task.title, "goal": task.goal,
                 "risk_class": task.risk_class.value, "executor": attempt.executor.value},
        "requirements": task.requirements,
        "acceptance_criteria": task.acceptance_criteria,
        "verification_steps": task.verification_steps,
        "allowed_paths": task.allowed_paths,
        "changed_files": attempt.changed_paths,
        "diff": attempt.diff_text[:60_000],
        "tests_run": attempt.tests_run,
        "test_results": attempt.test_results,
        "py_compile_ok": attempt.py_compile_ok,
        "deterministic_checks": det,
        "evidence_sufficiency": evidence,
        "worker_claim": attempt.worker_claim,
        "worker_abstained": attempt.abstained,
        "abstain_reason": attempt.abstain_reason,
    }


def build_claude_escalation_packet(task: EngineeringTaskV0, attempts: list[AttemptEvidence],
                                   verification: EngineeringVerificationV0,
                                   reason: str) -> dict[str, Any]:
    """Standalone escalation packet for Claude. Contains enough EVIDENCE (not
    hidden worker reasoning) to repair the task. Claude's output returns to
    independent verification and receives NO automatic success authority."""
    return {
        "escalation_reason": reason,
        "task": task.to_dict(),
        "base_sha": task.base_sha,
        "attempts": [
            {"attempt_id": a.attempt_id, "executor": a.executor.value,
             "changed_files": a.changed_paths, "diff": a.diff_text[:40_000],
             "tests_run": a.tests_run, "test_results": a.test_results,
             "worker_claim": a.worker_claim, "abstained": a.abstained}
            for a in attempts
        ],
        "verification": verification.to_dict(),
        "unresolved_requirements": verification.unresolved_requirements,
        "failure_class": verification.failure_class,
        "note": ("Claude must return a candidate correction that itself passes "
                 "independent verification; Claude does not self-certify."),
    }


# --- durable outcome / learning record (append-only) -------------------------
@dataclass
class OutcomeRecord:
    task_id: str
    title: str
    risk_class: str
    executor: str
    attempt_count: int
    failure_classes: list[str]
    escalated: bool
    supervisor_verdict: str | None
    final_status: str
    tests_run: list[str]
    policy_violation: bool
    human_intervention: bool
    disposition: str
    recorded_at: str
    # --- EW-0B attribution (all additive; every field has a default, so records
    # written before this change still load and still validate) --------------
    mission_id: str | None = None
    candidate_sha: str | None = None
    #: Identity of the execution configuration that produced the supervisor
    #: decision. Present so G1 can later group outcomes by configuration; the
    #: full identity is on the verification record, this is the join key.
    execution_id: str | None = None
    #: Safe (screened) execution identity attributes. No secrets: it is built
    #: by execution_identity.build_execution_identity, which validates every
    #: field and writes UNAVAILABLE_AT_RECORD_TIME rather than guessing.
    execution_identity: dict[str, Any] | None = None
    #: One entry per attempt: attempt_id, executor, candidate_sha, verdict,
    #: failure_class, escalated_from_attempt_id. This is what makes the record
    #: machine-readable enough to answer "what failed, what was repaired, what
    #: did the supervisor decide" without parsing prose.
    attempt_lineage: list[dict[str, Any]] = field(default_factory=list)
    schema_version: str = OUTCOME_SCHEMA_VERSION
    schema_kind: str = SCHEMA_KIND


def append_outcome(path: str, rec: OutcomeRecord) -> None:
    """Append one JSONL outcome record. Best-effort append-only institutional
    learning; never contains secrets/diffs (metadata only)."""
    line = json.dumps(asdict(rec), ensure_ascii=True, sort_keys=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_outcomes(path: str) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out


# --- the deterministic certification orchestrator ----------------------------
def _decision_from_record(record: Any) -> SupervisorDecision:
    """Rebuild a SupervisorDecision from a durably recorded verdict.

    Used only on the recovery path, and only after the journal has proved the
    verdict is backed by a packet that still verifies."""
    if isinstance(record, SupervisorDecision):
        return record
    data = record if isinstance(record, dict) else {}
    try:
        verdict = SupervisorVerdict(data.get("verdict"))
    except ValueError:
        verdict = SupervisorVerdict.SUPERVISOR_UNAVAILABLE
    return SupervisorDecision(
        verdict=verdict, reasons=list(data.get("reasons") or []),
        unresolved_requirements=list(data.get("unresolved_requirements") or []),
        model=data.get("model"), decided_at=data.get("decided_at"),
        error=data.get("error"))


def certify_attempt(task: EngineeringTaskV0, attempt: AttemptEvidence,
                    supervisor: Callable[[dict[str, Any]], SupervisorDecision],
                    now_fn: Callable[[], str], verification_id: str,
                    *, certification: "ReviewContext",
                    candidate: Any = None) -> EngineeringVerificationV0:
    """Authoritative, deterministic certification of ONE attempt.

    Order of authority (all fail-closed):
      1. worker abstention -> ABSTAIN (never success).
      2. deterministic gate (canonical-untouched, protected-path, scope, policy,
         tests). If it fails -> FAIL/POLICY_VIOLATION and the supervisor is NOT
         consulted (a broken/hostile attempt cannot be certified regardless).
      3. INDEPENDENT GPT supervisor verdict. VERIFIED only if the supervisor
         returns PASS. REPAIR/ESCALATE/ABSTAIN map through; SUPERVISOR_UNAVAILABLE
         leaves the work UNVERIFIED.
    The worker's own 'IMPLEMENTATION_COMPLETE' claim is never sufficient."""
    base = dict(verification_id=verification_id, task_id=task.task_id,
                attempt_id=attempt.attempt_id, verified_at=now_fn(),
                canonical_repo_untouched=not attempt.canonical_repo_touched)

    if attempt.abstained:
        return EngineeringVerificationV0(
            verdict=VerificationVerdict.ABSTAIN, deterministic_ok=True,
            protected_path_ok=True, scope_ok=True, policy_ok=True, tests_ok=True,
            failure_class=FailureClass.AMBIGUOUS_REQUIREMENT.value,
            unresolved_requirements=[attempt.abstain_reason or "worker abstained"],
            **base)

    assessment = assess_evidence(task, attempt)
    prot, scope, pol, tests, unresolved, fc = deterministic_check(task, attempt)
    det_ok = prot and scope and pol and tests and not attempt.canonical_repo_touched
    det_summary = {"protected_path_ok": prot, "scope_ok": scope, "policy_ok": pol,
                   "tests_ok": tests, "canonical_repo_untouched": not attempt.canonical_repo_touched}

    if not det_ok:
        # Deterministic failure: the supervisor is NOT consulted. A protected-path
        # or scope/policy breach is a POLICY_VIOLATION (no auto-retry); a plain
        # test failure is REPAIR.
        verdict = (VerificationVerdict.FAIL if fc is FailureClass.POLICY_VIOLATION
                   else VerificationVerdict.REPAIR)
        return EngineeringVerificationV0(
            verdict=verdict, deterministic_ok=False, protected_path_ok=prot,
            scope_ok=scope, policy_ok=pol, tests_ok=tests,
            failure_class=(fc.value if fc else FailureClass.VERIFICATION_FAILURE.value),
            unresolved_requirements=unresolved,
            evidence_assessment=assessment.to_dict(), **base)

    if not assessment.sufficient:
        # The attempt cleared the deterministic gate, but only because the gate
        # quantifies over sets the attempt left empty. The supervisor is NOT
        # consulted: asking a model to notice an omission is strictly weaker
        # than refusing the omission here, and a reviewer call spends real money
        # on a question whose answer is already determined.
        return EngineeringVerificationV0(
            verdict=VerificationVerdict.REPAIR, deterministic_ok=True,
            protected_path_ok=prot, scope_ok=scope, policy_ok=pol, tests_ok=tests,
            failure_class=FailureClass.EVIDENCE_INSUFFICIENT.value,
            unresolved_requirements=list(assessment.details),
            evidence_assessment=assessment.to_dict(), **base)

    # Deterministic gate passed -> consult the INDEPENDENT supervisor, through
    # the durable certification path. The reviewer is reached ONLY via
    # dispatch_durably, so a verdict cannot exist without persisted bytes, a
    # write-ahead journal record and terminal HEAD evidence.
    packet = build_supervisor_packet(task, attempt, det_summary,
                                     assessment.to_dict())
    # The binding may be supplied per-call or carried on the context; either
    # way it is REQUIRED, and dispatch_durably refuses without one.
    # EW-0B: an attempt may carry its OWN binding. A repair that really
    # changes code commits a new candidate, and the binding taken for the
    # previous attempt describes a tree that is no longer checked out.
    candidate = (candidate if candidate is not None
                 else getattr(attempt, "candidate_binding", None))
    if candidate is None:
        candidate = certification.candidate_binding
    outcome = dispatch_durably(
        packet, supervisor, context=certification,
        candidate_sha=(candidate.head_at_binding if candidate is not None else ""),
        attempt_id=attempt.attempt_id, task_id=task.task_id,
        acceptance_criteria=task.acceptance_criteria,
        candidate_binding=candidate)

    if not outcome.dispatched:
        if outcome.refusal == "VERDICT_ALREADY_RECORDED":
            # A restart found this exact review already answered. Reuse it;
            # asking again would be a reroll, not a recovery.
            decision = _decision_from_record(outcome.decision)
        else:
            # Every other refusal leaves the work UNVERIFIED. This is terminal
            # here rather than policy-conditional: routing it through a
            # config-gated branch would put a boolean between fail-closed and a
            # second reviewer call.
            return EngineeringVerificationV0(
                verdict=VerificationVerdict.SUPERVISOR_UNAVAILABLE,
                deterministic_ok=True, protected_path_ok=prot, scope_ok=scope,
                policy_ok=pol, tests_ok=tests,
                supervisor_verdict=SupervisorVerdict.SUPERVISOR_UNAVAILABLE.value,
                supervisor_reasons=[outcome.refusal or "certification refused",
                                    outcome.detail],
                failure_class=FailureClass.VERIFICATION_FAILURE.value,
                evidence_refs=outcome.evidence_refs,
                execution_identity=outcome.execution_identity,
                evidence_assessment=assessment.to_dict(),
                candidate_sha=_candidate_sha(candidate), **base)
    else:
        decision = outcome.decision
    sup = decision.verdict

    if sup is SupervisorVerdict.PASS:
        v = VerificationVerdict.PASS
        fclass = None
    elif sup is SupervisorVerdict.REPAIR:
        v, fclass = VerificationVerdict.REPAIR, FailureClass.VERIFICATION_FAILURE.value
    elif sup is SupervisorVerdict.ESCALATE:
        v, fclass = VerificationVerdict.ESCALATE, FailureClass.ARCHITECTURE_ESCALATION.value
    elif sup is SupervisorVerdict.ABSTAIN:
        v, fclass = VerificationVerdict.ABSTAIN, FailureClass.AMBIGUOUS_REQUIREMENT.value
    else:  # SUPERVISOR_UNAVAILABLE — never a pass
        v, fclass = VerificationVerdict.SUPERVISOR_UNAVAILABLE, FailureClass.VERIFICATION_FAILURE.value

    return EngineeringVerificationV0(
        verdict=v, deterministic_ok=True, protected_path_ok=prot, scope_ok=scope,
        policy_ok=pol, tests_ok=tests,
        supervisor_verdict=sup.value, supervisor_reasons=decision.reasons,
        unresolved_requirements=decision.unresolved_requirements,
        failure_class=fclass, evidence_refs=outcome.evidence_refs,
        execution_identity=outcome.execution_identity,
        evidence_assessment=assessment.to_dict(),
        candidate_sha=_candidate_sha(candidate), **base)


def status_for_verdict(v: VerificationVerdict) -> TaskStatus:
    """Map a verification verdict to the authoritative terminal/next task status."""
    return {
        VerificationVerdict.PASS: TaskStatus.VERIFIED,
        VerificationVerdict.REPAIR: TaskStatus.REPAIR_REQUIRED,
        VerificationVerdict.ESCALATE: TaskStatus.ESCALATION_REQUIRED,
        VerificationVerdict.ABSTAIN: TaskStatus.ABSTAINED,
        VerificationVerdict.FAIL: TaskStatus.FAILED_VALIDATION,
        VerificationVerdict.SUPERVISOR_UNAVAILABLE: TaskStatus.VERIFYING,  # stays unverified
    }[v]
