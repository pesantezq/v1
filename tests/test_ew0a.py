"""Hermetic tests for the EW-0A certification machinery.

Proves the anti-self-certification invariant: an attempt is VERIFIED only when the
deterministic gate passes AND the independent supervisor returns PASS. A worker's
own 'IMPLEMENTATION_COMPLETE' claim, a protected-path breach, a scope/policy breach,
a failed test, or an unavailable supervisor can never yield VERIFIED.
"""
from __future__ import annotations

import pytest

from portfolio_automation.engineer_worker import ew0a
from portfolio_automation.engineer_worker.ew0a import (
    RiskClass, Executor, TaskStatus, FailureClass, NextAction, VerificationVerdict,
    EngineeringTaskV0, AttemptEvidence, certify_attempt, deterministic_check,
    assign_executor, worker_may_execute, default_executor, action_for_failure,
    status_for_verdict, EW0AError, OutcomeRecord, append_outcome, read_outcomes)
from portfolio_automation.engineer_worker.gpt_supervisor import (
    SupervisorDecision, SupervisorVerdict)


def _clock():
    n = {"i": 0}
    def now():
        n["i"] += 1
        return f"2026-08-11T00:00:{n['i']:02d}Z"
    return now


def _sup(verdict, reasons=None, unresolved=None):
    d = SupervisorDecision(verdict, reasons=reasons or [], unresolved_requirements=unresolved or [])
    called = {"n": 0}
    def fn(packet):
        called["n"] += 1
        fn.last_packet = packet
        return d
    fn.called = called
    return fn


def _task(**over):
    d = dict(task_id="t1", title="add docstring", goal="doc", risk_class=RiskClass.E1_ROUTINE,
             executor=Executor.ENGINEER, allowed_paths=["docs/", "tests/"],
             allowed_tests=["tests/test_x.py"], acceptance_criteria=["docstring present"],
             requirements=["no behavior change"])
    d.update(over)
    return EngineeringTaskV0(**d)


def _attempt(**over):
    d = dict(attempt_id="a1", executor=Executor.ENGINEER, worker_claim="IMPLEMENTATION_COMPLETE",
             changed_paths=["docs/foo.md"], diff_text="+text", tests_run=["tests/test_x.py"],
             test_results={"tests/test_x.py": "PASS (2 passed)"}, py_compile_ok=True,
             canonical_repo_touched=False)
    d.update(over)
    return AttemptEvidence(**d)


# --- risk / executor ---------------------------------------------------------
def test_default_executor_mapping():
    assert default_executor(RiskClass.E1_ROUTINE) is Executor.ENGINEER
    assert default_executor(RiskClass.E2_MODERATE) is Executor.ENGINEER_STRICT
    assert default_executor(RiskClass.E3_HIGH) is Executor.CLAUDE
    assert default_executor(RiskClass.E4_CONSEQUENTIAL) is Executor.CLAUDE_HUMAN


def test_worker_cannot_execute_high_risk():
    assert worker_may_execute(RiskClass.E1_ROUTINE) and worker_may_execute(RiskClass.E2_MODERATE)
    assert not worker_may_execute(RiskClass.E3_HIGH)
    assert not worker_may_execute(RiskClass.E4_CONSEQUENTIAL)


@pytest.mark.parametrize("risk", [RiskClass.E3_HIGH, RiskClass.E4_CONSEQUENTIAL])
def test_engineer_refused_on_high_risk(risk):
    with pytest.raises(EW0AError):
        assign_executor(risk, Executor.ENGINEER)
    # default routing goes to Claude(/human) instead
    assert assign_executor(risk) in (Executor.CLAUDE, Executor.CLAUDE_HUMAN)


def test_failure_action_mapping():
    assert action_for_failure(FailureClass.POLICY_VIOLATION) is NextAction.STOP_NO_RETRY
    assert action_for_failure(FailureClass.AMBIGUOUS_REQUIREMENT) is NextAction.ABSTAIN
    assert action_for_failure(FailureClass.ARCHITECTURE_ESCALATION) is NextAction.ESCALATE_CLAUDE
    assert action_for_failure(FailureClass.SECURITY_ESCALATION) is NextAction.ESCALATE_HUMAN
    assert action_for_failure(FailureClass.INTERRUPTED) is NextAction.REMAIN_UNVERIFIED


# --- deterministic gate ------------------------------------------------------
def test_deterministic_pass():
    prot, scope, pol, tests, unresolved, fc = deterministic_check(_task(), _attempt())
    assert prot and scope and pol and tests and not unresolved and fc is None


def test_deterministic_protected_path_fails():
    a = _attempt(changed_paths=["portfolio_automation/scoring/engine.py"])
    prot, scope, pol, tests, unresolved, fc = deterministic_check(_task(allowed_paths=["portfolio_automation/"]), a)
    assert not prot and fc is FailureClass.POLICY_VIOLATION


def test_deterministic_scope_fails():
    a = _attempt(changed_paths=["src/secret.py"])
    prot, scope, pol, tests, unresolved, fc = deterministic_check(_task(), a)
    assert not scope and fc is FailureClass.POLICY_VIOLATION


def test_deterministic_test_failure():
    a = _attempt(test_results={"tests/test_x.py": "FAIL (1 failed)"})
    prot, scope, pol, tests, unresolved, fc = deterministic_check(_task(), a)
    assert prot and scope and not tests and fc is FailureClass.TEST_FAILURE


# --- certify_attempt: the core invariant -------------------------------------
def test_verified_requires_det_ok_AND_supervisor_pass():
    sup = _sup(SupervisorVerdict.PASS, reasons=["all criteria met"])
    v = certify_attempt(_task(), _attempt(), sup, _clock(), "v1")
    assert v.verdict is VerificationVerdict.PASS
    assert status_for_verdict(v.verdict) is TaskStatus.VERIFIED
    assert sup.called["n"] == 1


def test_protected_path_breach_never_consults_supervisor():
    sup = _sup(SupervisorVerdict.PASS)   # even if GPT would PASS...
    a = _attempt(changed_paths=["decision_engine.py"])
    v = certify_attempt(_task(allowed_paths=["."]), a, sup, _clock(), "v1")
    assert v.verdict is VerificationVerdict.FAIL           # ...deterministic breach wins
    assert v.failure_class == FailureClass.POLICY_VIOLATION.value
    assert sup.called["n"] == 0                            # supervisor NOT consulted
    assert status_for_verdict(v.verdict) is TaskStatus.FAILED_VALIDATION


def test_worker_claim_alone_does_not_certify():
    # deterministic ok but a required test failed -> REPAIR regardless of the loud claim
    sup = _sup(SupervisorVerdict.PASS)
    a = _attempt(worker_claim="DONE! SHIP IT!", test_results={"tests/test_x.py": "FAIL"})
    v = certify_attempt(_task(), a, sup, _clock(), "v1")
    assert v.verdict is VerificationVerdict.REPAIR and sup.called["n"] == 0


def test_canonical_repo_touched_is_policy_violation():
    sup = _sup(SupervisorVerdict.PASS)
    a = _attempt(canonical_repo_touched=True)
    v = certify_attempt(_task(), a, sup, _clock(), "v1")
    assert v.verdict is VerificationVerdict.FAIL and sup.called["n"] == 0
    assert not v.canonical_repo_untouched


def test_supervisor_repair_maps_through():
    sup = _sup(SupervisorVerdict.REPAIR, unresolved=["criterion X unmet"])
    v = certify_attempt(_task(), _attempt(), sup, _clock(), "v1")
    assert v.verdict is VerificationVerdict.REPAIR
    assert status_for_verdict(v.verdict) is TaskStatus.REPAIR_REQUIRED


def test_supervisor_escalate_maps_through():
    v = certify_attempt(_task(), _attempt(), _sup(SupervisorVerdict.ESCALATE), _clock(), "v1")
    assert v.verdict is VerificationVerdict.ESCALATE
    assert status_for_verdict(v.verdict) is TaskStatus.ESCALATION_REQUIRED


def test_supervisor_unavailable_is_not_a_pass():
    v = certify_attempt(_task(), _attempt(), _sup(SupervisorVerdict.SUPERVISOR_UNAVAILABLE), _clock(), "v1")
    assert v.verdict is VerificationVerdict.SUPERVISOR_UNAVAILABLE
    assert status_for_verdict(v.verdict) is not TaskStatus.VERIFIED


def test_worker_abstention():
    a = _attempt(abstained=True, abstain_reason="requirements ambiguous")
    v = certify_attempt(_task(), a, _sup(SupervisorVerdict.PASS), _clock(), "v1")
    assert v.verdict is VerificationVerdict.ABSTAIN
    assert status_for_verdict(v.verdict) is TaskStatus.ABSTAINED


def test_supervisor_packet_has_no_secrets_by_construction():
    sup = _sup(SupervisorVerdict.PASS)
    certify_attempt(_task(), _attempt(), sup, _clock(), "v1")
    import json
    blob = json.dumps(sup.last_packet)
    for leak in ("Authorization", "Bearer", "sk-", "/.ssh/", "stockbot_engineer"):
        assert leak not in blob


# --- outcome record ----------------------------------------------------------
def test_outcome_append_read(tmp_path):
    p = str(tmp_path / "outcomes.jsonl")
    rec = OutcomeRecord(task_id="t1", title="x", risk_class="E1_ROUTINE", executor="ENGINEER",
                        attempt_count=1, failure_classes=[], escalated=False,
                        supervisor_verdict="PASS", final_status="VERIFIED",
                        tests_run=["tests/test_x.py"], policy_violation=False,
                        human_intervention=False, disposition="ok", recorded_at="2026-08-11T00:00:00Z")
    append_outcome(p, rec)
    append_outcome(p, rec)
    rows = read_outcomes(p)
    assert len(rows) == 2 and rows[0]["final_status"] == "VERIFIED"
