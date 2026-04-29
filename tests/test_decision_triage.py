"""Tests for portfolio_automation/decision_triage.py"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from portfolio_automation.decision_triage import (
    ALL_BUCKETS,
    BUCKET_ACTION,
    BUCKET_CRITICAL,
    BUCKET_IGNORE,
    BUCKET_MONITOR,
    TRIAGE_JSON_RELATIVE_PATH,
    TRIAGE_MD_RELATIVE_PATH,
    _classify_row,
    _has_degraded_signal,
    _is_portfolio_rebalance,
    _rank_within_bucket,
    build_triage,
    render_triage_md,
    run_triage,
    triage_single_decision,
)
from gui_operator_data import load_decision_triage

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _decision_plan(decisions: list[dict]) -> dict:
    return {
        "generated_at": "2026-04-29T09:00:00",
        "observe_only": True,
        "total_decisions": len(decisions),
        "decisions": decisions,
    }


def _ai_validation(validations: list[dict] | None = None) -> dict:
    return {
        "available": True,
        "observe_only": True,
        "total_validated": len(validations or []),
        "validations": validations or [],
    }


def _validation_record(
    symbol: str,
    decision: str,
    status: str,
    watch_next: list[str] | None = None,
) -> dict:
    return {
        "symbol": symbol,
        "decision": decision,
        "validation_status": status,
        "watch_next": watch_next or [],
    }


def _sell_row(
    symbol: str = "QQQ",
    risk_flags: list[str] | None = None,
    urgency: str = "high",
    priority: float = 0.90,
    source: str = "structural",
) -> dict:
    return {
        "symbol": symbol,
        "decision": "SELL",
        "priority": priority,
        "urgency": urgency,
        "source": source,
        "risk_flags": risk_flags or [],
        "decision_reason_structured": {"band": "A", "strategy": "structural_risk"},
    }


def _buy_row(
    symbol: str = "AAPL",
    priority: float = 0.80,
    urgency: str = "medium",
    risk_flags: list[str] | None = None,
) -> dict:
    return {
        "symbol": symbol,
        "decision": "BUY",
        "priority": priority,
        "urgency": urgency,
        "source": "signals",
        "risk_flags": risk_flags or [],
        "decision_reason_structured": {"band": "B", "strategy": "momentum"},
    }


def _scale_row(
    symbol: str = "SPY",
    priority: float = 0.75,
    source: str = "signals",
    strategy: str = "momentum",
    risk_flags: list[str] | None = None,
) -> dict:
    return {
        "symbol": symbol,
        "decision": "SCALE",
        "priority": priority,
        "urgency": "medium",
        "source": source,
        "risk_flags": risk_flags or [],
        "decision_reason_structured": {"band": "B", "strategy": strategy},
    }


def _wait_row(
    symbol: str = "MSFT",
    priority: float = 0.50,
    risk_flags: list[str] | None = None,
    urgency: str = "low",
) -> dict:
    return {
        "symbol": symbol,
        "decision": "WAIT",
        "priority": priority,
        "urgency": urgency,
        "source": "signals",
        "risk_flags": risk_flags or [],
        "decision_reason_structured": {"band": "C", "strategy": "watchful"},
    }


def _hold_row(symbol: str = "NVDA", priority: float = 0.45) -> dict:
    return {
        "symbol": symbol,
        "decision": "HOLD",
        "priority": priority,
        "urgency": "low",
        "source": "signals",
        "risk_flags": [],
        "decision_reason_structured": {"band": "C", "strategy": "hold"},
    }


def _avoid_row(symbol: str = "XYZ", priority: float = 0.20) -> dict:
    return {
        "symbol": symbol,
        "decision": "AVOID",
        "priority": priority,
        "urgency": "low",
        "source": "signals",
        "risk_flags": [],
        "decision_reason_structured": {"band": "D", "strategy": "avoid"},
    }


# ---------------------------------------------------------------------------
# Class 1: Core classification rules
# ---------------------------------------------------------------------------


class TestClassificationRules:
    def test_sell_with_leverage_breach_is_critical(self):
        row = _sell_row(risk_flags=["leverage_breach"])
        bucket, severity, reason, source = _classify_row(row, "aligned")
        assert bucket == BUCKET_CRITICAL
        assert severity == "critical"
        assert "leverage_breach" in reason
        assert source == "sell_guardrail_breach"

    def test_sell_with_concentration_breach_is_critical(self):
        row = _sell_row(risk_flags=["concentration_breach"])
        bucket, severity, _, source = _classify_row(row, "aligned")
        assert bucket == BUCKET_CRITICAL
        assert source == "sell_guardrail_breach"

    def test_sell_with_both_guardrail_flags_is_critical(self):
        row = _sell_row(risk_flags=["leverage_breach", "concentration_breach"])
        bucket, severity, reason, source = _classify_row(row, "aligned")
        assert bucket == BUCKET_CRITICAL
        assert "concentration_breach" in reason
        assert "leverage_breach" in reason

    def test_contradiction_is_critical_action(self):
        row = _buy_row()
        bucket, severity, reason, source = _classify_row(row, "contradiction")
        assert bucket == BUCKET_CRITICAL
        assert severity == "critical"
        assert source == "validation_contradiction"

    def test_urgency_critical_is_critical_action(self):
        row = _wait_row(urgency="critical")
        bucket, severity, _, source = _classify_row(row, "caution")
        assert bucket == BUCKET_CRITICAL
        assert severity == "critical"
        assert source == "urgency_critical"

    def test_caution_with_guardrail_is_elevated_critical(self):
        row = _buy_row(risk_flags=["leverage_breach"])
        bucket, severity, reason, source = _classify_row(row, "caution")
        assert bucket == BUCKET_CRITICAL
        assert severity == "high"
        assert source == "caution_structural_risk"

    def test_aligned_high_priority_buy_is_action_candidate(self):
        row = _buy_row(priority=0.85)
        bucket, severity, reason, source = _classify_row(row, "aligned")
        assert bucket == BUCKET_ACTION
        assert severity == "high"
        assert source == "aligned_high_priority"

    def test_aligned_high_priority_scale_is_action_candidate(self):
        row = _scale_row(priority=0.80)
        bucket, severity, _, source = _classify_row(row, "aligned")
        assert bucket == BUCKET_ACTION
        assert source == "aligned_high_priority"

    def test_aligned_buy_below_threshold_is_not_action_candidate(self):
        row = _buy_row(priority=0.65)
        bucket, _, _, _ = _classify_row(row, "aligned")
        # 0.65 < 0.70 threshold; should fall to caution → monitor
        assert bucket != BUCKET_ACTION

    def test_scale_rebalance_mid_priority_is_action_candidate(self):
        row = _scale_row(priority=0.55, source="portfolio_rebalance")
        bucket, severity, _, source = _classify_row(row, "caution")
        assert bucket == BUCKET_ACTION
        assert severity == "medium"
        assert source == "scale_rebalance"

    def test_scale_rebalance_below_mid_priority_is_not_action(self):
        row = _scale_row(priority=0.45, source="portfolio_rebalance")
        bucket, _, _, _ = _classify_row(row, "caution")
        assert bucket != BUCKET_ACTION

    def test_wait_with_degraded_data_is_monitor(self):
        row = _wait_row(risk_flags=["degraded_data"])
        bucket, severity, _, source = _classify_row(row, "caution")
        assert bucket == BUCKET_MONITOR
        assert severity == "medium"
        assert source == "wait_hold_degraded"

    def test_hold_with_degraded_data_is_monitor(self):
        row = _hold_row()
        row["risk_flags"] = ["degraded_mode"]
        bucket, _, _, source = _classify_row(row, "caution")
        assert bucket == BUCKET_MONITOR
        assert source == "wait_hold_degraded"

    def test_caution_without_flags_is_monitor(self):
        row = _buy_row(priority=0.50)
        bucket, _, _, source = _classify_row(row, "caution")
        assert bucket == BUCKET_MONITOR
        assert source == "caution_monitor"

    def test_avoid_is_ignore_for_now(self):
        row = _avoid_row()
        bucket, severity, _, source = _classify_row(row, "unknown")
        assert bucket == BUCKET_IGNORE
        assert severity == "low"
        assert source == "avoid_decision"

    def test_insufficient_context_is_ignore_for_now(self):
        row = _buy_row(priority=0.60)
        bucket, _, _, source = _classify_row(row, "insufficient_context")
        assert bucket == BUCKET_IGNORE
        assert source == "insufficient_context"

    def test_low_priority_is_ignore_for_now(self):
        row = _buy_row(priority=0.25)
        bucket, _, reason, source = _classify_row(row, "caution")
        assert bucket == BUCKET_IGNORE
        assert source == "low_priority"
        assert "0.25" in reason

    def test_priority_exactly_at_low_threshold_is_not_ignored(self):
        # priority == 0.30 is NOT below the threshold (strict <)
        row = _buy_row(priority=0.30)
        bucket, _, _, _ = _classify_row(row, "caution")
        assert bucket != BUCKET_IGNORE

    def test_default_monitor_for_unmatched_case(self):
        row = _hold_row(priority=0.40)
        bucket, _, _, source = _classify_row(row, "unknown")
        assert bucket == BUCKET_MONITOR
        assert source == "default_monitor"

    def test_sell_without_guardrail_flag_does_not_go_critical_by_flag_rule(self):
        row = _sell_row(risk_flags=[])
        bucket, _, _, source = _classify_row(row, "caution")
        # No guardrail flag, no contradiction, no critical urgency → should not be guardrail rule
        assert source != "sell_guardrail_breach"


# ---------------------------------------------------------------------------
# Class 2: Helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_has_degraded_signal_degraded_data_flag(self):
        row = {"risk_flags": ["degraded_data"]}
        assert _has_degraded_signal(row) is True

    def test_has_degraded_signal_cache_only_flag(self):
        row = {"risk_flags": ["cache_only"]}
        assert _has_degraded_signal(row) is True

    def test_has_degraded_signal_no_flags(self):
        row = {"risk_flags": []}
        assert _has_degraded_signal(row) is False

    def test_has_degraded_signal_irrelevant_flags(self):
        row = {"risk_flags": ["leverage_breach"]}
        assert _has_degraded_signal(row) is False

    def test_is_portfolio_rebalance_by_source(self):
        row = _scale_row(source="portfolio_rebalance")
        assert _is_portfolio_rebalance(row) is True

    def test_is_portfolio_rebalance_by_strategy(self):
        row = _scale_row(strategy="portfolio_rebalance_strategy")
        assert _is_portfolio_rebalance(row) is True

    def test_is_portfolio_rebalance_source_contains_portfolio(self):
        row = _scale_row(source="portfolio_adjustment")
        assert _is_portfolio_rebalance(row) is True

    def test_is_not_portfolio_rebalance(self):
        row = _scale_row(source="signals", strategy="momentum")
        assert _is_portfolio_rebalance(row) is False


# ---------------------------------------------------------------------------
# Class 3: triage_single_decision schema
# ---------------------------------------------------------------------------


class TestTriageSingleDecision:
    def test_output_schema_complete(self):
        row = _sell_row(risk_flags=["leverage_breach"])
        ai_val = _ai_validation([_validation_record("QQQ", "SELL", "aligned")])
        result = triage_single_decision(row, ai_val)

        required_keys = {
            "symbol", "decision", "triage_bucket", "triage_rank",
            "severity", "reason", "next_action", "source",
            "priority", "priority_score", "validation_status",
            "risk_flags", "watch_next",
        }
        assert required_keys.issubset(set(result.keys()))

    def test_priority_and_priority_score_are_equal(self):
        row = _buy_row(priority=0.75)
        ai_val = _ai_validation()
        result = triage_single_decision(row, ai_val)
        assert result["priority"] == result["priority_score"]

    def test_validation_status_pulled_from_ai_validation(self):
        row = _buy_row(symbol="AAPL", priority=0.80)
        ai_val = _ai_validation([_validation_record("AAPL", "BUY", "aligned")])
        result = triage_single_decision(row, ai_val)
        assert result["validation_status"] == "aligned"

    def test_validation_status_unknown_when_not_found(self):
        row = _buy_row(symbol="AAPL")
        result = triage_single_decision(row, _ai_validation())
        assert result["validation_status"] == "unknown"

    def test_watch_next_pulled_from_ai_validation(self):
        row = _buy_row(symbol="AAPL")
        ai_val = _ai_validation(
            [_validation_record("AAPL", "BUY", "aligned", watch_next=["Check earnings."])]
        )
        result = triage_single_decision(row, ai_val)
        assert "Check earnings." in result["watch_next"]

    def test_triage_rank_zero_before_ranking(self):
        row = _buy_row()
        result = triage_single_decision(row, _ai_validation())
        assert result["triage_rank"] == 0

    def test_risk_flags_preserved(self):
        row = _sell_row(risk_flags=["leverage_breach", "concentration_breach"])
        result = triage_single_decision(row, _ai_validation())
        assert set(result["risk_flags"]) == {"leverage_breach", "concentration_breach"}

    def test_next_action_critical_contains_review(self):
        row = _sell_row(risk_flags=["leverage_breach"])
        result = triage_single_decision(row, _ai_validation())
        assert "Review immediately" in result["next_action"]

    def test_next_action_action_candidate_contains_evaluate(self):
        row = _buy_row(priority=0.80)
        ai_val = _ai_validation([_validation_record("AAPL", "BUY", "aligned")])
        result = triage_single_decision(row, ai_val)
        assert "Evaluate" in result["next_action"]

    def test_next_action_monitor_contains_watch(self):
        row = _wait_row(priority=0.50)
        ai_val = _ai_validation([_validation_record("MSFT", "WAIT", "caution")])
        result = triage_single_decision(row, ai_val)
        assert "Watch" in result["next_action"]

    def test_next_action_ignore_contains_no_action(self):
        row = _avoid_row()
        result = triage_single_decision(row, _ai_validation())
        assert "No action" in result["next_action"]


# ---------------------------------------------------------------------------
# Class 4: Ranking
# ---------------------------------------------------------------------------


class TestRanking:
    def test_rank_within_bucket_assigns_1_based_ranks(self):
        rows = [
            {"severity": "low", "priority": 0.50, "triage_rank": 0},
            {"severity": "high", "priority": 0.80, "triage_rank": 0},
            {"severity": "medium", "priority": 0.60, "triage_rank": 0},
        ]
        ranked = _rank_within_bucket(rows)
        ranks = [r["triage_rank"] for r in ranked]
        assert sorted(ranks) == [1, 2, 3]

    def test_rank_within_bucket_severity_order(self):
        rows = [
            {"severity": "low", "priority": 0.90, "triage_rank": 0},
            {"severity": "critical", "priority": 0.50, "triage_rank": 0},
            {"severity": "high", "priority": 0.70, "triage_rank": 0},
        ]
        ranked = _rank_within_bucket(rows)
        # First rank should be the critical severity row
        assert ranked[0]["severity"] == "critical"
        assert ranked[0]["triage_rank"] == 1

    def test_rank_within_bucket_same_severity_sorted_by_priority(self):
        rows = [
            {"severity": "high", "priority": 0.60, "triage_rank": 0},
            {"severity": "high", "priority": 0.85, "triage_rank": 0},
            {"severity": "high", "priority": 0.75, "triage_rank": 0},
        ]
        ranked = _rank_within_bucket(rows)
        priorities = [r["priority"] for r in ranked]
        assert priorities == [0.85, 0.75, 0.60]

    def test_rank_empty_list(self):
        assert _rank_within_bucket([]) == []


# ---------------------------------------------------------------------------
# Class 5: build_triage — bucket counts and structure
# ---------------------------------------------------------------------------


class TestBuildTriage:
    def test_bucket_counts_correct(self):
        decisions = [
            _sell_row(risk_flags=["leverage_breach"]),   # critical
            _buy_row(priority=0.80),                      # action_candidate (if aligned)
            _wait_row(priority=0.50),                     # monitor (caution)
            _avoid_row(),                                 # ignore
        ]
        ai_val = _ai_validation([
            _validation_record("QQQ", "SELL", "aligned"),
            _validation_record("AAPL", "BUY", "aligned"),
            _validation_record("MSFT", "WAIT", "caution"),
            _validation_record("XYZ", "AVOID", "unknown"),
        ])
        payload = build_triage(_decision_plan(decisions), ai_val)
        counts = payload["bucket_counts"]
        assert counts["critical_action"] == 1
        assert counts["action_candidate"] == 1
        assert counts["monitor"] == 1
        assert counts["ignore_for_now"] == 1

    def test_total_decisions_correct(self):
        decisions = [_sell_row(), _buy_row(), _wait_row()]
        payload = build_triage(_decision_plan(decisions), _ai_validation())
        assert payload["total_decisions"] == 3

    def test_all_bucket_keys_present(self):
        payload = build_triage(_decision_plan([]), _ai_validation())
        for bucket in ALL_BUCKETS:
            assert bucket in payload["buckets"]
            assert bucket in payload["bucket_counts"]

    def test_top_actions_max_5(self):
        # 3 critical + 4 action candidates = 7 combined, but top_actions capped at 5
        decisions = [
            _sell_row("A", risk_flags=["leverage_breach"]),
            _sell_row("B", risk_flags=["concentration_breach"]),
            _sell_row("C", urgency="critical"),
            _buy_row("D", priority=0.80),
            _buy_row("E", priority=0.75),
            _buy_row("F", priority=0.72),
            _buy_row("G", priority=0.71),
        ]
        ai_val = _ai_validation([
            _validation_record("A", "SELL", "aligned"),
            _validation_record("B", "SELL", "aligned"),
            _validation_record("C", "SELL", "aligned"),
            _validation_record("D", "BUY", "aligned"),
            _validation_record("E", "BUY", "aligned"),
            _validation_record("F", "BUY", "aligned"),
            _validation_record("G", "BUY", "aligned"),
        ])
        payload = build_triage(_decision_plan(decisions), ai_val)
        assert len(payload["top_actions"]) <= 5

    def test_top_actions_critical_before_action(self):
        decisions = [
            _buy_row("AAPL", priority=0.85),
            _sell_row("QQQ", risk_flags=["leverage_breach"]),
        ]
        ai_val = _ai_validation([
            _validation_record("AAPL", "BUY", "aligned"),
            _validation_record("QQQ", "SELL", "aligned"),
        ])
        payload = build_triage(_decision_plan(decisions), ai_val)
        top = payload["top_actions"]
        # Critical row should appear first
        assert top[0]["triage_bucket"] == BUCKET_CRITICAL

    def test_observe_only_always_true(self):
        payload = build_triage(_decision_plan([]), _ai_validation())
        assert payload["observe_only"] is True

    def test_generated_at_present(self):
        payload = build_triage(_decision_plan([]), _ai_validation())
        assert payload.get("generated_at")

    def test_empty_decisions_all_zero_counts(self):
        payload = build_triage(_decision_plan([]), _ai_validation())
        assert payload["total_decisions"] == 0
        for count in payload["bucket_counts"].values():
            assert count == 0

    def test_missing_validation_handled_gracefully(self):
        # No ai_validation → validation_status = "unknown"
        decisions = [_sell_row(risk_flags=["leverage_breach"])]
        payload = build_triage(_decision_plan(decisions), {})
        assert payload["total_decisions"] == 1
        assert payload["bucket_counts"]["critical_action"] == 1

    def test_bucket_rows_are_ranked(self):
        decisions = [
            _sell_row("QQQ", risk_flags=["leverage_breach"], priority=0.90),
            _sell_row("SPY", urgency="critical", priority=0.70),
        ]
        payload = build_triage(_decision_plan(decisions), _ai_validation())
        critical_rows = payload["buckets"]["critical_action"]
        ranks = [r["triage_rank"] for r in critical_rows]
        assert 1 in ranks
        assert 2 in ranks


# ---------------------------------------------------------------------------
# Class 6: Markdown rendering
# ---------------------------------------------------------------------------


class TestMarkdownOutput:
    def test_markdown_contains_observe_only_disclaimer(self):
        payload = build_triage(_decision_plan([]), _ai_validation())
        md = render_triage_md(payload)
        assert "Observe-only" in md or "observe-only" in md.lower()

    def test_markdown_contains_summary_table(self):
        payload = build_triage(_decision_plan([]), _ai_validation())
        md = render_triage_md(payload)
        assert "Critical Action" in md
        assert "Action Candidate" in md
        assert "Monitor" in md
        assert "Ignore For Now" in md

    def test_markdown_contains_critical_section_when_rows_exist(self):
        decisions = [_sell_row(risk_flags=["leverage_breach"])]
        payload = build_triage(_decision_plan(decisions), _ai_validation())
        md = render_triage_md(payload)
        assert "## Critical Action" in md
        assert "QQQ" in md

    def test_markdown_no_critical_section_when_empty(self):
        decisions = [_wait_row()]
        payload = build_triage(_decision_plan(decisions), _ai_validation())
        md = render_triage_md(payload)
        assert "## Critical Action" not in md

    def test_markdown_top_actions_section(self):
        decisions = [_sell_row(risk_flags=["leverage_breach"])]
        payload = build_triage(_decision_plan(decisions), _ai_validation())
        md = render_triage_md(payload)
        assert "Top Actions Today" in md

    def test_markdown_ends_with_newline(self):
        payload = build_triage(_decision_plan([]), _ai_validation())
        md = render_triage_md(payload)
        assert md.endswith("\n")

    def test_markdown_unavailable_payload(self):
        payload = {
            "generated_at": "2026-04-29T09:00:00",
            "observe_only": True,
            "available": False,
            "total_decisions": 0,
            "bucket_counts": {b: 0 for b in ALL_BUCKETS},
            "top_actions": [],
            "buckets": {b: [] for b in ALL_BUCKETS},
        }
        md = render_triage_md(payload)
        assert "| **Total** | **0** |" in md


# ---------------------------------------------------------------------------
# Class 7: run_triage I/O integration
# ---------------------------------------------------------------------------


class TestRunTriage:
    def _write_plan(self, tmp: Path, decisions: list[dict]) -> None:
        path = tmp / "outputs" / "latest"
        path.mkdir(parents=True, exist_ok=True)
        (path / "decision_plan.json").write_text(
            json.dumps(_decision_plan(decisions)), encoding="utf-8"
        )

    def _write_validation(self, tmp: Path, validations: list[dict]) -> None:
        path = tmp / "outputs" / "latest" / "ai_decision_validation.json"
        path.write_text(
            json.dumps(_ai_validation(validations)), encoding="utf-8"
        )

    def test_writes_json_and_md_files(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write_plan(tmp, [_sell_row(risk_flags=["leverage_breach"])])
            payload, md = run_triage(tmp)
            json_path = tmp.joinpath(*TRIAGE_JSON_RELATIVE_PATH)
            md_path = tmp.joinpath(*TRIAGE_MD_RELATIVE_PATH)
            assert json_path.exists()
            assert md_path.exists()
            stored = json.loads(json_path.read_text())
            assert stored["total_decisions"] == 1

    def test_skips_write_when_write_files_false(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write_plan(tmp, [_sell_row(risk_flags=["leverage_breach"])])
            run_triage(tmp, write_files=False)
            json_path = tmp.joinpath(*TRIAGE_JSON_RELATIVE_PATH)
            assert not json_path.exists()

    def test_returns_available_false_when_plan_missing(self):
        with tempfile.TemporaryDirectory() as td:
            payload, md = run_triage(Path(td), write_files=False)
            assert payload["available"] is False
            assert payload["total_decisions"] == 0

    def test_returns_available_false_when_plan_malformed(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            path = tmp / "outputs" / "latest"
            path.mkdir(parents=True, exist_ok=True)
            (path / "decision_plan.json").write_text("not json", encoding="utf-8")
            payload, _ = run_triage(tmp, write_files=False)
            assert payload["available"] is False

    def test_missing_validation_artifact_handled_gracefully(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write_plan(tmp, [_buy_row(priority=0.80)])
            # No ai_decision_validation.json written
            payload, _ = run_triage(tmp, write_files=False)
            assert payload["available"] is True
            assert payload["total_decisions"] == 1

    def test_classification_with_validation_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write_plan(tmp, [_buy_row(symbol="AAPL", priority=0.80)])
            self._write_validation(tmp, [_validation_record("AAPL", "BUY", "aligned")])
            payload, _ = run_triage(tmp, write_files=False)
            assert payload["bucket_counts"]["action_candidate"] == 1

    def test_summary_line_in_payload(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write_plan(tmp, [_sell_row(risk_flags=["leverage_breach"])])
            payload, _ = run_triage(tmp, write_files=False)
            assert "triaged" in payload["summary_line"]

    def test_pipeline_non_fatal_on_bad_root(self):
        # Should not raise even with a non-existent root
        payload, md = run_triage(Path("/nonexistent/path"), write_files=False)
        assert payload["available"] is False


# ---------------------------------------------------------------------------
# Class 8: GUI data layer — load_decision_triage
# ---------------------------------------------------------------------------


class TestGuiDataLayer:
    def test_returns_empty_when_file_missing(self):
        with tempfile.TemporaryDirectory() as td:
            result = load_decision_triage(Path(td))
            assert result["available"] is False
            assert "not available" in result["summary_line"]

    def test_returns_empty_on_malformed_file(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            path = tmp / "outputs" / "latest"
            path.mkdir(parents=True, exist_ok=True)
            (path / "decision_triage.json").write_text("{bad json}", encoding="utf-8")
            result = load_decision_triage(tmp)
            assert result["available"] is False
            assert "could not be read" in result["summary_line"]

    def test_returns_empty_on_non_dict_file(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            path = tmp / "outputs" / "latest"
            path.mkdir(parents=True, exist_ok=True)
            (path / "decision_triage.json").write_text("[]", encoding="utf-8")
            result = load_decision_triage(tmp)
            assert result["available"] is False

    def test_loads_valid_triage_file(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write_triage(tmp, total_decisions=3, critical=1)
            result = load_decision_triage(tmp)
            assert result["available"] is True
            assert result["total_decisions"] == 3

    def test_defaults_available_true_when_not_present(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            path = tmp / "outputs" / "latest"
            path.mkdir(parents=True, exist_ok=True)
            payload = {"total_decisions": 2, "bucket_counts": {b: 0 for b in ALL_BUCKETS}}
            (path / "decision_triage.json").write_text(json.dumps(payload), encoding="utf-8")
            result = load_decision_triage(tmp)
            assert result["available"] is True

    def test_summary_line_defaults_when_absent(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            path = tmp / "outputs" / "latest"
            path.mkdir(parents=True, exist_ok=True)
            payload = {"total_decisions": 5, "available": True}
            (path / "decision_triage.json").write_text(json.dumps(payload), encoding="utf-8")
            result = load_decision_triage(tmp)
            assert "5" in result["summary_line"]

    def _write_triage(self, tmp: Path, total_decisions: int = 2, critical: int = 0) -> None:
        path = tmp / "outputs" / "latest"
        path.mkdir(parents=True, exist_ok=True)
        payload = {
            "available": True,
            "total_decisions": total_decisions,
            "bucket_counts": {
                "critical_action": critical,
                "action_candidate": 0,
                "monitor": total_decisions - critical,
                "ignore_for_now": 0,
            },
            "top_actions": [],
            "buckets": {b: [] for b in ALL_BUCKETS},
            "summary_line": f"{total_decisions} decisions triaged.",
        }
        (path / "decision_triage.json").write_text(json.dumps(payload), encoding="utf-8")
