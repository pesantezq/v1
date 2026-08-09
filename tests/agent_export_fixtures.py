"""Hermetic fixtures for the Agent Lab export tests.

Every fixture builds a self-contained fake repo under ``tmp_path``: its own
``outputs/`` tree, its own git checkout, its own run manifest. Nothing here
reads ``/opt/stockbot`` or the real production artifacts, so the suite is
portable to the operator laptop and to a future Agent Lab machine — which is
exactly the portability property this subsystem is supposed to model.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from portfolio_automation.agent_export.allowlist import AllowlistEntry

#: A deliberately tiny allowlist. Tests inject this instead of the production
#: one so they assert the *mechanism* rather than the current artifact census —
#: adding an artifact to production must not break the security tests.
TEST_ALLOWLIST: tuple[AllowlistEntry, ...] = (
    AllowlistEntry("decision_plan", "outputs/latest/decision_plan.json",
                   "core_decision", required=True, producer="decision_engine"),
    AllowlistEntry("run_manifest", "outputs/policy/run_manifest.json",
                   "governance", required=True, producer="run_manifest"),
    AllowlistEntry("daily_run_status", "outputs/latest/daily_run_status.json",
                   "health", required=True, producer="daily_run_status"),
    AllowlistEntry("artifact_registry_status", "outputs/latest/artifact_registry_status.json",
                   "governance", required=True, producer="artifact_registry"),
    AllowlistEntry("confidence_calibration", "outputs/latest/confidence_calibration.json",
                   "outcome_learning", producer="confidence_calibration"),
)

OPTIONAL_NAME = "confidence_calibration"
OPTIONAL_PATH = "outputs/latest/confidence_calibration.json"
REQUIRED_PATH = "outputs/latest/decision_plan.json"

FIXED_NOW = "2026-08-09T00:00:00+00:00"


def _run_git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def init_git_repo(root: Path) -> str:
    """Initialise a throwaway checkout with one commit. Returns the full SHA.

    Config is set locally (never ``--global``) so running the suite cannot touch
    the developer's or the VPS's git configuration.
    """
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "tests@example.invalid")
    _run_git(root, "config", "user.name", "Agent Export Tests")
    _run_git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("fixture repo\n", encoding="utf-8")
    _run_git(root, "add", "README.md")
    _run_git(root, "commit", "-q", "-m", "init")
    return _run_git(root, "rev-parse", "HEAD")


def add_commit(root: Path, filename: str, content: str) -> str:
    """Add one more commit on top; returns its SHA. Used for ahead/behind tests."""
    (root / filename).write_text(content, encoding="utf-8")
    _run_git(root, "add", filename)
    _run_git(root, "commit", "-q", "-m", f"add {filename}")
    return _run_git(root, "rev-parse", "HEAD")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def make_fake_repo(
    tmp_path: Path,
    *,
    run_status: str = "complete",
    include_optional: bool = True,
    include_required: bool = True,
    registry_status: str = "green",
    daily_status: str = "ok",
    with_git: bool = True,
    run_id: str = "2026-08-08_daily_official",
) -> Path:
    """Build a fake production repo whose last run is ready to export.

    The knobs exist so each failure mode is produced by *removing or changing one
    thing* from a known-good baseline, rather than by hand-assembling a broken
    tree — which keeps the tests honest about what actually caused the failure.
    """
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)

    sha = init_git_repo(root) if with_git else "0" * 40

    latest = root / "outputs" / "latest"
    policy = root / "outputs" / "policy"
    latest.mkdir(parents=True, exist_ok=True)
    policy.mkdir(parents=True, exist_ok=True)

    if include_required:
        write_json(latest / "decision_plan.json", {
            "schema_version": "1", "observe_only": True, "run_id": run_id,
            "generated_at": "2026-08-08T09:10:00+00:00",
            "decisions": [{"symbol": "AAA", "action": "HOLD"}],
        })

    write_json(latest / "daily_run_status.json", {
        "schema_version": "1", "observe_only": True,
        "overall_status": daily_status, "required_missing_count": 0,
        "generated_at": "2026-08-08T09:13:00+00:00",
    })
    write_json(latest / "artifact_registry_status.json", {
        "schema_version": "1", "observe_only": True,
        "overall_status": registry_status,
        "counts": {"total": 5, "present": 5, "missing_required": 0},
        "generated_at": "2026-08-08T09:14:00+00:00",
    })
    if include_optional:
        write_json(latest / "confidence_calibration.json", {
            "schema_version": "1", "observe_only": True, "buckets": [],
            "generated_at": "2026-08-08T09:12:00+00:00",
        })

    write_json(policy / "run_manifest.json", {
        "schema_version": "1", "artifact_type": "run_manifest", "observe_only": True,
        "run_id": run_id,
        "started_at": "2026-08-08T09:02:00+00:00",
        "completed_at": "2026-08-08T09:14:00+00:00",
        "data_as_of": "2026-08-08T09:02:00+00:00",
        "source_commit": sha[:8],
        "config_hash": "c" * 64,
        "pipeline_mode": "daily",
        "runtime": {"python": "3.12.3", "platform": "Linux", "host": "fixture"},
        "status": run_status,
        "failure_stage": None,
    })
    return root


def build(root: Path, *, created_at: str = FIXED_NOW, **kwargs):
    """Invoke the builder with the test allowlist and a fixed clock."""
    from portfolio_automation.agent_export.builder import build_agent_snapshot
    kwargs.setdefault("allowlist", TEST_ALLOWLIST)
    kwargs.setdefault("registry", {})
    return build_agent_snapshot(root, created_at=created_at, **kwargs)


def snapshot_dir_of(root: Path, snapshot_id: str) -> Path:
    return root / "outputs" / "agent_export" / "snapshots" / snapshot_id


def make_production_shaped_repo(tmp_path: Path, **kwargs) -> Path:
    """A fake repo carrying every artifact the PRODUCTION allowlist marks required.

    Derived from ``ARTIFACT_ALLOWLIST`` rather than hardcoded, so adding a
    required artifact to production automatically extends this fixture instead
    of silently breaking the end-to-end CLI tests. Used where a test must
    exercise the real allowlist (the CLI) while staying hermetic.
    """
    from portfolio_automation.agent_export.allowlist import ARTIFACT_ALLOWLIST

    root = make_fake_repo(tmp_path, **kwargs)
    for entry in ARTIFACT_ALLOWLIST:
        if not entry.required:
            continue
        path = root / entry.source_path
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".json":
            write_json(path, {
                "schema_version": "1", "observe_only": True,
                "generated_at": "2026-08-08T09:12:00+00:00",
                "fixture_for": entry.logical_name,
            })
        else:
            path.write_text(f"# fixture: {entry.logical_name}\n", encoding="utf-8")
    return root
