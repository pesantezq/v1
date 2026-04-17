import gc
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.outcome_reporting import (
    build_outcome_analytics_summary,
    generate_outcome_analytics_reports,
    render_outcome_analytics_markdown,
)
from watchlist_scanner.state import WatchlistStateStore


class TestWatchlistOutcomeReporting(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.output_dir = Path(self.tmp.name) / "reports"
        self.store = WatchlistStateStore(self.db_path)

    def tearDown(self):
        self.store = None
        gc.collect()
        self.tmp.cleanup()

    def _resolved_row(
        self,
        *,
        ticker: str,
        quality: str,
        source: str,
        return_pct: float,
        label: str,
        evidence_breadth: int,
        confirmation_count: int,
        portfolio_priority: float,
    ) -> dict:
        payload = {
            "ticker": ticker,
            "watchlist_source": source,
            "notification_status": "alerted",
            "alert_priority": "high",
            "alert_quality_tier": quality,
            "confirmation_count": confirmation_count,
            "evidence_breadth": evidence_breadth,
            "portfolio_priority": portfolio_priority,
            "price": 100.0,
            "signal_score": 0.7,
            "confidence_score": 0.9,
        }
        created = self.store.record_alert_surface(f"{ticker}|{source}|price_move", "hash1", payload)
        resolved = self.store.resolve_alert_lifecycle(
            int(created["id"]),
            evaluation_price=100.0 * (1 + return_pct / 100.0),
            return_pct=return_pct,
            evaluated_at="2026-04-13T00:00:00",
            outcome_label=label,
            outcome_status="resolved_1d",
        )
        return resolved or {}

    def test_no_resolved_rows_produces_empty_summary_and_markdown(self):
        summary = build_outcome_analytics_summary([])
        self.assertEqual(summary["resolved_count"], 0)
        self.assertEqual(summary["outcome_labels"], {})

        md = render_outcome_analytics_markdown(summary)
        self.assertIn("No resolved watchlist alert outcomes yet.", md)

    def test_mixed_positive_flat_negative_rows_are_summarized(self):
        rows = [
            self._resolved_row(
                ticker="AMD",
                quality="broad",
                source="static",
                return_pct=3.0,
                label="positive",
                evidence_breadth=3,
                confirmation_count=3,
                portfolio_priority=1.0,
            ),
            self._resolved_row(
                ticker="NVDA",
                quality="confirmed",
                source="extended_theme",
                return_pct=0.2,
                label="flat",
                evidence_breadth=2,
                confirmation_count=2,
                portfolio_priority=0.0,
            ),
            self._resolved_row(
                ticker="TSLA",
                quality="thin",
                source="static",
                return_pct=-2.0,
                label="negative",
                evidence_breadth=1,
                confirmation_count=1,
                portfolio_priority=-1.0,
            ),
        ]
        summary = build_outcome_analytics_summary(rows)
        self.assertEqual(summary["resolved_count"], 3)
        self.assertEqual(summary["outcome_labels"]["positive"], 1)
        self.assertEqual(summary["outcome_labels"]["flat"], 1)
        self.assertEqual(summary["outcome_labels"]["negative"], 1)
        self.assertAlmostEqual(summary["avg_return_pct"], 0.4)

    def test_grouping_by_alert_quality_tier(self):
        rows = [
            self._resolved_row(
                ticker="AMD",
                quality="broad",
                source="static",
                return_pct=3.0,
                label="positive",
                evidence_breadth=3,
                confirmation_count=3,
                portfolio_priority=1.0,
            ),
            self._resolved_row(
                ticker="MSFT",
                quality="broad",
                source="static",
                return_pct=1.0,
                label="positive",
                evidence_breadth=3,
                confirmation_count=3,
                portfolio_priority=1.0,
            ),
            self._resolved_row(
                ticker="TSLA",
                quality="thin",
                source="static",
                return_pct=-2.0,
                label="negative",
                evidence_breadth=1,
                confirmation_count=1,
                portfolio_priority=-1.0,
            ),
        ]
        summary = build_outcome_analytics_summary(rows)
        broad = summary["by_alert_quality_tier"]["broad"]
        thin = summary["by_alert_quality_tier"]["thin"]
        self.assertEqual(broad["count"], 2)
        self.assertAlmostEqual(broad["avg_return_pct"], 2.0)
        self.assertEqual(thin["count"], 1)
        self.assertAlmostEqual(thin["avg_return_pct"], -2.0)

    def test_grouping_by_watchlist_source_and_report_files(self):
        self._resolved_row(
            ticker="AMD",
            quality="broad",
            source="static",
            return_pct=3.0,
            label="positive",
            evidence_breadth=3,
            confirmation_count=3,
            portfolio_priority=1.0,
        )
        self._resolved_row(
            ticker="NVDA",
            quality="confirmed",
            source="extended_theme",
            return_pct=0.0,
            label="flat",
            evidence_breadth=2,
            confirmation_count=2,
            portfolio_priority=0.0,
        )

        report = generate_outcome_analytics_reports(
            db_path=self.db_path,
            output_dir=self.output_dir,
        )
        summary = report["summary"]
        self.assertEqual(summary["by_watchlist_source"]["static"]["count"], 1)
        self.assertEqual(summary["by_watchlist_source"]["extended_theme"]["count"], 1)

        json_path = Path(report["paths"]["json_path"])
        md_path = Path(report["paths"]["markdown_path"])
        self.assertTrue(json_path.exists())
        self.assertTrue(md_path.exists())

        saved = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["resolved_count"], 2)
        markdown = md_path.read_text(encoding="utf-8")
        self.assertIn("## By Alert Quality", markdown)
        self.assertIn("## By Watchlist Source", markdown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
