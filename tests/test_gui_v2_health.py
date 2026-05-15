"""Tests for gui_v2/data/health.py."""
from __future__ import annotations

from pathlib import Path

import pytest

from gui_v2.data.health import (
    collect_health_view,
    overall_severity,
    SEV_OK, SEV_INFO, SEV_WARN, SEV_FAIL,
)


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "stockbot"
    repo.mkdir()
    (repo / "main.py").write_text("# marker\n", encoding="utf-8")
    (repo / "outputs").mkdir()
    return repo


class TestCollect:
    def test_returns_top_level_keys(self, fake_repo: Path):
        h = collect_health_view(fake_repo)
        assert set(h.keys()) >= {
            "advisory_only", "no_trade",
            "status", "smoke", "env", "registry",
        }
        assert h["advisory_only"] is True
        assert h["no_trade"] is True

    def test_never_raises_with_empty_repo(self, fake_repo: Path):
        h = collect_health_view(fake_repo)
        for key in ("status", "smoke", "env", "registry"):
            assert isinstance(h[key], dict)


class TestOverallSeverity:
    def test_all_ok(self):
        h = {
            "status": {"overall_severity": SEV_OK},
            "smoke": {"overall_severity": SEV_OK},
            "env": {"summary": {"required_missing": 0}},
        }
        assert overall_severity(h) == SEV_OK

    def test_worst_wins(self):
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
            "env": {"summary": {"required_missing": 1}},
        }
        assert overall_severity(h) == SEV_FAIL
