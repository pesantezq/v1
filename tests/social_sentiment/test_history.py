"""Tests for SentimentHistoryTracker (Phase 11)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portfolio_automation.social_sentiment.history import (
    MAX_HISTORY_DAYS,
    MIN_HISTORY_DAYS,
    SentimentHistoryTracker,
    _slope,
)


class TestSlope(unittest.TestCase):
    def test_positive_slope(self):
        self.assertGreater(_slope([1.0, 2.0, 3.0, 4.0]), 0)

    def test_negative_slope(self):
        self.assertLess(_slope([4.0, 3.0, 2.0, 1.0]), 0)

    def test_zero_slope_flat(self):
        self.assertAlmostEqual(_slope([1.0, 1.0, 1.0]), 0.0, places=6)

    def test_single_value_is_zero(self):
        self.assertEqual(_slope([5.0]), 0.0)

    def test_empty_is_zero(self):
        self.assertEqual(_slope([]), 0.0)


class TestSentimentHistoryTracker(unittest.TestCase):
    def _tracker(self, tmpdir):
        path = Path(tmpdir) / "test_history.jsonl"
        return SentimentHistoryTracker(path)

    def test_record_and_retrieve(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = self._tracker(tmpdir)
            tracker.record_daily("NVDA", "bluesky", 0.5, 0.8, 15, date="2026-06-21")
            history = tracker.get_ticker_history("NVDA")
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["ticker"], "NVDA")
            self.assertEqual(history[0]["source"], "bluesky")
            self.assertAlmostEqual(history[0]["sentiment_score"], 0.5, places=4)

    def test_idempotent_same_ticker_source_date(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = self._tracker(tmpdir)
            tracker.record_daily("NVDA", "bluesky", 0.5, 0.8, 15, date="2026-06-21")
            tracker.record_daily("NVDA", "bluesky", 0.7, 0.9, 20, date="2026-06-21")
            history = tracker.get_ticker_history("NVDA")
            self.assertEqual(len(history), 1)  # second call was no-op

    def test_different_dates_appended(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = self._tracker(tmpdir)
            tracker.record_daily("NVDA", "bluesky", 0.5, 0.8, 15, date="2026-06-21")
            tracker.record_daily("NVDA", "bluesky", 0.6, 0.7, 12, date="2026-06-20")
            history = tracker.get_ticker_history("NVDA")
            self.assertEqual(len(history), 2)

    def test_different_sources_separate_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = self._tracker(tmpdir)
            tracker.record_daily("NVDA", "bluesky", 0.5, 0.8, 15, date="2026-06-21")
            tracker.record_daily("NVDA", "mastodon", 0.3, 0.7, 10, date="2026-06-21")
            history = tracker.get_ticker_history("NVDA")
            self.assertEqual(len(history), 2)

    def test_get_ticker_history_empty_when_no_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = self._tracker(tmpdir)
            self.assertEqual(tracker.get_ticker_history("AAPL"), [])

    def test_history_sorted_by_date(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = self._tracker(tmpdir)
            for date in ["2026-06-19", "2026-06-21", "2026-06-20"]:
                tracker.record_daily("NVDA", "bluesky", 0.5, 0.8, 10, date=date)
            history = tracker.get_ticker_history("NVDA")
            dates = [r["date"] for r in history]
            self.assertEqual(dates, sorted(dates))

    def test_prunes_beyond_max_history_days(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = self._tracker(tmpdir)
            for i in range(MAX_HISTORY_DAYS + 5):
                date = f"2026-{(6 + i // 30 + 1):02d}-{(i % 28 + 1):02d}"
                try:
                    tracker.record_daily("NVDA", "bluesky", 0.5, 0.8, 10, date=date)
                except Exception:
                    pass
            history = tracker.get_ticker_history("NVDA")
            self.assertLessEqual(len(history), MAX_HISTORY_DAYS)

    def test_ticker_isolation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = self._tracker(tmpdir)
            tracker.record_daily("NVDA", "bluesky", 0.8, 0.9, 15, date="2026-06-21")
            tracker.record_daily("AAPL", "bluesky", -0.3, 0.6, 10, date="2026-06-21")
            nvda_hist = tracker.get_ticker_history("NVDA")
            aapl_hist = tracker.get_ticker_history("AAPL")
            self.assertEqual(len(nvda_hist), 1)
            self.assertEqual(len(aapl_hist), 1)

    def test_get_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = self._tracker(tmpdir)
            tracker.record_daily("NVDA", "bluesky", 0.5, 0.8, 15, date="2026-06-21")
            summary = tracker.get_summary()
            self.assertEqual(summary["total_rows"], 1)
            self.assertEqual(summary["unique_tickers"], 1)


class TestTrendStateComputation(unittest.TestCase):
    def _tracker_with_history(self, tmpdir, scores, ticker="NVDA", source="bluesky"):
        tracker = SentimentHistoryTracker(Path(tmpdir) / "h.jsonl")
        for i, score in enumerate(scores):
            date = f"2026-06-{(i + 1):02d}"
            tracker.record_daily(ticker, source, score, 0.8, 15, date=date)
        return tracker

    def test_building_history_when_too_few_points(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = self._tracker_with_history(tmpdir, [0.5, 0.6, 0.7])
            self.assertEqual(tracker.compute_trend_state("NVDA"), "building_history")

    def test_empty_history_is_building(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = SentimentHistoryTracker(Path(tmpdir) / "empty.jsonl")
            self.assertEqual(tracker.compute_trend_state("NVDA"), "building_history")

    def test_positive_rising_trend(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scores = [0.2, 0.3, 0.4, 0.5, 0.6]  # rising positive
            tracker = self._tracker_with_history(tmpdir, scores)
            state = tracker.compute_trend_state("NVDA")
            self.assertEqual(state, "positive_rising")

    def test_negative_falling_trend(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scores = [-0.2, -0.3, -0.4, -0.5, -0.6]  # falling negative
            tracker = self._tracker_with_history(tmpdir, scores)
            state = tracker.compute_trend_state("NVDA")
            self.assertEqual(state, "negative_falling")

    def test_neutral_near_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scores = [0.01, -0.01, 0.02, -0.02, 0.0]  # flat near zero
            tracker = self._tracker_with_history(tmpdir, scores)
            state = tracker.compute_trend_state("NVDA")
            self.assertEqual(state, "neutral")

    def test_mixed_high_variance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scores = [0.8, -0.8, 0.9, -0.7, 0.8]  # high variance
            tracker = self._tracker_with_history(tmpdir, scores)
            state = tracker.compute_trend_state("NVDA")
            self.assertEqual(state, "mixed")
