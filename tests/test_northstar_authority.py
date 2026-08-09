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
        assert phases[name]["status"] == "not_started", (
            f"{name} must be not_started — future phases are not implemented"
        )
    assert phases["northstar_phase_0a"]["status"] == "complete"
    assert phases["northstar_phase_0b"]["status"] == "active"


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


def test_phase_0b_active_contracts_only(phase):
    p0b = phase["stockbot_northstar_redesign"]["phases"]["northstar_phase_0b"]
    assert p0b["status"] == "active"
    assert p0b["step"] == "northstar_0b_canonical_contracts"
    # The handoff records extensible replaceable data sources as an Evidence
    # Plane requirement — never vendor schemas embedded in the engines.
    reqs = " ".join(p0b["requirements"])
    assert "replaceable data sources" in reqs
    assert "point-in-time" in reqs
    # Contract-first: no engine/runtime/source integration in 0B.
    assert "Contracts only" in p0b["purpose"]


def test_no_future_phase_marked_complete_in_phase_status(phase):
    phases = phase["stockbot_northstar_redesign"]["phases"]
    for name in FUTURE_PHASES:
        assert phases[name]["status"] == "not_started"


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
