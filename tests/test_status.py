"""
Tests for tools/status.py — read-only production health CLI.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools import status as tool


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Minimal repo layout with the marker file."""
    repo = tmp_path / "stockbot"
    repo.mkdir()
    (repo / "main.py").write_text("# marker\n", encoding="utf-8")
    (repo / "outputs" / "latest").mkdir(parents=True)
    (repo / "outputs" / "policy").mkdir(parents=True)
    (repo / "outputs" / "sandbox" / "discovery").mkdir(parents=True)
    (repo / "outputs" / "portfolio").mkdir(parents=True)
    return repo


def _write_pipeline_status(repo: Path, **overrides) -> Path:
    payload = {
        "generated_at": _iso_now(),
        "run_id": "2026-05-15_daily_official",
        "run_mode": "daily",
        "source": "main",
        "observe_only": True,
        "no_trade": True,
        "success": True,
        "exit_code": 0,
        "steps_attempted": 1,
        "steps_succeeded": 1,
        "steps_skipped": 0,
        "steps_failed": 0,
        "steps": [{"name": "run_portfolio_update", "status": "succeeded", "duration_seconds": 5.0}],
        "errors": [],
        "warnings": [],
        "artifacts_written": [],
        "summary": {"degraded_mode": False, "data_mode": "live"},
    }
    payload.update(overrides)
    path = repo / "outputs" / "latest" / "pipeline_run_status.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_sandbox_status(repo: Path, **overrides) -> Path:
    payload = {
        "generated_at": _iso_now(),
        "run_id": "2026-05-15_daily_sandbox",
        "run_mode": "discovery",
        "observe_only": True,
        "no_trade": True,
        "steps_attempted": 3,
        "steps_succeeded": 3,
        "steps_skipped": 0,
        "steps_failed": 0,
        "steps": [],
        "errors": [],
    }
    payload.update(overrides)
    path = repo / "outputs" / "sandbox" / "discovery" / "sandbox_run_status.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_ai_budget(repo: Path, **overrides) -> Path:
    payload = {
        "generated_at": _iso_now(),
        "enabled": True,
        "blocked": False,
        "warning": False,
        "daily_token_total": 1000,
        "daily_cost_total_usd": 0.10,
        "monthly_cost_total_usd": 1.50,
        "daily_cost_limit_usd": 2.00,
        "monthly_cost_limit_usd": 50.00,
        "warnings": [],
        "summary_line": "within budget",
        "event_count": 5,
        "events": [],
    }
    payload.update(overrides)
    path = repo / "outputs" / "latest" / "ai_budget_summary.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_memo_status(repo: Path, **overrides) -> Path:
    payload = {
        "generated_at": _iso_now(),
        "enabled": False,
        "sent": False,
        "skipped": True,
        "reason": "disabled",
        "recipients_count": 0,
    }
    payload.update(overrides)
    path = repo / "outputs" / "latest" / "memo_delivery_status.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# detect_repo_root
# ---------------------------------------------------------------------------

class TestDetectRepoRoot:
    def test_explicit_root_with_marker(self, fake_repo: Path):
        assert tool.detect_repo_root(fake_repo) == fake_repo.resolve()

    def test_explicit_root_without_marker(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            tool.detect_repo_root(tmp_path)

    def test_default_root_uses_repo_marker(self):
        root = tool.detect_repo_root()
        assert (root / "main.py").exists()


# ---------------------------------------------------------------------------
# probe_pipeline_run_status
# ---------------------------------------------------------------------------

class TestProbePipelineRunStatus:
    def test_missing_artifact_is_fail(self, fake_repo: Path):
        check = tool.probe_pipeline_run_status(fake_repo)
        assert check.severity == tool.SEV_FAIL
        assert "missing or unreadable" in check.message

    def test_fresh_successful_run_is_ok(self, fake_repo: Path):
        _write_pipeline_status(fake_repo)
        check = tool.probe_pipeline_run_status(fake_repo)
        assert check.severity == tool.SEV_OK
        assert check.details["success"] is True
        assert check.details["exit_code"] == 0

    def test_failure_is_fail(self, fake_repo: Path):
        _write_pipeline_status(fake_repo, success=False, exit_code=1, steps_failed=1)
        check = tool.probe_pipeline_run_status(fake_repo)
        assert check.severity == tool.SEV_FAIL
        assert "failure" in check.message

    def test_stale_is_warn(self, fake_repo: Path):
        _write_pipeline_status(fake_repo, generated_at=_iso_hours_ago(48))
        check = tool.probe_pipeline_run_status(fake_repo)
        assert check.severity == tool.SEV_WARN
        assert "old" in check.message

    def test_skip_status_is_info(self, fake_repo: Path):
        _write_pipeline_status(
            fake_repo,
            steps_skipped=1,
            steps_succeeded=0,
            steps=[{"name": "run_portfolio_update", "status": "skipped",
                    "duration_seconds": 0.0, "skip_reason": "idempotent_already_completed"}],
        )
        check = tool.probe_pipeline_run_status(fake_repo)
        assert check.severity == tool.SEV_INFO
        assert "skipped" in check.message
        assert check.details["skip_reason"] == "idempotent_already_completed"

    def test_malformed_json_is_fail(self, fake_repo: Path):
        (fake_repo / "outputs" / "latest" / "pipeline_run_status.json").write_text(
            "{not json", encoding="utf-8",
        )
        check = tool.probe_pipeline_run_status(fake_repo)
        assert check.severity == tool.SEV_FAIL


# ---------------------------------------------------------------------------
# probe_sandbox_run_status
# ---------------------------------------------------------------------------

class TestProbeSandboxRunStatus:
    def test_missing_is_info(self, fake_repo: Path):
        check = tool.probe_sandbox_run_status(fake_repo)
        assert check.severity == tool.SEV_INFO

    def test_fresh_ok(self, fake_repo: Path):
        _write_sandbox_status(fake_repo)
        check = tool.probe_sandbox_run_status(fake_repo)
        assert check.severity == tool.SEV_OK

    def test_failed_step_is_warn(self, fake_repo: Path):
        _write_sandbox_status(fake_repo, steps_failed=1)
        check = tool.probe_sandbox_run_status(fake_repo)
        assert check.severity == tool.SEV_WARN

    def test_stale_is_warn(self, fake_repo: Path):
        _write_sandbox_status(fake_repo, generated_at=_iso_hours_ago(72))
        check = tool.probe_sandbox_run_status(fake_repo)
        assert check.severity == tool.SEV_WARN


# ---------------------------------------------------------------------------
# probe_ai_budget
# ---------------------------------------------------------------------------

class TestProbeAiBudget:
    def test_missing_is_info(self, fake_repo: Path):
        assert tool.probe_ai_budget(fake_repo).severity == tool.SEV_INFO

    def test_within_budget_is_ok(self, fake_repo: Path):
        _write_ai_budget(fake_repo)
        check = tool.probe_ai_budget(fake_repo)
        assert check.severity == tool.SEV_OK

    def test_warning_flag_is_warn(self, fake_repo: Path):
        _write_ai_budget(fake_repo, warning=True)
        assert tool.probe_ai_budget(fake_repo).severity == tool.SEV_WARN

    def test_over_80pct_of_daily_limit_is_warn(self, fake_repo: Path):
        _write_ai_budget(fake_repo, daily_cost_total_usd=1.60, daily_cost_limit_usd=2.00)
        assert tool.probe_ai_budget(fake_repo).severity == tool.SEV_WARN

    def test_blocked_is_fail(self, fake_repo: Path):
        _write_ai_budget(fake_repo, blocked=True)
        assert tool.probe_ai_budget(fake_repo).severity == tool.SEV_FAIL


# ---------------------------------------------------------------------------
# probe_memo_delivery
# ---------------------------------------------------------------------------

class TestProbeMemoDelivery:
    def test_missing_is_info(self, fake_repo: Path):
        assert tool.probe_memo_delivery(fake_repo).severity == tool.SEV_INFO

    def test_disabled_is_info(self, fake_repo: Path):
        _write_memo_status(fake_repo, enabled=False)
        check = tool.probe_memo_delivery(fake_repo)
        assert check.severity == tool.SEV_INFO
        assert "disabled" in check.message

    def test_sent_is_ok(self, fake_repo: Path):
        _write_memo_status(fake_repo, enabled=True, sent=True, skipped=False, reason="sent")
        assert tool.probe_memo_delivery(fake_repo).severity == tool.SEV_OK

    def test_enabled_but_not_sent_is_warn(self, fake_repo: Path):
        _write_memo_status(
            fake_repo, enabled=True, sent=False, skipped=False, reason="smtp_error",
        )
        check = tool.probe_memo_delivery(fake_repo)
        assert check.severity == tool.SEV_WARN
        assert "smtp_error" in check.message


# ---------------------------------------------------------------------------
# probe_registry_artifacts
# ---------------------------------------------------------------------------

class TestProbeRegistryArtifacts:
    def test_empty_outputs_warns_about_required(self, fake_repo: Path):
        check = tool.probe_registry_artifacts(fake_repo)
        assert check.severity == tool.SEV_WARN
        assert check.details["missing_required"]  # non-empty list

    def test_all_required_present(self, fake_repo: Path):
        """Create every non-optional, non-append-only registered artifact."""
        from portfolio_automation.artifacts_registry import REGISTRY, artifact_path
        for art in REGISTRY:
            if art.append_only or art.optional:
                continue
            p = artifact_path(art.name, base_dir=fake_repo / "outputs")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}" if art.format == "json" else "", encoding="utf-8")
        check = tool.probe_registry_artifacts(fake_repo)
        assert check.severity in (tool.SEV_OK, tool.SEV_INFO)
        assert check.details["missing_required"] == []


# ---------------------------------------------------------------------------
# collect_status — overall aggregation
# ---------------------------------------------------------------------------

class TestCollectStatus:
    def test_empty_repo_overall_fail(self, fake_repo: Path):
        report = tool.collect_status(fake_repo)
        assert report.overall_severity == tool.SEV_FAIL  # pipeline_run_status missing
        assert report.severity_counts[tool.SEV_FAIL] >= 1

    def test_healthy_repo_overall_ok_or_info(self, fake_repo: Path):
        _write_pipeline_status(fake_repo)
        _write_sandbox_status(fake_repo)
        _write_ai_budget(fake_repo)
        _write_memo_status(fake_repo, enabled=True, sent=True, skipped=False, reason="sent")
        # Need registry artifacts too to avoid registry WARN.
        from portfolio_automation.artifacts_registry import REGISTRY, artifact_path
        for art in REGISTRY:
            if art.append_only or art.optional:
                continue
            p = artifact_path(art.name, base_dir=fake_repo / "outputs")
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                p.write_text("{}", encoding="utf-8")
        report = tool.collect_status(fake_repo)
        # OK or INFO is acceptable; FAIL/WARN must not appear in this setup.
        assert report.severity_counts[tool.SEV_FAIL] == 0
        assert report.severity_counts[tool.SEV_WARN] == 0

    def test_never_raises(self, fake_repo: Path):
        # Write a deliberately broken artifact and confirm no exception escapes.
        (fake_repo / "outputs" / "latest" / "pipeline_run_status.json").write_text(
            "{}}}", encoding="utf-8",
        )
        report = tool.collect_status(fake_repo)
        # Either FAIL (bad parse) or some other severity, but always returns a report.
        assert isinstance(report, tool.StatusReport)
        assert report.checks  # non-empty


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class TestRenderText:
    def test_includes_overall_severity(self, fake_repo: Path):
        report = tool.collect_status(fake_repo)
        out = tool.render_text(report)
        assert "Portfolio Automation" in out
        assert "Overall:" in out

    def test_verbose_includes_ok_checks(self, fake_repo: Path):
        _write_pipeline_status(fake_repo)
        report = tool.collect_status(fake_repo)
        out = tool.render_text(report, verbose=True)
        # pipeline_run_status is OK; should appear when verbose
        assert "pipeline_run_status" in out

    def test_advisory_disclaimer(self, fake_repo: Path):
        report = tool.collect_status(fake_repo)
        out = tool.render_text(report)
        assert "Advisory only" in out


class TestRenderJson:
    def test_round_trips(self, fake_repo: Path):
        _write_pipeline_status(fake_repo)
        report = tool.collect_status(fake_repo)
        s = tool.render_json(report)
        parsed = json.loads(s)
        assert parsed["overall_severity"] in (tool.SEV_OK, tool.SEV_WARN, tool.SEV_FAIL, tool.SEV_INFO)
        assert parsed["advisory_only"] is True
        assert parsed["no_trade"] is True
        assert "checks" in parsed
        assert isinstance(parsed["checks"], list)


class TestRenderMarkdown:
    def test_has_required_sections(self, fake_repo: Path):
        report = tool.collect_status(fake_repo)
        out = tool.render_markdown(report)
        assert out.startswith("# Portfolio Automation")
        assert "## Checks" in out
        assert "Advisory only" in out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_text_default_exits_zero(self, fake_repo: Path, capsys: pytest.CaptureFixture):
        rc = tool.main(["--repo-root", str(fake_repo)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Overall" in out

    def test_json_format(self, fake_repo: Path, capsys: pytest.CaptureFixture):
        rc = tool.main(["--repo-root", str(fake_repo), "--format", "json"])
        out = capsys.readouterr().out
        assert rc == 0
        parsed = json.loads(out)
        assert "overall_severity" in parsed

    def test_md_format(self, fake_repo: Path, capsys: pytest.CaptureFixture):
        rc = tool.main(["--repo-root", str(fake_repo), "--format", "md"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "# Portfolio Automation" in out

    def test_strict_exits_one_on_warn(self, fake_repo: Path, capsys: pytest.CaptureFixture):
        # Empty repo → at least one FAIL/WARN
        rc = tool.main(["--repo-root", str(fake_repo), "--strict"])
        assert rc == 1

    def test_strict_exits_zero_when_clean(self, fake_repo: Path, capsys: pytest.CaptureFixture):
        _write_pipeline_status(fake_repo)
        _write_sandbox_status(fake_repo)
        _write_ai_budget(fake_repo)
        _write_memo_status(fake_repo, enabled=True, sent=True, skipped=False, reason="sent")
        from portfolio_automation.artifacts_registry import REGISTRY, artifact_path
        for art in REGISTRY:
            if art.append_only or art.optional:
                continue
            p = artifact_path(art.name, base_dir=fake_repo / "outputs")
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                p.write_text("{}", encoding="utf-8")
        rc = tool.main(["--repo-root", str(fake_repo), "--strict"])
        assert rc == 0

    def test_missing_marker_exit_two(self, tmp_path: Path, capsys: pytest.CaptureFixture):
        rc = tool.main(["--repo-root", str(tmp_path)])
        assert rc == 2
