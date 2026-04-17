import sqlite3
import sys
import tempfile
import unittest
import gc
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.cache_manager import CacheManager
from watchlist_scanner.outcome_evaluator import evaluate_pending_alert_outcomes
from watchlist_scanner.state import WatchlistStateStore


class TestWatchlistOutcomeEvaluator(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.cache_dir = Path(self.tmp.name) / "cache"
        self.store = WatchlistStateStore(self.db_path)
        self.cache = CacheManager(self.cache_dir)

    def tearDown(self):
        self.store = None
        self.cache = None
        gc.collect()
        self.tmp.cleanup()

    def _seed_lifecycle(self, surfaced_at: datetime, baseline_price: float = 100.0, ticker: str = "AMD") -> int:
        payload = {
            "ticker": ticker,
            "watchlist_source": "static",
            "notification_status": "alerted",
            "alert_priority": "high",
            "alert_quality_tier": "broad",
            "confirmation_count": 3,
            "evidence_breadth": 3,
            "portfolio_priority": 1.0,
            "price": baseline_price,
            "signal_score": 0.72,
            "confidence_score": 0.90,
        }
        created = self.store.record_alert_surface(f"{ticker}|static|price_move", "hash1", payload)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "UPDATE watchlist_alert_outcomes SET surfaced_at=?, last_seen_at=? WHERE id=?",
                (surfaced_at.isoformat(), surfaced_at.isoformat(), int(created["id"])),
            )
            conn.commit()
        return int(created["id"])

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

    def test_pending_lifecycle_gets_evaluated_when_due(self):
        surfaced_at = datetime(2026, 4, 10, 12, 0, 0)
        outcome_id = self._seed_lifecycle(surfaced_at)
        self._seed_daily_cache(
            "AMD",
            {
                "2026-04-11": 103.0,
                "2026-04-10": 100.0,
            },
        )

        summary = evaluate_pending_alert_outcomes(
            db_path=self.db_path,
            cache_dir=self.cache_dir,
            as_of=datetime(2026, 4, 12, 9, 0, 0),
        )

        self.assertEqual(summary["evaluated"], 1)
        row = next(r for r in self.store.list_alert_lifecycles() if int(r["id"]) == outcome_id)
        self.assertEqual(row["outcome_pending"], 0)
        self.assertEqual(row["outcome_status"], "resolved_1d")
        self.assertEqual(row["outcome_label"], "positive")
        self.assertAlmostEqual(row["evaluation_price"], 103.0)
        self.assertAlmostEqual(row["return_pct"], 3.0)

    def test_lifecycle_remains_pending_when_not_due(self):
        surfaced_at = datetime(2026, 4, 12, 12, 0, 0)
        outcome_id = self._seed_lifecycle(surfaced_at)
        self._seed_daily_cache("AMD", {"2026-04-12": 101.0})

        summary = evaluate_pending_alert_outcomes(
            db_path=self.db_path,
            cache_dir=self.cache_dir,
            as_of=datetime(2026, 4, 12, 18, 0, 0),
        )

        self.assertEqual(summary["evaluated"], 0)
        self.assertEqual(summary["not_due"], 1)
        row = next(r for r in self.store.list_alert_lifecycles() if int(r["id"]) == outcome_id)
        self.assertEqual(row["outcome_pending"], 1)
        self.assertEqual(row["outcome_status"], "pending")

    def test_missing_price_data_fails_gracefully(self):
        surfaced_at = datetime(2026, 4, 10, 12, 0, 0)
        outcome_id = self._seed_lifecycle(surfaced_at)

        summary = evaluate_pending_alert_outcomes(
            db_path=self.db_path,
            cache_dir=self.cache_dir,
            as_of=datetime(2026, 4, 12, 9, 0, 0),
        )

        self.assertEqual(summary["evaluated"], 0)
        self.assertEqual(summary["missing_price"], 1)
        row = next(r for r in self.store.list_alert_lifecycles() if int(r["id"]) == outcome_id)
        self.assertEqual(row["outcome_pending"], 1)
        self.assertEqual(row["outcome_status"], "pending")

    def test_evaluated_rows_are_not_re_evaluated(self):
        surfaced_at = datetime(2026, 4, 10, 12, 0, 0)
        outcome_id = self._seed_lifecycle(surfaced_at)
        self._seed_daily_cache(
            "AMD",
            {
                "2026-04-11": 102.0,
                "2026-04-10": 100.0,
            },
        )

        first = evaluate_pending_alert_outcomes(
            db_path=self.db_path,
            cache_dir=self.cache_dir,
            as_of=datetime(2026, 4, 12, 9, 0, 0),
        )
        second = evaluate_pending_alert_outcomes(
            db_path=self.db_path,
            cache_dir=self.cache_dir,
            as_of=datetime(2026, 4, 13, 9, 0, 0),
        )

        self.assertEqual(first["evaluated"], 1)
        self.assertEqual(second["evaluated"], 0)
        row = next(r for r in self.store.list_alert_lifecycles() if int(r["id"]) == outcome_id)
        self.assertEqual(row["outcome_pending"], 0)
        self.assertEqual(row["outcome_status"], "resolved_1d")


if __name__ == "__main__":
    unittest.main(verbosity=2)
