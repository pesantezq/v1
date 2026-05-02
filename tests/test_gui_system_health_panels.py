"""Tests for GUI system health panel loaders: data quality, AI budget, calibration, discovery."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gui_operator_data import (
    load_data_quality_report,
    load_ai_budget_summary,
    load_confidence_calibration_latest,
    load_discovery_sandbox_status,
)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel: str, payload: dict) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _write_text(self, rel: str, content: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# load_data_quality_report
# ---------------------------------------------------------------------------

class TestLoadDataQualityReport(_Base):
    _PATH = "outputs/latest/data_quality_report.json"

    def _payload(self, **overrides) -> dict:
        base = {
            "generated_at": "2026-05-01T09:00:00",
            "observe_only": True,
            "available": True,
            "total_symbols": 10,
            "healthy_symbols": 8,
            "warning_symbols": 1,
            "critical_symbols": 1,
            "missing_price_count": 1,
            "stale_price_count": 0,
            "fallback_count": 2,
            "cached_count": 0,
            "summary_line": "10 symbols evaluated.",
            "issues": [],
            "symbols": [],
        }
        base.update(overrides)
        return base

    def test_missing_file_returns_unavailable(self):
        result = load_data_quality_report(self.root)
        self.assertFalse(result["available"])

    def test_missing_file_has_summary_line(self):
        result = load_data_quality_report(self.root)
        self.assertIsInstance(result["summary_line"], str)
        self.assertTrue(len(result["summary_line"]) > 0)

    def test_missing_file_observe_only_true(self):
        result = load_data_quality_report(self.root)
        self.assertTrue(result["observe_only"])

    def test_returns_payload_when_file_exists(self):
        self._write(self._PATH, self._payload())
        result = load_data_quality_report(self.root)
        self.assertTrue(result["available"])

    def test_total_symbols_present(self):
        self._write(self._PATH, self._payload(total_symbols=15))
        result = load_data_quality_report(self.root)
        self.assertEqual(result["total_symbols"], 15)

    def test_critical_symbols_present(self):
        self._write(self._PATH, self._payload(critical_symbols=3))
        result = load_data_quality_report(self.root)
        self.assertEqual(result["critical_symbols"], 3)

    def test_warning_symbols_present(self):
        self._write(self._PATH, self._payload(warning_symbols=2))
        result = load_data_quality_report(self.root)
        self.assertEqual(result["warning_symbols"], 2)

    def test_issues_list_present(self):
        issues = [{"issue_type": "MISSING_PRICE", "severity": "critical", "message": "No price"}]
        self._write(self._PATH, self._payload(issues=issues))
        result = load_data_quality_report(self.root)
        self.assertEqual(len(result["issues"]), 1)
        self.assertEqual(result["issues"][0]["severity"], "critical")

    def test_summary_line_defaults_when_missing(self):
        payload = self._payload()
        del payload["summary_line"]
        self._write(self._PATH, payload)
        result = load_data_quality_report(self.root)
        self.assertIsInstance(result["summary_line"], str)

    def test_corrupt_json_returns_unavailable(self):
        path = self.root / self._PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-json", encoding="utf-8")
        result = load_data_quality_report(self.root)
        self.assertFalse(result["available"])

    def test_non_dict_returns_unavailable(self):
        path = self.root / self._PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        result = load_data_quality_report(self.root)
        self.assertFalse(result["available"])

    def test_fallback_count_present(self):
        self._write(self._PATH, self._payload(fallback_count=4))
        result = load_data_quality_report(self.root)
        self.assertEqual(result["fallback_count"], 4)


# ---------------------------------------------------------------------------
# load_ai_budget_summary
# ---------------------------------------------------------------------------

class TestLoadAiBudgetSummary(_Base):
    _PATH = "outputs/latest/ai_budget_summary.json"

    def _payload(self, **overrides) -> dict:
        base = {
            "generated_at": "2026-05-01T09:00:00",
            "observe_only": True,
            "enabled": True,
            "daily_token_total": 5000,
            "daily_cost_total_usd": 0.0025,
            "monthly_cost_total_usd": 0.012,
            "daily_cost_limit_usd": 1.0,
            "monthly_cost_limit_usd": 20.0,
            "warning": False,
            "blocked": False,
            "warnings": [],
            "event_count": 3,
            "summary_line": "$0.0025 USD today / $0.012 USD this month",
            "events": [],
        }
        base.update(overrides)
        return base

    def test_missing_file_returns_unavailable(self):
        result = load_ai_budget_summary(self.root)
        self.assertFalse(result["available"])

    def test_missing_file_observe_only_true(self):
        result = load_ai_budget_summary(self.root)
        self.assertTrue(result["observe_only"])

    def test_missing_file_has_summary_line(self):
        result = load_ai_budget_summary(self.root)
        self.assertIsInstance(result["summary_line"], str)

    def test_returns_payload_when_file_exists(self):
        self._write(self._PATH, self._payload())
        result = load_ai_budget_summary(self.root)
        self.assertTrue(result["available"])

    def test_daily_cost_present(self):
        self._write(self._PATH, self._payload(daily_cost_total_usd=0.05))
        result = load_ai_budget_summary(self.root)
        self.assertAlmostEqual(result["daily_cost_total_usd"], 0.05)

    def test_monthly_cost_present(self):
        self._write(self._PATH, self._payload(monthly_cost_total_usd=1.5))
        result = load_ai_budget_summary(self.root)
        self.assertAlmostEqual(result["monthly_cost_total_usd"], 1.5)

    def test_warning_flag_present(self):
        self._write(self._PATH, self._payload(warning=True, warnings=["At 85% of daily limit"]))
        result = load_ai_budget_summary(self.root)
        self.assertTrue(result["warning"])
        self.assertEqual(len(result["warnings"]), 1)

    def test_blocked_flag_present(self):
        self._write(self._PATH, self._payload(blocked=True))
        result = load_ai_budget_summary(self.root)
        self.assertTrue(result["blocked"])

    def test_event_count_present(self):
        self._write(self._PATH, self._payload(event_count=7))
        result = load_ai_budget_summary(self.root)
        self.assertEqual(result["event_count"], 7)

    def test_no_limits_allowed(self):
        self._write(self._PATH, self._payload(daily_cost_limit_usd=None, monthly_cost_limit_usd=None))
        result = load_ai_budget_summary(self.root)
        self.assertIsNone(result["daily_cost_limit_usd"])
        self.assertIsNone(result["monthly_cost_limit_usd"])

    def test_summary_line_defaults_when_missing(self):
        payload = self._payload()
        del payload["summary_line"]
        self._write(self._PATH, payload)
        result = load_ai_budget_summary(self.root)
        self.assertIsInstance(result["summary_line"], str)

    def test_corrupt_json_returns_unavailable(self):
        path = self.root / self._PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("bad", encoding="utf-8")
        result = load_ai_budget_summary(self.root)
        self.assertFalse(result["available"])


# ---------------------------------------------------------------------------
# load_confidence_calibration_latest
# ---------------------------------------------------------------------------

class TestLoadConfidenceCalibrationLatest(_Base):
    _PATH = "outputs/latest/confidence_calibration.json"

    def _payload(self, **overrides) -> dict:
        base = {
            "generated_at": "2026-05-01T09:00:00",
            "observe_only": True,
            "available": True,
            "insufficient_data": False,
            "total_resolved": 25,
            "overall_hit_rate": 0.6,
            "overall_avg_return": 0.012,
            "buckets_5": [
                {"label": "very_low", "count": 3, "attributable_count": 3, "hit_rate": 0.33, "avg_return_5d": -0.01, "small_sample": True},
                {"label": "low", "count": 5, "attributable_count": 5, "hit_rate": 0.4, "avg_return_5d": 0.0, "small_sample": False},
                {"label": "medium", "count": 7, "attributable_count": 7, "hit_rate": 0.57, "avg_return_5d": 0.01, "small_sample": False},
                {"label": "high", "count": 6, "attributable_count": 6, "hit_rate": 0.67, "avg_return_5d": 0.02, "small_sample": False},
                {"label": "very_high", "count": 4, "attributable_count": 4, "hit_rate": 0.75, "avg_return_5d": 0.03, "small_sample": True},
            ],
            "signal_results": [],
            "dq_warnings": [],
            "summary_line": "25 resolved decisions analyzed.",
        }
        base.update(overrides)
        return base

    def test_missing_file_returns_unavailable(self):
        result = load_confidence_calibration_latest(self.root)
        self.assertFalse(result["available"])

    def test_missing_file_has_empty_buckets(self):
        result = load_confidence_calibration_latest(self.root)
        self.assertEqual(result["buckets_5"], [])

    def test_missing_file_observe_only_true(self):
        result = load_confidence_calibration_latest(self.root)
        self.assertTrue(result["observe_only"])

    def test_returns_payload_when_file_exists(self):
        self._write(self._PATH, self._payload())
        result = load_confidence_calibration_latest(self.root)
        self.assertTrue(result["available"])

    def test_buckets_5_count_is_five(self):
        self._write(self._PATH, self._payload())
        result = load_confidence_calibration_latest(self.root)
        self.assertEqual(len(result["buckets_5"]), 5)

    def test_bucket_labels(self):
        self._write(self._PATH, self._payload())
        result = load_confidence_calibration_latest(self.root)
        labels = [b["label"] for b in result["buckets_5"]]
        self.assertIn("very_low", labels)
        self.assertIn("very_high", labels)

    def test_total_resolved_present(self):
        self._write(self._PATH, self._payload(total_resolved=42))
        result = load_confidence_calibration_latest(self.root)
        self.assertEqual(result["total_resolved"], 42)

    def test_insufficient_data_flag(self):
        self._write(self._PATH, self._payload(insufficient_data=True, total_resolved=3))
        result = load_confidence_calibration_latest(self.root)
        self.assertTrue(result["insufficient_data"])

    def test_signal_results_present(self):
        sigs = [{"signal_source": "watchlist", "resolved_count": 10, "hit_rate": 0.6, "calibration_gap": 0.1, "suggested_review": False}]
        self._write(self._PATH, self._payload(signal_results=sigs))
        result = load_confidence_calibration_latest(self.root)
        self.assertEqual(len(result["signal_results"]), 1)

    def test_dq_warnings_present(self):
        self._write(self._PATH, self._payload(dq_warnings=["Price data stale for 3 symbols"]))
        result = load_confidence_calibration_latest(self.root)
        self.assertEqual(len(result["dq_warnings"]), 1)

    def test_corrupt_json_returns_unavailable(self):
        path = self.root / self._PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json", encoding="utf-8")
        result = load_confidence_calibration_latest(self.root)
        self.assertFalse(result["available"])

    def test_summary_line_defaults_when_missing(self):
        payload = self._payload()
        del payload["summary_line"]
        self._write(self._PATH, payload)
        result = load_confidence_calibration_latest(self.root)
        self.assertIsInstance(result["summary_line"], str)

    def test_reads_from_latest_not_policy(self):
        # Write to LATEST path — should be found
        self._write(self._PATH, self._payload(total_resolved=99))
        result = load_confidence_calibration_latest(self.root)
        self.assertEqual(result["total_resolved"], 99)

    def test_policy_path_not_used(self):
        # Write only to policy path — LATEST loader should not find it
        self._write("outputs/policy/confidence_calibration.json", self._payload(total_resolved=99))
        result = load_confidence_calibration_latest(self.root)
        self.assertFalse(result["available"])


# ---------------------------------------------------------------------------
# load_discovery_sandbox_status
# ---------------------------------------------------------------------------

class TestLoadDiscoverySandboxStatus(_Base):
    _EMERGING = "outputs/sandbox/discovery/emerging_candidates.json"
    _REJECTED = "outputs/sandbox/discovery/rejected_candidates.json"
    _MEMORY = "outputs/sandbox/discovery/discovery_memory.json"
    _MEMO = "outputs/sandbox/discovery/discovery_memo_section.md"

    def _emerging_payload(self, **overrides) -> dict:
        base = {
            "discovery_only": True,
            "sandbox_only": True,
            "observe_only": True,
            "can_execute_trades": False,
            "disclaimer": "Discovery candidates are not buy/sell recommendations.",
            "run_id": "test_run",
            "generated_at": "2026-05-01T09:00:00",
            "candidates": [
                {
                    "ticker": "NVDA",
                    "status": "watch",
                    "score": 3.5,
                    "event_type": "earnings",
                    "mention_count": 5,
                    "unique_source_count": 3,
                    "risk_flag": False,
                    "corroboration_met": False,
                    "first_seen": "2026-05-01T00:00:00+00:00",
                },
                {
                    "ticker": "AAPL",
                    "status": "discovered",
                    "score": 1.2,
                    "event_type": "guidance",
                    "mention_count": 2,
                    "unique_source_count": 1,
                    "risk_flag": False,
                    "corroboration_met": False,
                    "first_seen": "2026-05-01T00:00:00+00:00",
                },
            ],
        }
        base.update(overrides)
        return base

    def _rejected_payload(self, **overrides) -> dict:
        base = {
            "total_rejected": 1,
            "rejected_candidates": [
                {"ticker": "BADCO", "status": "rejected", "score": 0.1, "rejection_reason": "risk"}
            ],
        }
        base.update(overrides)
        return base

    def _memory_payload(self, **overrides) -> dict:
        base = {
            "discovery_only": True,
            "sandbox_only": True,
            "entry_count": 2,
            "entries": [],
        }
        base.update(overrides)
        return base

    def test_missing_all_files_returns_unavailable(self):
        result = load_discovery_sandbox_status(self.root)
        self.assertFalse(result["available"])

    def test_missing_files_governance_flags(self):
        result = load_discovery_sandbox_status(self.root)
        self.assertTrue(result["discovery_only"])
        self.assertTrue(result["sandbox_only"])
        self.assertTrue(result["observe_only"])
        self.assertFalse(result["can_execute_trades"])
        self.assertFalse(result["official_watchlist_modified"])

    def test_available_when_emerging_present(self):
        self._write(self._EMERGING, self._emerging_payload())
        result = load_discovery_sandbox_status(self.root)
        self.assertTrue(result["available"])

    def test_watch_count_correct(self):
        self._write(self._EMERGING, self._emerging_payload())
        result = load_discovery_sandbox_status(self.root)
        self.assertEqual(result["watch_count"], 1)

    def test_discovered_count_correct(self):
        self._write(self._EMERGING, self._emerging_payload())
        result = load_discovery_sandbox_status(self.root)
        self.assertEqual(result["discovered_count"], 1)

    def test_total_candidates_correct(self):
        self._write(self._EMERGING, self._emerging_payload())
        result = load_discovery_sandbox_status(self.root)
        self.assertEqual(result["total_candidates"], 2)

    def test_rejected_count_correct(self):
        self._write(self._EMERGING, self._emerging_payload())
        self._write(self._REJECTED, self._rejected_payload())
        result = load_discovery_sandbox_status(self.root)
        self.assertEqual(result["total_rejected"], 1)

    def test_memory_entry_count_correct(self):
        self._write(self._EMERGING, self._emerging_payload())
        self._write(self._MEMORY, self._memory_payload(entry_count=3))
        result = load_discovery_sandbox_status(self.root)
        self.assertEqual(result["memory_entry_count"], 3)

    def test_memory_entry_count_zero_when_missing(self):
        self._write(self._EMERGING, self._emerging_payload())
        result = load_discovery_sandbox_status(self.root)
        self.assertEqual(result["memory_entry_count"], 0)

    def test_memo_md_loaded(self):
        self._write(self._EMERGING, self._emerging_payload())
        self._write_text(self._MEMO, "# Discovery Memo\n\nResearch only.")
        result = load_discovery_sandbox_status(self.root)
        self.assertIn("Discovery Memo", result["memo_md"])

    def test_memo_md_empty_when_missing(self):
        self._write(self._EMERGING, self._emerging_payload())
        result = load_discovery_sandbox_status(self.root)
        self.assertEqual(result["memo_md"], "")

    def test_disclaimer_present(self):
        self._write(self._EMERGING, self._emerging_payload())
        result = load_discovery_sandbox_status(self.root)
        self.assertIn("not buy/sell", result["disclaimer"].lower())

    def test_watch_candidates_list(self):
        self._write(self._EMERGING, self._emerging_payload())
        result = load_discovery_sandbox_status(self.root)
        self.assertEqual(len(result["watch_candidates"]), 1)
        self.assertEqual(result["watch_candidates"][0]["ticker"], "NVDA")

    def test_discovered_candidates_list(self):
        self._write(self._EMERGING, self._emerging_payload())
        result = load_discovery_sandbox_status(self.root)
        self.assertEqual(len(result["discovered_candidates"]), 1)
        self.assertEqual(result["discovered_candidates"][0]["ticker"], "AAPL")

    def test_rejected_candidates_list(self):
        self._write(self._EMERGING, self._emerging_payload())
        self._write(self._REJECTED, self._rejected_payload())
        result = load_discovery_sandbox_status(self.root)
        self.assertEqual(len(result["rejected_candidates"]), 1)
        self.assertEqual(result["rejected_candidates"][0]["ticker"], "BADCO")

    def test_run_id_present(self):
        self._write(self._EMERGING, self._emerging_payload(run_id="2026-05-01_discovery"))
        result = load_discovery_sandbox_status(self.root)
        self.assertEqual(result["run_id"], "2026-05-01_discovery")

    def test_artifacts_paths_present(self):
        result = load_discovery_sandbox_status(self.root)
        self.assertIn("emerging_candidates", result["artifacts"])
        self.assertIn("rejected_candidates", result["artifacts"])
        self.assertIn("discovery_memory", result["artifacts"])
        self.assertIn("discovery_memo_section", result["artifacts"])

    def test_corrupt_emerging_falls_back_gracefully(self):
        path = self.root / self._EMERGING
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("bad json", encoding="utf-8")
        result = load_discovery_sandbox_status(self.root)
        self.assertEqual(result["total_candidates"], 0)

    def test_empty_candidates_list(self):
        self._write(self._EMERGING, self._emerging_payload(candidates=[]))
        result = load_discovery_sandbox_status(self.root)
        self.assertEqual(result["watch_count"], 0)
        self.assertEqual(result["discovered_count"], 0)
        self.assertEqual(result["total_candidates"], 0)


# ---------------------------------------------------------------------------
# load_operator_dashboard_data integration — new keys present
# ---------------------------------------------------------------------------

class TestOperatorDashboardNewKeys(_Base):
    def test_bundle_has_data_quality_key(self):
        from gui_operator_data import load_operator_dashboard_data
        bundle = load_operator_dashboard_data(self.root)
        self.assertIn("data_quality_report", bundle)

    def test_bundle_has_ai_budget_key(self):
        from gui_operator_data import load_operator_dashboard_data
        bundle = load_operator_dashboard_data(self.root)
        self.assertIn("ai_budget_summary", bundle)

    def test_bundle_has_confidence_calibration_latest_key(self):
        from gui_operator_data import load_operator_dashboard_data
        bundle = load_operator_dashboard_data(self.root)
        self.assertIn("confidence_calibration_latest", bundle)

    def test_bundle_has_discovery_sandbox_status_key(self):
        from gui_operator_data import load_operator_dashboard_data
        bundle = load_operator_dashboard_data(self.root)
        self.assertIn("discovery_sandbox_status", bundle)

    def test_data_quality_unavailable_by_default(self):
        from gui_operator_data import load_operator_dashboard_data
        bundle = load_operator_dashboard_data(self.root)
        self.assertFalse(bundle["data_quality_report"]["available"])

    def test_ai_budget_unavailable_by_default(self):
        from gui_operator_data import load_operator_dashboard_data
        bundle = load_operator_dashboard_data(self.root)
        self.assertFalse(bundle["ai_budget_summary"]["available"])

    def test_calibration_latest_unavailable_by_default(self):
        from gui_operator_data import load_operator_dashboard_data
        bundle = load_operator_dashboard_data(self.root)
        self.assertFalse(bundle["confidence_calibration_latest"]["available"])

    def test_discovery_sandbox_unavailable_by_default(self):
        from gui_operator_data import load_operator_dashboard_data
        bundle = load_operator_dashboard_data(self.root)
        self.assertFalse(bundle["discovery_sandbox_status"]["available"])

    def test_existing_keys_still_present(self):
        from gui_operator_data import load_operator_dashboard_data
        bundle = load_operator_dashboard_data(self.root)
        for key in ("health", "overview", "confidence_calibration", "decision_triage"):
            self.assertIn(key, bundle, f"Expected existing key '{key}' still in bundle")


# ---------------------------------------------------------------------------
# Safety tests — GUI loaders must not write, call APIs, or mutate state
# ---------------------------------------------------------------------------

class TestLoaderSafetyConstraints(_Base):
    """Verify that all loaders are purely read-only: no writes, no external calls."""

    def _write(self, rel: str, payload: dict) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _write_text(self, rel: str, content: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_load_data_quality_does_not_write_files(self):
        before = list(self.root.rglob("*"))
        load_data_quality_report(self.root)
        after = list(self.root.rglob("*"))
        self.assertEqual(before, after, "load_data_quality_report must not create files")

    def test_load_ai_budget_does_not_write_files(self):
        before = list(self.root.rglob("*"))
        load_ai_budget_summary(self.root)
        after = list(self.root.rglob("*"))
        self.assertEqual(before, after, "load_ai_budget_summary must not create files")

    def test_load_calibration_does_not_write_files(self):
        before = list(self.root.rglob("*"))
        load_confidence_calibration_latest(self.root)
        after = list(self.root.rglob("*"))
        self.assertEqual(before, after, "load_confidence_calibration_latest must not create files")

    def test_load_discovery_does_not_write_files(self):
        before = list(self.root.rglob("*"))
        load_discovery_sandbox_status(self.root)
        after = list(self.root.rglob("*"))
        self.assertEqual(before, after, "load_discovery_sandbox_status must not create files")

    def test_load_data_quality_does_not_write_to_latest(self):
        load_data_quality_report(self.root)
        self.assertFalse((self.root / "outputs" / "latest").exists())

    def test_load_ai_budget_does_not_write_to_policy(self):
        load_ai_budget_summary(self.root)
        self.assertFalse((self.root / "outputs" / "policy").exists())

    def test_load_discovery_does_not_write_to_sandbox(self):
        load_discovery_sandbox_status(self.root)
        self.assertFalse((self.root / "outputs" / "sandbox").exists())

    def test_load_discovery_does_not_mutate_official_watchlist(self):
        result = load_discovery_sandbox_status(self.root)
        self.assertFalse(result["official_watchlist_modified"])

    def test_load_discovery_can_execute_trades_false(self):
        result = load_discovery_sandbox_status(self.root)
        self.assertFalse(result["can_execute_trades"])

    def test_loaders_do_not_import_run_discovery_engine(self):
        import gui_operator_data
        source = Path(gui_operator_data.__file__).read_text(encoding="utf-8")
        self.assertNotIn("run_discovery_engine", source)

    def test_loaders_do_not_import_assert_can_write(self):
        import gui_operator_data
        source = Path(gui_operator_data.__file__).read_text(encoding="utf-8")
        self.assertNotIn("assert_can_write_namespace", source)

    def test_discovery_loader_observe_only_always_true(self):
        result = load_discovery_sandbox_status(self.root)
        self.assertTrue(result["observe_only"])

    def test_discovery_loader_sandbox_only_always_true(self):
        result = load_discovery_sandbox_status(self.root)
        self.assertTrue(result["sandbox_only"])

    def test_discovery_loader_discovery_only_always_true(self):
        result = load_discovery_sandbox_status(self.root)
        self.assertTrue(result["discovery_only"])

    def test_calibration_loader_does_not_read_policy_path(self):
        # Write only to POLICY path — LATEST loader must ignore it
        path = self.root / "outputs" / "policy" / "confidence_calibration.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"total_resolved": 999, "available": True}), encoding="utf-8")
        result = load_confidence_calibration_latest(self.root)
        self.assertFalse(result["available"])

    def test_no_affirmative_buy_sell_in_discovery_disclaimer(self):
        result = load_discovery_sandbox_status(self.root)
        disclaimer = result["disclaimer"].lower()
        # Disclaimer must negate buy/sell — must contain "not"
        self.assertIn("not", disclaimer)
        # Must not contain any standalone actionable-trade phrases
        self.assertNotIn("buy signal", disclaimer)
        self.assertNotIn("sell signal", disclaimer)
        self.assertNotIn("action required", disclaimer)
        self.assertNotIn("execute trade", disclaimer)


# ---------------------------------------------------------------------------
# Governance flags — discover sandbox status always has correct flags
# ---------------------------------------------------------------------------

class TestDiscoverySandboxGovernanceFlags(_Base):
    def _emerging(self, **overrides) -> dict:
        base = {
            "discovery_only": True,
            "sandbox_only": True,
            "observe_only": True,
            "can_execute_trades": False,
            "disclaimer": "Discovery candidates are not buy/sell recommendations.",
            "candidates": [],
        }
        base.update(overrides)
        return base

    def _write(self, rel: str, payload: dict) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_can_execute_trades_false_even_with_watch_candidates(self):
        payload = self._emerging(candidates=[
            {"ticker": "NVDA", "status": "watch", "score": 5.0, "event_type": "earnings",
             "mention_count": 10, "unique_source_count": 5, "risk_flag": False, "corroboration_met": False,
             "first_seen": "2026-05-01T00:00:00+00:00"}
        ])
        self._write("outputs/sandbox/discovery/emerging_candidates.json", payload)
        result = load_discovery_sandbox_status(self.root)
        self.assertFalse(result["can_execute_trades"])

    def test_official_watchlist_modified_always_false(self):
        payload = self._emerging(candidates=[
            {"ticker": "AAPL", "status": "watch", "score": 3.0, "event_type": "guidance",
             "mention_count": 4, "unique_source_count": 2, "risk_flag": False, "corroboration_met": False,
             "first_seen": "2026-05-01T00:00:00+00:00"}
        ])
        self._write("outputs/sandbox/discovery/emerging_candidates.json", payload)
        result = load_discovery_sandbox_status(self.root)
        self.assertFalse(result["official_watchlist_modified"])

    def test_observe_only_always_true_regardless_of_artifact(self):
        # Even if the artifact erroneously sets observe_only=False, loader defaults to True
        payload = self._emerging(observe_only=False, candidates=[])
        self._write("outputs/sandbox/discovery/emerging_candidates.json", payload)
        result = load_discovery_sandbox_status(self.root)
        self.assertTrue(result["observe_only"])
