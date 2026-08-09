"""Health-artifact and CLI tests.

Two things are under test:

* the export lane's health grading (GREEN/AMBER/RED), which is the
  Analysis+Health pairing CLAUDE.md requires for any new producer; and
* the CLI, exercised end to end against the REAL production allowlist in a
  throwaway repo — so the entrypoint is covered without touching /opt/stockbot.
"""
from __future__ import annotations

import json

import pytest

from portfolio_automation.agent_export.health import (
    HEALTH_FILENAME, build_export_health, load_export_health, run_agent_export_health,
)
from portfolio_automation.agent_export.manifest import AMBER, GREEN, RED
from tests.agent_export_fixtures import (
    TEST_ALLOWLIST, build, make_fake_repo, make_production_shaped_repo,
)

NOW = "2026-08-09T00:00:00+00:00"


def _now(iso: str = NOW):
    from datetime import datetime
    return datetime.fromisoformat(iso)


def _health(root, **kwargs):
    kwargs.setdefault("allowlist", TEST_ALLOWLIST)
    kwargs.setdefault("now", _now())
    return build_export_health(root, **kwargs)


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


def test_no_snapshot_yet_is_amber_not_red(tmp_path):
    """A lane that has never run is degraded, not broken.

    Grading a fresh install RED would train the operator to ignore the signal.
    """
    root = make_fake_repo(tmp_path)
    health = _health(root)
    assert health["status"] == AMBER
    assert any("no_snapshot_yet" in w for w in health["warnings"])
    assert health["errors"] == []
    assert health["snapshot_count"] == 0


def test_valid_current_snapshot_is_green(tmp_path):
    root = make_fake_repo(tmp_path)
    result = build(root, created_at=NOW)
    health = _health(root)

    assert health["status"] == GREEN
    assert health["latest_snapshot_id"] == result.snapshot_id
    assert health["hash_validation"] == "pass"
    assert health["schema_validation"] == "pass"
    assert health["production_git_sha"] == result.manifest["production"]["production_git_sha"]
    assert health["production_run_id"] == "2026-08-08_daily_official"
    assert health["required_artifacts_present"] == health["required_artifacts_expected"]
    assert health["errors"] == []


def test_stale_snapshot_is_amber(tmp_path):
    root = make_fake_repo(tmp_path)
    build(root, created_at="2026-08-01T00:00:00+00:00")
    health = _health(root)  # eight days later

    assert health["status"] == AMBER
    assert any("snapshot_stale" in w for w in health["warnings"])
    assert health["latest_snapshot_age_hours"] > 36


def test_degraded_snapshot_propagates_amber(tmp_path):
    """An AMBER snapshot must not be graded GREEN by the lane check."""
    root = make_fake_repo(tmp_path, include_optional=False)
    build(root, created_at=NOW)
    health = _health(root)

    assert health["status"] == AMBER
    assert health["snapshot_health_status"] == AMBER
    assert any("optional_artifact_missing" in w for w in health["warnings"])


def test_tampered_snapshot_is_red(tmp_path):
    """Verifiable corruption — and only that — is RED."""
    root = make_fake_repo(tmp_path)
    result = build(root, created_at=NOW)
    target = result.snapshot_dir / "artifacts" / "core_decision" / "decision_plan.json"
    data = bytearray(target.read_bytes())
    data[0:1] = b" "
    target.write_bytes(bytes(data))

    health = _health(root)
    assert health["status"] == RED
    assert health["hash_validation"] == "fail"
    assert any("validation_failed" in e for e in health["errors"])


def test_smuggled_file_in_snapshot_is_red(tmp_path):
    root = make_fake_repo(tmp_path)
    result = build(root, created_at=NOW)
    (result.snapshot_dir / "artifacts" / "core_decision" / "surprise.json").write_text(
        "{}", encoding="utf-8")

    health = _health(root)
    assert health["status"] == RED
    assert any("unexpected file" in e for e in health["errors"])


def test_missing_pointer_target_degrades_but_still_checks_a_snapshot(tmp_path):
    """A lost pointer must degrade the grade, not blind the check."""
    root = make_fake_repo(tmp_path)
    build(root, created_at=NOW)
    pointer_path = root / "outputs" / "agent_export" / "latest.json"
    payload = json.loads(pointer_path.read_text(encoding="utf-8"))
    payload["snapshot_id"] = "does-not-exist"
    pointer_path.write_text(json.dumps(payload), encoding="utf-8")

    health = _health(root)
    assert any("pointer_target_missing" in w for w in health["warnings"])
    assert health["latest_snapshot_id"] is not None
    assert health["schema_validation"] == "pass"


def test_health_never_mutates_the_snapshot(tmp_path):
    root = make_fake_repo(tmp_path)
    result = build(root, created_at=NOW)

    def fingerprint():
        return {
            str(p): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
            for p in sorted(result.snapshot_dir.rglob("*")) if p.is_file()
        }

    before = fingerprint()
    _health(root)
    _health(root)
    assert fingerprint() == before


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_health_is_written_to_the_policy_namespace(tmp_path):
    root = make_fake_repo(tmp_path)
    build(root, created_at=NOW)
    payload = run_agent_export_health(
        root, now=_now(), allowlist=TEST_ALLOWLIST)

    written = root / "outputs" / "policy" / HEALTH_FILENAME
    assert written.is_file(), "health artifact must land in outputs/policy/"
    on_disk = json.loads(written.read_text(encoding="utf-8"))
    assert on_disk["status"] == payload["status"] == GREEN
    assert on_disk["observe_only"] is True
    assert on_disk["feeds_decision_engine"] is False
    assert load_export_health(root)["latest_snapshot_id"] == payload["latest_snapshot_id"]


def test_health_can_be_computed_without_writing(tmp_path):
    root = make_fake_repo(tmp_path)
    run_agent_export_health(root, now=_now(), allowlist=TEST_ALLOWLIST, write=False)
    assert not (root / "outputs" / "policy" / HEALTH_FILENAME).exists()


# ---------------------------------------------------------------------------
# CLI — exercised against the real production allowlist, hermetically
# ---------------------------------------------------------------------------


@pytest.fixture
def cli():
    import importlib.util
    from pathlib import Path as _Path
    spec = importlib.util.spec_from_file_location(
        "_run_agent_export_cli",
        _Path(__file__).resolve().parent.parent / "scripts" / "run_agent_export.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_builds_validates_and_reports(tmp_path, cli, capsys):
    root = make_production_shaped_repo(tmp_path)
    assert cli.main(["--root", str(root), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["mode"] == "build"
    assert payload["export_health_status"] in {GREEN, AMBER}
    assert (root / "outputs" / "policy" / HEALTH_FILENAME).is_file()


def test_cli_validate_only_verifies_without_building(tmp_path, cli, capsys):
    root = make_production_shaped_repo(tmp_path)
    assert cli.main(["--root", str(root)]) == 0
    capsys.readouterr()

    assert cli.main(["--root", str(root), "--validate-only", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "validate"
    assert payload["ok"] is True


def test_cli_dry_run_writes_no_snapshot(tmp_path, cli, capsys):
    root = make_production_shaped_repo(tmp_path)
    assert cli.main(["--root", str(root), "--dry-run", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    from portfolio_automation.agent_export.builder import list_snapshots
    assert list_snapshots(root) == []
    assert not (root / "outputs" / "policy" / HEALTH_FILENAME).exists()


def test_cli_refuses_an_incomplete_run_with_exit_1(tmp_path, cli, capsys):
    root = make_production_shaped_repo(tmp_path, run_status="running")
    assert cli.main(["--root", str(root), "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"] == "SnapshotBuildError"
    from portfolio_automation.agent_export.builder import list_snapshots
    assert list_snapshots(root) == []


def test_cli_rejects_contradictory_flags(tmp_path, cli):
    root = make_production_shaped_repo(tmp_path)
    assert cli.main(["--root", str(root), "--dry-run", "--validate-only"]) == 2
    assert cli.main(["--root", str(root), "--snapshot-id", "x"]) == 2


def test_cli_validate_only_with_no_snapshot_fails_closed(tmp_path, cli):
    root = make_production_shaped_repo(tmp_path)
    assert cli.main(["--root", str(root), "--validate-only"]) == 1
