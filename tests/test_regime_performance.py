import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.cache_manager import CacheManager
from watchlist_scanner.performance_feedback import (
    build_regime_performance_summary,
    generate_regime_performance_reports,
    record_scan_signals,
)
from watchlist_scanner.state import WatchlistStateStore


class TestRegimePerformance(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "portfolio.db"
        self.store = WatchlistStateStore(self.db_path)
        self.cache = CacheManager(self.root / "cache")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _scan_result() -> dict:
        row = {
            "ticker": "AMD",
            "watchlist_source": "static",
            "signal_score": 0.80,
            "confidence_score": 0.90,
            "effective_score": 0.72,
            "conviction_score": 0.68,
            "conviction_band": "normal",
            "normalized_allocation": 0.01,
            "price": 100.0,
            "data_mode": "live",
            "notification_status": "alerted",
        }
        return {
            "generated_at": "2026-04-10T12:00:00",
            "data_mode": "live",
            "degraded_mode": False,
            "market_regime": {
                "regime_label": "risk_on",
                "regime_confidence": 0.72,
                "regime_data_quality": "partial",
            },
            "results": [dict(row)],
            "alerts": [dict(row)],
            "scan_summary": {},
        }

    def test_regime_tagging_correctness(self):
        record_scan_signals(self._scan_result(), db_path=self.db_path)
        rows = self.store.list_signal_feedback(limit=10)
        self.assertEqual(rows[0]["regime_label"], "risk_on")
        self.assertAlmostEqual(rows[0]["regime_confidence"], 0.72)
        self.assertEqual(rows[0]["regime_data_quality"], "partial")

    def test_regime_aggregation_accuracy(self):
        self.store.record_signal_feedback(
            signal_key="NVDA|static|2026-04-01T12:00:00",
            ticker="NVDA",
            signal_time="2026-04-01T12:00:00",
            signal_score=0.90,
            confidence_score=0.92,
            effective_score=0.83,
            conviction_score=0.85,
            conviction_band="high_conviction",
            normalized_allocation=0.02,
            price_at_signal=100.0,
            degraded_mode=False,
            regime_label="risk_on",
            regime_confidence=0.75,
            regime_data_quality="full",
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
            signal_key="XLU|static|2026-04-02T12:00:00",
            ticker="XLU",
            signal_time="2026-04-02T12:00:00",
            signal_score=0.65,
            confidence_score=0.70,
            effective_score=0.46,
            conviction_score=0.32,
            conviction_band="observe",
            normalized_allocation=0.00,
            price_at_signal=100.0,
            degraded_mode=True,
            regime_label="risk_off",
            regime_confidence=0.68,
            regime_data_quality="degraded",
        )
        self.store.resolve_signal_feedback(
            2,
            window_days=3,
            outcome_price=97.0,
            return_pct=-3.0,
            outcome_success=False,
            direction_correct=False,
            evaluated_at="2026-04-05T00:00:00",
        )

        summary = build_regime_performance_summary(self.store.list_signal_feedback(limit=10))
        self.assertEqual(summary["by_regime"]["risk_on"]["total_signals"], 1)
        self.assertEqual(summary["by_regime"]["risk_on"]["best_conviction_band"], "high_conviction")
        self.assertAlmostEqual(summary["by_regime"]["risk_on"]["avg_return_pct"], 5.0)
        self.assertAlmostEqual(summary["by_regime"]["risk_off"]["avg_return_pct"], -3.0)

    def test_regime_reports_are_written_without_affecting_rows(self):
        record_scan_signals(self._scan_result(), db_path=self.db_path)
        self.store.resolve_signal_feedback(
            1,
            window_days=3,
            outcome_price=104.0,
            return_pct=4.0,
            outcome_success=True,
            direction_correct=True,
            evaluated_at="2026-04-13T00:00:00",
        )
        before = self.store.list_signal_feedback(limit=10)
        report = generate_regime_performance_reports(
            db_path=self.db_path,
            output_dir=self.root / "outputs" / "regime",
        )
        after = self.store.list_signal_feedback(limit=10)

        json_path = Path(report["paths"]["json_path"])
        md_path = Path(report["paths"]["markdown_path"])
        self.assertTrue(json_path.exists())
        self.assertTrue(md_path.exists())
        self.assertEqual(before[0]["outcome_return_3d"], after[0]["outcome_return_3d"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
