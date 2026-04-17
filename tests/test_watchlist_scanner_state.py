import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.state import WatchlistStateStore


class TestWatchlistStateStore(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.store = WatchlistStateStore(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_or_create_alert_lifecycle_reuses_pending_state(self):
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
        first = self.store.get_or_create_alert_lifecycle("AMD|static|price_move", "hash1", payload)
        second = self.store.get_or_create_alert_lifecycle("AMD|static|price_move", "hash1", payload)
        self.assertEqual(first["id"], second["id"])

    def test_cooldown_state_methods_delegate_without_behavior_change(self):
        fingerprint = "AMD|static|price_move"
        self.assertFalse(self.store.should_suppress_alert(fingerprint, cooldown_days=3, severity="high", state_hash="hash1"))
        self.store.touch_alert_state(fingerprint, severity="high", state_hash="hash1")
        self.store.mark_alert_notified(fingerprint)
        self.assertTrue(self.store.should_suppress_alert(fingerprint, cooldown_days=3, severity="high", state_hash="hash1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
