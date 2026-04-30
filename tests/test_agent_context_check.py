"""
Tests for scripts/agent_context_check.py and the .agent/ YAML files.

Coverage:
- project_state.yaml exists and has required keys
- phase_status.yaml exists and has required keys
- advisory_only is enforced (mode == advisory_only)
- no_auto_trading is true
- forbidden_changes is non-empty
- role_split includes gpt, claude, codex
- script runs successfully (exit 0)
- missing file scenario exits nonzero
- next_official_step is present and non-empty
- completed_steps is a non-empty list
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = REPO_ROOT / ".agent" / "project_state.yaml"
PHASE_FILE = REPO_ROOT / ".agent" / "phase_status.yaml"
SCRIPT = REPO_ROOT / "scripts" / "agent_context_check.py"

# ── YAML loading helper ────────────────────────────────────────────────────


def _load_yaml(path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        pytest.skip("pyyaml not installed")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ── File existence ─────────────────────────────────────────────────────────


class TestFileExistence:

    def test_project_state_yaml_exists(self):
        assert STATE_FILE.exists(), f"Missing: {STATE_FILE}"

    def test_phase_status_yaml_exists(self):
        assert PHASE_FILE.exists(), f"Missing: {PHASE_FILE}"

    def test_script_exists(self):
        assert SCRIPT.exists(), f"Missing: {SCRIPT}"


# ── project_state.yaml keys ────────────────────────────────────────────────


class TestProjectStateKeys:

    def setup_method(self):
        self.state = _load_yaml(STATE_FILE)

    def test_project_name_present(self):
        assert "project_name" in self.state

    def test_mode_present(self):
        assert "mode" in self.state

    def test_advisory_only(self):
        assert self.state.get("mode") == "advisory_only", (
            f"Expected mode='advisory_only', got {self.state.get('mode')!r}"
        )

    def test_no_auto_trading_true(self):
        assert self.state.get("no_auto_trading") is True, (
            "no_auto_trading must be True in project_state.yaml"
        )

    def test_ai_role_present(self):
        assert "ai_role" in self.state
        assert self.state["ai_role"]  # non-empty

    def test_current_phase_present(self):
        assert "current_phase" in self.state
        assert self.state["current_phase"]

    def test_current_step_present(self):
        assert "current_step" in self.state
        assert self.state["current_step"]

    def test_completed_steps_is_non_empty_list(self):
        completed = self.state.get("completed_steps", [])
        assert isinstance(completed, list), "completed_steps must be a list"
        assert len(completed) > 0, "completed_steps must not be empty"

    def test_next_official_step_present(self):
        nos = self.state.get("next_official_step")
        assert nos is not None, "next_official_step must be present"
        # May be a string, dict, or list — just must be non-empty
        if isinstance(nos, dict):
            assert nos  # non-empty dict
        elif isinstance(nos, list):
            assert len(nos) > 0
        else:
            assert str(nos).strip()

    def test_forbidden_changes_non_empty(self):
        forbidden = self.state.get("forbidden_changes", [])
        assert isinstance(forbidden, list), "forbidden_changes must be a list"
        assert len(forbidden) > 0, "forbidden_changes must not be empty"

    def test_forbidden_changes_includes_no_auto_trading(self):
        forbidden = self.state.get("forbidden_changes", [])
        trading_items = [f for f in forbidden if "trading" in f.lower() or "execution" in f.lower()]
        assert len(trading_items) > 0, (
            "forbidden_changes should include at least one trading/execution restriction"
        )

    def test_forbidden_changes_includes_scoring(self):
        forbidden = self.state.get("forbidden_changes", [])
        scoring_items = [f for f in forbidden if "scoring" in f.lower()]
        assert len(scoring_items) > 0, (
            "forbidden_changes should include scoring behavior protection"
        )

    def test_role_split_present(self):
        assert "role_split" in self.state
        assert isinstance(self.state["role_split"], dict)

    def test_role_split_includes_gpt(self):
        role_split = self.state.get("role_split", {})
        assert "gpt" in role_split, "role_split must include 'gpt'"
        assert isinstance(role_split["gpt"], list)
        assert len(role_split["gpt"]) > 0

    def test_role_split_includes_claude(self):
        role_split = self.state.get("role_split", {})
        assert "claude" in role_split, "role_split must include 'claude'"
        assert isinstance(role_split["claude"], list)
        assert len(role_split["claude"]) > 0

    def test_role_split_includes_codex(self):
        role_split = self.state.get("role_split", {})
        assert "codex" in role_split, "role_split must include 'codex'"
        assert isinstance(role_split["codex"], list)
        assert len(role_split["codex"]) > 0

    def test_output_namespace_policy_present(self):
        assert "output_namespace_policy" in self.state

    def test_required_test_policy_present(self):
        assert "required_test_policy" in self.state

    def test_deployment_context_present(self):
        assert "deployment_context" in self.state

    def test_vps_context_flags(self):
        dc = self.state.get("deployment_context", {})
        assert dc.get("claude_does_not_run_on_vps") is True, (
            "deployment_context.claude_does_not_run_on_vps must be True"
        )
        assert dc.get("vps_validation_is_manual") is True, (
            "deployment_context.vps_validation_is_manual must be True"
        )


# ── phase_status.yaml keys ─────────────────────────────────────────────────


class TestPhaseStatusKeys:

    def setup_method(self):
        self.phase = _load_yaml(PHASE_FILE)

    def test_phase_0_present(self):
        assert "phase_0" in self.phase

    def test_phase_0_status_complete(self):
        phase_0 = self.phase.get("phase_0", {})
        assert phase_0.get("status") == "complete", (
            "phase_0.status must be 'complete'"
        )

    def test_phase_0_has_steps(self):
        phase_0 = self.phase.get("phase_0", {})
        steps = phase_0.get("steps", {})
        assert isinstance(steps, dict)
        assert len(steps) >= 5, "Phase 0 should have at least 5 steps"

    def test_agent_orchestration_layer_present(self):
        assert "agent_orchestration_layer" in self.phase

    def test_post_phase_0_present(self):
        assert "post_phase_0" in self.phase

    def test_discovery_engine_not_started(self):
        post = self.phase.get("post_phase_0", {})
        discovery = post.get("discovery_engine_foundation", {})
        status = discovery.get("status", "not_started")
        assert status in ("not_started", "deferred"), (
            f"discovery_engine_foundation.status must be 'not_started' or 'deferred', got {status!r}"
        )

    def test_auto_trading_out_of_scope(self):
        deferred = self.phase.get("permanently_deferred", {})
        auto_trading = deferred.get("auto_trading", {})
        assert auto_trading.get("status") == "out_of_scope", (
            "auto_trading must be marked out_of_scope in permanently_deferred"
        )

    def test_permanently_deferred_present(self):
        assert "permanently_deferred" in self.phase


# ── Script execution ───────────────────────────────────────────────────────


class TestScriptExecution:

    def test_script_runs_successfully(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"agent_context_check.py exited {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_script_output_contains_advisory_only(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        assert "advisory" in result.stdout.lower()

    def test_script_output_contains_no_auto_trade(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        assert "auto" in result.stdout.lower()

    def test_script_output_contains_current_phase(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        assert "phase" in result.stdout.lower()

    def test_script_output_contains_forbidden_count(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        assert "forbidden" in result.stdout.lower()

    def test_script_exits_nonzero_with_missing_state_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            # Create phase file but not state file
            (tmppath / ".agent").mkdir()
            import yaml
            (tmppath / ".agent" / "phase_status.yaml").write_text(
                yaml.dump({"phase_0": {}, "agent_orchestration_layer": {},
                           "post_phase_0": {}, "permanently_deferred": {}})
            )
            result = subprocess.run(
                [sys.executable, str(SCRIPT)],
                capture_output=True,
                text=True,
                cwd=tmpdir,
            )
            assert result.returncode != 0, (
                "Script should exit nonzero when project_state.yaml is missing"
            )

    def test_script_exits_nonzero_with_missing_phase_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / ".agent").mkdir()
            import yaml
            (tmppath / ".agent" / "project_state.yaml").write_text(
                yaml.dump({
                    "project_name": "test", "mode": "advisory_only",
                    "no_auto_trading": True, "ai_role": "test",
                    "current_phase": "test", "current_step": "test",
                    "completed_steps": ["a"], "next_official_step": "b",
                    "deferred_steps": [], "forbidden_changes": ["x"],
                    "required_test_policy": {}, "output_namespace_policy": {},
                    "role_split": {"gpt": ["a"], "claude": ["b"], "codex": ["c"]},
                })
            )
            result = subprocess.run(
                [sys.executable, str(SCRIPT)],
                capture_output=True,
                text=True,
                cwd=tmpdir,
            )
            assert result.returncode != 0, (
                "Script should exit nonzero when phase_status.yaml is missing"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
