"""
Tests for portfolio_automation/market_session.py (Reliability Program D2).

Covers:
  - is_trading_day: weekday/weekend/holiday classification
  - latest_completed_session: Saturday, market holiday, pre-market weekday
    (the exact three scenarios named in the D1+D2 verification spec)
  - sessions_between: zero across a weekend-only gap, positive across a
    real new close
  - session_provenance: field shape + coverage_exceeded flag
  - coverage horizon: explicit, not silent
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.market_session import (
    HOLIDAY_COVERAGE_THROUGH,
    NYSE_HOLIDAYS,
    is_past_coverage_horizon,
    is_trading_day,
    latest_completed_session,
    previous_trading_day,
    session_provenance,
    sessions_between,
)


class TestIsTradingDay(unittest.TestCase):
    def test_ordinary_weekday_is_trading_day(self):
        # 2026-07-28 is a Tuesday, not a holiday.
        self.assertTrue(is_trading_day(date(2026, 7, 28)))

    def test_saturday_is_not_trading_day(self):
        self.assertFalse(is_trading_day(date(2026, 8, 1)))

    def test_sunday_is_not_trading_day(self):
        self.assertFalse(is_trading_day(date(2026, 8, 2)))

    def test_known_holiday_is_not_trading_day(self):
        # Labor Day 2026-09-07 (Monday).
        self.assertIn(date(2026, 9, 7), NYSE_HOLIDAYS)
        self.assertFalse(is_trading_day(date(2026, 9, 7)))

    def test_past_coverage_horizon_degrades_to_weekday_only(self):
        # 2028-01-03 is a Monday, past HOLIDAY_COVERAGE_THROUGH -- no holiday
        # data exists past that date, so a plain weekday reads as a trading
        # day (weekday-only degrade), and the coverage flag must say so.
        far_future = date(2028, 1, 3)
        self.assertTrue(is_past_coverage_horizon(far_future))
        self.assertTrue(is_trading_day(far_future))


class TestPreviousTradingDay(unittest.TestCase):
    def test_monday_previous_is_friday(self):
        # 2026-07-27 is a Monday.
        self.assertEqual(previous_trading_day(date(2026, 7, 27)), date(2026, 7, 24))

    def test_skips_holiday_monday(self):
        # 2026-09-08 (Tuesday) previous trading day must skip the Labor Day
        # Monday (2026-09-07) and the weekend before it, landing on Friday
        # 2026-09-04.
        self.assertEqual(previous_trading_day(date(2026, 9, 8)), date(2026, 9, 4))


class TestLatestCompletedSession(unittest.TestCase):
    """The three scenarios named explicitly in the verification spec."""

    def test_as_of_saturday(self):
        # 2026-08-01 is a Saturday; the prior Friday is 2026-07-31.
        sat = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(latest_completed_session(sat), date(2026, 7, 31))

    def test_as_of_market_holiday(self):
        # Labor Day 2026-09-07 (Monday) -> most recent completed session is
        # the Friday before it (2026-09-04), not the holiday itself.
        holiday = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(latest_completed_session(holiday), date(2026, 9, 4))

    def test_as_of_premarket_weekday(self):
        # This is the exact bug the WS8 audit flagged: a run at 09:00 UTC
        # (~04:00-05:00 ET) is well before the 09:30 ET open. Today's session
        # has not happened yet, so the honest answer is the PRIOR trading
        # day, not "today" just because today is a weekday.
        premarket = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(latest_completed_session(premarket), date(2026, 7, 27))

    def test_as_of_postclose_weekday_is_same_day(self):
        # Contrast case: once the conservative close boundary has passed,
        # today's own session IS the latest completed one.
        postclose = datetime(2026, 7, 28, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(latest_completed_session(postclose), date(2026, 7, 28))

    def test_naive_datetime_treated_as_utc(self):
        naive = datetime(2026, 7, 28, 9, 0)
        self.assertEqual(latest_completed_session(naive), date(2026, 7, 27))


class TestSessionsBetween(unittest.TestCase):
    def test_zero_across_weekend_only(self):
        # Friday post-close vs. the following Monday pre-market: both
        # represent the SAME completed session (Friday's close) -- no new
        # session has completed yet.
        fri_close = datetime(2026, 7, 24, 22, 0, tzinfo=timezone.utc)
        mon_premarket = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(sessions_between(fri_close, mon_premarket), 0)

    def test_positive_across_a_real_new_close(self):
        fri_close = datetime(2026, 7, 24, 22, 0, tzinfo=timezone.utc)
        mon_close = datetime(2026, 7, 27, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(sessions_between(fri_close, mon_close), 1)

    def test_reverse_or_equal_is_zero(self):
        t = datetime(2026, 7, 27, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(sessions_between(t, t), 0)
        earlier = datetime(2026, 7, 24, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(sessions_between(t, earlier), 0)


class TestSessionProvenance(unittest.TestCase):
    def test_shape_and_values(self):
        premarket = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)
        prov = session_provenance(premarket)
        self.assertEqual(prov["latest_session_represented"], "2026-07-27")
        self.assertEqual(prov["source_data_through"], "2026-07-27T21:00:00+00:00")
        self.assertFalse(prov["coverage_exceeded"])

    def test_coverage_exceeded_flag_past_horizon(self):
        far_future = datetime(2028, 6, 1, 15, 0, tzinfo=timezone.utc)
        prov = session_provenance(far_future)
        self.assertTrue(prov["coverage_exceeded"])

    def test_never_raises_on_naive_datetime(self):
        naive = datetime(2026, 7, 28, 9, 0)
        prov = session_provenance(naive)
        self.assertIn("latest_session_represented", prov)


class TestCoverageHorizonExplicit(unittest.TestCase):
    def test_constant_matches_max_holiday_date(self):
        self.assertEqual(HOLIDAY_COVERAGE_THROUGH, max(NYSE_HOLIDAYS))


if __name__ == "__main__":
    unittest.main()
