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


def test_A1_5_main_write_denied(legacy_ctx, stationary_binding):
    with pytest.raises(AuthorityError):
        assert_operation_allowed(Lvl.A1_ASSISTED_ENGINEERING, "MAIN_WRITE")
    # and a candidate that touches the canonical repo fails deterministic certification
    task = EngineeringTaskV0(task_id="t", title="x", goal="x", risk_class=RiskClass.E1_ROUTINE,
                             executor=Executor.ENGINEER, allowed_paths=["tests/"])
    att = AttemptEvidence(attempt_id="a", executor=Executor.ENGINEER, worker_claim="done",
                          changed_paths=["tests/x.py"], canonical_repo_touched=True)
    v = certify_attempt(task, att, lambda p: (_ for _ in ()).throw(AssertionError("no supervisor")),
                        lambda: "t", "v",
                        certification=legacy_ctx, candidate=stationary_binding)
    assert v.verdict is VerificationVerdict.FAIL and v.failure_class == FailureClass.POLICY_VIOLATION.value


@pytest.mark.parametrize("op", ["PRODUCTION_WRITE", "OPT_STOCKBOT_WRITE", "DEPLOY", "SERVICE_RESTART",
                                "BROKER_ACTION", "CAPITAL_DECISION"])
def test_A1_6_production_and_capital_ops_denied(op):
    with pytest.raises(AuthorityError):
        assert_operation_allowed(Lvl.A1_ASSISTED_ENGINEERING, op)


def test_A1_7_protected_path_write_denied(legacy_ctx, stationary_binding):
    assert policy.is_protected("decision_engine.py")
    assert policy.is_protected("portfolio_automation/scoring/rank.py")
    assert policy.is_protected("config/ew0a_authority.json")
    task = EngineeringTaskV0(task_id="t", title="x", goal="x", risk_class=RiskClass.E1_ROUTINE,
                             executor=Executor.ENGINEER, allowed_paths=["."])
    att = AttemptEvidence(attempt_id="a", executor=Executor.ENGINEER, worker_claim="done",
                          changed_paths=["decision_engine.py"], canonical_repo_touched=False)
    v = certify_attempt(task, att, lambda p: (_ for _ in ()).throw(AssertionError("no supervisor")),
                        lambda: "t", "v",
                        certification=legacy_ctx, candidate=stationary_binding)
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


# ═══════════════════════════════════════════════════════════════════════════
# GUI-SR — BLOCKER A: read_authority_level must be TOTAL
#
# The fail-closed intent was already here, but a syntactically valid non-object
# JSON root reached data["level"] and raised TypeError, which the guard did not
# name. The reader threw instead of failing closed, and every caller downstream
# of it -- including the whole WCC dashboard -- died with it.
# ═══════════════════════════════════════════════════════════════════════════
import json as _gsr_json
from pathlib import Path as _GsrPath

from portfolio_automation.engineer_worker.ew0a_authority import (
    read_authority_level as _gsr_read_level, EngineerAuthorityLevel as _GsrLvl)

_GSR_MARKER = "sk-GUI-SR-MUST-NOT-RENDER-999"


def _gsr_authority_root(tmp_path, body=None, raw=None):
    target = tmp_path / "config" / "ew0a_authority.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        target.write_bytes(raw)
    else:
        target.write_text(body, encoding="utf-8")
    return tmp_path


def test_gsr_authority_reader_is_total_over_json_root_shapes(tmp_path):
    """Every shape fails CLOSED to A0 instead of raising."""
    for label, body in (("null", "null"), ("list", "[1,2]"), ("int", "123"),
                        ("float", "1.5"), ("string", '"text"'), ("bool", "true"),
                        ("empty object", "{}"),
                        ("no level", '{"grants": []}'),
                        ("invalid level", '{"level": "NOT_A_LEVEL"}'),
                        ("null level", '{"level": null}'),
                        ("dict level", _gsr_json.dumps({"level": {"api_key": _GSR_MARKER}})),
                        ("list level", _gsr_json.dumps({"level": [_GSR_MARKER]})),
                        ("invalid json", "{oops")):
        root = _gsr_authority_root(tmp_path / f"a_{label.replace(' ', '_')}", body)
        assert _gsr_read_level(root) is _GsrLvl.A0_DIAGNOSTIC, label


def test_gsr_authority_reader_handles_io_and_decode_failures(tmp_path):
    undecodable = _gsr_authority_root(tmp_path / "utf8", raw=b"\xff\xfebad")
    assert _gsr_read_level(undecodable) is _GsrLvl.A0_DIAGNOSTIC
    # absent file
    assert _gsr_read_level(tmp_path / "nothing_here") is _GsrLvl.A0_DIAGNOSTIC
    # a directory in place of the file -> IsADirectoryError (an OSError)
    as_dir = tmp_path / "dir"
    (as_dir / "config" / "ew0a_authority.json").mkdir(parents=True)
    assert _gsr_read_level(as_dir) is _GsrLvl.A0_DIAGNOSTIC


def test_gsr_authority_reader_still_reads_both_valid_levels(tmp_path):
    for value, expected in (("A0_DIAGNOSTIC", _GsrLvl.A0_DIAGNOSTIC),
                            ("A1_ASSISTED_ENGINEERING", _GsrLvl.A1_ASSISTED_ENGINEERING)):
        root = _gsr_authority_root(tmp_path / f"ok_{value}",
                                   _gsr_json.dumps({"level": value}))
        assert _gsr_read_level(root) is expected


def test_gsr_authority_semantics_are_untouched():
    """Reader hardening only: no level, grant, denial or gate semantics moved."""
    assert {lvl.value for lvl in _GsrLvl} == {
        "A0_DIAGNOSTIC", "A1_ASSISTED_ENGINEERING"}
    assert FORBIDDEN_OPS == frozenset({
        "MAIN_WRITE", "MERGE", "AUTONOMOUS_PUSH", "PRODUCTION_WRITE",
        "OPT_STOCKBOT_WRITE", "DEPLOY", "SERVICE_RESTART", "CREDENTIAL_ACCESS",
        "SECURITY_POLICY_SELF_MOD", "PROTECTED_SCORING_MODIFICATION",
        "BROKER_ACTION", "CAPITAL_DECISION", "E3_SELF_ASSIGN", "E4_SELF_ASSIGN",
        "SELF_PROMOTION"})


def test_gsr_a_marker_never_reaches_the_reader_result(tmp_path):
    """The reader returns an enum, so there is no channel for payload at all --
    pinned so a future 'helpful' error message cannot open one."""
    root = _gsr_authority_root(tmp_path / "marker",
                               _gsr_json.dumps({"level": {"api_key": _GSR_MARKER}}))
    result = _gsr_read_level(root)
    assert _GSR_MARKER not in repr(result)
    assert result is _GsrLvl.A0_DIAGNOSTIC
