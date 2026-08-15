"""NORTHSTAR_0A authority/roadmap reconciliation — project-state contracts.

Extends (does not duplicate) tests/test_agent_context_check.py, which covers
the generic .agent/ schema. This file pins the Northstar-specific state:

- project-state / phase-status YAML remain parseable (reqs 1-2)
- agent_context_check reports the new program/phase/step (req 3)
- Northstar future phases are not falsely marked complete (req 9)
- observe_and_iterate history is represented, not erased (req 10)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = REPO_ROOT / ".agent" / "project_state.yaml"
PHASE_FILE = REPO_ROOT / ".agent" / "phase_status.yaml"
SCRIPT = REPO_ROOT / "scripts" / "agent_context_check.py"

FUTURE_PHASES = [
    "northstar_phase_0c",
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
# claim that anything was built. The guards below still forbid `complete` and
# `active` for every future phase, and restrict `ready` to the single phase whose
# dependency is actually satisfied, so `ready` cannot creep down the roadmap.
NOT_IMPLEMENTED_STATUSES = {"not_started", "ready"}

# The only future phase permitted to be `ready`: 0B is complete, so 0C's
# dependency is met. Implementation has NOT begun.
DEPENDENCY_SATISFIED_PHASE = "northstar_phase_0c"


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
    # 2026-08-09 PM: Phase 0A closed (gate NORTHSTAR_GOVERNANCE_FOUNDATION_READY);
    # Phase 0B (canonical contracts) is active.
    assert state["current_phase"] == "northstar_phase_0b"
    assert state["current_step"] == "northstar_0b_canonical_contracts"


def test_next_official_step_is_canonical_contracts(state):
    assert state["next_official_step"]["primary"] == "northstar_0b_canonical_contracts"


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
    assert "northstar_phase_0b" in out
    assert "northstar_0b_canonical_contracts" in out
    # The stale claim must be gone from the summary.
    assert "Claude runs locally. Return VPS commands" not in out


# ── Req 9: future phases not falsely complete ──────────────────────────────


def test_no_future_phase_marked_complete_in_project_state(state):
    phases = state["northstar_program"]["phases"]
    for name in FUTURE_PHASES:
        status = phases[name]["status"]
        assert status in NOT_IMPLEMENTED_STATUSES, (
            f"{name} must not be implemented — future phases are not started"
        )
        if name != DEPENDENCY_SATISFIED_PHASE:
            assert status == "not_started", (
                f"{name} must be not_started — only {DEPENDENCY_SATISFIED_PHASE} "
                "has its dependency satisfied"
            )
    assert phases["northstar_phase_0a"]["status"] == "complete"
    assert phases["northstar_phase_0b"]["status"] == "complete"


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


def test_engineer_runtime_is_idle_after_phase_closure(phase):
    """After a mission completes the runtime must be IDLE — not pointed at a
    completed mission (stale) and not pre-authorizing the next phase."""
    rt = phase["stockbot_northstar_redesign"]["engineer_runtime_state"]
    assert rt["mission_id"] == ""
    assert rt["c1"] == "DISABLED"
    assert rt["authority"] == "A1_ASSISTED_ENGINEERING"
    assert "0c" not in str(rt["mission_id"]).lower()


def test_no_future_phase_marked_complete_in_phase_status(phase):
    phases = phase["stockbot_northstar_redesign"]["phases"]
    for name in FUTURE_PHASES:
        status = phases[name]["status"]
        assert status in NOT_IMPLEMENTED_STATUSES
        assert status not in ("complete", "active")
        if name != DEPENDENCY_SATISFIED_PHASE:
            assert status == "not_started"


def test_dependency_satisfied_phase_has_no_implementation(phase):
    """`ready` must mean ONLY that the dependency is met. If 0C is ever marked
    ready while claiming implementation, that is a roadmap-integrity failure."""
    p0c = phase["stockbot_northstar_redesign"]["phases"][DEPENDENCY_SATISFIED_PHASE]
    if p0c["status"] == "ready":
        assert p0c["implementation_started"] is False
        assert p0c["depends_on"] == "northstar_phase_0b"


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
