"""Tests for the three stub data collectors."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from gui_v2.data.portfolio import collect_portfolio_stub
from gui_v2.data.research import collect_research_stub, collect_research_view
from gui_v2.data.operations import collect_operations_stub


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "stockbot"
    repo.mkdir()
    (repo / "main.py").write_text("# marker\n", encoding="utf-8")
    (repo / "outputs" / "portfolio").mkdir(parents=True)
    (repo / "outputs" / "sandbox" / "discovery").mkdir(parents=True)
    (repo / "data").mkdir()
    return repo


class TestPortfolioStub:
    def test_empty_repo_returns_defaults(self, fake_repo: Path):
        v = collect_portfolio_stub(fake_repo)
        assert v["advisory_only"] is True
        assert v["available"] is False

    def test_reads_snapshot(self, fake_repo: Path):
        (fake_repo / "outputs" / "portfolio" / "portfolio_snapshot.json").write_text(
            json.dumps({
                "generated_at": "2026-05-15T15:00:00+00:00",
                "total_value": 7745.68,
                "cash_available": 464.16,
            }),
            encoding="utf-8",
        )
        v = collect_portfolio_stub(fake_repo)
        assert v["available"] is True
        assert v["total_value"] == 7745.68
        assert v["cash_available"] == 464.16


class TestResearchStub:
    def test_empty_repo_returns_zero_counts(self, fake_repo: Path):
        v = collect_research_stub(fake_repo)
        assert v["advisory_only"] is True
        assert v["counts"]["emerging"] == 0
        assert v["counts"]["rejected"] == 0

    def test_reads_counts(self, fake_repo: Path):
        (fake_repo / "outputs" / "sandbox" / "discovery" / "emerging_candidates.json").write_text(
            json.dumps({"candidates": [{"ticker": "AAA"}, {"ticker": "BBB"}]}),
            encoding="utf-8",
        )
        v = collect_research_stub(fake_repo)
        assert v["counts"]["emerging"] == 2


class TestResearchFullView:
    def test_returns_stub_shape_plus_auto_promotion(self, fake_repo: Path):
        v = collect_research_view(fake_repo)
        # Stub keys preserved
        assert v["advisory_only"] is True
        assert "counts" in v
        # New auto_promotion block present (even if unavailable)
        assert "auto_promotion" in v
        assert isinstance(v["auto_promotion"], dict)

    def test_empty_repo_auto_promotion_unavailable(self, fake_repo: Path):
        v = collect_research_view(fake_repo)
        assert v["auto_promotion"].get("available") is False

    def test_with_promotion_artifact_present(self, fake_repo: Path):
        # Materialize a minimal automatic_promotion_candidates.json that
        # load_automatic_promotion_data will consider "available"
        ap_path = (
            fake_repo / "outputs" / "sandbox" / "discovery"
            / "automatic_promotion_candidates.json"
        )
        ap_path.write_text(json.dumps({
            "available": True,
            "generated_at": "2026-05-15T15:00:00+00:00",
            "run_mode": "discovery",
            "run_id": "x",
            "observe_only": True,
            "no_trade": True,
            "not_recommendation": True,
            "discovery_only": True,
            "sandbox_only": True,
            "no_portfolio_mutation": True,
            "no_watchlist_mutation": True,
            "no_allocation_policy_change": True,
            "no_decision_override": True,
            "decision_count": 0,
            "decisions": [],
        }), encoding="utf-8")
        v = collect_research_view(fake_repo)
        assert v["auto_promotion"].get("available") is True
        assert v["auto_promotion"].get("run_mode") == "discovery"


class TestOperationsStub:
    def test_empty_repo_returns_no_rows(self, fake_repo: Path):
        v = collect_operations_stub(fake_repo)
        assert v["advisory_only"] is True
        assert v["recent_runs"] == []

    def test_reads_sqlite_run_history(self, fake_repo: Path):
        db = fake_repo / "data" / "portfolio.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE run_history(run_id TEXT, status TEXT, started_at TEXT, completed_at TEXT)"
        )
        conn.execute(
            "INSERT INTO run_history VALUES (?, ?, ?, ?)",
            ("2026-05-15_daily", "completed", "2026-05-15T09:00", "2026-05-15T09:05"),
        )
        conn.commit()
        conn.close()
        v = collect_operations_stub(fake_repo)
        assert len(v["recent_runs"]) == 1
        assert v["recent_runs"][0]["run_id"] == "2026-05-15_daily"
