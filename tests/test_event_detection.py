"""
Tests for event_detection.py — focused on signal quality.

Covers BREAKOUT_PROXY quality guards (upward pressure threshold and volume
confirmation) that prevent false breakout signals from propagating into the
compounder labelling and strategy routing stages.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from universal_scanner import ScanResult
from event_detection import detect_events, EventType


def _sr(symbol="TEST", **overrides):
    """Build a minimal ScanResult with sensible defaults."""
    defaults = dict(
        price=100.0,
        pct_change_1d=2.0,
        rel_volume=1.5,
        day_range_pct=2.5,
        pct_from_year_high=-1.0,   # within 2% proximity threshold
        avg_volume=1_000_000,
        volume=1_500_000,
    )
    defaults.update(overrides)
    return ScanResult(symbol=symbol, **defaults)


def _events_for(sr: ScanResult) -> set:
    events = detect_events([sr])
    return {e.event_type for e in events if e.symbol == sr.symbol}


# ---------------------------------------------------------------------------
# BREAKOUT_PROXY — minimum upward pressure threshold (1.0%)
# ---------------------------------------------------------------------------

class TestBreakoutProxyPressureThreshold(unittest.TestCase):

    def test_exactly_1pct_upward_pressure_fires(self):
        sr = _sr(pct_change_1d=1.0, pct_from_year_high=-1.0, rel_volume=1.5)
        self.assertIn(EventType.BREAKOUT_PROXY, _events_for(sr))

    def test_just_above_1pct_fires(self):
        sr = _sr(pct_change_1d=1.1, pct_from_year_high=-1.0, rel_volume=1.5)
        self.assertIn(EventType.BREAKOUT_PROXY, _events_for(sr))

    def test_0_7pct_no_longer_fires(self):
        # Old threshold was 0.5% — this would have fired before; it should NOT now.
        sr = _sr(pct_change_1d=0.7, pct_from_year_high=-1.0, rel_volume=1.5)
        self.assertNotIn(EventType.BREAKOUT_PROXY, _events_for(sr))

    def test_exactly_old_threshold_05pct_no_longer_fires(self):
        sr = _sr(pct_change_1d=0.5, pct_from_year_high=-0.5, rel_volume=2.0)
        self.assertNotIn(EventType.BREAKOUT_PROXY, _events_for(sr))

    def test_negative_change_never_fires(self):
        sr = _sr(pct_change_1d=-0.5, pct_from_year_high=-0.5, rel_volume=2.0)
        self.assertNotIn(EventType.BREAKOUT_PROXY, _events_for(sr))

    def test_large_move_at_high_fires(self):
        sr = _sr(pct_change_1d=5.0, pct_from_year_high=0.0, rel_volume=3.0)
        self.assertIn(EventType.BREAKOUT_PROXY, _events_for(sr))


# ---------------------------------------------------------------------------
# BREAKOUT_PROXY — volume confirmation
# ---------------------------------------------------------------------------

class TestBreakoutProxyVolumeConfirmation(unittest.TestCase):

    def test_rel_volume_above_08_fires(self):
        sr = _sr(pct_change_1d=1.5, pct_from_year_high=-1.0, rel_volume=0.8)
        self.assertIn(EventType.BREAKOUT_PROXY, _events_for(sr))

    def test_rel_volume_below_08_does_not_fire(self):
        # Low-volume drift near high is a noise signal, not a real breakout.
        sr = _sr(pct_change_1d=1.5, pct_from_year_high=-1.0, rel_volume=0.5)
        self.assertNotIn(EventType.BREAKOUT_PROXY, _events_for(sr))

    def test_rel_volume_exactly_08_fires(self):
        sr = _sr(pct_change_1d=1.5, pct_from_year_high=-1.0, rel_volume=0.8)
        self.assertIn(EventType.BREAKOUT_PROXY, _events_for(sr))

    def test_missing_volume_data_does_not_block_breakout(self):
        # Absent volume data should not penalise the signal — we don't
        # require data we don't have.
        sr = _sr(pct_change_1d=1.5, pct_from_year_high=-1.0)
        sr.rel_volume = None  # explicit None
        self.assertIn(EventType.BREAKOUT_PROXY, _events_for(sr))

    def test_high_volume_strong_move_near_high_fires(self):
        sr = _sr(pct_change_1d=3.5, pct_from_year_high=-0.3, rel_volume=4.0)
        self.assertIn(EventType.BREAKOUT_PROXY, _events_for(sr))

    def test_low_volume_blocks_even_strong_move(self):
        # 3.5% move near the high but only 60% average volume → no breakout signal.
        sr = _sr(pct_change_1d=3.5, pct_from_year_high=-0.5, rel_volume=0.6)
        self.assertNotIn(EventType.BREAKOUT_PROXY, _events_for(sr))


# ---------------------------------------------------------------------------
# BREAKOUT_PROXY — proximity threshold still respected
# ---------------------------------------------------------------------------

class TestBreakoutProxyProximity(unittest.TestCase):

    def test_too_far_below_high_does_not_fire(self):
        sr = _sr(pct_change_1d=2.0, pct_from_year_high=-5.0, rel_volume=2.0)
        self.assertNotIn(EventType.BREAKOUT_PROXY, _events_for(sr))

    def test_at_exactly_2pct_below_high_fires(self):
        sr = _sr(pct_change_1d=2.0, pct_from_year_high=-2.0, rel_volume=2.0)
        self.assertIn(EventType.BREAKOUT_PROXY, _events_for(sr))

    def test_custom_proximity_threshold(self):
        # 3% below the high: fires when proximity_threshold = 4%, doesn't fire at 2%.
        sr = _sr(pct_change_1d=1.5, pct_from_year_high=-3.0, rel_volume=1.5)
        events_tight = detect_events([sr], config={"breakout_proximity_pct": 2.0})
        events_loose = detect_events([sr], config={"breakout_proximity_pct": 4.0})
        tight_types = {e.event_type for e in events_tight if e.symbol == "TEST"}
        loose_types = {e.event_type for e in events_loose if e.symbol == "TEST"}
        self.assertNotIn(EventType.BREAKOUT_PROXY, tight_types)
        self.assertIn(EventType.BREAKOUT_PROXY, loose_types)


# ---------------------------------------------------------------------------
# Other events unaffected by BREAKOUT_PROXY changes
# ---------------------------------------------------------------------------

class TestOtherEventsUnchanged(unittest.TestCase):

    def test_strong_move_up_still_fires_at_3pct(self):
        sr = _sr(pct_change_1d=3.0, pct_from_year_high=-20.0, rel_volume=1.0)
        self.assertIn(EventType.STRONG_MOVE_UP, _events_for(sr))

    def test_volume_spike_fires_independently(self):
        # High volume, minimal move — no BREAKOUT_PROXY but VOLUME_SPIKE fires.
        sr = _sr(pct_change_1d=0.3, pct_from_year_high=-10.0, rel_volume=3.0)
        ev = _events_for(sr)
        self.assertIn(EventType.VOLUME_SPIKE, ev)
        self.assertNotIn(EventType.BREAKOUT_PROXY, ev)

    def test_volatility_expansion_independent_of_breakout(self):
        sr = _sr(pct_change_1d=0.2, day_range_pct=5.0, pct_from_year_high=-20.0)
        self.assertIn(EventType.VOLATILITY_EXPANSION, _events_for(sr))


if __name__ == "__main__":
    unittest.main(verbosity=2)
