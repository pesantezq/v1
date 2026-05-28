"""
Tests for portfolio_automation/daily_run_status.py.

Covers:
  - Stage log parsing recognises OK / WARN lines + groups preflight subsections
  - Missing log degrades safely
  - Artifact scanning correctly classifies fresh/stale/missing
  - overall_status decision ladder
  - run() writes both artifacts
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.daily_run_status import (
    build_daily_run_status,
    run_daily_run_status,
    scan_content_liveness,
    scan_expected_artifacts,
    scan_log_stages,
)


_FAKE_LOG = """== Daily Safe Wrapper ==
Repo root: /tmp

== Preflight ==

== Repo Root ==
Repo root: /tmp

== Virtual Environment ==
PASS: Activated venv

== Summary ==
PASS: Preflight completed successfully

== Runtime Environment ==

== News intelligence (pre-pipeline) ==
articles: 50 packets: 15
News intelligence (pre-pipeline): OK

== Daily Pipeline ==
Command: python main.py --run-mode daily

== Weight tuning ==
recommended: current
Weight tuning: OK

== Risk delta panel ==
status: ok overall: near_cap
Risk delta panel: OK

== Daily memo + email ==
Subject: ...
Daily memo + email: OK

DAILY RUN PASSED
"""


class TestStageLogParsing(unittest.TestCase):
    def test_recognises_ok_lines(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "test.log"
            log.write_text(_FAKE_LOG)
            stages = scan_log_stages(log)
            names = {s["name"]: s["status"] for s in stages}
            self.assertEqual(names.get("News intelligence (pre-pipeline)"), "ok")
            self.assertEqual(names.get("Weight tuning"), "ok")
            self.assertEqual(names.get("Risk delta panel"), "ok")
            self.assertEqual(names.get("Daily memo + email"), "ok")

    def test_groups_preflight_subsections(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "test.log"
            log.write_text(_FAKE_LOG)
            stages = scan_log_stages(log)
            preflight_count = sum(1 for s in stages if s["name"] == "Preflight")
            self.assertEqual(preflight_count, 1)
            # Subsections that should be grouped under Preflight:
            for hidden in ("Repo Root", "Virtual Environment", "Summary"):
                self.assertNotIn(hidden, [s["name"] for s in stages])

    def test_daily_pipeline_inferred_ok_from_passed_banner(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "test.log"
            log.write_text(_FAKE_LOG)
            stages = scan_log_stages(log)
            dp = [s for s in stages if s["name"] == "Daily Pipeline"]
            self.assertEqual(len(dp), 1)
            self.assertEqual(dp[0]["status"], "ok")

    def test_missing_log_returns_empty(self):
        result = scan_log_stages(Path("/tmp/nonexistent_daily_safe.log"))
        self.assertEqual(result, [])


class TestArtifactScanning(unittest.TestCase):
    def test_classifies_fresh_vs_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            today = datetime.now(timezone.utc).date().isoformat()
            (root / "outputs" / "latest").mkdir(parents=True)
            # Write decision_plan.json fresh
            (root / "outputs" / "latest" / "decision_plan.json").write_text("{}")
            results = scan_expected_artifacts(root)
            for r in results:
                if r["path"] == "outputs/latest/decision_plan.json":
                    self.assertTrue(r["exists"])
                    self.assertTrue(r["fresh_today"])
                elif r["path"] == "outputs/latest/daily_memo.md":
                    self.assertFalse(r["exists"])


class TestOverallStatus(unittest.TestCase):
    def test_ok_when_all_stages_ok_and_no_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = root / "logs" / "test.log"
            log.parent.mkdir(parents=True)
            log.write_text(_FAKE_LOG)
            # Drop all required artifacts as fresh
            today = datetime.now(timezone.utc).date()
            for rel in [
                "outputs/latest/decision_plan.json",
                "outputs/latest/decision_plan.md",
                "outputs/latest/system_decision_summary.json",
                "outputs/latest/daily_memo.md",
                "outputs/latest/daily_memo.txt",
                "outputs/latest/news_intelligence.json",
                "outputs/latest/risk_delta.json",
                "outputs/portfolio/portfolio_snapshot.json",
            ]:
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("{}")
            payload = build_daily_run_status(root=root, log_path=log)
            self.assertEqual(payload["overall_status"], "ok")

    def test_partial_when_required_artifact_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = root / "logs" / "test.log"
            log.parent.mkdir(parents=True)
            log.write_text(_FAKE_LOG)
            # Don't write any artifacts
            payload = build_daily_run_status(root=root, log_path=log)
            self.assertEqual(payload["overall_status"], "partial")
            self.assertGreater(payload["required_missing_count"], 0)


class TestContentLiveness(unittest.TestCase):
    """Content-liveness checks: empty payloads downgrade to warn."""

    def _write_theme_signals(self, root: Path, themes: list) -> None:
        p = root / "outputs" / "latest" / "theme_signals.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"themes": themes}))

    def test_empty_themes_warns(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_theme_signals(root, themes=[])
            results = scan_content_liveness(root)
            theme_row = next(r for r in results if r["name"] == "theme_signals.themes")
            self.assertEqual(theme_row["status"], "warn")
            self.assertEqual(theme_row["observed"], 0)

    def test_nonempty_themes_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_theme_signals(root, themes=[{"name": "AI"}, {"name": "Cyber"}])
            results = scan_content_liveness(root)
            theme_row = next(r for r in results if r["name"] == "theme_signals.themes")
            self.assertEqual(theme_row["status"], "ok")
            self.assertEqual(theme_row["observed"], 2)

    def test_missing_artifact_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            results = scan_content_liveness(root)
            theme_row = next(r for r in results if r["name"] == "theme_signals.themes")
            self.assertEqual(theme_row["status"], "unknown")
            self.assertEqual(theme_row.get("reason"), "artifact_missing")

    def _write_news_intelligence(self, root: Path, articles: int) -> None:
        p = root / "outputs" / "latest" / "news_intelligence.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"article_count_raw": articles}))

    def _write_scraped_intel(self, root: Path, degraded: bool, evidence: int = 0) -> None:
        p = root / "outputs" / "latest" / "scraped_intel_run_summary.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "degraded_mode": degraded,
            "scraped_intel": {"total_evidence": evidence},
        }))

    def _write_ai_budget(self, root: Path, events: int) -> None:
        p = root / "outputs" / "latest" / "ai_budget_summary.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"enabled": True, "event_count": events}))

    def test_zero_news_articles_warns(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_news_intelligence(root, articles=0)
            results = scan_content_liveness(root)
            row = next(r for r in results if r["name"] == "news_intelligence.article_count_raw")
            self.assertEqual(row["status"], "warn")

    def test_nonzero_news_articles_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_news_intelligence(root, articles=42)
            results = scan_content_liveness(root)
            row = next(r for r in results if r["name"] == "news_intelligence.article_count_raw")
            self.assertEqual(row["status"], "ok")
            self.assertEqual(row["observed"], 42)

    def test_scraped_intel_degraded_warns(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_scraped_intel(root, degraded=True, evidence=0)
            results = scan_content_liveness(root)
            row = next(r for r in results if r["name"] == "scraped_intel.degraded_mode")
            self.assertEqual(row["status"], "warn")

    def test_scraped_intel_healthy_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_scraped_intel(root, degraded=False, evidence=15)
            results = scan_content_liveness(root)
            row = next(r for r in results if r["name"] == "scraped_intel.degraded_mode")
            self.assertEqual(row["status"], "ok")
            self.assertEqual(row["observed"], 15)

    def test_ai_budget_zero_events_warns(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_ai_budget(root, events=0)
            results = scan_content_liveness(root)
            row = next(r for r in results if r["name"] == "ai_budget.event_count")
            self.assertEqual(row["status"], "warn")

    def test_ai_budget_with_events_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_ai_budget(root, events=3)
            results = scan_content_liveness(root)
            row = next(r for r in results if r["name"] == "ai_budget.event_count")
            self.assertEqual(row["status"], "ok")
            self.assertEqual(row["observed"], 3)

    def _write_pulse_status(self, root: Path, payload: dict) -> None:
        p = root / "outputs" / "latest" / "discovery_pulse_status.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload))

    def _write_historical_backfill_status(self, root: Path, payload: dict) -> None:
        p = root / "outputs" / "latest" / "historical_backfill_status.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload))

    def test_historical_backfill_fully_errored_warns(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_historical_backfill_status(root, {
                "universe_size": 10, "fetched": 0, "errored": 10, "skipped_fresh": 0,
            })
            row = next(r for r in scan_content_liveness(root)
                       if r["name"] == "historical_backfill.last_run")
            self.assertEqual(row["status"], "warn")

    def test_historical_backfill_mixed_run_is_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_historical_backfill_status(root, {
                "universe_size": 10, "fetched": 8, "errored": 2, "skipped_fresh": 0,
            })
            row = next(r for r in scan_content_liveness(root)
                       if r["name"] == "historical_backfill.last_run")
            self.assertEqual(row["status"], "ok")

    def test_historical_backfill_missing_is_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            row = next(r for r in scan_content_liveness(root)
                       if r["name"] == "historical_backfill.last_run")
            # No artifact OR artifact without universe_size → unknown, not warn
            self.assertIn(row["status"], ("unknown",))

    def test_pulse_last_run_age_fresh_is_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            now = datetime.now(timezone.utc).isoformat()
            self._write_pulse_status(root, {
                "generated_at": now,
                "last_run_at": now,
                "usage": {"total_runs_month": 3},
            })
            results = scan_content_liveness(root)
            row = next(r for r in results if r["name"] == "discovery_pulse.last_run_age")
            self.assertEqual(row["status"], "ok")

    def test_pulse_last_run_age_stale_warns(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # 8 hours ago — beyond the 6h warn threshold
            from datetime import timedelta
            stale = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()
            self._write_pulse_status(root, {
                "generated_at": stale,
                "last_run_at": stale,
                "usage": {"total_runs_month": 3},
            })
            results = scan_content_liveness(root)
            row = next(r for r in results if r["name"] == "discovery_pulse.last_run_age")
            self.assertEqual(row["status"], "warn")
            self.assertGreater(row["observed"], 360)

    def test_pulse_zero_runs_is_unknown_not_warn(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_pulse_status(root, {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "usage": {"total_runs_month": 0},
            })
            results = scan_content_liveness(root)
            row = next(r for r in results if r["name"] == "discovery_pulse.last_run_age")
            self.assertEqual(row["status"], "unknown")

    def test_pulse_cap_status_under_90_is_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_pulse_status(root, {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "usage": {
                    "openai_cost_usd_month": 1.0,
                    "fmp_calls_month": 100,
                    "total_runs_month": 5,
                },
                "caps": {"openai_cost_usd_max": 10.0, "fmp_calls_max": 5000},
            })
            results = scan_content_liveness(root)
            row = next(r for r in results if r["name"] == "discovery_pulse.monthly_cap_status")
            self.assertEqual(row["status"], "ok")
            self.assertLess(row["observed"], 90)

    def test_pulse_cap_status_over_90_warns(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_pulse_status(root, {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "usage": {
                    "openai_cost_usd_month": 9.5,  # 95% of $10
                    "fmp_calls_month": 100,
                    "total_runs_month": 5,
                },
                "caps": {"openai_cost_usd_max": 10.0, "fmp_calls_max": 5000},
            })
            results = scan_content_liveness(root)
            row = next(r for r in results if r["name"] == "discovery_pulse.monthly_cap_status")
            self.assertEqual(row["status"], "warn")
            self.assertGreaterEqual(row["observed"], 90)

    def test_empty_themes_escalates_overall_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = root / "logs" / "test.log"
            log.parent.mkdir(parents=True)
            log.write_text(_FAKE_LOG)
            for rel in [
                "outputs/latest/decision_plan.json",
                "outputs/latest/decision_plan.md",
                "outputs/latest/system_decision_summary.json",
                "outputs/latest/daily_memo.md",
                "outputs/latest/daily_memo.txt",
                "outputs/latest/news_intelligence.json",
                "outputs/latest/risk_delta.json",
                "outputs/portfolio/portfolio_snapshot.json",
            ]:
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("{}")
            self._write_theme_signals(root, themes=[])
            payload = build_daily_run_status(root=root, log_path=log)
            self.assertEqual(payload["overall_status"], "ok_with_warnings")
            self.assertEqual(payload["content_warn_count"], 1)


class TestRunOrchestrator(unittest.TestCase):
    def test_writes_both_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = root / "logs" / "test.log"
            log.parent.mkdir(parents=True)
            log.write_text(_FAKE_LOG)
            r = run_daily_run_status(root=root, log_path=log)
            self.assertEqual(r["status"], "ok")
            self.assertTrue((root / "outputs" / "latest" / "daily_run_status.json").exists())
            self.assertTrue((root / "outputs" / "latest" / "daily_run_status.md").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
