"""Tests for gui_v2/data/today.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gui_v2.data.today import collect_today_view


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "stockbot"
    repo.mkdir()
    (repo / "main.py").write_text("# marker\n", encoding="utf-8")
    (repo / "outputs" / "latest").mkdir(parents=True)
    return repo


def _write_pipeline_status(repo: Path, **overrides) -> None:
    payload = {
        "generated_at": "2026-05-15T15:00:00+00:00",
        "run_id": "2026-05-15_daily_official",
        "run_mode": "daily",
        "success": True,
        "exit_code": 0,
        "summary": {},
    }
    payload.update(overrides)
    (repo / "outputs" / "latest" / "pipeline_run_status.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


def _write_decision_plan(repo: Path, decisions: list[dict]) -> None:
    (repo / "outputs" / "latest" / "decision_plan.json").write_text(
        json.dumps({"generated_at": "x", "total_decisions": len(decisions),
                    "decisions": decisions, "observe_only": True}),
        encoding="utf-8",
    )


def _write_memo(repo: Path, body: str) -> None:
    (repo / "outputs" / "latest" / "daily_memo.md").write_text(body, encoding="utf-8")


class TestShape:
    def test_returns_top_level_keys(self, fake_repo: Path):
        view = collect_today_view(fake_repo)
        assert set(view.keys()) >= {
            "advisory_only", "no_trade",
            "header", "decisions", "capital_actions", "risk_focus",
            "top_movers", "memo_html",
        }
        assert view["advisory_only"] is True
        assert view["no_trade"] is True

    def test_empty_repo_does_not_raise(self, fake_repo: Path):
        view = collect_today_view(fake_repo)
        assert view["decisions"] == []
        assert view["memo_html"] == ""


class TestHeader:
    def test_header_pulls_run_id_and_success(self, fake_repo: Path):
        _write_pipeline_status(fake_repo, run_id="2026-05-15_daily_official",
                               success=True)
        view = collect_today_view(fake_repo)
        assert view["header"]["run_id"] == "2026-05-15_daily_official"
        assert view["header"]["success"] is True


class TestDecisions:
    def test_top_5_only(self, fake_repo: Path):
        _write_decision_plan(fake_repo, [
            {"symbol": f"S{i}", "decision": "BUY", "priority": float(i),
             "urgency": "high", "source": "test", "reason": f"reason {i}"}
            for i in range(10)
        ])
        view = collect_today_view(fake_repo)
        assert len(view["decisions"]) == 5


class TestCapitalActions:
    def test_totals_grouped_by_action(self, fake_repo: Path):
        _write_decision_plan(fake_repo, [
            {"symbol": "A", "decision": "SELL", "recommended_amount": 100},
            {"symbol": "B", "decision": "SELL", "recommended_amount": 50},
            {"symbol": "C", "decision": "BUY",  "recommended_amount": 200},
            {"symbol": "D", "decision": "HOLD"},
        ])
        view = collect_today_view(fake_repo)
        ca = view["capital_actions"]
        assert ca["SELL"] == 150
        assert ca["BUY"] == 200


class TestMemo:
    def test_markdown_rendered_to_html(self, fake_repo: Path):
        _write_memo(fake_repo, "# Hello\n\nWorld")
        view = collect_today_view(fake_repo)
        assert "<h1>" in view["memo_html"]
        assert "Hello" in view["memo_html"]

    def test_missing_memo_is_empty(self, fake_repo: Path):
        view = collect_today_view(fake_repo)
        assert view["memo_html"] == ""
