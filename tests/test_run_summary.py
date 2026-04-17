"""
Tests for scraped_intel/run_summary.py

Coverage:
  1.  Run summary schema — all required top-level keys present
  2.  Signal/confidence lift counts in output
  3.  Adapter evidence aggregation
  4.  Markdown artifact creation
  5.  JSON artifact creation
  6.  Dry-run skips artifact writes
  7.  FMP success path reflected correctly
  8.  FMP failure + fallback reflected correctly
  9.  Partial scraped_intel stats (missing keys default gracefully)
  10. No contamination of production scoring records
  11. Output dir created if absent
  12. Markdown content contains key diagnostic fields
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from scraped_intel.run_summary import build_run_summary, _render_markdown


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _full_stats(**overrides) -> dict:
    """Return a complete scraped_intel_stats dict for testing."""
    base = {
        "symbols_processed": 5,
        "total_evidence": 42,
        "evidence_by_source": {"rss_news": 30, "sec_filings": 12},
        "symbols_with_features": 4,
        "symbols_with_signal_lift": 2,
        "symbols_with_confidence_lift": 3,
        "adapter_failures": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestRunSummarySchema(unittest.TestCase):
    """Output structure correctness."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_top_level_keys_present(self):
        summary = build_run_summary(
            run_mode="daily",
            output_dir=self.tmpdir,
            dry_run=True,
        )
        required = {
            "timestamp",
            "run_mode",
            "dry_run",
            "scanner",
            "scraped_intel",
            "market_coverage",
            "artifacts",
        }
        self.assertFalse(required - summary.keys(), f"Missing keys: {required - summary.keys()}")
        self.assertIn("market_regime", summary)

    def test_scanner_sub_keys_present(self):
        summary = build_run_summary(
            run_mode="daily",
            output_dir=self.tmpdir,
            dry_run=True,
        )
        scanner_keys = {
            "fmp_attempted", "fmp_succeeded", "fmp_error",
            "fallback_used", "watchlist_source",
            "symbols_processed", "symbol_count",
        }
        self.assertFalse(
            scanner_keys - summary["scanner"].keys(),
            f"Missing scanner keys: {scanner_keys - summary['scanner'].keys()}",
        )

    def test_scraped_intel_sub_keys_present(self):
        summary = build_run_summary(
            run_mode="daily",
            output_dir=self.tmpdir,
            dry_run=True,
        )
        si_keys = {
            "symbol_count", "total_evidence", "evidence_by_source",
            "symbols_with_features", "symbols_with_signal_lift",
            "symbols_with_confidence_lift", "adapter_failures",
        }
        self.assertFalse(
            si_keys - summary["scraped_intel"].keys(),
            f"Missing scraped_intel keys: {si_keys - summary['scraped_intel'].keys()}",
        )

    def test_run_mode_preserved(self):
        for mode in ("daily", "weekly", "monthly"):
            with self.subTest(mode=mode):
                summary = build_run_summary(run_mode=mode, output_dir=self.tmpdir, dry_run=True)
                self.assertEqual(summary["run_mode"], mode)

    def test_timestamp_present_and_non_empty(self):
        summary = build_run_summary(run_mode="daily", output_dir=self.tmpdir, dry_run=True)
        self.assertTrue(summary["timestamp"])

    def test_custom_timestamp_preserved(self):
        ts = "2026-04-14T09:00:00"
        summary = build_run_summary(
            run_mode="daily", timestamp=ts, output_dir=self.tmpdir, dry_run=True
        )
        self.assertEqual(summary["timestamp"], ts)


class TestRunSummarySignalCounts(unittest.TestCase):
    """Signal/confidence lift counts are reflected correctly."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_signal_lift_count_from_stats(self):
        stats = _full_stats(symbols_with_signal_lift=3)
        summary = build_run_summary(
            run_mode="daily",
            scraped_intel_stats=stats,
            output_dir=self.tmpdir,
            dry_run=True,
        )
        self.assertEqual(summary["scraped_intel"]["symbols_with_signal_lift"], 3)

    def test_confidence_lift_count_from_stats(self):
        stats = _full_stats(symbols_with_confidence_lift=7)
        summary = build_run_summary(
            run_mode="daily",
            scraped_intel_stats=stats,
            output_dir=self.tmpdir,
            dry_run=True,
        )
        self.assertEqual(summary["scraped_intel"]["symbols_with_confidence_lift"], 7)

    def test_zero_lifts_when_no_comparison_mode(self):
        summary = build_run_summary(
            run_mode="daily",
            scraped_intel_stats={},
            output_dir=self.tmpdir,
            dry_run=True,
        )
        self.assertEqual(summary["scraped_intel"]["symbols_with_signal_lift"], 0)
        self.assertEqual(summary["scraped_intel"]["symbols_with_confidence_lift"], 0)


class TestRunSummaryEvidenceAggregation(unittest.TestCase):
    """Evidence counts and source breakdown."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_total_evidence_from_stats(self):
        stats = _full_stats(total_evidence=55)
        summary = build_run_summary(
            run_mode="weekly",
            scraped_intel_stats=stats,
            output_dir=self.tmpdir,
            dry_run=True,
        )
        self.assertEqual(summary["scraped_intel"]["total_evidence"], 55)

    def test_evidence_by_source_preserved(self):
        stats = _full_stats(evidence_by_source={"rss_news": 20, "sec_filings": 10})
        summary = build_run_summary(
            run_mode="weekly",
            scraped_intel_stats=stats,
            output_dir=self.tmpdir,
            dry_run=True,
        )
        self.assertEqual(summary["scraped_intel"]["evidence_by_source"]["rss_news"], 20)
        self.assertEqual(summary["scraped_intel"]["evidence_by_source"]["sec_filings"], 10)

    def test_adapter_failures_preserved(self):
        stats = _full_stats(adapter_failures={"sec_filings": "connection timeout"})
        summary = build_run_summary(
            run_mode="daily",
            scraped_intel_stats=stats,
            output_dir=self.tmpdir,
            dry_run=True,
        )
        self.assertIn("sec_filings", summary["scraped_intel"]["adapter_failures"])

    def test_missing_evidence_fields_default_to_zero(self):
        summary = build_run_summary(
            run_mode="daily",
            scraped_intel_stats=None,
            output_dir=self.tmpdir,
            dry_run=True,
        )
        si = summary["scraped_intel"]
        self.assertEqual(si["total_evidence"], 0)
        self.assertEqual(si["symbol_count"], 0)
        self.assertEqual(si["evidence_by_source"], {})


class TestRunSummaryArtifacts(unittest.TestCase):
    """File creation tests."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.json_path = Path(self.tmpdir) / "scraped_intel_run_summary.json"
        self.md_path   = Path(self.tmpdir) / "scraped_intel_run_summary.md"

    def test_json_artifact_created(self):
        build_run_summary(run_mode="daily", output_dir=self.tmpdir)
        self.assertTrue(self.json_path.exists())

    def test_markdown_artifact_created(self):
        build_run_summary(run_mode="daily", output_dir=self.tmpdir)
        self.assertTrue(self.md_path.exists())

    def test_json_is_valid(self):
        build_run_summary(run_mode="daily", output_dir=self.tmpdir)
        data = json.loads(self.json_path.read_text(encoding="utf-8"))
        self.assertIn("run_mode", data)

    def test_artifacts_paths_in_return_value(self):
        summary = build_run_summary(run_mode="daily", output_dir=self.tmpdir)
        self.assertIn("json", summary["artifacts"])
        self.assertIn("markdown", summary["artifacts"])

    def test_dry_run_skips_artifact_writes(self):
        build_run_summary(run_mode="daily", output_dir=self.tmpdir, dry_run=True)
        self.assertFalse(self.json_path.exists())
        self.assertFalse(self.md_path.exists())

    def test_dry_run_artifacts_dict_empty(self):
        summary = build_run_summary(run_mode="daily", output_dir=self.tmpdir, dry_run=True)
        self.assertEqual(summary["artifacts"], {})

    def test_output_dir_created_if_absent(self):
        nested = os.path.join(self.tmpdir, "nested", "deep")
        build_run_summary(run_mode="daily", output_dir=nested)
        self.assertTrue(Path(nested).exists())

    def test_json_schema_matches_summary_dict(self):
        """JSON on disk must match the returned dict (modulo artifact paths)."""
        summary = build_run_summary(
            run_mode="monthly",
            fmp_attempted=True,
            fmp_succeeded=True,
            watchlist_source="fmp",
            scraped_intel_stats=_full_stats(),
            output_dir=self.tmpdir,
        )
        on_disk = json.loads(self.json_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["run_mode"], summary["run_mode"])
        self.assertEqual(on_disk["scanner"]["fmp_succeeded"], True)
        self.assertEqual(
            on_disk["scraped_intel"]["total_evidence"],
            summary["scraped_intel"]["total_evidence"],
        )


class TestRunSummaryMarketCoverage(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_market_coverage_summary_preserved(self):
        summary = build_run_summary(
            run_mode="daily",
            market_coverage={
                "enabled": True,
                "symbols_scanned": 120,
                "symbols_with_price": 118,
                "promoted": [
                    {"symbol": "AAPL"},
                    {"symbol": "MSFT"},
                ],
                "portfolio_review": {
                    "summary_line": "Portfolio review: 1 scanner-confirmed idea, 1 new rotation candidate.",
                    "new_rotation_candidates": 1,
                },
            },
            output_dir=self.tmpdir,
            dry_run=True,
        )

        self.assertTrue(summary["market_coverage"]["enabled"])
        self.assertEqual(summary["market_coverage"]["promoted_count"], 2)
        self.assertEqual(summary["market_coverage"]["top_symbols"], ["AAPL", "MSFT"])
        self.assertEqual(summary["market_coverage"]["rotation_candidate_count"], 1)

    def test_markdown_contains_market_coverage_lines(self):
        summary = build_run_summary(
            run_mode="daily",
            market_coverage={
                "enabled": True,
                "symbols_scanned": 45,
                "promoted": [{"symbol": "NVDA"}],
                "portfolio_review": {
                    "summary_line": "Portfolio review: 1 new rotation candidate.",
                    "new_rotation_candidates": 1,
                },
            },
            output_dir=self.tmpdir,
            dry_run=True,
        )

        rendered = _render_markdown(summary)
        self.assertIn("## Market Coverage", rendered)
        self.assertIn("Promoted candidates: 1", rendered)
        self.assertIn("Top symbols: NVDA", rendered)


class TestRunSummaryFMPSuccessPath(unittest.TestCase):
    """FMP success is reflected correctly."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_fmp_success_flags(self):
        summary = build_run_summary(
            run_mode="monthly",
            fmp_attempted=True,
            fmp_succeeded=True,
            fmp_error=None,
            fallback_used=False,
            watchlist_source="fmp",
            symbols_processed=["NVDA", "MSFT"],
            output_dir=self.tmpdir,
            dry_run=True,
        )
        sc = summary["scanner"]
        self.assertTrue(sc["fmp_attempted"])
        self.assertTrue(sc["fmp_succeeded"])
        self.assertIsNone(sc["fmp_error"])
        self.assertFalse(sc["fallback_used"])
        self.assertEqual(sc["watchlist_source"], "fmp")
        self.assertEqual(sc["symbol_count"], 2)


class TestRunSummaryFallbackPath(unittest.TestCase):
    """FMP failure + fallback reflected correctly."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_fallback_flags(self):
        summary = build_run_summary(
            run_mode="monthly",
            fmp_attempted=True,
            fmp_succeeded=False,
            fmp_error="FMP authentication failed (HTTP 403)",
            fallback_used=True,
            watchlist_source="fallback",
            symbols_processed=["NVDA", "MSFT", "AMZN"],
            output_dir=self.tmpdir,
            dry_run=True,
        )
        sc = summary["scanner"]
        self.assertTrue(sc["fmp_attempted"])
        self.assertFalse(sc["fmp_succeeded"])
        self.assertIn("403", sc["fmp_error"])
        self.assertTrue(sc["fallback_used"])
        self.assertEqual(sc["watchlist_source"], "fallback")
        self.assertTrue(summary["degraded_mode"])
        self.assertEqual(summary["degraded_reason"], "fmp_403")
        self.assertEqual(summary["data_mode"], "fallback")
        self.assertTrue(sc["data_fallback_triggered"])

    def test_fallback_plus_themes_source(self):
        summary = build_run_summary(
            run_mode="daily",
            fallback_used=True,
            watchlist_source="fallback+themes",
            output_dir=self.tmpdir,
            dry_run=True,
        )
        self.assertEqual(summary["scanner"]["watchlist_source"], "fallback+themes")

    def test_circuit_breaker_path(self):
        """Circuit breaker fires → fmp_attempted=False (was pre-empted)."""
        summary = build_run_summary(
            run_mode="daily",
            fmp_attempted=False,
            fmp_succeeded=False,
            fmp_error="FMP circuit breaker open",
            fallback_used=True,
            watchlist_source="fallback",
            output_dir=self.tmpdir,
            dry_run=True,
        )
        sc = summary["scanner"]
        self.assertFalse(sc["fmp_attempted"])
        self.assertFalse(sc["fmp_succeeded"])
        self.assertTrue(sc["fallback_used"])
        self.assertTrue(summary["degraded_mode"])
        self.assertEqual(summary["degraded_reason"], "circuit_breaker")

    def test_data_latency_propagates_into_scanner_metadata(self):
        summary = build_run_summary(
            run_mode="weekly",
            fmp_attempted=True,
            fmp_succeeded=False,
            fmp_error="HTTP 403",
            fallback_used=True,
            watchlist_source="fallback",
            scraped_intel_stats={"data_latency_ms": 3210},
            output_dir=self.tmpdir,
            dry_run=True,
        )
        self.assertEqual(summary["scanner"]["data_latency_ms"], 3210)

    def test_market_regime_fields_propagate(self):
        summary = build_run_summary(
            run_mode="weekly",
            market_regime={
                "regime_label": "risk_on",
                "regime_confidence": 0.72,
                "regime_reasoning": "broad uptrend with supportive leadership",
                "regime_summary_line": "Market regime: risk_on (confidence 0.72) - broad uptrend with supportive leadership",
                "regime_inputs": {"breadth_sma50": 0.75},
                "regime_data_quality": "partial",
            },
            output_dir=self.tmpdir,
            dry_run=True,
        )
        self.assertEqual(summary["market_regime"]["regime_label"], "risk_on")
        self.assertEqual(summary["market_regime"]["regime_data_quality"], "partial")


class TestRunSummaryMarkdown(unittest.TestCase):
    """Markdown content includes key diagnostic fields."""

    def _render(self, **kwargs) -> str:
        tmpdir = tempfile.mkdtemp()
        build_run_summary(run_mode="daily", output_dir=tmpdir, **kwargs)
        return (Path(tmpdir) / "scraped_intel_run_summary.md").read_text(encoding="utf-8")

    def test_markdown_contains_mode(self):
        md = self._render()
        self.assertIn("daily", md)

    def test_markdown_contains_market_regime(self):
        md = self._render(
            market_regime={
                "regime_summary_line": "Market regime: risk_off (confidence 0.68) - weak breadth and defensive posture",
                "regime_data_quality": "partial",
            }
        )
        self.assertIn("Market Regime", md)
        self.assertIn("risk_off", md)

    def test_markdown_shows_fmp_failed(self):
        md = self._render(fmp_attempted=True, fmp_succeeded=False, fmp_error="HTTP 403")
        self.assertIn("FAILED", md)
        self.assertIn("403", md)

    def test_markdown_shows_fmp_succeeded(self):
        md = self._render(fmp_attempted=True, fmp_succeeded=True)
        self.assertIn("succeeded", md)

    def test_markdown_shows_fallback_active(self):
        md = self._render(fallback_used=True, watchlist_source="fallback")
        self.assertIn("ACTIVE", md)

    def test_markdown_uses_plain_ascii_fallback_text(self):
        md = self._render(
            fmp_attempted=True,
            fmp_succeeded=False,
            fmp_error="HTTP 403",
            fallback_used=True,
            watchlist_source="fallback",
        )
        self.assertIn("**FAILED** - `HTTP 403`", md)
        self.assertNotIn("â", md)

    def test_markdown_shows_evidence_counts(self):
        md = self._render(scraped_intel_stats=_full_stats(total_evidence=99))
        self.assertIn("99", md)

    def test_markdown_shows_adapter_failures(self):
        md = self._render(
            scraped_intel_stats=_full_stats(
                adapter_failures={"sec_filings": "connection refused"}
            )
        )
        self.assertIn("sec_filings", md)
        self.assertIn("connection refused", md)

    def test_markdown_is_non_empty(self):
        md = self._render()
        self.assertGreater(len(md.strip()), 50)

    def test_render_markdown_direct(self):
        """_render_markdown() works standalone for unit testing."""
        summary = {
            "timestamp": "2026-04-14T10:00:00",
            "run_mode": "weekly",
            "dry_run": False,
            "scanner": {
                "fmp_attempted": True,
                "fmp_succeeded": False,
                "fmp_error": "HTTP 403",
                "fallback_used": True,
                "watchlist_source": "fallback",
                "symbol_count": 20,
            },
            "scraped_intel": {
                "symbol_count": 20,
                "total_evidence": 10,
                "evidence_by_source": {"rss_news": 10},
                "symbols_with_features": 5,
                "symbols_with_signal_lift": 0,
                "symbols_with_confidence_lift": 0,
                "adapter_failures": {},
            },
            "artifacts": {},
        }
        md = _render_markdown(summary)
        self.assertIn("Scraped Intel Run Summary", md)
        self.assertIn("weekly", md)
        self.assertIn("FAILED", md)


class TestRunSummaryNoContamination(unittest.TestCase):
    """
    Ensure the run summary never touches production scoring tables
    or modifies any scan result rows.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_no_db_writes(self):
        """build_run_summary must not create or modify any .db file."""
        db_path = Path(self.tmpdir) / "portfolio.db"
        build_run_summary(run_mode="daily", output_dir=self.tmpdir)
        self.assertFalse(db_path.exists(), "Run summary must not touch portfolio.db")

    def test_no_side_effects_on_input_stats(self):
        """Passed scraped_intel_stats dict must not be mutated."""
        stats = _full_stats()
        original = dict(stats)
        build_run_summary(
            run_mode="daily",
            scraped_intel_stats=stats,
            output_dir=self.tmpdir,
            dry_run=True,
        )
        self.assertEqual(stats, original)

    def test_returns_dict_not_none(self):
        result = build_run_summary(run_mode="daily", output_dir=self.tmpdir, dry_run=True)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, dict)


if __name__ == "__main__":
    unittest.main()
