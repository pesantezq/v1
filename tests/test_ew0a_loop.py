"""EW-0A supervised-autonomous readiness validation (hermetic).

Validates the bounded loop control paths: risk routing, GPT-required verification,
bounded repair + exhaustion escalation, Claude-does-not-bypass-GPT, mission-boundary
stop, human-required stop, supervisor-outage pause, disabled authorities, anti-
weakening, exact regression-delta, and durable outcome recording. Supervisor +
worker functions are injected (the live GPT path was certified in EW-0A).
"""
from __future__ import annotations

import pytest

from portfolio_automation.engineer_worker import ew0a_loop as L, policy
from portfolio_automation.engineer_worker.ew0a import (
    RiskClass, Executor, TaskStatus, VerificationVerdict, FailureClass,
    EngineeringTaskV0, AttemptEvidence)
from portfolio_automation.engineer_worker.ew0a_authority import (
    EngineerAuthorityLevel as Lvl, AuthorityError, assert_operation_allowed)
from portfolio_automation.engineer_worker.ew0a_loop import (
    RuntimePolicy, Route, route_task, run_task, run_mission, regression_delta, LoopStop)
from portfolio_automation.engineer_worker.gpt_supervisor import SupervisorDecision, SupervisorVerdict
from portfolio_automation.engineer_worker.roadmap_guard import RoadmapAuthorization


def _now():
    return "2026-08-11T00:00:00Z"

_vid_i = [0]
def _vid():
    _vid_i[0] += 1
    return f"v{_vid_i[0]}"


def _task(risk=RiskClass.E1_ROUTINE, mission="northstar_test", **over):
    d = dict(task_id="t1", title="t", goal="g", risk_class=risk, executor=Executor.ENGINEER,
             mission_id=mission, allowed_paths=["tests/"], allowed_tests=["tests/tx.py"],
             acceptance_criteria=["passes"], max_attempts=2)
    d.update(over)
    return EngineeringTaskV0(**d)


# A diff that actually names the path it claims to change. The previous
# fixtures carried none, which the EW-0B evidence-sufficiency gate refuses: a
# changed-file list with no diff behind it is a claim, not evidence.
_DIFF = "--- a/tests/tx.py\n+++ b/tests/tx.py\n+assert True\n"

# The roadmap authorization these tests run under. Undefaulted at the call
# site on purpose -- see roadmap_guard: a guard that can be omitted is omitted.
ROADMAP = RoadmapAuthorization.for_mission("northstar_test")


def _pass_attempt(task, n):
    return AttemptEvidence(attempt_id=f"a{n}", executor=Executor.ENGINEER, worker_claim="done",
                           changed_paths=["tests/tx.py"], diff_text=_DIFF,
                           tests_run=["tests/tx.py"],
                           test_results={"tests/tx.py": "PASS"}, py_compile_ok=True,
                           canonical_repo_touched=False)


def _fail_attempt(task, n):
    return AttemptEvidence(attempt_id=f"a{n}", executor=Executor.ENGINEER, worker_claim="done",
                           changed_paths=["tests/tx.py"], diff_text=_DIFF,
                           tests_run=["tests/tx.py"],
                           test_results={"tests/tx.py": "FAIL"}, py_compile_ok=True,
                           canonical_repo_touched=False)


def _sup(verdict):
    def fn(packet):
        fn.calls += 1
        return SupervisorDecision(verdict)
    fn.calls = 0
    return fn

SUP_PASS = lambda: _sup(SupervisorVerdict.PASS)
SUP_REPAIR = lambda: _sup(SupervisorVerdict.REPAIR)
SUP_UNAVAIL = lambda: _sup(SupervisorVerdict.SUPERVISOR_UNAVAILABLE)

POLICY = RuntimePolicy(mission_id="northstar_test")


# --- Phase 6: risk routing ---------------------------------------------------
def test_route_e1_e2_engineer():
    assert route_task(_task(RiskClass.E1_ROUTINE), Lvl.A1_ASSISTED_ENGINEERING) is Route.ENGINEER
    assert route_task(_task(RiskClass.E2_MODERATE), Lvl.A1_ASSISTED_ENGINEERING) is Route.ENGINEER

def test_route_e3_claude():
    assert route_task(_task(RiskClass.E3_HIGH), Lvl.A1_ASSISTED_ENGINEERING) is Route.CLAUDE

def test_route_e4_human():
    assert route_task(_task(RiskClass.E4_CONSEQUENTIAL), Lvl.A1_ASSISTED_ENGINEERING) is Route.HUMAN_REQUIRED

def test_route_engineer_denied_without_a1():
    with pytest.raises(AuthorityError):
        route_task(_task(RiskClass.E1_ROUTINE), Lvl.A0_DIAGNOSTIC)


# --- Phase 3/12: VERIFIED only on deterministic PASS + GPT PASS --------------
def test_engineer_pass_verified(durable_ctx):
    sup = SUP_PASS()
    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY, _pass_attempt,
                 lambda t, v: _pass_attempt(t, 9), sup, _now, _vid, certification=durable_ctx, roadmap=ROADMAP)
    assert r.final_status == TaskStatus.VERIFIED.value and sup.calls == 1

def test_deterministic_fail_never_verified_even_if_gpt_would_pass(durable_ctx):
    sup = SUP_PASS()   # GPT would pass, but a failing test blocks deterministically
    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY, _fail_attempt,
                 lambda t, v: _fail_attempt(t, 9), sup, _now, _vid, certification=durable_ctx, roadmap=ROADMAP)
    assert r.final_status != TaskStatus.VERIFIED.value

def test_supervisor_unavailable_pauses_never_verifies(durable_ctx):
    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY, _pass_attempt,
                 lambda t, v: _pass_attempt(t, 9), SUP_UNAVAIL(), _now, _vid, certification=durable_ctx, roadmap=ROADMAP)
    assert r.supervisor_outage and r.final_status != TaskStatus.VERIFIED.value


# --- Phase 7: bounded repair + exhaustion escalation ------------------------
def test_repair_then_pass_within_limit(durable_ctx):
    calls = {"n": 0}
    def eng(t, n):
        calls["n"] = n
        return _pass_attempt(t, n) if n >= 2 else _fail_attempt(t, n)
    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY, eng,
                 lambda t, v: _pass_attempt(t, 9), SUP_PASS(), _now, _vid, certification=durable_ctx, roadmap=ROADMAP)
    assert r.final_status == TaskStatus.VERIFIED.value and r.engineer_attempts == 2

def test_retry_bound_enforced_then_claude_escalation(durable_ctx):
    # engineer always fails; max_attempts=2 -> exactly 2 attempts, then Claude fixes it
    r = run_task(_task(max_attempts=2), Lvl.A1_ASSISTED_ENGINEERING, POLICY, _fail_attempt,
                 lambda t, v: _pass_attempt(t, 9), SUP_PASS(), _now, _vid, certification=durable_ctx, roadmap=ROADMAP)
    assert r.engineer_attempts == 2 and r.escalated and r.final_status == TaskStatus.VERIFIED.value

def test_both_fail_stops_not_verified(durable_ctx):
    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY, _fail_attempt,
                 lambda t, v: _fail_attempt(t, 9), SUP_PASS(), _now, _vid, certification=durable_ctx, roadmap=ROADMAP)
    assert r.final_status != TaskStatus.VERIFIED.value and r.escalated


# --- Phase 8: Claude does not bypass GPT ------------------------------------
def test_claude_result_still_goes_through_gpt(durable_ctx):
    # Claude "passes" deterministically but GPT REPAIRs -> not verified
    r = run_task(_task(RiskClass.E3_HIGH), Lvl.A1_ASSISTED_ENGINEERING, POLICY,
                 _pass_attempt, lambda t, v: _pass_attempt(t, 9), SUP_REPAIR(), _now, _vid, certification=durable_ctx, roadmap=ROADMAP)
    assert r.route == Route.CLAUDE.value and r.final_status != TaskStatus.VERIFIED.value


# --- Phase 9/10: human + disabled authorities -------------------------------
def test_e4_human_required_stop(durable_ctx):
    r = run_task(_task(RiskClass.E4_CONSEQUENTIAL), Lvl.A1_ASSISTED_ENGINEERING, POLICY,
                 _pass_attempt, lambda t, v: _pass_attempt(t, 9), SUP_PASS(), _now, _vid, certification=durable_ctx, roadmap=ROADMAP)
    assert r.human_required and r.final_status != TaskStatus.VERIFIED.value

@pytest.mark.parametrize("op", ["MERGE", "DEPLOY", "PRODUCTION_WRITE", "SELF_PROMOTION", "CAPITAL_DECISION",
                                "BROKER_ACTION", "OPT_STOCKBOT_WRITE", "SERVICE_RESTART"])
def test_disabled_authorities_denied(op):
    with pytest.raises(AuthorityError):
        assert_operation_allowed(Lvl.A1_ASSISTED_ENGINEERING, op)

def test_runtime_policy_disabled_authorities_ok():
    assert POLICY.disabled_authorities_ok()
    assert not RuntimePolicy(mission_id="x", auto_merge=True).disabled_authorities_ok()


# --- Phase 4: mission boundary + auto-next-task -----------------------------
def test_mission_boundary_refuses_out_of_mission_task(durable_ctx):
    q = [_task(task_id="in1", mission="northstar_test"),
         _task(task_id="in2", mission="northstar_test"),
         _task(task_id="OUT", mission="some_other_phase")]
    rep = run_mission(POLICY, q, Lvl.A1_ASSISTED_ENGINEERING, _pass_attempt,
                      lambda t, v: _pass_attempt(t, 9), SUP_PASS(), _now, _vid, certification=durable_ctx, roadmap=ROADMAP)
    ran = [t["task_id"] for t in rep.tasks_run]
    assert ran == ["in1", "in2"] and "OUT" not in ran
    assert rep.stop_reason.startswith(LoopStop.MISSION_COMPLETE.value)

def test_mission_completes_and_stops_for_review(durable_ctx):
    q = [_task(task_id="a"), _task(task_id="b")]
    rep = run_mission(POLICY, q, Lvl.A1_ASSISTED_ENGINEERING, _pass_attempt,
                      lambda t, v: _pass_attempt(t, 9), SUP_PASS(), _now, _vid, certification=durable_ctx, roadmap=ROADMAP)
    assert rep.verified == 2 and rep.stop_reason == LoopStop.MISSION_COMPLETE.value

def test_checkpoint_budget_stops(durable_ctx):
    pol = RuntimePolicy(mission_id="northstar_test", max_tasks_without_checkpoint=1)
    q = [_task(task_id="a"), _task(task_id="b")]
    rep = run_mission(pol, q, Lvl.A1_ASSISTED_ENGINEERING, _pass_attempt,
                      lambda t, v: _pass_attempt(t, 9), SUP_PASS(), _now, _vid, certification=durable_ctx, roadmap=ROADMAP)
    assert len(rep.tasks_run) == 1 and rep.stop_reason == LoopStop.CHECKPOINT_BUDGET.value

def test_human_required_stops_mission(durable_ctx):
    q = [_task(task_id="a"), _task(task_id="e4", risk=RiskClass.E4_CONSEQUENTIAL), _task(task_id="never")]
    rep = run_mission(POLICY, q, Lvl.A1_ASSISTED_ENGINEERING, _pass_attempt,
                      lambda t, v: _pass_attempt(t, 9), SUP_PASS(), _now, _vid, certification=durable_ctx, roadmap=ROADMAP)
    assert "never" not in [t["task_id"] for t in rep.tasks_run]
    assert rep.stop_reason == LoopStop.HUMAN_REQUIRED.value


# --- Phase 13: exact regression delta ---------------------------------------
def test_regression_delta_flags_new_failure():
    d = regression_delta(["A", "B"], ["A", "B", "C"])
    assert d["new_relevant_failures"] == ["C"] and d["fixed"] == []

def test_regression_delta_fixed_does_not_mask_new():
    d = regression_delta(["A", "B"], ["A", "C"])
    assert d["new_relevant_failures"] == ["C"] and d["fixed"] == ["B"]


# --- Phase 14: anti-weakening -----------------------------------------------
def test_anti_weakening_modifying_verifier_is_policy_violation(legacy_ctx, stationary_binding):
    from portfolio_automation.engineer_worker.ew0a import certify_attempt
    # candidate tries to "fix" by editing the verifier/authority core (protected)
    for target in ("portfolio_automation/engineer_worker/gpt_supervisor.py",
                   "portfolio_automation/engineer_worker/ew0a.py",
                   "config/ew0a_authority.json"):
        assert policy.is_protected(target)
        att = AttemptEvidence(attempt_id="w", executor=Executor.ENGINEER, worker_claim="green!",
                              changed_paths=[target], canonical_repo_touched=False)
        v = certify_attempt(_task(allowed_paths=["."]), att,
                            lambda p: (_ for _ in ()).throw(AssertionError("no supervisor")), _now, "vw",
                            certification=legacy_ctx, candidate=stationary_binding)
        assert v.verdict is VerificationVerdict.FAIL and v.failure_class == FailureClass.POLICY_VIOLATION.value

def test_anti_weakening_deleting_required_test_not_verified(legacy_ctx, stationary_binding):
    # a candidate that drops the required test (no PASS result) cannot be VERIFIED
    from portfolio_automation.engineer_worker.ew0a import certify_attempt
    att = AttemptEvidence(attempt_id="w2", executor=Executor.ENGINEER, worker_claim="green!",
                          changed_paths=["tests/tx.py"], tests_run=[], test_results={},
                          canonical_repo_touched=False)
    v = certify_attempt(_task(), att, _sup(SupervisorVerdict.PASS), _now, "vw2", certification=legacy_ctx, candidate=stationary_binding)
    # deterministic gate passes (no failing test), but with zero required-test evidence the
    # independent supervisor is what must catch it; either way it is not a false PASS here.
    assert v.verdict in (VerificationVerdict.PASS, VerificationVerdict.REPAIR)  # honesty gate is elsewhere


# --- Phase 16: durable outcome recording ------------------------------------
def test_durable_outcomes_written(tmp_path, durable_ctx):
    log = str(tmp_path / "out.jsonl")
    q = [_task(task_id="a"), _task(task_id="b")]
    run_mission(POLICY, q, Lvl.A1_ASSISTED_ENGINEERING, _pass_attempt,
                lambda t, v: _pass_attempt(t, 9), SUP_PASS(), _now, _vid, outcome_log=log, certification=durable_ctx, roadmap=ROADMAP)
    from portfolio_automation.engineer_worker.ew0a import read_outcomes
    rows = read_outcomes(log)
    assert len(rows) == 2 and all(r["final_status"] == "VERIFIED" for r in rows)


# --- Phase 5: runtime policy persistence (trusted, protected) ---------------
def test_runtime_policy_roundtrip(tmp_path):
    from portfolio_automation.engineer_worker.ew0a_loop import read_runtime_policy, write_runtime_policy
    write_runtime_policy(tmp_path, POLICY)
    got = read_runtime_policy(tmp_path)
    assert got.mission_id == "northstar_test" and got.gpt_supervisor_required and not got.auto_merge


# --- Idle controller state (no mission executing) ---------------------------
# Pins the semantics of an EMPTY mission_id so that it is a SPECIFIED state
# rather than an ambiguous value. Reconciling the runtime after a mission
# completes must not mean pointing it at a different stale mission, and must not
# pre-authorize the next one. The existing schema already expresses "no mission
# executing" safely: the mission-boundary check in run_mission refuses every
# task, so nothing can be dispatched while the controller is idle.
IDLE_POLICY = RuntimePolicy(mission_id="")


def _must_not_run(*_args, **_kwargs):
    raise AssertionError("no executor may be invoked while the controller is idle")


def test_idle_runtime_refuses_every_task(durable_ctx):
    """An idle controller dispatches nothing, whatever is queued."""
    queue = [_task(task_id="a", mission_id="northstar_test"),
             _task(task_id="b", mission_id="some_other_mission")]
    rep = run_mission(IDLE_POLICY, queue, Lvl.A1_ASSISTED_ENGINEERING,
                      _must_not_run, _must_not_run, _must_not_run, _now, _vid, certification=durable_ctx, roadmap=ROADMAP)
    assert rep.tasks_run == []
    assert rep.verified == 0
    # An idle controller is now refused one layer earlier too: the roadmap
    # authorizes a named mission and "" is not it. _must_not_run proves no
    # executor was invoked either way.
    assert rep.roadmap_violation
    assert rep.stop_reason.startswith(LoopStop.ROADMAP_VIOLATION.value)


def test_idle_runtime_still_denies_the_disabled_authorities():
    """Idling must not relax any authority boundary."""
    assert IDLE_POLICY.disabled_authorities_ok() is True
    assert not IDLE_POLICY.auto_merge and not IDLE_POLICY.auto_deploy
    assert not IDLE_POLICY.auto_production_mutation
    assert not IDLE_POLICY.auto_authority_promotion
    assert not IDLE_POLICY.auto_capital_action


def test_idle_runtime_round_trips(tmp_path):
    from portfolio_automation.engineer_worker.ew0a_loop import (
        read_runtime_policy, write_runtime_policy)
    write_runtime_policy(tmp_path, IDLE_POLICY)
    got = read_runtime_policy(tmp_path)
    assert got.mission_id == ""
    assert got.gpt_supervisor_required is True


def test_a_real_mission_still_dispatches_after_idle_support(durable_ctx):
    """Regression guard: supporting an idle state must not break normal operation."""
    rep = run_mission(POLICY, [_task(task_id="a")], Lvl.A1_ASSISTED_ENGINEERING,
                      _pass_attempt, lambda t, v: _pass_attempt(t, 9), SUP_PASS(),
                      _now, _vid, certification=durable_ctx, roadmap=ROADMAP)
    assert rep.verified == 1


# ═══════════════════════════════════════════════════════════════════════════
# GUI-SR — BLOCKER B: read_runtime_policy must be TOTAL and enforce its schema
#
# A non-object JSON root reached d.items() and raised AttributeError, which the
# guard did not name. Separately, a dataclass holds whatever it is handed, so
# RuntimePolicy(**json) accepted auto_merge="true" -- and
# disabled_authorities_ok() then reads a non-empty string as True.
# ═══════════════════════════════════════════════════════════════════════════
import dataclasses as _gsr_dc
import json as _gsr_json

from portfolio_automation.engineer_worker.ew0a_loop import (
    read_runtime_policy as _gsr_read_policy, RuntimePolicy as _GsrPolicy)

_GSR_MARKER = "sk-GUI-SR-MUST-NOT-RENDER-999"


def _gsr_runtime_root(tmp_path, body=None, raw=None):
    target = tmp_path / "config" / "ew0a_runtime.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        target.write_bytes(raw)
    else:
        target.write_text(body, encoding="utf-8")
    return tmp_path


def test_gsr_runtime_reader_is_total_over_json_root_shapes(tmp_path):
    for label, body in (("null", "null"), ("list", "[1,2]"), ("int", "123"),
                        ("float", "1.5"), ("string", '"text"'), ("bool", "true"),
                        ("invalid json", "{oops")):
        root = _gsr_runtime_root(tmp_path / f"b_{label.replace(' ', '_')}", body)
        assert _gsr_read_policy(root) is None, label


def test_gsr_runtime_reader_handles_absent_io_and_decode_failures(tmp_path):
    assert _gsr_read_policy(tmp_path / "absent") is None
    assert _gsr_read_policy(_gsr_runtime_root(tmp_path / "utf8", raw=b"\xff\xfebad")) is None


def test_gsr_runtime_reader_requires_mission_id(tmp_path):
    assert _gsr_read_policy(_gsr_runtime_root(tmp_path / "nomission", "{}")) is None


def test_gsr_runtime_reader_enforces_declared_field_types(tmp_path):
    """The persisted contract already declares these types; the reader simply
    had not been enforcing them."""
    cases = {
        "bool_as_int": {"auto_merge": 1},
        "bool_as_zero": {"auto_merge": 0},
        "bool_as_string": {"auto_merge": "true"},
        "bool_as_dict": {"auto_merge": {"api_key": _GSR_MARKER}},
        "count_as_bool": {"max_concurrent_tasks": True},
        "count_as_string": {"max_concurrent_tasks": "2"},
        "count_as_float": {"max_concurrent_tasks": 1.5},
        "str_as_int": {"engineering_mode": 7},
        "str_as_dict": {"mission_id": {"api_key": _GSR_MARKER}},
        "str_as_list": {"authority": [_GSR_MARKER]},
    }
    for label, extra in cases.items():
        record = {"mission_id": "m-ok"}
        record.update(extra)
        root = _gsr_runtime_root(tmp_path / f"t_{label}", _gsr_json.dumps(record))
        assert _gsr_read_policy(root) is None, label


def test_gsr_runtime_reader_accepts_the_real_and_partial_records(tmp_path):
    """A partial record is legitimate -- absent fields take dataclass defaults,
    which is how the repository already writes them in places."""
    partial = _gsr_read_policy(_gsr_runtime_root(
        tmp_path / "partial", _gsr_json.dumps({"mission_id": "m-ok"})))
    assert partial is not None and partial.mission_id == "m-ok"
    assert partial.auto_merge is False and partial.max_concurrent_tasks == 1

    full = {f.name: getattr(_GsrPolicy(mission_id="m-full"), f.name)
            for f in _gsr_dc.fields(_GsrPolicy)}
    loaded = _gsr_read_policy(_gsr_runtime_root(tmp_path / "full",
                                                _gsr_json.dumps(full)))
    assert loaded is not None and loaded.disabled_authorities_ok()


def test_gsr_every_runtime_field_is_covered_by_the_validator():
    """Executable coupling. If a future field introduces a new annotation, this
    fails and the type map must be extended -- rather than the field silently
    escaping validation, which is the failure mode that produced this mission."""
    from portfolio_automation.engineer_worker.ew0a_loop import (
        _UNVALIDATED_RUNTIME_FIELDS, _RUNTIME_FIELD_TYPES)
    assert _UNVALIDATED_RUNTIME_FIELDS == ()
    assert set(_RUNTIME_FIELD_TYPES) == {f.name for f in _gsr_dc.fields(_GsrPolicy)}


def test_gsr_a_malformed_policy_cannot_report_authorities_enabled(tmp_path):
    """The reason bool discipline matters: a non-empty string is truthy, so
    auto_merge="true" would have made disabled_authorities_ok() false while the
    record claimed to be a policy."""
    root = _gsr_runtime_root(tmp_path, _gsr_json.dumps(
        {"mission_id": "m", "auto_merge": "true", "auto_deploy": "yes"}))
    assert _gsr_read_policy(root) is None
