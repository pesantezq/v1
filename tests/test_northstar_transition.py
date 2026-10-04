from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "northstar_transition.py"

spec = importlib.util.spec_from_file_location("northstar_transition", SCRIPT)
assert spec and spec.loader
transition = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transition)


def _copy(tmp_path: Path, rel: str) -> None:
    src = REPO_ROOT / rel
    dst = tmp_path / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def test_restore_paused_adapter_transition_preserves_exact_scope(tmp_path, monkeypatch):
    for rel in (
        ".agent/project_state.yaml",
        ".agent/phase_status.yaml",
        "config/ew0a_runtime.json",
        "docs/NORTHSTAR_REDESIGN.md",
        "docs/roadmap.md",
        "docs/vs002_production_evidence_runner.md",
    ):
        _copy(tmp_path, rel)

    before = yaml.safe_load((tmp_path / ".agent/phase_status.yaml").read_text())
    p0c_before = before["stockbot_northstar_redesign"]["phases"]["northstar_phase_0c"]
    paused_before = p0c_before["bounded_authorization"]["paused_bounded_authorization"]
    exact_scope = " ".join(paused_before["scope"].split())

    monkeypatch.setattr(transition, "REPO_ROOT", tmp_path)
    completion_sha = "a" * 40
    transition._restore_paused_adapter(
        "northstar_continuous_mission_orchestration_foundation",
        "northstar_vs002_execution_adapter_foundation",
        completion_sha,
    )

    project = yaml.safe_load((tmp_path / ".agent/project_state.yaml").read_text())
    phase = yaml.safe_load((tmp_path / ".agent/phase_status.yaml").read_text())
    runtime = json.loads((tmp_path / "config/ew0a_runtime.json").read_text())

    assert project["current_step"] == "northstar_vs002_execution_adapter_foundation"
    assert project["next_official_step"]["primary"] == "northstar_vs002_execution_adapter_foundation"
    assert project["next_official_step"]["secondary"] == []
    assert project["next_official_step"]["prior_primary"] == "northstar_continuous_mission_orchestration_foundation"

    ns = phase["stockbot_northstar_redesign"]
    p0c = ns["phases"]["northstar_phase_0c"]
    assert ns["engineer_runtime_state"]["mission_id"] == "northstar_vs002_execution_adapter_foundation"
    assert p0c["step"] == "northstar_vs002_execution_adapter_foundation"
    assert runtime["mission_id"] == "northstar_vs002_execution_adapter_foundation"

    active_auth = p0c["bounded_authorization"]
    assert active_auth["authorized_mission"] == "northstar_vs002_execution_adapter_foundation"
    assert " ".join(active_auth["scope"].split()) == exact_scope

    prior = active_auth["prior_bounded_authorization"]
    assert prior["authorized_mission"] == "northstar_continuous_mission_orchestration_foundation"
    assert prior["status"] == "COMPLETE"
    assert prior["merged_main_sha"] == completion_sha
    assert prior["post_merge_main_ci_result"] == "SUCCESS"

    milestone = p0c["milestones"]["continuous_mission_orchestration_foundation"]
    assert milestone["durable"] is True
    assert milestone["merged_main_sha"] == completion_sha


def test_transition_rejects_unregistered_edge(tmp_path, monkeypatch):
    monkeypatch.setattr(transition, "REPO_ROOT", tmp_path)
    try:
        transition._restore_paused_adapter("wrong", "also_wrong", "b" * 40)
    except RuntimeError as exc:
        assert "unsupported automatic transition source" in str(exc)
    else:
        raise AssertionError("unsupported transition must fail closed")
