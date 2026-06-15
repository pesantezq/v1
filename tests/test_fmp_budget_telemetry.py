"""
Tests for portfolio_automation/fmp_budget_telemetry.py.

Covers:
  - Budget state read with present/absent counter
  - News outcome read shape
  - Cache stats degradation when dir missing
  - overall_status decision ladder (ok / near_cap / exhausted / news_empty)
  - History ledger dedup
  - run() writes both artifacts
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.fmp_budget_telemetry import (
    append_to_history,
    build_fmp_budget_status,
    read_budget_state,
    read_news_outcome,
    read_cache_stats,
    run_fmp_budget_telemetry,
)


def _make_repo(td: str, *, budget_count: int, budget_cap: int,
               news_articles: int = 0, news_packets: int = 0) -> Path:
    root = Path(td)
    (root / "config.json").write_text(json.dumps({
        "api_limits": {"fmp_daily_calls_budget": budget_cap},
    }))
    (root / "data" / "fmp_cache").mkdir(parents=True)
    (root / "data" / "fmp_cache" / "call_counter.json").write_text(json.dumps({
        "date": "2026-05-19", "count": budget_count,
    }))
    (root / "outputs" / "latest").mkdir(parents=True)
    (root / "outputs" / "latest" / "news_intelligence.json").write_text(json.dumps({
        "generated_at": "2026-05-19T01:00:00+00:00",
        "article_count_raw": news_articles,
        "article_count_deduped": news_articles,
        "evidence_packet_count": news_packets,
        "official_monitoring_count": news_packets,
        "sandbox_count": 0,
    }))
    return root


class TestReadBudgetState(unittest.TestCase):
    def test_ok_status_when_well_under_budget(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_repo(td, budget_count=50, budget_cap=250)
            r = read_budget_state(root)
            self.assertTrue(r["available"])
            self.assertEqual(r["status"], "ok")
            self.assertEqual(r["headroom"], 200)

    def test_near_cap_when_over_90pct(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_repo(td, budget_count=230, budget_cap=250)
            r = read_budget_state(root)
            self.assertEqual(r["status"], "near_cap")

    def test_exhausted_when_at_or_over_budget(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_repo(td, budget_count=250, budget_cap=250)
            r = read_budget_state(root)
            self.assertEqual(r["status"], "exhausted")
            self.assertEqual(r["headroom"], 0)

    def test_unavailable_when_counter_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config.json").write_text(json.dumps(
                {"api_limits": {"fmp_daily_calls_budget": 250}}
            ))
            r = read_budget_state(root)
            self.assertFalse(r["available"])

    def test_uncapped_when_budget_zero(self):
        # Regression: fmp_daily_calls_budget=0 means "no daily cap" (the
        # 2026-06-12 convention), NOT "no budget configured". The telemetry
        # must report it as an available, uncapped, ok state — otherwise the
        # daily check reads a misleading no_budget_configured / unavailable.
        with tempfile.TemporaryDirectory() as td:
            root = _make_repo(td, budget_count=900, budget_cap=0)
            r = read_budget_state(root)
            self.assertTrue(r["available"])
            self.assertTrue(r.get("uncapped"))
            self.assertEqual(r["status"], "ok")
            self.assertEqual(r["budget"], 0)

    def test_absent_budget_key_is_unconfigured(self):
        # No key at all → genuinely unconfigured (distinct from explicit 0).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config.json").write_text(json.dumps({"api_limits": {}}))
            (root / "data" / "fmp_cache").mkdir(parents=True)
            (root / "data" / "fmp_cache" / "call_counter.json").write_text(
                json.dumps({"date": "2026-05-19", "count": 5})
            )
            r = read_budget_state(root)
            self.assertFalse(r["available"])
            self.assertEqual(r.get("reason"), "no_budget_configured")


class TestReadNewsOutcome(unittest.TestCase):
    def test_reads_counts(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_repo(td, budget_count=50, budget_cap=250,
                              news_articles=42, news_packets=10)
            r = read_news_outcome(root)
            self.assertTrue(r["available"])
            self.assertEqual(r["article_count_raw"], 42)
            self.assertEqual(r["evidence_packet_count"], 10)

    def test_unavailable_when_artifact_missing(self):
        with tempfile.TemporaryDirectory() as td:
            r = read_news_outcome(Path(td))
            self.assertFalse(r["available"])


class TestReadCacheStats(unittest.TestCase):
    def test_returns_file_count_and_size(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "fmp_cache").mkdir(parents=True)
            (root / "data" / "fmp_cache" / "a.json").write_text("x" * 100)
            (root / "data" / "fmp_cache" / "b.json").write_text("y" * 200)
            r = read_cache_stats(root)
            self.assertTrue(r["available"])
            self.assertEqual(r["file_count"], 2)
            self.assertEqual(r["total_size_bytes"], 300)


class TestOverallStatus(unittest.TestCase):
    def test_news_empty_status_when_budget_ok_but_zero_articles(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_repo(td, budget_count=10, budget_cap=250,
                              news_articles=0, news_packets=0)
            payload = build_fmp_budget_status(root=root)
            self.assertEqual(payload["overall_status"], "news_empty")

    def test_ok_status_when_budget_ok_and_articles_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_repo(td, budget_count=10, budget_cap=250,
                              news_articles=5, news_packets=2)
            payload = build_fmp_budget_status(root=root)
            self.assertEqual(payload["overall_status"], "ok")

    def test_exhausted_status_propagates(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_repo(td, budget_count=250, budget_cap=250,
                              news_articles=5, news_packets=2)
            payload = build_fmp_budget_status(root=root)
            self.assertEqual(payload["overall_status"], "exhausted")


class TestHistory(unittest.TestCase):
    def test_dedups_consecutive_identical_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_repo(td, budget_count=10, budget_cap=250,
                              news_articles=5, news_packets=2)
            p1 = build_fmp_budget_status(root=root)
            a1 = append_to_history(p1, root=root)
            a2 = append_to_history(p1, root=root)
            self.assertTrue(a1)
            self.assertFalse(a2)


class TestRunOrchestrator(unittest.TestCase):
    def test_writes_both_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = _make_repo(td, budget_count=10, budget_cap=250,
                              news_articles=5, news_packets=2)
            r = run_fmp_budget_telemetry(root=root)
            self.assertEqual(r["status"], "ok")
            self.assertTrue((root / "outputs" / "latest" / "fmp_budget_status.json").exists())
            self.assertTrue((root / "outputs" / "latest" / "fmp_budget_status.md").exists())
            self.assertIn("FMP budget", r["memo_line"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
