"""EW-0A A1 authority-model tests (the eight A1 activation authority checks + more).

Proves the bounded A1 promotion: E1/E2 admitted to the Engineer only at A1; E3/E4
always routed to Claude(/human); main/production/protected-path writes and worker
self-promotion denied at every level; the authority state is not worker-writable.
"""
from __future__ import annotations

import pytest

from portfolio_automation.engineer_worker import policy
from portfolio_automation.engineer_worker.ew0a import (
    RiskClass, Executor, TaskStatus, VerificationVerdict, FailureClass,
    EngineeringTaskV0, AttemptEvidence, certify_attempt)
from portfolio_automation.engineer_worker.ew0a_authority import (
    EngineerAuthorityLevel as Lvl, AuthorityError, admit_engineer_task,
    assert_operation_allowed, read_authority_level, set_authority_level,
    FORBIDDEN_OPS, DEFAULT_STATE_REL)


def _clock():
    return "2026-08-11T12:00:00Z"


# --- authority state persistence + fail-closed default -----------------------
def test_default_is_a0_when_absent(tmp_path):
    assert read_authority_level(tmp_path) is Lvl.A0_DIAGNOSTIC


def test_set_and_read_roundtrip(tmp_path):
    set_authority_level(tmp_path, Lvl.A1_ASSISTED_ENGINEERING, actor="operator", now=_clock())
    assert read_authority_level(tmp_path) is Lvl.A1_ASSISTED_ENGINEERING
    set_authority_level(tmp_path, Lvl.A0_DIAGNOSTIC, actor="operator", now=_clock())
    assert read_authority_level(tmp_path) is Lvl.A0_DIAGNOSTIC


def test_malformed_state_defaults_a0(tmp_path):
    p = tmp_path / DEFAULT_STATE_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert read_authority_level(tmp_path) is Lvl.A0_DIAGNOSTIC


# --- A0 is diagnostic-only ---------------------------------------------------
def test_a0_engineer_has_no_source_edit_authority():
    with pytest.raises(AuthorityError):
        admit_engineer_task(Lvl.A0_DIAGNOSTIC, RiskClass.E1_ROUTINE)


# === the eight A1 activation authority tests =================================
def test_A1_1_e1_engineer_admitted():
    assert admit_engineer_task(Lvl.A1_ASSISTED_ENGINEERING, RiskClass.E1_ROUTINE) is Executor.ENGINEER


def test_A1_2_e2_engineer_admitted_stricter():
    assert admit_engineer_task(Lvl.A1_ASSISTED_ENGINEERING, RiskClass.E2_MODERATE) is Executor.ENGINEER_STRICT


def test_A1_3_e3_denied_routes_to_claude():
    with pytest.raises(AuthorityError):
        admit_engineer_task(Lvl.A1_ASSISTED_ENGINEERING, RiskClass.E3_HIGH)


def test_A1_4_e4_denied_routes_to_claude_human():
    with pytest.raises(AuthorityError):
        admit_engineer_task(Lvl.A1_ASSISTED_ENGINEERING, RiskClass.E4_CONSEQUENTIAL)


def test_A1_5_main_write_denied():
    with pytest.raises(AuthorityError):
        assert_operation_allowed(Lvl.A1_ASSISTED_ENGINEERING, "MAIN_WRITE")
    # and a candidate that touches the canonical repo fails deterministic certification
    task = EngineeringTaskV0(task_id="t", title="x", goal="x", risk_class=RiskClass.E1_ROUTINE,
                             executor=Executor.ENGINEER, allowed_paths=["tests/"])
    att = AttemptEvidence(attempt_id="a", executor=Executor.ENGINEER, worker_claim="done",
                          changed_paths=["tests/x.py"], canonical_repo_touched=True)
    v = certify_attempt(task, att, lambda p: (_ for _ in ()).throw(AssertionError("no supervisor")),
                        lambda: "t", "v")
    assert v.verdict is VerificationVerdict.FAIL and v.failure_class == FailureClass.POLICY_VIOLATION.value


@pytest.mark.parametrize("op", ["PRODUCTION_WRITE", "OPT_STOCKBOT_WRITE", "DEPLOY", "SERVICE_RESTART",
                                "BROKER_ACTION", "CAPITAL_DECISION"])
def test_A1_6_production_and_capital_ops_denied(op):
    with pytest.raises(AuthorityError):
        assert_operation_allowed(Lvl.A1_ASSISTED_ENGINEERING, op)


def test_A1_7_protected_path_write_denied():
    assert policy.is_protected("decision_engine.py")
    assert policy.is_protected("portfolio_automation/scoring/rank.py")
    assert policy.is_protected("config/ew0a_authority.json")
    task = EngineeringTaskV0(task_id="t", title="x", goal="x", risk_class=RiskClass.E1_ROUTINE,
                             executor=Executor.ENGINEER, allowed_paths=["."])
    att = AttemptEvidence(attempt_id="a", executor=Executor.ENGINEER, worker_claim="done",
                          changed_paths=["decision_engine.py"], canonical_repo_touched=False)
    v = certify_attempt(task, att, lambda p: (_ for _ in ()).throw(AssertionError("no supervisor")),
                        lambda: "t", "v")
    assert v.verdict is VerificationVerdict.FAIL and not v.protected_path_ok


def test_A1_8_worker_self_promotion_denied():
    with pytest.raises(AuthorityError):
        assert_operation_allowed(Lvl.A1_ASSISTED_ENGINEERING, "SELF_PROMOTION")
    with pytest.raises(AuthorityError):
        assert_operation_allowed(Lvl.A1_ASSISTED_ENGINEERING, "E3_SELF_ASSIGN")
    # the authority state file is a protected path AND outside the worker's repair scope
    assert policy.is_protected("config/ew0a_authority.json")
    assert not policy.is_repair_allowed("config/ew0a_authority.json")


def test_all_forbidden_ops_denied_even_at_a1():
    for op in FORBIDDEN_OPS:
        with pytest.raises(AuthorityError):
            assert_operation_allowed(Lvl.A1_ASSISTED_ENGINEERING, op)
