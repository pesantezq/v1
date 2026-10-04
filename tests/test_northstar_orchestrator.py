from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "northstar_orchestrator.py"

spec = importlib.util.spec_from_file_location("northstar_orchestrator", SCRIPT)
assert spec and spec.loader
orch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orch)


def test_marker_round_trip():
    text = orch.marker_text(
        kind="implementation",
        mission_id="northstar_example",
        starting_main_sha="abc123",
    )
    assert orch.parse_marker(text) == {
        "kind": "implementation",
        "mission_id": "northstar_example",
        "starting_main_sha": "abc123",
    }


def test_latest_checks_uses_latest_run_id():
    runs = [
        {"id": 1, "name": "Northstar governance", "conclusion": "success"},
        {"id": 3, "name": "Northstar governance", "conclusion": "failure"},
        {"id": 2, "name": "other", "conclusion": "success"},
    ]
    latest = orch.latest_checks(runs)
    assert latest["Northstar governance"]["id"] == 3


def test_codex_issue_comment_can_certify_short_exact_head():
    head = "b8983ac4e42ab2abab1f6448e6177170d5803af5"
    issue_comments = [
        {
            "user": {"login": orch.CODEX_LOGIN},
            "body": "Codex Review: Didn't find any major issues.\n\n**Reviewed commit:** `b8983ac4e4`",
        }
    ]
    assert orch.codex_review_complete(head, [], issue_comments)


def test_codex_review_object_can_certify_exact_head():
    head = "a" * 40
    reviews = [{"user": {"login": orch.CODEX_LOGIN}, "commit_id": head}]
    assert orch.codex_review_complete(head, reviews, [])


def test_material_findings_bind_to_original_commit_not_carried_forward_commit():
    old = "1" * 40
    new = "2" * 40
    comment = {
        "user": {"login": orch.CODEX_LOGIN},
        "original_commit_id": old,
        "commit_id": new,
        "body": "**<sub>P1 Badge</sub>** problem",
    }
    assert orch.exact_head_material_findings(new, [comment]) == []
    assert orch.exact_head_material_findings(old, [comment]) == [comment]


def test_review_request_is_exact_head_bound():
    head = "3" * 40
    comments = [{"body": f"@codex review the exact head {head}"}]
    assert orch.review_already_requested(head, comments)
    assert not orch.review_already_requested("4" * 40, comments)


def test_implementation_marker_must_match_protected_current_mission():
    registry = {
        "missions": {
            "mission_a": {"on_success": {"policy": "human_required"}},
        }
    }
    state = orch.DispatchState(mission_id="mission_a", phase="phase")
    orch.validate_marker_authority(
        {"kind": "implementation", "mission_id": "mission_a"},
        state,
        registry,
    )
    with pytest.raises(orch.OrchestrationError):
        orch.validate_marker_authority(
            {"kind": "implementation", "mission_id": "mission_b"},
            state,
            registry,
        )


def test_transition_marker_requires_preauthorized_edge():
    registry = {
        "missions": {
            "mission_a": {
                "on_success": {
                    "policy": "preauthorized_auto",
                    "next_mission": "mission_b",
                }
            },
            "mission_b": {"on_success": {"policy": "human_required"}},
        }
    }
    state = orch.DispatchState(mission_id="mission_a", phase="phase")
    orch.validate_marker_authority(
        {
            "kind": "governance_transition",
            "transition_from": "mission_a",
            "transition_to": "mission_b",
        },
        state,
        registry,
    )
    with pytest.raises(orch.OrchestrationError):
        orch.validate_marker_authority(
            {
                "kind": "governance_transition",
                "transition_from": "mission_a",
                "transition_to": "mission_c",
            },
            state,
            registry,
        )


def test_registry_marks_e4_frozen_execution_human_required():
    registry = orch.load_registry()
    frozen = registry["missions"]["northstar_vs002_frozen_execution"]
    assert frozen["risk_class"] == "E4"
    assert frozen["auto_dispatch"] is False
    assert frozen["on_success"]["policy"] == "human_required"


def test_controller_paths_are_protected_for_ordinary_missions():
    from portfolio_automation.engineer_worker.policy import is_protected

    registry = orch.load_registry()
    assert orch.path_is_controller_protected(
        ".github/workflows/northstar-orchestrator.yml",
        registry,
    )
    assert is_protected(".github/workflows/northstar-orchestrator.yml")
    assert is_protected(".github/workflows/claude-authorized-mission.yml")
    assert is_protected("scripts/northstar_orchestrator.py")
    assert is_protected("scripts/northstar_transition.py")
    assert orch.path_is_controller_protected(
        ".agent/missions/northstar_vs002_execution_adapter_foundation.md",
        registry,
    )
    assert not orch.path_is_controller_protected(
        "portfolio_automation/vs002_evidence/execution.py",
        registry,
    )
