"""
Tests for gui/production_health_page.py.

Covers the data-collection layer only.  Streamlit rendering is not exercised
here — it has no testable invariants beyond "imports cleanly" and the
project test suite already ignores Streamlit-runtime tests.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gui.production_health_page import (
    SEV_FAIL,
    SEV_INFO,
    SEV_OK,
    SEV_WARN,
    collect_production_health,
    overall_severity,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "stockbot"
    repo.mkdir()
    (repo / "main.py").write_text("# marker\n", encoding="utf-8")
    (repo / "outputs").mkdir()
    return repo


def _write_pipeline_status(repo: Path, **overrides) -> None:
    """Materialize a minimal pipeline_run_status.json for the status probe."""
    payload = {
        "generated_at": "2026-05-15T15:00:00+00:00",
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
        "steps": [{"name": "run_portfolio_update", "status": "succeeded",
                   "duration_seconds": 5.0}],
        "errors": [],
        "warnings": [],
        "artifacts_written": [],
        "summary": {},
    }
    payload.update(overrides)
    out = repo / "outputs" / "latest"
    out.mkdir(parents=True, exist_ok=True)
    (out / "pipeline_run_status.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Module import — independent of Streamlit
# ---------------------------------------------------------------------------

class TestModuleImport:
    def test_collect_function_exists(self):
        assert callable(collect_production_health)

    def test_overall_severity_function_exists(self):
        assert callable(overall_severity)

    def test_render_importable_without_streamlit_running(self):
        # Importing the rendering function must not call streamlit.  It is
        # only imported on demand by gui/app.py at page-render time.
        from gui.production_health_page import render_production_health_page
        assert callable(render_production_health_page)


# ---------------------------------------------------------------------------
# collect_production_health
# ---------------------------------------------------------------------------

class TestCollect:
    def test_advisory_flags_hardcoded(self, fake_repo: Path):
        h = collect_production_health(fake_repo)
        assert h["advisory_only"] is True
        assert h["no_trade"] is True
        assert h["repo_root"] == str(fake_repo)

    def test_returns_all_top_level_keys(self, fake_repo: Path):
        h = collect_production_health(fake_repo)
        assert set(h.keys()) >= {
            "advisory_only", "no_trade", "repo_root",
            "status", "smoke", "env", "registry",
        }

    def test_status_section_populates_from_artifact(self, fake_repo: Path):
        _write_pipeline_status(fake_repo)
        h = collect_production_health(fake_repo)
        status = h["status"]
        assert "error" not in status
        # tools.status surfaces a pipeline_run_status check among its results
        names = {c.get("name") for c in status.get("checks", [])}
        assert "pipeline_run_status" in names

    def test_smoke_section_populates(self, fake_repo: Path):
        _write_pipeline_status(fake_repo)
        h = collect_production_health(fake_repo)
        assert "error" not in h["smoke"]
        # smoke reports per-artifact results; structure check only
        assert "results" in h["smoke"]
        assert "severity_counts" in h["smoke"]

    def test_env_section_populates(self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch):
        # Make sure check_state runs cleanly even if FMP_API_KEY is unset.
        monkeypatch.delenv("FMP_API_KEY", raising=False)
        h = collect_production_health(fake_repo)
        env = h["env"]
        assert "error" not in env
        assert "summary" in env
        assert "groups" in env

    def test_registry_section_populates(self, fake_repo: Path):
        h = collect_production_health(fake_repo)
        reg = h["registry"]
        assert "error" not in reg
        assert reg["total"] > 0
        # by_namespace is a dict with at least 'latest' present
        assert "latest" in reg["by_namespace"]
        # entries is a list of dicts with name + namespace
        assert all("name" in e and "namespace" in e for e in reg["entries"])

    def test_never_raises_when_repo_root_invalid(self, tmp_path: Path):
        # No main.py marker — status / smoke fall back to error sections,
        # but collect_production_health itself must not raise.
        h = collect_production_health(tmp_path)
        # status / smoke probes both call detect_repo_root which raises;
        # the _safe wrapper turns that into an "error" key.
        assert "status" in h
        assert "smoke" in h
        assert isinstance(h["status"], dict)
        assert isinstance(h["smoke"], dict)


# ---------------------------------------------------------------------------
# overall_severity
# ---------------------------------------------------------------------------

class TestOverallSeverity:
    def test_ok_when_all_ok(self, fake_repo: Path):
        # Empty registry view + sections without overall_severity defaults to OK.
        h = {
            "status": {"overall_severity": SEV_OK},
            "smoke": {"overall_severity": SEV_OK},
            "env": {"summary": {"required_missing": 0}},
        }
        assert overall_severity(h) == SEV_OK

    def test_worst_wins_status_warn_smoke_ok(self):
        h = {
            "status": {"overall_severity": SEV_WARN},
            "smoke": {"overall_severity": SEV_OK},
            "env": {"summary": {"required_missing": 0}},
        }
        assert overall_severity(h) == SEV_WARN

    def test_fail_dominates(self):
        h = {
            "status": {"overall_severity": SEV_WARN},
            "smoke": {"overall_severity": SEV_FAIL},
            "env": {"summary": {"required_missing": 0}},
        }
        assert overall_severity(h) == SEV_FAIL

    def test_missing_required_env_promotes_to_warn(self):
        h = {
            "status": {"overall_severity": SEV_OK},
            "smoke": {"overall_severity": SEV_OK},
            "env": {"summary": {"required_missing": 1}},
        }
        assert overall_severity(h) == SEV_WARN

    def test_missing_required_env_does_not_downgrade_fail(self):
        h = {
            "status": {"overall_severity": SEV_FAIL},
            "smoke": {"overall_severity": SEV_OK},
            "env": {"summary": {"required_missing": 5}},
        }
        assert overall_severity(h) == SEV_FAIL

    def test_handles_missing_sections(self):
        # Defensive: pass an empty dict and it should not raise.
        assert overall_severity({}) == SEV_OK
