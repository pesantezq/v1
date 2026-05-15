"""Tests for the three stub data collectors."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from gui_v2.data.portfolio import collect_portfolio_stub
from gui_v2.data.research import collect_research_stub
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
