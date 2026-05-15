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


# Decision Center sections migrated from gui/page_decision_center


def _write_validation(repo: Path, validations: list[dict], **overrides) -> None:
    payload = {
        "generated_at": "x",
        "observe_only": True,
        "available": True,
        "total_validated": len(validations),
        "aligned_count": sum(1 for v in validations if v.get("validation_status") == "aligned"),
        "caution_count": sum(1 for v in validations if v.get("validation_status") == "caution"),
        "contradiction_count": 0,
        "insufficient_context_count": 0,
        "ai_used": False,
        "summary_line": "",
        "validations": validations,
    }
    payload.update(overrides)
    (repo / "outputs" / "latest" / "ai_decision_validation.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


def _write_explanations(repo: Path, explanations: list[dict]) -> None:
    (repo / "outputs" / "latest" / "decision_explanations.json").write_text(
        json.dumps({
            "generated_at": "x", "available": True, "observe_only": True,
            "summary_line": "", "source_artifacts": [], "explanations": explanations,
        }),
        encoding="utf-8",
    )


def _write_outcome_summary(repo: Path, **overrides) -> None:
    policy = repo / "outputs" / "policy"
    policy.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": "x",
        "total_decisions": 10,
        "resolved": 5,
        "unresolved": 5,
        "hit_rate": 0.6,
        "avg_return_pct": 0.012,
        "by_decision": {},
        "by_validation_status": {},
        "last_10_resolved": [],
        "best_decision": None,
        "worst_decision": None,
    }
    payload.update(overrides)
    (policy / "decision_outcome_summary.json").write_text(json.dumps(payload), encoding="utf-8")


class TestFullDecisions:
    def test_returns_all_rows(self, fake_repo: Path):
        _write_decision_plan(fake_repo, [
            {"symbol": f"S{i}", "decision": "BUY", "priority": float(i)}
            for i in range(7)
        ])
        view = collect_today_view(fake_repo)
        assert len(view["full_decisions"]) == 7
        # Top 5 (the compact view) still capped
        assert len(view["decisions"]) == 5

    def test_empty_plan(self, fake_repo: Path):
        view = collect_today_view(fake_repo)
        assert view["full_decisions"] == []


class TestValidationCounts:
    def test_unavailable_when_missing(self, fake_repo: Path):
        view = collect_today_view(fake_repo)
        assert view["validation_counts"]["available"] is False

    def test_counts_passthrough(self, fake_repo: Path):
        _write_validation(fake_repo, [
            {"symbol": "A", "decision": "BUY", "validation_status": "aligned",
             "plain_english_summary": "ok"},
            {"symbol": "B", "decision": "SELL", "validation_status": "caution",
             "plain_english_summary": "watch"},
        ])
        view = collect_today_view(fake_repo)
        vc = view["validation_counts"]
        assert vc["available"] is True
        assert vc["total"] == 2
        assert vc["aligned"] == 1
        assert vc["caution"] == 1


class TestValidationsBySymbol:
    def test_lookup_by_symbol(self, fake_repo: Path):
        _write_validation(fake_repo, [
            {"symbol": "ZZZX", "decision": "BUY", "validation_status": "aligned",
             "plain_english_summary": "looks good", "contradictions": [],
             "watch_next": ["earnings", "RSI"]},
        ])
        view = collect_today_view(fake_repo)
        v = view["validations_by_symbol"].get("ZZZX")
        assert v is not None
        assert v["status"] == "aligned"
        assert v["summary"] == "looks good"
        assert v["watch_next"] == ["earnings", "RSI"]


class TestExplanationsBySymbol:
    def test_lookup_by_symbol(self, fake_repo: Path):
        _write_explanations(fake_repo, [
            {"decision_id": 1, "symbol": "ABCD", "action": "BUY",
             "concise_explanation": "strong tape",
             "risks": ["vol-spike"], "what_to_watch_next": ["earnings"]},
        ])
        view = collect_today_view(fake_repo)
        e = view["explanations_by_symbol"].get("ABCD")
        assert e is not None
        assert e["concise"] == "strong tape"
        assert e["risks"] == ["vol-spike"]


class TestDecisionPerformance:
    def test_unavailable_when_missing(self, fake_repo: Path):
        view = collect_today_view(fake_repo)
        assert view["decision_performance"]["available"] is False

    def test_passthrough(self, fake_repo: Path):
        _write_outcome_summary(fake_repo, hit_rate=0.75, avg_return_pct=0.025)
        view = collect_today_view(fake_repo)
        dp = view["decision_performance"]
        assert dp["available"] is True
        assert dp["hit_rate"] == 0.75
        assert dp["avg_return_pct"] == 0.025


def _write_news_evidence(repo: Path, **overrides) -> None:
    payload = {
        "generated_at": "x",
        "observe_only": True,
        "no_trade": True,
        "source": "news_evidence_layer",
        "influence_cap": "context_only",
        "data_available": True,
        "portfolio_context": "Tech-heavy",
        "memo_bullets": ["Bullet 1", "Bullet 2"],
        "catalyst_evidence": [
            {"label": "earnings_beat", "count": 3, "tickers": ["NVDA"], "description": "strong Q"},
        ],
        "risk_evidence": [
            {"label": "regulatory", "count": 1, "tickers": ["META"], "description": "FTC probe"},
        ],
        "operator_review_flags": [],
    }
    payload.update(overrides)
    (repo / "outputs" / "latest" / "news_evidence_layer.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


def _write_narrative(repo: Path, period: str, **overrides) -> None:
    payload = {
        "narrative_period": period,
        "generated_at": f"2026-05-15T00:00:00+00:00",
        "observe_only": True,
        "no_trade": True,
        "data_available": True,
        "top_headline": f"{period.title()} headline",
        "executive_summary": f"{period.title()} summary text",
        "key_themes": [f"theme-{i}" for i in range(3)],
        "risks_to_watch": ["regulatory", "macro"],
        "catalysts_to_watch": ["earnings"],
        "operator_watchlist": ["NVDA", "AAPL"],
    }
    payload.update(overrides)
    (repo / "outputs" / "latest" / f"market_narrative_{period}.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


class TestMarketNarratives:
    def test_unavailable_when_all_missing(self, fake_repo: Path):
        view = collect_today_view(fake_repo)
        assert view["market_narratives"]["available"] is False

    def test_partial_availability(self, fake_repo: Path):
        _write_narrative(fake_repo, "daily")
        view = collect_today_view(fake_repo)
        nm = view["market_narratives"]
        assert nm["available"] is True
        assert nm["daily"]["available"] is True
        assert nm["weekly"]["available"] is False
        assert nm["monthly"]["available"] is False

    def test_data_available_false_treated_unavailable(self, fake_repo: Path):
        _write_narrative(fake_repo, "daily", data_available=False)
        nm = collect_today_view(fake_repo)["market_narratives"]
        assert nm["daily"]["available"] is False
        assert nm["available"] is False

    def test_all_three_periods(self, fake_repo: Path):
        _write_narrative(fake_repo, "daily")
        _write_narrative(fake_repo, "weekly")
        _write_narrative(fake_repo, "monthly")
        nm = collect_today_view(fake_repo)["market_narratives"]
        assert nm["daily"]["top_headline"] == "Daily headline"
        assert nm["weekly"]["top_headline"] == "Weekly headline"
        assert nm["monthly"]["top_headline"] == "Monthly headline"

    def test_themes_capped_at_six(self, fake_repo: Path):
        _write_narrative(fake_repo, "daily", key_themes=[f"t{i}" for i in range(20)])
        nm = collect_today_view(fake_repo)["market_narratives"]["daily"]
        assert len(nm["key_themes"]) == 6


class TestNewsEvidence:
    def test_unavailable_when_missing(self, fake_repo: Path):
        view = collect_today_view(fake_repo)
        assert view["news_evidence"]["available"] is False

    def test_unavailable_when_data_available_false(self, fake_repo: Path):
        _write_news_evidence(fake_repo, data_available=False, missing_inputs=["x"])
        view = collect_today_view(fake_repo)
        assert view["news_evidence"]["available"] is False

    def test_memo_bullets_and_evidence_passthrough(self, fake_repo: Path):
        _write_news_evidence(fake_repo)
        ne = collect_today_view(fake_repo)["news_evidence"]
        assert ne["available"] is True
        assert ne["influence_cap"] == "context_only"
        assert "Bullet 1" in ne["memo_bullets"]
        assert ne["catalyst_evidence"][0]["label"] == "earnings_beat"
        assert ne["risk_evidence"][0]["label"] == "regulatory"

    def test_memo_bullets_capped_at_six(self, fake_repo: Path):
        _write_news_evidence(fake_repo, memo_bullets=[f"B{i}" for i in range(10)])
        ne = collect_today_view(fake_repo)["news_evidence"]
        assert len(ne["memo_bullets"]) == 6
