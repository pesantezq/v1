"""
Unit tests for run-mode gating, run lock, and contribution plan.

These tests are fully offline — no network calls, no live prices, no file
system side effects (temp dirs are used and cleaned up).
"""

import os
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from unittest.mock import patch

# Ensure project root is on the path when run from tests/ or root
sys.path.insert(0, str(Path(__file__).parent.parent))

from run_lock import acquire_run_lock, release_run_lock, STALE_AFTER_MINUTES
from contribution_engine import ContributionEngine, EQUITY_ASSET_CLASSES
from drawdown import DrawdownState, DrawdownTracker, DRAWDOWN_THRESHOLDS_DEFAULT


# ---------------------------------------------------------------------------
# Lightweight stubs (no live prices needed)
# ---------------------------------------------------------------------------

@dataclass
class _Holding:
    symbol: str
    shares: float
    target_weight: float
    asset_class: str
    is_leveraged: bool = False
    leverage_factor: float = 1.0
    current_price: Optional[float] = 100.0
    market_value: Optional[float] = None
    actual_weight: Optional[float] = None
    drift: Optional[float] = None

    @property
    def effective_exposure(self) -> float:
        return (self.market_value or 0.0) * self.leverage_factor


@dataclass
class _Analysis:
    symbol: str
    drift: Optional[float] = None
    is_breached: bool = False


# ---------------------------------------------------------------------------
# Run Lock tests
# ---------------------------------------------------------------------------

class TestRunLock(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock_file = Path(self.tmp.name) / "run.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_acquire_succeeds_when_no_lock_exists(self):
        self.assertTrue(acquire_run_lock(self.lock_file))
        self.assertTrue(self.lock_file.exists())

    def test_acquire_fails_when_recent_lock_exists(self):
        # First acquisition
        self.assertTrue(acquire_run_lock(self.lock_file))
        # Second acquisition should fail (lock is fresh)
        self.assertFalse(acquire_run_lock(self.lock_file))

    def test_acquire_succeeds_after_stale_lock(self):
        # Write a lock file with an old modification time
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self.lock_file.write_text("99999")
        # Back-date the mtime by more than STALE_AFTER_MINUTES
        stale_time = datetime.now() - timedelta(minutes=STALE_AFTER_MINUTES + 1)
        stale_ts = stale_time.timestamp()
        os.utime(self.lock_file, (stale_ts, stale_ts))

        self.assertTrue(acquire_run_lock(self.lock_file))

    def test_release_removes_lock_file(self):
        acquire_run_lock(self.lock_file)
        self.assertTrue(self.lock_file.exists())
        release_run_lock(self.lock_file)
        self.assertFalse(self.lock_file.exists())

    def test_release_is_safe_when_no_lock(self):
        """release_run_lock should not raise if there is no lock file."""
        release_run_lock(self.lock_file)  # Should not raise


# ---------------------------------------------------------------------------
# Run-mode email gating logic tests
# ---------------------------------------------------------------------------

class TestRunModeGating(unittest.TestCase):
    """
    Tests for the email-gating decision logic introduced by run_mode.

    We test the logic in isolation rather than through the full
    run_portfolio_update pipeline so there are no API calls or file writes.
    """

    def _has_urgent(self, portfolio_adjustments, scored_recommendations) -> bool:
        """Mirror of the 'daily' urgency check in main.py."""
        from scoring import ActionLevel
        from adjustment import ActionLevel as AdjActionLevel

        return any(
            a.action_level == AdjActionLevel.ACTION_REQUIRED
            for a in portfolio_adjustments
        ) or any(
            r.action_level == ActionLevel.ACTION_REQUIRED
            for r in scored_recommendations
        )

    def test_daily_silent_when_no_action_required(self):
        """Daily mode: no ACTION_REQUIRED → no email should be sent."""
        from adjustment import (
            PortfolioAdjustment, ActionLevel as AdjActionLevel,
            AdjustmentMode, RecommendationType,
        )

        adj = PortfolioAdjustment(
            symbol='QQQ',
            rec_key='qr:QQQ',
            recommendation_type=RecommendationType.PORTFOLIO_ADJUSTMENT,
            title='Monitor drift',
            what='QQQ is slightly underweight',
            why='Drift within tolerance',
            do='No action needed',
            next_check='Monthly',
            action_level=AdjActionLevel.MONITOR,
            adjustment_mode=AdjustmentMode.CONTRIBUTE_ONLY,
            final_score=25,
            drift=None,
            is_leveraged=False,
        )
        self.assertFalse(self._has_urgent([adj], []))

    def test_daily_sends_when_action_required_in_adjustments(self):
        """Daily mode: ACTION_REQUIRED in adjustments → email should be sent."""
        from adjustment import (
            PortfolioAdjustment, ActionLevel as AdjActionLevel,
            AdjustmentMode, RecommendationType,
        )

        adj = PortfolioAdjustment(
            symbol='QLD',
            rec_key='sv:QLD',
            recommendation_type=RecommendationType.RISK_ALERT,
            title='Leverage cap exceeded',
            what='QLD exposure is 20%',
            why='Exceeds 15% leverage cap',
            do='Trim QLD',
            next_check='Immediately',
            action_level=AdjActionLevel.ACTION_REQUIRED,
            adjustment_mode=AdjustmentMode.TRIM_LEVERAGE_FIRST,
            final_score=95,
            drift=0.05,
            is_leveraged=True,
        )
        self.assertTrue(self._has_urgent([adj], []))

    def test_daily_sends_when_action_required_in_scored_recs(self):
        """Daily mode: ACTION_REQUIRED in scored recommendations → email triggered.

        action_level is a computed property: score >= 75 → ACTION_REQUIRED.
        """
        from scoring import ActionLevel, FinanceRecommendation, ScoringComponents, ImpactArea

        # severity(30) + persistence(20) + impact(15) + priority(10) = 75 → ACTION_REQUIRED
        components = ScoringComponents(severity=30, persistence=20, impact=15, priority=10, confidence=100)
        rec = FinanceRecommendation(
            id='test-001',
            title='Concentration cap breached',
            trigger='concentration',
            what_changed='QQQ > 40%',
            why_it_matters='Portfolio too concentrated',
            action='Trim QQQ',
            next_check='This week',
            evidence='QQQ at 42%',
            impact_area=ImpactArea.PORTFOLIO_RISK,
            components=components,
        )
        self.assertEqual(rec.action_level, ActionLevel.ACTION_REQUIRED)
        self.assertTrue(self._has_urgent([], [rec]))

    def test_daily_silent_with_only_recommended_level(self):
        """Daily mode: RECOMMENDED items alone should not trigger an email.

        action_level is RECOMMENDED when score is in [50, 74].
        """
        from scoring import ActionLevel, FinanceRecommendation, ScoringComponents, ImpactArea

        # severity(20) + persistence(10) + impact(10) + priority(10) = 50 → RECOMMENDED
        components = ScoringComponents(severity=20, persistence=10, impact=10, priority=10, confidence=100)
        rec = FinanceRecommendation(
            id='test-002',
            title='Consider rebalancing',
            trigger='drift',
            what_changed='Slight drift',
            why_it_matters='Minor drift',
            action='Monitor',
            next_check='Monthly',
            evidence='Drift 5%',
            impact_area=ImpactArea.PORTFOLIO_RISK,
            components=components,
        )
        self.assertEqual(rec.action_level, ActionLevel.RECOMMENDED)
        self.assertFalse(self._has_urgent([], [rec]))


# ---------------------------------------------------------------------------
# Contribution plan total-budget tests
# ---------------------------------------------------------------------------

class TestContributionBudget(unittest.TestCase):

    def _make_portfolio(self):
        """Two underweight non-leveraged holdings, one overweight, one leveraged."""
        holdings = [
            _Holding('QQQ',  shares=6,  target_weight=0.45, asset_class='us_equity',
                     market_value=500, actual_weight=0.25),
            _Holding('GLD',  shares=4,  target_weight=0.20, asset_class='commodity',
                     market_value=400, actual_weight=0.20),
            _Holding('VXUS', shares=0,  target_weight=0.10, asset_class='international_equity',
                     market_value=0,   actual_weight=0.00),
            _Holding('QLD',  shares=8,  target_weight=0.05, asset_class='us_equity_leveraged',
                     is_leveraged=True, market_value=100, actual_weight=0.05),
        ]
        analyses = [
            _Analysis('QQQ',  drift=-0.20),   # 20% underweight
            _Analysis('GLD',  drift=0.0),     # at target
            _Analysis('VXUS', drift=-0.10),   # 10% underweight
            _Analysis('QLD',  drift=0.0),     # at target (leveraged)
        ]
        return holdings, analyses

    def test_total_allocated_never_exceeds_contribution(self):
        holdings, analyses = self._make_portfolio()
        engine = ContributionEngine(concentration_cap=0.40)
        result = engine.allocate(
            holdings=holdings,
            analyses=analyses,
            total_portfolio=2000.0,
            monthly_contribution=1000.0,
            drawdown_regime='normal',
        )
        total = sum(a.recommended_dollars for a in result)
        self.assertLessEqual(total, 1000.0 + 0.01)  # allow for float rounding

    def test_leveraged_holding_never_receives_contribution(self):
        holdings, analyses = self._make_portfolio()
        engine = ContributionEngine(concentration_cap=0.40)
        result = engine.allocate(
            holdings=holdings,
            analyses=analyses,
            total_portfolio=2000.0,
            monthly_contribution=1000.0,
        )
        symbols = {a.symbol for a in result}
        self.assertNotIn('QLD', symbols)

    def test_equity_priority_in_drawdown_regime(self):
        """During a drawdown, equity holdings should be first in the plan."""
        holdings, analyses = self._make_portfolio()
        # Make both QQQ and VXUS underweight so the order matters
        analyses[0].drift = -0.15   # QQQ (equity)
        analyses[2].drift = -0.12   # VXUS (international equity)

        engine = ContributionEngine(concentration_cap=0.40)
        result = engine.allocate(
            holdings=holdings,
            analyses=analyses,
            total_portfolio=2000.0,
            monthly_contribution=1000.0,
            drawdown_regime='modest_dip',
        )
        if len(result) >= 2:
            # Both should be equity class
            self.assertIn(result[0].asset_class, EQUITY_ASSET_CLASSES)


# ---------------------------------------------------------------------------
# Drawdown regime tests (complementary to test_growth_mode.py)
# ---------------------------------------------------------------------------

class TestDrawdownRegimes(unittest.TestCase):

    def _state(self, high: float, current: float) -> DrawdownState:
        s = DrawdownState()
        s.rolling_12m_high = high
        s.current_value = current
        return s

    def test_normal_regime_below_threshold(self):
        s = self._state(1000, 950)   # 5% drawdown
        tracker = DrawdownTracker.__new__(DrawdownTracker)
        tracker._state = s
        self.assertEqual(tracker.get_regime(DRAWDOWN_THRESHOLDS_DEFAULT), 'normal')

    def test_modest_dip_at_boundary(self):
        s = self._state(1000, 900)   # exactly 10% drawdown
        tracker = DrawdownTracker.__new__(DrawdownTracker)
        tracker._state = s
        self.assertEqual(tracker.get_regime(DRAWDOWN_THRESHOLDS_DEFAULT), 'modest_dip')

    def test_significant_dip(self):
        s = self._state(1000, 750)   # 25% drawdown
        tracker = DrawdownTracker.__new__(DrawdownTracker)
        tracker._state = s
        self.assertEqual(tracker.get_regime(DRAWDOWN_THRESHOLDS_DEFAULT), 'significant_dip')

    def test_severe_dip(self):
        s = self._state(1000, 650)   # 35% drawdown
        tracker = DrawdownTracker.__new__(DrawdownTracker)
        tracker._state = s
        self.assertEqual(tracker.get_regime(DRAWDOWN_THRESHOLDS_DEFAULT), 'severe_dip')

    def test_suppress_sells_threshold(self):
        """Sells should be suppressed at exactly 20% drawdown."""
        s = self._state(1000, 800)
        tracker = DrawdownTracker.__new__(DrawdownTracker)
        tracker._state = s
        self.assertTrue(tracker.should_suppress_sells(leverage_violation=False))

    def test_suppress_sells_bypassed_for_leverage(self):
        """Leverage cap violations bypass anti-panic gating."""
        s = self._state(1000, 800)
        tracker = DrawdownTracker.__new__(DrawdownTracker)
        tracker._state = s
        self.assertFalse(tracker.should_suppress_sells(leverage_violation=True))


if __name__ == '__main__':
    unittest.main(verbosity=2)
