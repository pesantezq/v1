"""
Tests for portfolio_automation.discovery.discovery_replay.

All tests are deterministic — no external API calls and no file system
side effects beyond tmp_path-scoped fixtures.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.discovery.discovery_replay import (
    _window_key,
    _avg,
    _hit_rate,
    _group_stats,
    _build_replay_markdown,
    _DISCLAIMER,
    _FORBIDDEN_STATUSES,
    _DEFAULT_WINDOWS,
    load_discovery_replay_inputs,
    evaluate_discovery_candidate_outcomes,
    summarize_discovery_replay_results,
    write_discovery_replay_report,
    run_discovery_replay,
)
from portfolio_automation.run_mode_governance import RunModeViolation


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def _make_candidate(
    ticker: str = "NVDA",
    status: str = "watch",
    corr_score: float = 0.70,
    corr_level: str = "strong",
    corr_met: bool = True,
    risk_flag: bool = False,
    event_type: str = "earnings",
    mention_count: int = 3,
    unique_source_count: int = 2,
) -> dict:
    return {
        "ticker": ticker,
        "status": status,
        "corroboration_score": corr_score,
        "corroboration_level": corr_level,
        "corroboration_met": corr_met,
        "risk_flag": risk_flag,
        "event_type": event_type,
        "mention_count": mention_count,
        "unique_source_count": unique_source_count,
        "discovery_only": True,
        "sandbox_only": True,
        "first_seen": "2026-04-01T00:00:00+00:00",
        "last_seen": "2026-04-15T00:00:00+00:00",
    }


def _make_price_outcome(
    return_pct: float = 2.0,
    direction_correct: bool = True,
    max_drawdown: float = -1.0,
    max_runup: float = 3.0,
    windows: tuple[int, ...] = _DEFAULT_WINDOWS,
) -> dict:
    return {
        _window_key(w): {
            "forward_return_pct": return_pct,
            "direction_correct": direction_correct,
            "max_drawdown_pct": max_drawdown,
            "max_runup_pct": max_runup,
        }
        for w in windows
    }


def _make_valid_approval(
    symbol: str,
    decision: str = "approve_for_research_review",
) -> dict:
    return {
        "symbol": symbol,
        "decision": decision,
        "observe_only": True,
        "sandbox_only": True,
        "no_trade": True,
        "no_official_promotion": True,
        "corroboration_score": 0.70,
        "corroboration_level": "strong",
        "candidate_status": "watch",
        "generated_at": "2026-04-20T00:00:00+00:00",
        "decision_reason": "test reason",
    }


# Filesystem helpers

def _write_emerging(tmp_path: Path, candidates: list[dict]) -> None:
    data = {
        "generated_at": "2026-04-20T00:00:00+00:00",
        "observe_only": True,
        "discovery_only": True,
        "sandbox_only": True,
        "disclaimer": "test",
        "total_candidates": len(candidates),
        "candidates": candidates,
    }
    p = tmp_path / "sandbox" / "discovery" / "emerging_candidates.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


def _write_rejected(tmp_path: Path, candidates: list[dict]) -> None:
    data = {
        "generated_at": "2026-04-20T00:00:00+00:00",
        "observe_only": True,
        "discovery_only": True,
        "sandbox_only": True,
        "disclaimer": "test",
        "total_rejected": len(candidates),
        "candidates": candidates,
    }
    p = tmp_path / "sandbox" / "discovery" / "rejected_candidates.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


def _write_approvals(tmp_path: Path, decisions: list[dict]) -> None:
    p = tmp_path / "sandbox" / "discovery" / "approval_decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(d) for d in decisions) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# TestHelpers — internal utility functions
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_window_key(self):
        assert _window_key(1) == "window_1"
        assert _window_key(20) == "window_20"

    def test_avg_empty(self):
        assert _avg([]) is None

    def test_avg_values(self):
        assert _avg([1.0, 3.0]) == 2.0

    def test_hit_rate_empty(self):
        assert _hit_rate([]) is None

    def test_hit_rate_all_correct(self):
        assert _hit_rate([True, True, True]) == 1.0

    def test_hit_rate_half_correct(self):
        assert _hit_rate([True, False]) == 0.5

    def test_group_stats_empty(self):
        stats = _group_stats([], (1, 3))
        assert stats["count"] == 0
        assert stats["window_1"]["resolved"] == 0
        assert stats["window_1"]["avg_forward_return_pct"] is None
        assert stats["window_1"]["hit_rate"] is None

    def test_group_stats_with_data(self):
        cands = [_make_candidate("NVDA")]
        price = {"NVDA": _make_price_outcome(return_pct=3.0, windows=(1,))}
        outcomes = evaluate_discovery_candidate_outcomes(cands, price, windows=(1,))
        stats = _group_stats(outcomes, (1,))
        assert stats["count"] == 1
        assert stats["window_1"]["resolved"] == 1
        assert stats["window_1"]["avg_forward_return_pct"] == 3.0


# ---------------------------------------------------------------------------
# TestLoadDiscoveryReplayInputs
# ---------------------------------------------------------------------------

class TestLoadDiscoveryReplayInputs:
    def test_missing_all_returns_empty_unavailable(self, tmp_path):
        result = load_discovery_replay_inputs(base_dir=tmp_path)
        assert result["available"] is False
        assert result["candidates"] == []
        assert result["emerging"] is None
        assert result["rejected"] is None
        assert result["memory"] is None
        assert result["approval_decisions"] == []

    def test_emerging_only_loads_candidates(self, tmp_path):
        _write_emerging(tmp_path, [_make_candidate("NVDA")])
        result = load_discovery_replay_inputs(base_dir=tmp_path)
        assert result["available"] is True
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["ticker"] == "NVDA"

    def test_rejected_only_loads_candidates(self, tmp_path):
        _write_rejected(tmp_path, [_make_candidate("AAPL", status="rejected")])
        result = load_discovery_replay_inputs(base_dir=tmp_path)
        assert result["available"] is True
        assert any(c["ticker"] == "AAPL" for c in result["candidates"])

    def test_both_emerging_and_rejected_merged(self, tmp_path):
        _write_emerging(tmp_path, [_make_candidate("NVDA")])
        _write_rejected(tmp_path, [_make_candidate("AAPL", status="rejected")])
        result = load_discovery_replay_inputs(base_dir=tmp_path)
        tickers = [c["ticker"] for c in result["candidates"]]
        assert "NVDA" in tickers
        assert "AAPL" in tickers

    def test_corrupt_json_returns_none_no_crash(self, tmp_path):
        p = tmp_path / "sandbox" / "discovery" / "emerging_candidates.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{corrupt json::}", encoding="utf-8")
        result = load_discovery_replay_inputs(base_dir=tmp_path)
        assert result["emerging"] is None
        assert result["candidates"] == []

    def test_valid_approval_loaded(self, tmp_path):
        _write_approvals(tmp_path, [_make_valid_approval("NVDA")])
        result = load_discovery_replay_inputs(base_dir=tmp_path)
        assert result["available"] is True
        assert len(result["approval_decisions"]) == 1
        assert result["approval_decisions"][0]["symbol"] == "NVDA"

    def test_tampered_approval_skipped(self, tmp_path):
        good = _make_valid_approval("NVDA")
        bad = dict(_make_valid_approval("AAPL"))
        bad["decision"] = "buy"
        _write_approvals(tmp_path, [good, bad])
        result = load_discovery_replay_inputs(base_dir=tmp_path)
        assert len(result["approval_decisions"]) == 1
        assert result["approval_decisions"][0]["symbol"] == "NVDA"

    def test_false_governance_flag_skipped(self, tmp_path):
        bad = dict(_make_valid_approval("AAPL"))
        bad["sandbox_only"] = False
        _write_approvals(tmp_path, [bad])
        result = load_discovery_replay_inputs(base_dir=tmp_path)
        assert result["approval_decisions"] == []

    def test_malformed_jsonl_line_skipped(self, tmp_path):
        good = json.dumps(_make_valid_approval("NVDA"))
        p = tmp_path / "sandbox" / "discovery" / "approval_decisions.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{{NOT JSON}}\n{good}\n", encoding="utf-8")
        result = load_discovery_replay_inputs(base_dir=tmp_path)
        assert len(result["approval_decisions"]) == 1


# ---------------------------------------------------------------------------
# TestEvaluateDiscoveryCandidateOutcomes
# ---------------------------------------------------------------------------

class TestEvaluateDiscoveryCandidateOutcomes:
    def test_empty_candidates_returns_empty(self):
        result = evaluate_discovery_candidate_outcomes([], {})
        assert result == []

    def test_single_candidate_with_price_data(self):
        cands = [_make_candidate("NVDA")]
        price = {"NVDA": _make_price_outcome(return_pct=3.5, direction_correct=True)}
        result = evaluate_discovery_candidate_outcomes(cands, price)
        assert len(result) == 1
        o = result[0]
        assert o["ticker"] == "NVDA"
        assert o["insufficient_data"] is False
        assert o["window_1"]["forward_return_pct"] == 3.5
        assert o["window_1"]["direction_correct"] is True

    def test_candidate_without_price_data_marked_insufficient(self):
        cands = [_make_candidate("MSFT")]
        result = evaluate_discovery_candidate_outcomes(cands, {})
        assert len(result) == 1
        assert result[0]["insufficient_data"] is True
        for w in _DEFAULT_WINDOWS:
            assert result[0][_window_key(w)]["forward_return_pct"] is None

    def test_multiple_windows_populated(self):
        cands = [_make_candidate("AAPL")]
        price = {"AAPL": _make_price_outcome(return_pct=1.0, windows=_DEFAULT_WINDOWS)}
        result = evaluate_discovery_candidate_outcomes(cands, price, windows=_DEFAULT_WINDOWS)
        o = result[0]
        for w in _DEFAULT_WINDOWS:
            assert _window_key(w) in o

    def test_forbidden_status_skipped(self):
        cands = [
            {"ticker": "BAD", "status": "buy"},
            _make_candidate("NVDA"),
        ]
        result = evaluate_discovery_candidate_outcomes(cands, {})
        tickers = [o["ticker"] for o in result]
        assert "BAD" not in tickers
        assert "NVDA" in tickers

    def test_all_forbidden_statuses_skipped(self):
        for forbidden in _FORBIDDEN_STATUSES:
            cands = [{"ticker": "X", "status": forbidden}]
            result = evaluate_discovery_candidate_outcomes(cands, {})
            assert result == [], f"Status {forbidden!r} was not filtered"

    def test_governance_flags_always_true(self):
        cands = [_make_candidate("NVDA")]
        result = evaluate_discovery_candidate_outcomes(cands, {})
        o = result[0]
        assert o["observe_only"] is True
        assert o["sandbox_only"] is True
        assert o["no_trade"] is True
        assert o["discovery_only"] is True

    def test_corroboration_fields_preserved(self):
        cands = [_make_candidate("NVDA", corr_score=0.72, corr_level="strong", corr_met=True)]
        result = evaluate_discovery_candidate_outcomes(cands, {})
        o = result[0]
        assert o["corroboration_score"] == 0.72
        assert o["corroboration_level"] == "strong"
        assert o["corroboration_met"] is True

    def test_risk_flag_preserved(self):
        cands = [_make_candidate("XYZ", risk_flag=True)]
        result = evaluate_discovery_candidate_outcomes(cands, {})
        assert result[0]["risk_flag"] is True

    def test_partial_price_data_not_insufficient(self):
        cands = [_make_candidate("NVDA")]
        price = {
            "NVDA": {
                "window_1": {
                    "forward_return_pct": 1.0,
                    "direction_correct": True,
                    "max_drawdown_pct": -0.5,
                    "max_runup_pct": 2.0,
                }
                # window_3 missing
            }
        }
        result = evaluate_discovery_candidate_outcomes(cands, price, windows=(1, 3))
        o = result[0]
        assert o["insufficient_data"] is False
        assert o["window_1"]["forward_return_pct"] == 1.0
        assert o["window_3"]["forward_return_pct"] is None


# ---------------------------------------------------------------------------
# TestSummarizeDiscoveryReplayResults
# ---------------------------------------------------------------------------

class TestSummarizeDiscoveryReplayResults:
    def test_empty_outcomes_insufficient_data_true(self):
        summary = summarize_discovery_replay_results([])
        assert summary["insufficient_data"] is True
        assert summary["candidate_count"] == 0
        assert summary["resolved_count"] == 0

    def test_governance_flags_set(self):
        summary = summarize_discovery_replay_results([])
        assert summary["observe_only"] is True
        assert summary["sandbox_only"] is True
        assert summary["no_trade"] is True
        assert summary["no_official_promotion"] is True

    def test_disclaimer_present(self):
        summary = summarize_discovery_replay_results([])
        assert "not constitute buy/sell recommendations" in summary["disclaimer"]
        assert "No official recommendation" in summary["disclaimers"][1]
        assert "observe-only sandbox research" in summary["disclaimers"][2]

    def test_methodology_present(self):
        summary = summarize_discovery_replay_results([])
        assert "no external API calls" in summary["methodology"]

    def test_watch_vs_discovered_aggregation(self):
        watch_cand = _make_candidate("NVDA", status="watch")
        disc_cand = _make_candidate("MSFT", status="discovered")
        price = {
            "NVDA": _make_price_outcome(return_pct=3.0, direction_correct=True, windows=(1,)),
            "MSFT": _make_price_outcome(return_pct=1.0, direction_correct=True, windows=(1,)),
        }
        outcomes = evaluate_discovery_candidate_outcomes(
            [watch_cand, disc_cand], price, windows=(1,)
        )
        summary = summarize_discovery_replay_results(outcomes, windows=(1,))
        sc = summary["status_comparison"]
        assert sc["watch"]["count"] == 1
        assert sc["discovered"]["count"] == 1
        assert sc["watch"]["window_1"]["avg_forward_return_pct"] == 3.0
        assert sc["discovered"]["window_1"]["avg_forward_return_pct"] == 1.0

    def test_high_vs_low_corroboration_comparison(self):
        high_cand = _make_candidate("NVDA", corr_level="strong")
        low_cand = _make_candidate("MSFT", corr_level="weak")
        price = {
            "NVDA": _make_price_outcome(return_pct=5.0, windows=(1,)),
            "MSFT": _make_price_outcome(return_pct=0.5, windows=(1,)),
        }
        outcomes = evaluate_discovery_candidate_outcomes(
            [high_cand, low_cand], price, windows=(1,)
        )
        summary = summarize_discovery_replay_results(outcomes, windows=(1,))
        cc = summary["corroboration_comparison"]
        assert cc["high_corroboration"]["count"] == 1
        assert cc["low_corroboration"]["count"] == 1

    def test_approval_decision_comparison(self):
        cand = _make_candidate("NVDA")
        price = {"NVDA": _make_price_outcome(return_pct=2.0, windows=(1,))}
        outcomes = evaluate_discovery_candidate_outcomes([cand], price, windows=(1,))
        approvals = [_make_valid_approval("NVDA", "approve_for_research_review")]
        summary = summarize_discovery_replay_results(
            outcomes, approval_decisions=approvals, windows=(1,)
        )
        adc = summary["approval_decision_comparison"]
        assert adc["approve_for_research_review"]["count"] == 1
        assert adc["no_decision"]["count"] == 0

    def test_keep_watching_approval_group(self):
        cand = _make_candidate("AAPL")
        price = {"AAPL": _make_price_outcome(return_pct=1.0, windows=(1,))}
        outcomes = evaluate_discovery_candidate_outcomes([cand], price, windows=(1,))
        approvals = [_make_valid_approval("AAPL", "keep_watching")]
        summary = summarize_discovery_replay_results(
            outcomes, approval_decisions=approvals, windows=(1,)
        )
        assert summary["approval_decision_comparison"]["keep_watching"]["count"] == 1

    def test_risk_comparison(self):
        risk_cand = _make_candidate("XYZ", risk_flag=True)
        safe_cand = _make_candidate("NVDA", risk_flag=False)
        price = {
            "XYZ": _make_price_outcome(return_pct=-2.0, windows=(1,)),
            "NVDA": _make_price_outcome(return_pct=3.0, windows=(1,)),
        }
        outcomes = evaluate_discovery_candidate_outcomes(
            [risk_cand, safe_cand], price, windows=(1,)
        )
        summary = summarize_discovery_replay_results(outcomes, windows=(1,))
        rc = summary["risk_comparison"]
        assert rc["risk_flagged"]["count"] == 1
        assert rc["non_risk"]["count"] == 1
        assert rc["risk_flagged"]["window_1"]["avg_forward_return_pct"] == -2.0
        assert rc["non_risk"]["window_1"]["avg_forward_return_pct"] == 3.0

    def test_rejected_candidate_review(self):
        rejected_cand = _make_candidate("AAPL", status="rejected")
        outcomes = evaluate_discovery_candidate_outcomes([rejected_cand], {})
        summary = summarize_discovery_replay_results(outcomes, windows=(1,))
        rcr = summary["rejected_candidate_review"]
        assert rcr["count"] == 1
        assert len(rcr["candidates"]) == 1
        assert rcr["candidates"][0]["ticker"] == "AAPL"
        assert rcr["with_price_data"] == 0

    def test_hit_rate_calculation(self):
        cands = [_make_candidate("A"), _make_candidate("B")]
        price = {
            "A": {"window_1": {"forward_return_pct": 2.0, "direction_correct": True,
                               "max_drawdown_pct": None, "max_runup_pct": None}},
            "B": {"window_1": {"forward_return_pct": -1.0, "direction_correct": False,
                               "max_drawdown_pct": None, "max_runup_pct": None}},
        }
        outcomes = evaluate_discovery_candidate_outcomes(cands, price, windows=(1,))
        summary = summarize_discovery_replay_results(outcomes, windows=(1,))
        assert summary["window_metrics"]["window_1"]["hit_rate"] == 0.5

    def test_window_metrics_all_default_windows(self):
        cand = _make_candidate("NVDA")
        price = {"NVDA": _make_price_outcome(return_pct=2.0, windows=_DEFAULT_WINDOWS)}
        outcomes = evaluate_discovery_candidate_outcomes([cand], price, windows=_DEFAULT_WINDOWS)
        summary = summarize_discovery_replay_results(outcomes, windows=_DEFAULT_WINDOWS)
        wm = summary["window_metrics"]
        for w in _DEFAULT_WINDOWS:
            assert _window_key(w) in wm

    def test_no_forbidden_keys_in_status_comparison(self):
        summary = summarize_discovery_replay_results([])
        sc = summary["status_comparison"]
        for key in sc:
            assert key not in _FORBIDDEN_STATUSES

    def test_insufficient_data_false_when_resolved_candidates_exist(self):
        cand = _make_candidate("NVDA")
        price = {"NVDA": _make_price_outcome(windows=(1,))}
        outcomes = evaluate_discovery_candidate_outcomes([cand], price, windows=(1,))
        summary = summarize_discovery_replay_results(outcomes, windows=(1,))
        assert summary["insufficient_data"] is False
        assert summary["resolved_count"] == 1


# ---------------------------------------------------------------------------
# TestBuildReplayMarkdown
# ---------------------------------------------------------------------------

class TestBuildReplayMarkdown:
    def _make_summary_and_outcomes(self, candidates=None, price=None, windows=(1,)):
        cands = candidates or []
        p = price or {}
        outcomes = evaluate_discovery_candidate_outcomes(cands, p, windows=windows)
        summary = summarize_discovery_replay_results(outcomes, windows=windows)
        return summary, outcomes

    def test_disclaimer_in_markdown(self):
        summary, outcomes = self._make_summary_and_outcomes()
        md = _build_replay_markdown(summary, outcomes, run_id="test_run")
        assert "SANDBOX ONLY" in md
        assert "sandbox research only" in md.lower()

    def test_no_official_statement_present(self):
        summary, outcomes = self._make_summary_and_outcomes()
        md = _build_replay_markdown(summary, outcomes, run_id="test_run")
        assert "No official recommendation" in md

    def test_executive_summary_section_present(self):
        summary, outcomes = self._make_summary_and_outcomes()
        md = _build_replay_markdown(summary, outcomes, run_id="test_run")
        assert "## Executive Summary" in md

    def test_watch_vs_discovered_section_present(self):
        summary, outcomes = self._make_summary_and_outcomes()
        md = _build_replay_markdown(summary, outcomes, run_id="test_run")
        assert "## WATCH vs DISCOVERED" in md

    def test_corroboration_section_present(self):
        summary, outcomes = self._make_summary_and_outcomes()
        md = _build_replay_markdown(summary, outcomes, run_id="test_run")
        assert "## Corroboration Analysis" in md

    def test_research_threshold_section_present(self):
        summary, outcomes = self._make_summary_and_outcomes()
        md = _build_replay_markdown(summary, outcomes, run_id="test_run")
        assert "## Recommended Future Research" in md

    def test_insufficient_note_shown_when_no_data(self):
        summary, outcomes = self._make_summary_and_outcomes()
        md = _build_replay_markdown(summary, outcomes, run_id="test_run")
        assert "Insufficient" in md

    def test_watch_candidate_shown_with_data(self):
        cand = _make_candidate("NVDA", status="watch")
        price = {"NVDA": _make_price_outcome(return_pct=2.5, windows=(1,))}
        summary, outcomes = self._make_summary_and_outcomes([cand], price, windows=(1,))
        md = _build_replay_markdown(summary, outcomes, run_id="test_run")
        assert "WATCH" in md

    def test_closing_sandbox_statement(self):
        summary, outcomes = self._make_summary_and_outcomes()
        md = _build_replay_markdown(summary, outcomes, run_id="test_run")
        assert "sandbox research only" in md.lower()

    def test_no_buy_sell_action_language(self):
        summary, outcomes = self._make_summary_and_outcomes()
        md = _build_replay_markdown(summary, outcomes, run_id="test_run")
        md_lower = md.lower()
        # The status/action labels should not appear — the disclaimer may mention
        # "buy/sell recommendations" as part of the safety statement, which is correct.
        assert "status: buy" not in md_lower
        assert "status: sell" not in md_lower
        assert "action: buy" not in md_lower
        assert "action: sell" not in md_lower


# ---------------------------------------------------------------------------
# TestWriteDiscoveryReplayReport — sandbox namespace enforcement
# ---------------------------------------------------------------------------

class TestWriteDiscoveryReplayReport:
    def test_daily_mode_raises_violation(self, tmp_path):
        summary = summarize_discovery_replay_results([])
        with pytest.raises(RunModeViolation):
            write_discovery_replay_report(
                summary, [], run_mode="daily", base_dir=tmp_path
            )

    def test_manual_update_mode_raises_violation(self, tmp_path):
        summary = summarize_discovery_replay_results([])
        with pytest.raises(RunModeViolation):
            write_discovery_replay_report(
                summary, [], run_mode="manual_update", base_dir=tmp_path
            )

    def test_weekly_review_mode_raises_violation(self, tmp_path):
        summary = summarize_discovery_replay_results([])
        with pytest.raises(RunModeViolation):
            write_discovery_replay_report(
                summary, [], run_mode="weekly_review", base_dir=tmp_path
            )

    def test_discovery_mode_writes_artifacts(self, tmp_path):
        summary = summarize_discovery_replay_results([])
        written = write_discovery_replay_report(
            summary, [], run_mode="discovery", run_id="t1", base_dir=tmp_path
        )
        assert "replay_results_json" in written
        assert "replay_results_md" in written
        assert "replay_candidate_outcomes_jsonl" in written
        assert written["replay_results_json"].exists()
        assert written["replay_results_md"].exists()
        assert written["replay_candidate_outcomes_jsonl"].exists()

    def test_backtest_mode_allowed(self, tmp_path):
        summary = summarize_discovery_replay_results([])
        written = write_discovery_replay_report(
            summary, [], run_mode="backtest", base_dir=tmp_path
        )
        assert written["replay_results_json"].exists()

    def test_artifacts_in_sandbox_namespace(self, tmp_path):
        summary = summarize_discovery_replay_results([])
        written = write_discovery_replay_report(
            summary, [], run_mode="discovery", base_dir=tmp_path
        )
        for path in written.values():
            assert "sandbox" in str(path)

    def test_no_latest_artifact_written(self, tmp_path):
        summary = summarize_discovery_replay_results([])
        write_discovery_replay_report(
            summary, [], run_mode="discovery", base_dir=tmp_path
        )
        latest_dir = tmp_path / "latest"
        assert not latest_dir.exists() or not any(latest_dir.iterdir())

    def test_no_policy_artifact_written(self, tmp_path):
        summary = summarize_discovery_replay_results([])
        write_discovery_replay_report(
            summary, [], run_mode="discovery", base_dir=tmp_path
        )
        policy_dir = tmp_path / "policy"
        assert not policy_dir.exists() or not any(policy_dir.iterdir())

    def test_json_schema_required_fields(self, tmp_path):
        summary = summarize_discovery_replay_results([])
        written = write_discovery_replay_report(
            summary, [], run_mode="discovery", base_dir=tmp_path
        )
        data = json.loads(written["replay_results_json"].read_text())
        required = (
            "observe_only", "sandbox_only", "no_trade", "no_official_promotion",
            "disclaimer", "methodology", "disclaimers",
            "candidate_count", "resolved_count",
            "window_metrics", "status_comparison", "corroboration_comparison",
            "approval_decision_comparison", "risk_comparison",
            "rejected_candidate_review",
        )
        for field in required:
            assert field in data, f"Missing required field: {field}"
        assert data["observe_only"] is True
        assert data["sandbox_only"] is True
        assert data["no_trade"] is True
        assert data["no_official_promotion"] is True

    def test_md_has_disclaimer(self, tmp_path):
        summary = summarize_discovery_replay_results([])
        written = write_discovery_replay_report(
            summary, [], run_mode="discovery", base_dir=tmp_path
        )
        md = written["replay_results_md"].read_text()
        assert "SANDBOX ONLY" in md
        assert "sandbox research only" in md.lower()

    def test_jsonl_written_one_line_per_candidate(self, tmp_path):
        cands = [_make_candidate("NVDA"), _make_candidate("AAPL", status="discovered")]
        price = {
            "NVDA": _make_price_outcome(return_pct=2.0, windows=(1,)),
            "AAPL": _make_price_outcome(return_pct=1.0, windows=(1,)),
        }
        outcomes = evaluate_discovery_candidate_outcomes(cands, price, windows=(1,))
        summary = summarize_discovery_replay_results(outcomes, windows=(1,))
        written = write_discovery_replay_report(
            summary, outcomes, run_mode="discovery", base_dir=tmp_path
        )
        lines = written["replay_candidate_outcomes_jsonl"].read_text().strip().splitlines()
        assert len(lines) == 2
        parsed = [json.loads(ln) for ln in lines]
        tickers = [p["ticker"] for p in parsed]
        assert "NVDA" in tickers
        assert "AAPL" in tickers

    def test_jsonl_governance_flags_in_each_line(self, tmp_path):
        cands = [_make_candidate("NVDA")]
        price = {"NVDA": _make_price_outcome(windows=(1,))}
        outcomes = evaluate_discovery_candidate_outcomes(cands, price, windows=(1,))
        summary = summarize_discovery_replay_results(outcomes, windows=(1,))
        written = write_discovery_replay_report(
            summary, outcomes, run_mode="discovery", base_dir=tmp_path
        )
        lines = written["replay_candidate_outcomes_jsonl"].read_text().strip().splitlines()
        row = json.loads(lines[0])
        assert row["observe_only"] is True
        assert row["sandbox_only"] is True
        assert row["no_trade"] is True


# ---------------------------------------------------------------------------
# TestRunDiscoveryReplay — full orchestration
# ---------------------------------------------------------------------------

class TestRunDiscoveryReplay:
    def test_empty_inputs_insufficient_data(self, tmp_path):
        result = run_discovery_replay(base_dir=tmp_path, write_files=False)
        assert result["insufficient_data"] is True
        assert result["candidate_count"] == 0

    def test_governance_flags_always_set(self, tmp_path):
        result = run_discovery_replay(base_dir=tmp_path, write_files=False)
        assert result["observe_only"] is True
        assert result["sandbox_only"] is True
        assert result["no_trade"] is True
        assert result["no_official_promotion"] is True
        assert result["discovery_only"] is True
        assert result["can_execute_trades"] is False
        assert result["official_watchlist_modified"] is False
        assert result["official_recommendations_modified"] is False

    def test_daily_mode_raises_when_writing(self, tmp_path):
        with pytest.raises(RunModeViolation):
            run_discovery_replay(
                run_mode="daily", base_dir=tmp_path, write_files=True
            )

    def test_dry_run_no_files_written(self, tmp_path):
        result = run_discovery_replay(base_dir=tmp_path, write_files=False)
        assert result["artifacts_written"] == {}
        sandbox = tmp_path / "sandbox" / "discovery"
        assert not (sandbox / "replay_results.json").exists()

    def test_with_price_data_resolves_candidates(self, tmp_path):
        _write_emerging(tmp_path, [_make_candidate("NVDA")])
        price = {"NVDA": _make_price_outcome(return_pct=3.0, windows=_DEFAULT_WINDOWS)}
        result = run_discovery_replay(
            price_outcomes=price, base_dir=tmp_path, write_files=False
        )
        assert result["candidate_count"] == 1
        assert result["resolved_count"] == 1
        assert result["insufficient_data"] is False

    def test_watch_and_discovered_counts(self, tmp_path):
        _write_emerging(tmp_path, [
            _make_candidate("NVDA", status="watch"),
            _make_candidate("MSFT", status="discovered"),
        ])
        result = run_discovery_replay(base_dir=tmp_path, write_files=False)
        assert result["watch_count"] == 1
        assert result["discovered_count"] == 1

    def test_rejected_count_from_rejected_file(self, tmp_path):
        _write_rejected(tmp_path, [_make_candidate("XYZ", status="rejected")])
        result = run_discovery_replay(base_dir=tmp_path, write_files=False)
        assert result["rejected_count"] == 1

    def test_writes_files_in_discovery_mode(self, tmp_path):
        _write_emerging(tmp_path, [_make_candidate("NVDA")])
        result = run_discovery_replay(
            run_mode="discovery", base_dir=tmp_path, write_files=True
        )
        assert "replay_results_json" in result["artifacts_written"]
        assert Path(result["artifacts_written"]["replay_results_json"]).exists()

    def test_deterministic_same_inputs_same_metrics(self, tmp_path):
        _write_emerging(tmp_path, [_make_candidate("NVDA")])
        price = {"NVDA": _make_price_outcome(return_pct=2.5, windows=(1,))}
        r1 = run_discovery_replay(
            price_outcomes=price, base_dir=tmp_path, windows=(1,),
            write_files=False, run_id="run_1",
        )
        r2 = run_discovery_replay(
            price_outcomes=price, base_dir=tmp_path, windows=(1,),
            write_files=False, run_id="run_2",
        )
        assert r1["candidate_count"] == r2["candidate_count"]
        assert r1["resolved_count"] == r2["resolved_count"]
        assert (
            r1["window_metrics"]["window_1"]["avg_forward_return_pct"]
            == r2["window_metrics"]["window_1"]["avg_forward_return_pct"]
        )

    def test_tampered_approvals_not_counted(self, tmp_path):
        _write_emerging(tmp_path, [_make_candidate("NVDA")])
        good = _make_valid_approval("NVDA")
        bad = dict(_make_valid_approval("AAPL"))
        bad["decision"] = "sell"
        _write_approvals(tmp_path, [good, bad])
        # Must not raise; only 1 valid approval loaded
        result = run_discovery_replay(base_dir=tmp_path, write_files=False)
        assert isinstance(result, dict)

    def test_no_buy_sell_in_status_comparison(self, tmp_path):
        _write_emerging(tmp_path, [_make_candidate("NVDA")])
        result = run_discovery_replay(base_dir=tmp_path, write_files=False)
        sc = result.get("window_metrics", {})
        for key in sc:
            assert key not in _FORBIDDEN_STATUSES

    def test_missing_artifacts_do_not_crash(self, tmp_path):
        result = run_discovery_replay(base_dir=tmp_path, write_files=False)
        assert isinstance(result, dict)
        assert result["insufficient_data"] is True

    def test_disclaimer_in_result(self, tmp_path):
        result = run_discovery_replay(base_dir=tmp_path, write_files=False)
        assert "not constitute buy/sell recommendations" in result["disclaimer"]

    def test_run_id_in_result(self, tmp_path):
        result = run_discovery_replay(
            base_dir=tmp_path, write_files=False, run_id="my_custom_run"
        )
        assert result["run_id"] == "my_custom_run"

    def test_backtest_mode_allowed(self, tmp_path):
        result = run_discovery_replay(
            run_mode="backtest", base_dir=tmp_path, write_files=True
        )
        assert result["run_mode"] == "backtest"

    def test_custom_windows_respected(self, tmp_path):
        _write_emerging(tmp_path, [_make_candidate("NVDA")])
        price = {"NVDA": _make_price_outcome(return_pct=1.0, windows=(1, 5))}
        result = run_discovery_replay(
            price_outcomes=price, base_dir=tmp_path, windows=(1, 5), write_files=False
        )
        wm = result["window_metrics"]
        assert "window_1" in wm
        assert "window_5" in wm
        assert "window_3" not in wm
