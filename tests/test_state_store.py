"""
Unit tests for PortfolioStateStore (state_store.py).

All tests use a temporary directory so no artifacts are left on disk.
Test isolation: each test gets a fresh PortfolioStateStore instance.
"""

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from state_store import PortfolioStateStore


class TestRunHistory(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.store = PortfolioStateStore(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_start_run_returns_true_for_new_run(self):
        self.assertTrue(self.store.start_run("2026-03-02_daily", "daily"))

    def test_start_run_returns_false_for_duplicate(self):
        self.store.start_run("2026-03-02_daily", "daily")
        self.assertFalse(self.store.start_run("2026-03-02_daily", "daily"))

    def test_is_completed_false_before_complete(self):
        self.store.start_run("2026-03-02_daily", "daily")
        self.assertFalse(self.store.is_completed("2026-03-02_daily"))

    def test_complete_run_marks_completed(self):
        self.store.start_run("2026-03-02_daily", "daily")
        self.store.complete_run("2026-03-02_daily")
        self.assertTrue(self.store.is_completed("2026-03-02_daily"))

    def test_fail_run_marks_failed(self):
        self.store.start_run("2026-03-02_daily", "daily")
        self.store.fail_run("2026-03-02_daily")
        row = self.store.check_run_status("2026-03-02_daily")
        self.assertEqual(row['status'], 'failed')

    def test_start_run_retries_same_day_failed_row(self):
        self.store.start_run("2026-03-02_daily", "daily")
        self.store.fail_run("2026-03-02_daily")
        self.assertTrue(self.store.start_run("2026-03-02_daily", "daily"))
        row = self.store.check_run_status("2026-03-02_daily")
        self.assertEqual(row['status'], 'running')
        self.assertIsNone(row['completed_at'])

    def test_check_run_status_none_when_missing(self):
        self.assertIsNone(self.store.check_run_status("2026-01-01_daily"))

    def test_is_stale_running_fresh_run_is_not_stale(self):
        self.store.start_run("2026-03-02_daily", "daily")
        self.assertFalse(self.store.is_stale_running("2026-03-02_daily", stale_minutes=30))

    def test_is_stale_running_old_run_is_stale(self):
        old_ts = (datetime.now() - timedelta(minutes=35)).isoformat()
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO run_history (run_id,run_date,mode,status,started_at) VALUES (?,?,?,?,?)",
            ("2026-03-01_daily", "2026-03-01", "daily", "running", old_ts)
        )
        conn.commit()
        conn.close()
        self.assertTrue(self.store.is_stale_running("2026-03-01_daily", stale_minutes=30))

    def test_is_stale_running_completed_run_not_stale(self):
        old_ts = (datetime.now() - timedelta(minutes=35)).isoformat()
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO run_history (run_id,run_date,mode,status,started_at,completed_at) "
            "VALUES (?,?,?,?,?,?)",
            ("2026-03-01_weekly", "2026-03-01", "weekly", "completed", old_ts, old_ts)
        )
        conn.commit()
        conn.close()
        self.assertFalse(self.store.is_stale_running("2026-03-01_weekly", stale_minutes=30))


class TestSnapshots(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.store = PortfolioStateStore(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_snapshot_succeeds(self):
        self.store.start_run("2026-03-02_daily", "daily")
        # Must not raise
        self.store.record_snapshot("2026-03-02_daily", 50000.0, 1000.0, 0.05)

    def test_record_snapshot_stored_in_db(self):
        self.store.start_run("2026-03-02_daily", "daily")
        self.store.record_snapshot("2026-03-02_daily", 75000.0, 500.0, 0.08)
        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT total_value FROM snapshots WHERE run_id='2026-03-02_daily'"
        ).fetchone()
        conn.close()
        self.assertAlmostEqual(row[0], 75000.0)


class TestEmailHistory(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.store = PortfolioStateStore(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_was_hash_sent_recently_false_when_never_sent(self):
        self.assertFalse(self.store.was_hash_sent_recently("abc123", days=7))

    def test_record_and_check_hash(self):
        self.store.record_email_sent("deadbeef", "weekly")
        self.assertTrue(self.store.was_hash_sent_recently("deadbeef", days=7))

    def test_old_hash_not_recent(self):
        old_ts = (datetime.now() - timedelta(days=8)).isoformat()
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO email_history (digest_hash, mode, sent_at) VALUES (?,?,?)",
            ("oldhash", "daily", old_ts)
        )
        conn.commit()
        conn.close()
        self.assertFalse(self.store.was_hash_sent_recently("oldhash", days=7))

    def test_record_email_sent_updates_existing(self):
        """INSERT OR REPLACE should update sent_at on re-send."""
        self.store.record_email_sent("myhash", "weekly")
        self.store.record_email_sent("myhash", "weekly")
        # Still only one recent record (no error raised)
        self.assertTrue(self.store.was_hash_sent_recently("myhash", days=7))


class TestPortfolioPeaks(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.store = PortfolioStateStore(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_and_get_peak(self):
        self.store.upsert_peak("all_time_high", 75000.0)
        self.assertAlmostEqual(self.store.get_peak("all_time_high"), 75000.0)

    def test_upsert_peak_updates_value(self):
        self.store.upsert_peak("all_time_high", 75000.0)
        self.store.upsert_peak("all_time_high", 80000.0)
        self.assertAlmostEqual(self.store.get_peak("all_time_high"), 80000.0)

    def test_get_peak_returns_none_for_missing_key(self):
        self.assertIsNone(self.store.get_peak("nonexistent_key"))

    def test_multiple_peaks_stored_independently(self):
        self.store.upsert_peak("all_time_high", 90000.0)
        self.store.upsert_peak("rolling_12m_high", 85000.0)
        self.assertAlmostEqual(self.store.get_peak("all_time_high"), 90000.0)
        self.assertAlmostEqual(self.store.get_peak("rolling_12m_high"), 85000.0)


class TestWatchlistAlertOutcomes(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.store = PortfolioStateStore(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_watchlist_alert_surface_reuses_pending_state(self):
        payload = {
            "ticker": "AMD",
            "watchlist_source": "static",
            "notification_status": "alerted",
            "alert_priority": "high",
            "alert_quality_tier": "broad",
            "confirmation_count": 3,
            "evidence_breadth": 3,
            "portfolio_priority": 1.0,
            "price": 100.0,
            "signal_score": 0.7,
            "confidence_score": 0.9,
        }
        first = self.store.record_watchlist_alert_surface("AMD|static|price_move", "hash1", payload)
        second = self.store.record_watchlist_alert_surface("AMD|static|price_move", "hash1", payload)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.get_watchlist_alert_outcomes()), 1)

    def test_upsert_alert_event_persists_recent_signal_metadata(self):
        self.store.upsert_alert_event(
            "AMD|static|price_move",
            severity="high",
            state_hash="hash1",
            alert_tier="high",
            reason_code="allowed_high",
            last_signal_score=0.82,
            last_confidence_score=0.91,
            last_action_taken="alerted",
        )
        row = self.store.get_alert_event("AMD|static|price_move")
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["last_signal_score"], 0.82)
        self.assertAlmostEqual(row["last_confidence_score"], 0.91)
        self.assertEqual(row["last_action_taken"], "alerted")

    def test_record_watchlist_alert_surface_new_state_creates_new_row(self):
        payload = {
            "ticker": "AMD",
            "watchlist_source": "static",
            "notification_status": "alerted",
            "alert_priority": "high",
            "alert_quality_tier": "confirmed",
            "confirmation_count": 2,
            "evidence_breadth": 2,
            "portfolio_priority": 0.0,
            "price": 100.0,
            "signal_score": 0.7,
            "confidence_score": 0.9,
        }
        first = self.store.record_watchlist_alert_surface("AMD|static|price_move", "hash1", payload)
        second = self.store.record_watchlist_alert_surface("AMD|static|price_move", "hash2", payload)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.get_watchlist_alert_outcomes()), 2)

    def test_resolve_watchlist_alert_outcome_marks_row_resolved(self):
        payload = {
            "ticker": "AMD",
            "watchlist_source": "static",
            "notification_status": "alerted",
            "alert_priority": "high",
            "alert_quality_tier": "confirmed",
            "confirmation_count": 2,
            "evidence_breadth": 2,
            "portfolio_priority": 0.0,
            "price": 100.0,
            "signal_score": 0.7,
            "confidence_score": 0.9,
        }
        created = self.store.record_watchlist_alert_surface("AMD|static|price_move", "hash1", payload)
        resolved = self.store.resolve_watchlist_alert_outcome(
            int(created["id"]),
            evaluation_price=103.0,
            return_pct=3.0,
            evaluated_at="2026-04-12T00:00:00",
            outcome_label="positive",
            outcome_status="resolved_1d",
        )
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["outcome_pending"], 0)
        self.assertEqual(resolved["outcome_status"], "resolved_1d")
        self.assertEqual(resolved["outcome_label"], "positive")
        self.assertAlmostEqual(resolved["evaluation_price"], 103.0)
        self.assertAlmostEqual(resolved["return_pct"], 3.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
