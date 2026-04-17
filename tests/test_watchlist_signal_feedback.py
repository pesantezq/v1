import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.cache_manager import CacheManager
from watchlist_scanner.performance_feedback import (
    annotate_scan_result_with_performance,
    build_signal_performance_summary,
    evaluate_pending_signal_feedback,
    record_scan_signals,
    run_signal_feedback_cycle,
)
from watchlist_scanner.state import WatchlistStateStore


class TestWatchlistSignalFeedback(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "portfolio.db"
        self.cache_dir = self.root / "cache"
        self.output_dir = self.root / "outputs" / "performance"
        self.store = WatchlistStateStore(self.db_path)
        self.cache = CacheManager(self.cache_dir)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _scan_result(*, generated_at: str = "2026-04-10T12:00:00") -> dict:
        row = {
            "ticker": "AMD",
            "watchlist_source": "static",
            "signal_score": 0.80,
            "confidence_score": 0.90,
            "effective_score": 0.72,
            "price": 100.0,
            "data_mode": "live",
            "notification_status": "alerted",
        }
        return {
            "generated_at": generated_at,
            "data_mode": "live",
            "degraded_mode": False,
            "results": [dict(row)],
            "alerts": [dict(row)],
            "scan_summary": {},
        }

    def _seed_daily_cache(self, ticker: str, closes_by_day: dict[str, float]) -> None:
        self.cache.set(
            f"daily_{ticker}",
            {
                "Time Series (Daily)": {
                    day: {
                        "1. open": str(close),
                        "2. high": str(close),
                        "3. low": str(close),
                        "4. close": str(close),
                        "5. volume": "1000000",
                    }
                    for day, close in closes_by_day.items()
                }
            },
        )

    def test_outcome_tracking_and_report_writes_work(self):
        self._seed_daily_cache(
            "AMD",
            {
                "2026-04-11": 103.0,
                "2026-04-14": 105.0,
                "2026-04-17": 108.0,
            },
        )
        scan_result = self._scan_result()

        record_summary = record_scan_signals(scan_result, db_path=self.db_path)
        eval_summary = evaluate_pending_signal_feedback(
            db_path=self.db_path,
            cache_dir=self.cache_dir,
            as_of=datetime(2026, 4, 18, 9, 0, 0),
        )
        report = run_signal_feedback_cycle(
            scan_result,
            db_path=self.db_path,
            cache_dir=self.cache_dir,
            output_dir=self.output_dir,
            dry_run=False,
        )

        self.assertEqual(record_summary["tracked"], 1)
        self.assertEqual(eval_summary["by_window"]["1d"]["evaluated"], 1)
        rows = self.store.list_signal_feedback(limit=10)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertAlmostEqual(row["outcome_return_1d"], 3.0)
        self.assertEqual(row["outcome_success_1d"], 1)
        self.assertAlmostEqual(row["outcome_return_3d"], 5.0)
        self.assertAlmostEqual(row["outcome_return_7d"], 8.0)

        csv_path = Path(report["paths"]["csv_path"])
        json_path = Path(report["paths"]["json_path"])
        self.assertTrue(csv_path.exists())
        self.assertTrue(json_path.exists())
        saved = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["tracked_signals"], 1)
        self.assertEqual(saved["resolved_signals"], 1)

    def test_aggregation_builds_ticker_and_global_metrics(self):
        self.store.record_signal_feedback(
            signal_key="NVDA|static|2026-04-01T12:00:00",
            ticker="NVDA",
            signal_time="2026-04-01T12:00:00",
            signal_score=0.90,
            confidence_score=0.92,
            effective_score=0.83,
            price_at_signal=100.0,
            degraded_mode=False,
        )
        self.store.resolve_signal_feedback(
            1,
            window_days=3,
            outcome_price=105.0,
            return_pct=5.0,
            outcome_success=True,
            direction_correct=True,
            evaluated_at="2026-04-04T00:00:00",
        )
        self.store.record_signal_feedback(
            signal_key="NVDA|static|2026-04-02T12:00:00",
            ticker="NVDA",
            signal_time="2026-04-02T12:00:00",
            signal_score=0.85,
            confidence_score=0.88,
            effective_score=0.75,
            price_at_signal=100.0,
            degraded_mode=False,
        )
        self.store.resolve_signal_feedback(
            2,
            window_days=3,
            outcome_price=104.0,
            return_pct=4.0,
            outcome_success=True,
            direction_correct=True,
            evaluated_at="2026-04-05T00:00:00",
        )
        self.store.record_signal_feedback(
            signal_key="TSLA|static|2026-04-03T12:00:00",
            ticker="TSLA",
            signal_time="2026-04-03T12:00:00",
            signal_score=0.70,
            confidence_score=0.55,
            effective_score=0.39,
            price_at_signal=100.0,
            degraded_mode=True,
        )
        self.store.resolve_signal_feedback(
            3,
            window_days=3,
            outcome_price=96.0,
            return_pct=-4.0,
            outcome_success=False,
            direction_correct=False,
            evaluated_at="2026-04-06T00:00:00",
        )

        rows = self.store.list_signal_feedback(limit=10)
        summary = build_signal_performance_summary(rows)

        self.assertEqual(summary["resolved_signals"], 3)
        self.assertIn("NVDA", summary["by_ticker"])
        self.assertGreater(summary["by_ticker"]["NVDA"]["historical_performance_score"], 0.6)
        self.assertEqual(summary["by_ticker"]["TSLA"]["signal_reliability"], "unproven")
        self.assertEqual(summary["global_metrics"]["high_confidence_success_rate"], 1.0)
        self.assertEqual(summary["global_metrics"]["degraded_mode_success_rate"], 0.0)

    def test_annotation_does_not_change_existing_pipeline_fields(self):
        scan_result = self._scan_result()
        summary = {
            "by_ticker": {
                "AMD": {
                    "historical_performance_score": 0.71,
                    "signal_reliability": "strong",
                }
            }
        }

        annotated = annotate_scan_result_with_performance(scan_result, summary)

        self.assertEqual(len(annotated["alerts"]), 1)
        self.assertAlmostEqual(annotated["results"][0]["signal_score"], 0.80)
        self.assertAlmostEqual(annotated["results"][0]["confidence_score"], 0.90)
        self.assertAlmostEqual(annotated["results"][0]["historical_performance_score"], 0.71)
        self.assertEqual(annotated["results"][0]["signal_reliability"], "strong")


if __name__ == "__main__":
    unittest.main(verbosity=2)
