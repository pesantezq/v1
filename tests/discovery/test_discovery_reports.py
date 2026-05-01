"""Tests for portfolio_automation.discovery.discovery_reports."""
import json
import pytest
from pathlib import Path
from datetime import datetime, timezone

from portfolio_automation.discovery.candidate_promotion_engine import (
    CandidateStatus,
    DiscoveryCandidate,
)
from portfolio_automation.discovery.event_classifier import EventType
from portfolio_automation.discovery.discovery_memory import DiscoveryMemory
from portfolio_automation.discovery.discovery_reports import (
    run_discovery_engine,
    write_discovery_reports,
    _DISCLAIMER,
)
from portfolio_automation.run_mode_governance import RunModeViolation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = "2026-05-01T00:00:00+00:00"


def _make_candidate(
    ticker: str,
    status: CandidateStatus = CandidateStatus.DISCOVERED,
    score: float = 1.0,
    risk_flag: bool = False,
) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        ticker=ticker,
        status=status,
        score=score,
        mention_count=2,
        unique_source_count=1,
        event_type=EventType.EARNINGS,
        event_confidence=0.5,
        risk_flag=risk_flag,
        rejection_reason="risk" if status == CandidateStatus.REJECTED else None,
        first_seen=_TS,
        last_seen=_TS,
    )


def _mem_with(*tickers) -> DiscoveryMemory:
    mem = DiscoveryMemory()
    cands = [_make_candidate(t) for t in tickers]
    mem.update(cands)
    return mem


# ---------------------------------------------------------------------------
# 1. write_discovery_reports — sandbox paths only
# ---------------------------------------------------------------------------

class TestWriteDiscoveryReports:
    def test_writes_to_sandbox_subdir(self, tmp_path):
        cands = [_make_candidate("NVDA", status=CandidateStatus.WATCH, score=3.0)]
        mem = _mem_with("NVDA")
        written = write_discovery_reports(
            cands, mem, run_mode="discovery", run_id="test_run", base_dir=str(tmp_path)
        )
        for name, path in written.items():
            assert "sandbox" in str(path), f"{name}: expected sandbox path, got {path}"

    def test_writes_to_discovery_subdir(self, tmp_path):
        cands = [_make_candidate("NVDA")]
        mem = _mem_with("NVDA")
        written = write_discovery_reports(
            cands, mem, run_mode="discovery", base_dir=str(tmp_path)
        )
        for path in written.values():
            assert "discovery" in str(path)

    def test_does_not_write_outside_sandbox_root(self, tmp_path):
        cands = [_make_candidate("NVDA")]
        mem = _mem_with("NVDA")
        written = write_discovery_reports(
            cands, mem, run_mode="discovery", base_dir=str(tmp_path)
        )
        sandbox_root = tmp_path / "sandbox"
        for name, path in written.items():
            assert path.is_relative_to(sandbox_root), (
                f"{name}: expected path under {sandbox_root}, got {path}"
            )

    def test_does_not_write_to_portfolio(self, tmp_path):
        cands = [_make_candidate("NVDA")]
        mem = _mem_with("NVDA")
        written = write_discovery_reports(
            cands, mem, run_mode="discovery", base_dir=str(tmp_path)
        )
        portfolio_root = tmp_path / "portfolio"
        for path in written.values():
            assert not path.is_relative_to(portfolio_root)

    def test_emerging_candidates_json_written(self, tmp_path):
        cands = [_make_candidate("NVDA", status=CandidateStatus.WATCH, score=3.0)]
        mem = _mem_with("NVDA")
        written = write_discovery_reports(cands, mem, run_mode="discovery", base_dir=str(tmp_path))
        assert "emerging_candidates" in written
        assert written["emerging_candidates"].exists()

    def test_rejected_candidates_json_written(self, tmp_path):
        cands = [_make_candidate("BADCO", status=CandidateStatus.REJECTED, score=0.1)]
        mem = _mem_with("BADCO")
        written = write_discovery_reports(cands, mem, run_mode="discovery", base_dir=str(tmp_path))
        assert "rejected_candidates" in written
        assert written["rejected_candidates"].exists()

    def test_discovery_memory_json_written(self, tmp_path):
        cands = [_make_candidate("NVDA")]
        mem = _mem_with("NVDA")
        written = write_discovery_reports(cands, mem, run_mode="discovery", base_dir=str(tmp_path))
        assert "discovery_memory" in written
        assert written["discovery_memory"].exists()

    def test_discovery_memo_md_written(self, tmp_path):
        cands = [_make_candidate("NVDA")]
        mem = _mem_with("NVDA")
        written = write_discovery_reports(cands, mem, run_mode="discovery", base_dir=str(tmp_path))
        assert "discovery_memo_section" in written
        assert written["discovery_memo_section"].exists()


# ---------------------------------------------------------------------------
# 2. JSON artifact contents
# ---------------------------------------------------------------------------

class TestJsonArtifactContents:
    def test_emerging_json_has_disclaimer(self, tmp_path):
        cands = [_make_candidate("NVDA", status=CandidateStatus.WATCH, score=3.0)]
        mem = _mem_with("NVDA")
        written = write_discovery_reports(cands, mem, run_mode="discovery", base_dir=str(tmp_path))
        data = json.loads(written["emerging_candidates"].read_text())
        assert "disclaimer" in data
        assert "not buy/sell" in data["disclaimer"].lower() or "not buy" in data["disclaimer"].lower()

    def test_emerging_json_discovery_only_true(self, tmp_path):
        cands = [_make_candidate("NVDA")]
        mem = _mem_with("NVDA")
        written = write_discovery_reports(cands, mem, run_mode="discovery", base_dir=str(tmp_path))
        data = json.loads(written["emerging_candidates"].read_text())
        assert data["discovery_only"] is True

    def test_emerging_json_sandbox_only_true(self, tmp_path):
        cands = [_make_candidate("NVDA")]
        mem = _mem_with("NVDA")
        written = write_discovery_reports(cands, mem, run_mode="discovery", base_dir=str(tmp_path))
        data = json.loads(written["emerging_candidates"].read_text())
        assert data["sandbox_only"] is True

    def test_emerging_json_observe_only_true(self, tmp_path):
        cands = [_make_candidate("NVDA")]
        mem = _mem_with("NVDA")
        written = write_discovery_reports(cands, mem, run_mode="discovery", base_dir=str(tmp_path))
        data = json.loads(written["emerging_candidates"].read_text())
        assert data["observe_only"] is True

    def test_rejected_json_has_rejected_candidates(self, tmp_path):
        cands = [_make_candidate("BADCO", status=CandidateStatus.REJECTED, score=0.1)]
        mem = _mem_with("BADCO")
        written = write_discovery_reports(cands, mem, run_mode="discovery", base_dir=str(tmp_path))
        data = json.loads(written["rejected_candidates"].read_text())
        assert data["total_rejected"] == 1

    def test_memory_json_has_entries(self, tmp_path):
        cands = [_make_candidate("NVDA"), _make_candidate("AAPL")]
        mem = _mem_with("NVDA", "AAPL")
        written = write_discovery_reports(cands, mem, run_mode="discovery", base_dir=str(tmp_path))
        data = json.loads(written["discovery_memory"].read_text())
        assert data["entry_count"] == 2


# ---------------------------------------------------------------------------
# 3. Markdown memo contents
# ---------------------------------------------------------------------------

class TestMarkdownMemoContents:
    def test_memo_contains_disclaimer(self, tmp_path):
        cands = [_make_candidate("NVDA")]
        mem = _mem_with("NVDA")
        written = write_discovery_reports(cands, mem, run_mode="discovery", base_dir=str(tmp_path))
        md = written["discovery_memo_section"].read_text()
        assert "not buy/sell" in md.lower() or "not buy" in md.lower()

    def test_memo_contains_no_official_action_warning(self, tmp_path):
        cands = [_make_candidate("NVDA")]
        mem = _mem_with("NVDA")
        written = write_discovery_reports(cands, mem, run_mode="discovery", base_dir=str(tmp_path))
        md = written["discovery_memo_section"].read_text()
        assert "not modified" in md.lower() or "official" in md.lower()

    def test_memo_contains_discovery_only_flag(self, tmp_path):
        cands = [_make_candidate("NVDA")]
        mem = _mem_with("NVDA")
        written = write_discovery_reports(cands, mem, run_mode="discovery", base_dir=str(tmp_path))
        md = written["discovery_memo_section"].read_text()
        assert "discovery_only" in md

    def test_memo_contains_sandbox_only_flag(self, tmp_path):
        cands = [_make_candidate("NVDA")]
        mem = _mem_with("NVDA")
        written = write_discovery_reports(cands, mem, run_mode="discovery", base_dir=str(tmp_path))
        md = written["discovery_memo_section"].read_text()
        assert "sandbox_only" in md

    def test_memo_contains_watch_section(self, tmp_path):
        cands = [_make_candidate("NVDA", status=CandidateStatus.WATCH, score=3.0)]
        mem = _mem_with("NVDA")
        written = write_discovery_reports(cands, mem, run_mode="discovery", base_dir=str(tmp_path))
        md = written["discovery_memo_section"].read_text()
        assert "WATCH" in md


# ---------------------------------------------------------------------------
# 4. Run mode governance enforcement
# ---------------------------------------------------------------------------

class TestRunModeGovernance:
    def test_discovery_mode_can_write_sandbox(self, tmp_path):
        cands = [_make_candidate("NVDA")]
        mem = _mem_with("NVDA")
        # Should not raise
        write_discovery_reports(cands, mem, run_mode="discovery", base_dir=str(tmp_path))

    def test_daily_mode_cannot_write_sandbox(self, tmp_path):
        cands = [_make_candidate("NVDA")]
        mem = _mem_with("NVDA")
        with pytest.raises(RunModeViolation):
            write_discovery_reports(cands, mem, run_mode="daily", base_dir=str(tmp_path))

    def test_manual_update_cannot_write_sandbox(self, tmp_path):
        cands = [_make_candidate("NVDA")]
        mem = _mem_with("NVDA")
        with pytest.raises(RunModeViolation):
            write_discovery_reports(cands, mem, run_mode="manual_update", base_dir=str(tmp_path))

    def test_weekly_review_cannot_write_sandbox(self, tmp_path):
        cands = [_make_candidate("NVDA")]
        mem = _mem_with("NVDA")
        with pytest.raises(RunModeViolation):
            write_discovery_reports(cands, mem, run_mode="weekly_review", base_dir=str(tmp_path))

    def test_historical_replay_cannot_write_sandbox(self, tmp_path):
        cands = [_make_candidate("NVDA")]
        mem = _mem_with("NVDA")
        with pytest.raises(RunModeViolation):
            write_discovery_reports(cands, mem, run_mode="historical_replay", base_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# 5. run_discovery_engine orchestration
# ---------------------------------------------------------------------------

class TestRunDiscoveryEngine:
    def test_returns_summary_dict(self):
        records = [{"title": "$NVDA beats earnings quarterly results"}]
        result = run_discovery_engine(records, run_mode="discovery", write_files=False)
        assert isinstance(result, dict)

    def test_summary_has_governance_flags(self):
        result = run_discovery_engine([], run_mode="discovery", write_files=False)
        assert result["discovery_only"] is True
        assert result["sandbox_only"] is True
        assert result["observe_only"] is True
        assert result["can_execute_trades"] is False
        assert result["official_watchlist_modified"] is False
        assert result["official_recommendations_modified"] is False

    def test_records_processed_count(self):
        records = [
            {"title": "$NVDA rises"},
            {"title": "$AAPL falls"},
        ]
        result = run_discovery_engine(records, run_mode="discovery", write_files=False)
        assert result["records_processed"] == 2

    def test_corroboration_required_in_summary(self):
        result = run_discovery_engine([], run_mode="discovery", write_files=False)
        assert result["corroboration_required"] is True

    def test_empty_records_no_crash(self):
        result = run_discovery_engine([], run_mode="discovery", write_files=False)
        assert result["tickers_extracted"] == 0
        assert result["total_candidates"] == 0

    def test_write_files_true_writes_sandbox(self, tmp_path):
        records = [{"title": "$NVDA beats earnings quarterly results", "source": "test"}]
        result = run_discovery_engine(
            records,
            run_mode="discovery",
            write_files=True,
            base_dir=str(tmp_path),
        )
        assert len(result["artifacts_written"]) > 0
        for path_str in result["artifacts_written"].values():
            assert "sandbox" in path_str

    def test_write_files_false_no_files_written(self, tmp_path):
        records = [{"title": "$NVDA beats earnings", "source": "test"}]
        result = run_discovery_engine(
            records,
            run_mode="discovery",
            write_files=False,
            base_dir=str(tmp_path),
        )
        assert result["artifacts_written"] == {}

    def test_daily_mode_with_write_raises(self, tmp_path):
        records = [{"title": "$NVDA"}]
        with pytest.raises(RunModeViolation):
            run_discovery_engine(
                records, run_mode="daily", write_files=True, base_dir=str(tmp_path)
            )

    def test_known_universe_filters_tickers(self):
        records = [{"title": "$NVDA and $AAPL"}]
        result = run_discovery_engine(
            records,
            run_mode="discovery",
            write_files=False,
            known_universe={"AAPL"},
        )
        assert all(t == "AAPL" for t in result["watch_tickers"] + result.get("discovered_tickers", []))

    def test_memory_path_none_starts_fresh(self):
        records = [{"title": "$NVDA beats earnings quarterly results"}]
        result = run_discovery_engine(
            records, run_mode="discovery", write_files=False, memory_path=None
        )
        assert result["tickers_extracted"] >= 1

    def test_disclaimer_in_summary(self):
        result = run_discovery_engine([], run_mode="discovery", write_files=False)
        assert "disclaimer" in result
        assert "not buy" in result["disclaimer"].lower()


# ---------------------------------------------------------------------------
# 6. Disclaimer constant
# ---------------------------------------------------------------------------

class TestDisclaimer:
    def test_disclaimer_contains_not_buy_sell(self):
        assert "not buy/sell" in _DISCLAIMER.lower() or "not buy" in _DISCLAIMER.lower()

    def test_disclaimer_is_non_empty_string(self):
        assert isinstance(_DISCLAIMER, str)
        assert len(_DISCLAIMER) > 20
