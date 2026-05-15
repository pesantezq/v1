"""Tests for gui_v2/data/health.py."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
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


class TestAiCostTrend:
    def test_unavailable_when_missing(self, fake_repo: Path):
        h = collect_health_view(fake_repo)
        assert h["ai_cost_trend"]["available"] is False

    def test_aggregates_by_day(self, fake_repo: Path):
        policy = fake_repo / "outputs" / "policy"
        policy.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        yesterday = (now - timedelta(days=1)).date().isoformat()
        events = [
            {"timestamp": now.isoformat(), "estimated_cost_usd": 0.10,
             "total_tokens": 100, "task_name": "x", "provider": "y", "model": "z"},
            {"timestamp": now.isoformat(), "estimated_cost_usd": 0.05,
             "total_tokens": 50,  "task_name": "x", "provider": "y", "model": "z"},
            {"timestamp": (now - timedelta(days=1)).isoformat(),
             "estimated_cost_usd": 0.20, "total_tokens": 200,
             "task_name": "x", "provider": "y", "model": "z"},
        ]
        (policy / "ai_usage_events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n",
            encoding="utf-8",
        )
        trend = collect_health_view(fake_repo)["ai_cost_trend"]
        assert trend["available"] is True
        # Pull the two day buckets we care about
        by_date = {d["date"]: d for d in trend["days"]}
        assert by_date[today]["cost_usd"] == pytest.approx(0.15)
        assert by_date[today]["event_count"] == 2
        assert by_date[yesterday]["cost_usd"] == pytest.approx(0.20)
        # Totals
        assert trend["total_cost_usd"] == pytest.approx(0.35)
        assert trend["total_tokens"] == 350

    def test_ignores_events_outside_window(self, fake_repo: Path):
        policy = fake_repo / "outputs" / "policy"
        policy.mkdir(parents=True, exist_ok=True)
        # 40 days ago should NOT count (default window is 30)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        (policy / "ai_usage_events.jsonl").write_text(
            json.dumps({"timestamp": old_ts, "estimated_cost_usd": 1000.0,
                        "total_tokens": 1000000}) + "\n",
            encoding="utf-8",
        )
        trend = collect_health_view(fake_repo)["ai_cost_trend"]
        assert trend["total_cost_usd"] == 0.0

    def test_handles_malformed_lines_gracefully(self, fake_repo: Path):
        policy = fake_repo / "outputs" / "policy"
        policy.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        (policy / "ai_usage_events.jsonl").write_text(
            "{not json\n"
            + json.dumps({"timestamp": now.isoformat(), "estimated_cost_usd": 0.5,
                          "total_tokens": 100}) + "\n"
            + "\n"
            + "{}\n",
            encoding="utf-8",
        )
        trend = collect_health_view(fake_repo)["ai_cost_trend"]
        assert trend["available"] is True
        assert trend["total_cost_usd"] == pytest.approx(0.5)

    def test_series_length_matches_window(self, fake_repo: Path):
        # Even empty log returns a stable 30-element series — important so
        # the chart never wobbles in width across pages.
        policy = fake_repo / "outputs" / "policy"
        policy.mkdir(parents=True, exist_ok=True)
        (policy / "ai_usage_events.jsonl").write_text("", encoding="utf-8")
        trend = collect_health_view(fake_repo)["ai_cost_trend"]
        assert trend["available"] is True
        assert len(trend["days"]) == 30
