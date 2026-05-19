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
