"""NORTHSTAR_0A authority/roadmap reconciliation — project-state contracts.

Extends (does not duplicate) tests/test_agent_context_check.py, which covers
the generic .agent/ schema. This file pins the Northstar-specific state:

- project-state / phase-status YAML remain parseable (reqs 1-2)
- agent_context_check reports the new program/phase/step (req 3)
- Northstar future phases are not falsely marked complete (req 9)
- observe_and_iterate history is represented, not erased (req 10)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = REPO_ROOT / ".agent" / "project_state.yaml"
PHASE_FILE = REPO_ROOT / ".agent" / "phase_status.yaml"
SCRIPT = REPO_ROOT / "scripts" / "agent_context_check.py"

AUTHORIZED_0C_MISSION = "northstar_0c_pit_evidence_gateway_research_store"

# 0C left this list on 2026-08-15 when the operator explicitly authorized it. It
# is now the CURRENT phase, guarded by its own tests below (which additionally
# assert that being active has NOT been confused with being implemented).
FUTURE_PHASES = [
    "northstar_phase_0d",
    "northstar_phase_1",
    "northstar_phase_2",
    "northstar_phase_3",
    "northstar_phase_4",
    "northstar_phase_5",
    "northstar_phase_6",
    "northstar_phase_7",
    "northstar_phase_8",
    "northstar_phase_9",
    "northstar_phase_10",
    "northstar_phase_11",
]

# Statuses that mean "no implementation exists". `ready` is included because it
# denotes ONLY that a phase's dependency is satisfied — it is emphatically not a
# claim that anything was built. Every phase after the current one must sit in
# this set, and `complete`/`active` remain forbidden for all of them.
NOT_IMPLEMENTED_STATUSES = {"not_started", "ready"}


def _load(path: Path) -> dict:
    try:
        import yaml
    except ImportError:  # pragma: no cover
        pytest.skip("pyyaml not installed")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def state() -> dict:
    return _load(STATE_FILE)


@pytest.fixture(scope="module")
def phase() -> dict:
    return _load(PHASE_FILE)


# ── Reqs 1-2: parseable ────────────────────────────────────────────────────


def test_project_state_parses_to_mapping(state):
    assert isinstance(state, dict) and state


def test_phase_status_parses_to_mapping(phase):
    assert isinstance(phase, dict) and phase


# ── Program / phase / step ─────────────────────────────────────────────────


def test_program_is_northstar(state):
    assert state["program"] == "stockbot_northstar_redesign"


def test_current_phase_and_step(state):
    # 2026-08-15: Phase 0B closed (gate NORTHSTAR_0B_CONTRACTS_READY) and Phase 0C
    # was authorized by explicit operator decision. The controller pointers must
    # follow the authorized mission — a stale pointer at a completed phase is the
    # defect this guard exists to catch.
    assert state["current_phase"] == "northstar_phase_0c"
    assert state["current_step"] == AUTHORIZED_0C_MISSION


def test_next_official_step_is_the_authorized_0c_mission(state):
    nos = state["next_official_step"]
    assert nos["primary"] == AUTHORIZED_0C_MISSION
    # History is carried forward, not erased.
    assert nos["prior_primary"] == "northstar_0b_canonical_contracts"


def test_controller_pointers_do_not_lag_the_phase_map(state, phase):
    """The top-level pointers and the phase map must agree.

    They drifted apart once already: the phase map said 0B complete / 0C ready
    while current_phase still pointed at 0B. Two authoritative surfaces that
    disagree mean at least one of them is lying."""
    phases = phase["stockbot_northstar_redesign"]["phases"]
    current = state["current_phase"]
    assert phases[current]["status"] == "active", (
        f"current_phase {current} must be the phase marked active"
    )
    assert state["northstar_program"]["phases"][current]["status"] == "active"


# ── Req 3: agent_context_check reports the new state ───────────────────────


def test_agent_context_check_reports_program_phase_step():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "stockbot_northstar_redesign" in out
    assert "northstar_phase_0c" in out
    assert AUTHORIZED_0C_MISSION in out
    # The stale claim must be gone from the summary.
    assert "Claude runs locally. Return VPS commands" not in out


# ── Req 9: future phases not falsely complete ──────────────────────────────


def test_no_future_phase_marked_complete_in_project_state(state):
    phases = state["northstar_program"]["phases"]
    for name in FUTURE_PHASES:
        assert phases[name]["status"] == "not_started", (
            f"{name} must be not_started — phases beyond the authorized one are "
            "neither started nor pre-authorized"
        )
    assert phases["northstar_phase_0a"]["status"] == "complete"
    assert phases["northstar_phase_0b"]["status"] == "complete"
    assert phases["northstar_phase_0c"]["status"] == "active"


def test_phase_0a_complete_with_gate_and_both_milestones(phase):
    # Phase 0A closed 2026-08-09 with remote CI evidence (northstar-ci run
    # 31338193791 GREEN) and GPT SESSION_CLOSED.
    program = phase["stockbot_northstar_redesign"]
    p0a = program["phases"]["northstar_phase_0a"]
    assert p0a["status"] == "complete"
    assert p0a["gate"] == "NORTHSTAR_GOVERNANCE_FOUNDATION_READY"
    assert "NORTHSTAR_GOVERNANCE_FOUNDATION_READY" in program["gates_achieved"]
    milestones = p0a["milestones"]
    assert milestones["northstar_0a_authority_roadmap_reconciliation"]["status"] == "complete"
    assert milestones["northstar_0a_ci_foundation"]["status"] == "complete"


def test_phase_0b_complete_contracts_only(phase):
    """0B closed 2026-08-15. Its contract-first character must survive closure:
    the phase delivered CONTRACTS, never engines, runtimes or source integrations."""
    program = phase["stockbot_northstar_redesign"]
    p0b = program["phases"]["northstar_phase_0b"]
    assert p0b["status"] == "complete"
    assert p0b["step"] == "northstar_0b_canonical_contracts"
    # Closure must be evidenced, not asserted.
    gate = p0b["exit_gate"]
    assert gate["status"] == "SATISFIED"
    assert gate["versioned"] == "PROVEN"
    assert gate["test_covered"] == "PROVEN"
    assert set(gate["reviewed"].values()) == {"PASS"}, (
        "every 0B milestone must be independently reviewed PASS"
    )
    assert gate["false_certifications"] == 0
    assert gate["authority_boundary_violations"] == 0
    assert "NORTHSTAR_0B_CONTRACTS_READY" in program["gates_achieved"]
    # The handoff records extensible replaceable data sources as an Evidence
    # Plane requirement — never vendor schemas embedded in the engines.
    reqs = " ".join(p0b["requirements"])
    assert "replaceable data sources" in reqs
    assert "point-in-time" in reqs
    # Contract-first: no engine/runtime/source integration in 0B.
    assert "Contracts only" in p0b["purpose"]


def test_learning_kernel_is_non_blocking_parallel_capability(phase):
    """The Engineer Learning Kernel is engineering-organization infrastructure,
    NOT a canonical Northstar contract. The 0B exit gate is about contracts, so
    the kernel's immature capability-graduation evidence must neither block 0B
    closure nor be counted as contributing to it."""
    p0b = phase["stockbot_northstar_redesign"]["phases"]["northstar_phase_0b"]
    lk = p0b["milestones"]["northstar_0b_engineering_learning_kernel"]
    assert lk["blocks_northstar_0b"] is False
    assert lk["classification"] == "NON_BLOCKING_FOR_NORTHSTAR_0B"
    assert lk["status"] == "certification_candidate"
    # It must not appear among the reviewed CONTRACT milestones.
    assert "northstar_0b_engineering_learning_kernel" not in p0b["exit_gate"]["reviewed"]


def test_engineer_runtime_points_at_the_authorized_mission(phase):
    """The runtime mission_id IS the bounded mission boundary. It must name the
    authorized mission — never a completed one (stale) and never an
    unauthorized future phase."""
    rt = phase["stockbot_northstar_redesign"]["engineer_runtime_state"]
    assert rt["mission_id"] == AUTHORIZED_0C_MISSION
    assert rt["c1"] == "DISABLED"
    assert rt["authority"] == "A1_ASSISTED_ENGINEERING"
    assert rt["engineering_mode"] == "SUPERVISED_AUTONOMOUS"


def test_runtime_config_matches_the_authorized_mission_and_grants_nothing():
    """Activation sets WHICH mission may be dispatched. It grants no authority."""
    runtime = json.loads((REPO_ROOT / "config" / "ew0a_runtime.json").read_text())
    assert runtime["mission_id"] == AUTHORIZED_0C_MISSION
    assert runtime["authority"] == "A1_ASSISTED_ENGINEERING"
    assert runtime["engineering_mode"] == "SUPERVISED_AUTONOMOUS"
    assert runtime["max_concurrent_tasks"] == 1
    for denied in ("auto_merge", "auto_deploy", "auto_production_mutation",
                   "auto_authority_promotion", "auto_capital_action"):
        assert runtime[denied] is False, f"{denied} must remain disabled"


def test_runtime_still_refuses_out_of_mission_tasks(durable_ctx):
    """The mission boundary must still bind: authorizing 0C authorizes 0C tasks
    and nothing else."""
    from portfolio_automation.engineer_worker.ew0a import (
        EngineeringTaskV0, Executor, RiskClass)
    from portfolio_automation.engineer_worker.ew0a_authority import EngineerAuthorityLevel
    from portfolio_automation.engineer_worker.ew0a_loop import (
        read_runtime_policy, run_mission)
    from portfolio_automation.engineer_worker.roadmap_guard import RoadmapAuthorization

    policy = read_runtime_policy(REPO_ROOT)
    # Resolved from the REAL roadmap record, so this exercises the production
    # guard rather than an in-memory stand-in.
    roadmap = RoadmapAuthorization.read(REPO_ROOT)
    assert policy is not None and policy.mission_id == AUTHORIZED_0C_MISSION

    def _must_not_run(*_a, **_k):
        raise AssertionError("an out-of-mission task must never be dispatched")

    foreign = EngineeringTaskV0(
        task_id="t-foreign", title="t", goal="g", risk_class=RiskClass.E1_ROUTINE,
        executor=Executor.ENGINEER, mission_id="northstar_phase_0d_something",
        allowed_paths=["tests/"], allowed_tests=["tests/tx.py"])
    rep = run_mission(policy, [foreign], EngineerAuthorityLevel.A1_ASSISTED_ENGINEERING,
                      _must_not_run, _must_not_run, _must_not_run,
                      lambda: "2026-08-15T00:00:00+00:00", lambda: "v1",
                      certification=durable_ctx, roadmap=roadmap)
    assert rep.tasks_run == []
    # The mission itself IS authorized; the foreign task is refused by the
    # mission boundary, not by the roadmap guard. Both layers are live.
    assert not rep.roadmap_violation
    assert "out-of-mission task" in rep.stop_reason


def test_no_future_phase_marked_complete_in_phase_status(phase):
    phases = phase["stockbot_northstar_redesign"]["phases"]
    for name in FUTURE_PHASES:
        status = phases[name]["status"]
        assert status in NOT_IMPLEMENTED_STATUSES
        assert status not in ("complete", "active")
        assert status == "not_started"


# ── Phase 0C activation: authorized, NOT implemented ───────────────────────


def test_phase_0c_is_authorized_and_active(phase):
    p0c = phase["stockbot_northstar_redesign"]["phases"]["northstar_phase_0c"]
    assert p0c["status"] == "active"
    assert p0c["step"] == AUTHORIZED_0C_MISSION
    auth = p0c["authorization"]
    assert auth["authorized_by"] == "operator"
    assert auth["authorized_mission"] == AUTHORIZED_0C_MISSION


def test_phase_0c_depends_on_a_completed_0b(phase):
    phases = phase["stockbot_northstar_redesign"]["phases"]
    p0c = phases["northstar_phase_0c"]
    assert p0c["depends_on"] == "northstar_phase_0b"
    assert phases[p0c["depends_on"]]["status"] == "complete"


def test_implementation_started_requires_durability_evidence(phase):
    """`active` records AUTHORIZATION; `implementation_started` records CONSTRUCTION.

    This guard originally asserted the flag was False, which was the truth at
    activation time. The flag has since flipped on real durable work, so it now
    enforces the invariant that actually matters and outlives both states: the
    flag may be true ONLY when durability evidence exists. A flag flipped without
    a merged SHA and a green main CI run would be the same overclaim the original
    guard was written to prevent."""
    p0c = phase["stockbot_northstar_redesign"]["phases"]["northstar_phase_0c"]
    if p0c["implementation_started"] is True:
        milestones = p0c.get("milestones") or {}
        durable = [m for m in milestones.values() if m.get("durable") is True]
        assert durable, "implementation_started=true requires a durable milestone"
        for m in durable:
            assert m.get("merged_main_sha")
            assert m.get("post_merge_main_ci_result") == "SUCCESS"
    # Whatever the flag says, the phase must not be claimed complete.
    assert p0c["status"] != "complete"


def test_activation_granted_no_vendor_or_purchase_authority(phase):
    p0c = phase["stockbot_northstar_redesign"]["phases"]["northstar_phase_0c"]
    vendor = p0c["vendor_authority"].lower()
    assert "no vendor is selected" in vendor
    assert "e4" in vendor


def test_activation_scope_excludes_later_phases_and_capital(phase):
    scope = phase["stockbot_northstar_redesign"]["phases"]["northstar_phase_0c"][
        "authorization"]["scope"].lower()
    for forbidden in ("0d", "capital", "portfolio", "production", "broker", "c1"):
        assert forbidden in scope, f"scope must explicitly exclude {forbidden}"


# ── Req 10: observe_and_iterate history preserved, not erased ──────────────


def test_observe_and_iterate_recorded_in_completed_steps(state):
    completed = state["completed_steps"]
    assert any("observe_and_iterate" in str(step) for step in completed), (
        "observe_and_iterate closure must be recorded in completed_steps"
    )


def test_observe_and_iterate_block_preserved_in_phase_status(phase):
    block = phase["observe_and_iterate"]
    assert block["status"] == "superseded"
    # Historical monthly-analysis evidence must survive.
    assert "last_monthly_analysis" in block


def test_observation_continues_as_parallel_workstream(state):
    streams = state["northstar_program"]["parallel_workstreams"]
    assert any("production_observation" in str(s) for s in streams)


# ── Authority reconciliation is machine-readable ───────────────────────────


def test_deployment_context_reconciled_to_two_environments(state):
    ctx = state["deployment_context"]
    assert ctx["claude_environments"] == ["operator_laptop", "production_vps"]
    assert ctx["authority_policy"] == "config/agent_policy.yaml"
    # The stale keys must be gone as live claims (history stays in comments).
    assert "claude_does_not_run_on_vps" not in ctx
    assert "claude_runs_locally" not in ctx


def test_role_split_points_to_authority_policy(state):
    assert state["role_split"]["authority_policy"] == "config/agent_policy.yaml"


def test_advisory_invariants_unchanged(state):
    # The program must not weaken the standing hard boundaries.
    assert state["mode"] == "advisory_only"
    assert state["no_auto_trading"] is True
    forbidden = state["forbidden_changes"]
    assert "introducing_auto_execution_or_trading" in forbidden
    assert "calling_broker_apis" in forbidden


# ══ 0C FOUNDATION STATE RECONCILIATION ═══════════════════════════════════
# After PR #20 merged durably, authoritative state still said 0C implementation
# had not begun. These tests pin the reconciliation in BOTH directions: state
# must admit that implementation started, and must NOT overclaim what is done.

FOUNDATION_MAIN_SHA = "33de038645238fcbaf0e0dd210bbdec48c704058"


def _p0c(phase):
    return phase["stockbot_northstar_redesign"]["phases"]["northstar_phase_0c"]


def test_phase_0c_remains_active_and_is_not_complete(phase):
    p0c = _p0c(phase)
    assert p0c["status"] == "active"
    assert p0c["status"] != "complete"


def test_0c_implementation_started_is_true(phase):
    """The defect this reconciliation fixed: durable implementation had shipped
    while state still claimed nothing was built."""
    assert _p0c(phase)["implementation_started"] is True


def test_phase_0b_remains_complete(phase):
    phases = phase["stockbot_northstar_redesign"]["phases"]
    assert phases["northstar_phase_0b"]["status"] == "complete"


def test_evidencegateway_foundation_durability_evidence_is_recorded(phase):
    m = _p0c(phase)["milestones"]["evidence_gateway_foundation"]
    assert m["durable"] is True
    assert m["merged_main_sha"] == FOUNDATION_MAIN_SHA
    assert m["post_merge_main_ci_run"] == 44
    assert m["post_merge_main_ci_result"] == "SUCCESS"
    assert m["session_id"] == "ns0c-evgw-foundation-001"


def test_0c_exit_gate_is_not_yet_satisfied(phase):
    """Foundation durable does NOT mean the phase gate is met."""
    p0c = _p0c(phase)
    assert p0c["exit_gate"] == "lookahead-audited PIT reads over the research store"
    assert "exit_gate_satisfied" not in p0c
    assert p0c.get("remaining_work"), "remaining 0C work must stay recorded"


def test_research_store_is_not_claimed_complete(phase):
    """The strongest overclaim risk: FOUNDATION completeness reading as STORE
    completeness. The foundation is now a reviewed candidate, so the guard moves
    from 'no such milestone' to 'the milestone must not claim durability'.
    Durable means merged with post-merge main CI, and neither has happened."""
    m = _p0c(phase)["milestones"]["research_store_foundation"]
    assert m["durable"] is False
    assert m["status"] != "complete"
    assert "merged_main_sha" not in m, "not merged, so no merged SHA may be recorded"
    # the FOUNDATION is not the whole store: historical reads remain outstanding
    remaining = " ".join(_p0c(phase)["remaining_work"]).lower()
    assert "as-of reads" in remaining
    assert "lookahead audit" in remaining


def test_next_bounded_candidate_is_a_candidate_not_an_authorization(phase):
    """The candidate names the NEXT bounded task and is never itself a
    milestone. The guard is about the roadmap not self-authorizing, not about
    which task happens to be queued — so it is asserted structurally rather than
    against a name that changes every session."""
    p0c = _p0c(phase)
    candidate = p0c["next_bounded_candidate"]
    assert candidate, "a next candidate must always be named"
    slug = candidate.lower().replace(" ", "_").replace("-", "_")
    for name in p0c["milestones"]:
        assert name not in slug, (
            f"{name} is queued as the next candidate AND recorded as a milestone; "
            "one of those is wrong")
    assert any(word in candidate.lower() for word in ("as-of", "as_of")), \
        "after the store foundation, historical as-of reads are next"


def test_revision_supersession_safety_durability_evidence_is_recorded(phase):
    """It shipped, so state must say so. Leaving it queued as a candidate is
    what would send the autonomous task resolver back to redo PR #22."""
    m = _p0c(phase)["milestones"]["revision_supersession_safety"]
    assert m["durable"] is True
    assert m["merged_main_sha"] == "3d97bb392b595f90baa8787b8c9d2aa592c00f8a"
    assert m["post_merge_main_ci_result"] == "SUCCESS"
    assert m["session_id"] == "ns0c-revision-supersession-002"
    assert m["pull_request"] == 22


def test_revision_safety_did_not_resolve_the_deferred_policies(phase):
    """Shipping the safety rule must not be read as having decided the two
    questions it deliberately left open."""
    preserved = " ".join(
        _p0c(phase)["milestones"]["revision_supersession_safety"]["preserved_unresolved"])
    assert "UNRESOLVED_NOT_INVENTED" in preserved
    assert "effective_period" in preserved


def test_effective_period_policy_remains_unresolved(phase):
    """A future worker must not infer the rule merely because the field exists."""
    note = _p0c(phase)["unresolved_semantics"]["effective_period_vs_as_of"].lower()
    assert "unresolved" in note and "not decided" in note
    assert "does not establish" in note


def test_later_phases_remain_not_started(phase):
    phases = phase["stockbot_northstar_redesign"]["phases"]
    for name in FUTURE_PHASES:
        assert phases[name]["status"] == "not_started"


def test_authority_and_runtime_unchanged_by_reconciliation(phase):
    rt = phase["stockbot_northstar_redesign"]["engineer_runtime_state"]
    assert rt["authority"] == "A1_ASSISTED_ENGINEERING"
    assert rt["c1"] == "DISABLED"
    assert rt["mission_id"] == AUTHORIZED_0C_MISSION
    runtime = json.loads((REPO_ROOT / "config" / "ew0a_runtime.json").read_text())
    assert runtime["mission_id"] == AUTHORIZED_0C_MISSION
    assert runtime["authority"] == "A1_ASSISTED_ENGINEERING"
    for denied in ("auto_merge", "auto_deploy", "auto_production_mutation",
                   "auto_authority_promotion", "auto_capital_action"):
        assert runtime[denied] is False


def test_no_authoritative_surface_still_says_implementation_has_not_begun():
    """Prose drift is the failure mode here, so it is checked directly."""
    stale = ("IMPLEMENTATION NOT BEGUN", "implementation NOT begun",
             "no 0C code exists")
    for rel in (".agent/phase_status.yaml", ".agent/project_state.yaml",
                "docs/roadmap.md", "docs/NORTHSTAR_REDESIGN.md"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for phrase in stale:
            assert phrase not in text, f"{rel} still says: {phrase}"
