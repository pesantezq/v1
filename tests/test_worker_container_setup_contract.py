"""Task 8 contract tests: operator provisioning runbook + setup script +
no-side-effect readiness guard."""
from pathlib import Path
import subprocess, sys

S = Path("scripts/worker_container_setup.sh").read_text()


def test_setup_covers_required_steps():
    for kw in ("useradd", "subuid", "subgid", "enable-linger", "podman build",
               "sha256", "stockbot-worker"):
        assert kw in S, f"keyword not found in setup script: {kw!r}"


def test_setup_does_not_auto_execute_dangerously():
    # must be guarded behind an explicit subcommand / main guard, not run on source
    assert ('"$1"' in S) or ('case "$1"' in S) or ('if [ "$#"' in S), (
        "setup script is not guarded by a subcommand dispatcher"
    )


def test_readiness_probe_has_no_side_effects(tmp_path, monkeypatch):
    # readiness must not mutate git refs / worktrees / decision artifacts
    import json
    from portfolio_automation.operator_worker_readiness import operator_worker_readiness
    (tmp_path / "config.json").write_text(
        json.dumps({"operator_control": {"worker_container": {"enabled": False}}})
    )
    dp = tmp_path / "outputs" / "latest"
    dp.mkdir(parents=True)
    (dp / "decision_plan.json").write_text('{"x":1}')
    before = (dp / "decision_plan.json").read_text()
    operator_worker_readiness(tmp_path)
    assert (dp / "decision_plan.json").read_text() == before
