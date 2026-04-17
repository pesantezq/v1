"""
Unit tests for Aggressive Wealth Growth Mode modules.

Tests:
  - Drawdown calculation and regime classification
  - Contribution allocation respecting concentration cap
  - Structural violation detection (concentration + leverage cap)
  - Projections and milestone monotonicity
"""

import sys
import os
import unittest
from dataclasses import dataclass
from typing import Optional

# Make sure the parent directory is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from drawdown import DrawdownState, DrawdownTracker, DRAWDOWN_THRESHOLDS_DEFAULT
from contribution_engine import ContributionEngine, ContributionAllocation
from projections import (
    project_future_value,
    estimate_milestone,
    compute_portfolio_cagr,
    compute_compounding_dashboard,
)
from adjustment import detect_structural_violations


# ---------------------------------------------------------------------------
# Lightweight stubs so tests don't need live market data
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


@dataclass
class _Analysis:
    symbol: str
    drift: Optional[float]
    actual_weight: float
    target_weight: float
    is_breached: bool = False
    drift_direction: str = 'neutral'


# ---------------------------------------------------------------------------
# Drawdown tests
# ---------------------------------------------------------------------------

class TestDrawdownState(unittest.TestCase):
    """Test DrawdownState property calculations."""

    def test_no_drawdown_at_ath(self):
        state = DrawdownState(
            all_time_high=10_000,
            rolling_12m_high=10_000,
            current_value=10_000,
        )
        self.assertAlmostEqual(state.drawdown_from_ath, 0.0)
        self.assertAlmostEqual(state.drawdown_from_12m_high, 0.0)

    def test_drawdown_calculation(self):
        state = DrawdownState(
            all_time_high=20_000,
            rolling_12m_high=15_000,
            current_value=12_000,
        )
        self.assertAlmostEqual(state.drawdown_from_ath, 0.40)
        self.assertAlmostEqual(state.drawdown_from_12m_high, 0.20)

    def test_no_negative_drawdown(self):
        """Current value above rolling high should return 0, not negative."""
        state = DrawdownState(
            all_time_high=10_000,
            rolling_12m_high=9_000,
            current_value=9_500,
        )
        self.assertEqual(state.drawdown_from_12m_high, 0.0)

    def test_regime_normal(self):
        state = DrawdownState(all_time_high=10_000, rolling_12m_high=10_000, current_value=9_500)
        tracker = DrawdownTracker.__new__(DrawdownTracker)
        tracker._state = state
        self.assertEqual(tracker.get_regime(), 'normal')

    def test_regime_modest_dip(self):
        state = DrawdownState(all_time_high=10_000, rolling_12m_high=10_000, current_value=8_500)
        tracker = DrawdownTracker.__new__(DrawdownTracker)
        tracker._state = state
        self.assertEqual(tracker.get_regime(), 'modest_dip')

    def test_regime_significant_dip(self):
        state = DrawdownState(all_time_high=10_000, rolling_12m_high=10_000, current_value=7_500)
        tracker = DrawdownTracker.__new__(DrawdownTracker)
        tracker._state = state
        self.assertEqual(tracker.get_regime(), 'significant_dip')

    def test_regime_severe_dip(self):
        state = DrawdownState(all_time_high=10_000, rolling_12m_high=10_000, current_value=6_500)
        tracker = DrawdownTracker.__new__(DrawdownTracker)
        tracker._state = state
        self.assertEqual(tracker.get_regime(), 'severe_dip')

    def test_suppress_sells_normal(self):
        state = DrawdownState(all_time_high=10_000, rolling_12m_high=10_000, current_value=9_500)
        tracker = DrawdownTracker.__new__(DrawdownTracker)
        tracker._state = state
        self.assertFalse(tracker.should_suppress_sells())

    def test_suppress_sells_significant_dip(self):
        """Drawdown >20% should suppress sells (unless leverage violation)."""
        state = DrawdownState(all_time_high=10_000, rolling_12m_high=10_000, current_value=7_000)
        tracker = DrawdownTracker.__new__(DrawdownTracker)
        tracker._state = state
        self.assertTrue(tracker.should_suppress_sells(leverage_violation=False))
        # Leverage violations still allowed
        self.assertFalse(tracker.should_suppress_sells(leverage_violation=True))


# ---------------------------------------------------------------------------
# Contribution engine tests
# ---------------------------------------------------------------------------

class TestContributionEngine(unittest.TestCase):

    def _make_pair(
        self,
        symbol,
        target_weight,
        actual_weight,
        asset_class='us_equity',
        is_leveraged=False,
    ):
        drift = actual_weight - target_weight
        holding = _Holding(
            symbol=symbol,
            shares=10,
            target_weight=target_weight,
            asset_class=asset_class,
            is_leveraged=is_leveraged,
            actual_weight=actual_weight,
        )
        analysis = _Analysis(
            symbol=symbol,
            drift=drift,
            actual_weight=actual_weight,
            target_weight=target_weight,
        )
        return holding, analysis

    def test_allocates_to_most_underweight(self):
        """Contribution goes to the most underweight holding."""
        h1, a1 = self._make_pair('AAA', target_weight=0.40, actual_weight=0.30)  # -10%
        h2, a2 = self._make_pair('BBB', target_weight=0.40, actual_weight=0.36)  # -4%

        engine = ContributionEngine(concentration_cap=0.60)
        plan = engine.allocate(
            holdings=[h1, h2], analyses=[a1, a2],
            total_portfolio=10_000, monthly_contribution=1_000,
        )
        # AAA should receive money first (most underweight)
        symbols = [a.symbol for a in plan]
        self.assertEqual(symbols[0], 'AAA')

    def test_respects_concentration_cap(self):
        """Allocation must not push a holding above the concentration cap."""
        # AAA is at 38% — cap is 40% — can only receive 2% × $10k = $200
        h1, a1 = self._make_pair('AAA', target_weight=0.50, actual_weight=0.38)
        engine = ContributionEngine(concentration_cap=0.40)
        plan = engine.allocate(
            holdings=[h1], analyses=[a1],
            total_portfolio=10_000, monthly_contribution=1_000,
        )
        for alloc in plan:
            if alloc.symbol == 'AAA':
                max_allowed = (0.40 - 0.38) * 10_000  # = $200
                self.assertLessEqual(alloc.recommended_dollars, max_allowed + 0.01)

    def test_skips_leveraged_holdings(self):
        """Leveraged holdings are never contribution targets."""
        h1, a1 = self._make_pair('LEV', 0.10, 0.05, is_leveraged=True)
        engine = ContributionEngine()
        plan = engine.allocate(
            holdings=[h1], analyses=[a1],
            total_portfolio=10_000, monthly_contribution=500,
        )
        self.assertEqual(len(plan), 0)

    def test_skips_overweight_holdings(self):
        """Overweight holdings get no new contribution."""
        h1, a1 = self._make_pair('OVR', 0.30, 0.42)  # +12% overweight
        engine = ContributionEngine()
        plan = engine.allocate(
            holdings=[h1], analyses=[a1],
            total_portfolio=10_000, monthly_contribution=500,
        )
        self.assertEqual(len(plan), 0)

    def test_no_contribution_when_zero(self):
        h1, a1 = self._make_pair('AAA', 0.40, 0.30)
        engine = ContributionEngine()
        plan = engine.allocate(
            holdings=[h1], analyses=[a1],
            total_portfolio=10_000, monthly_contribution=0,
        )
        self.assertEqual(plan, [])

    def test_equity_tilt_during_drawdown(self):
        """During a drawdown, equity holdings get prioritised over non-equity."""
        h_equity, a_equity = self._make_pair(
            'EQ', target_weight=0.40, actual_weight=0.35, asset_class='us_equity'
        )
        h_commodity, a_commodity = self._make_pair(
            'COMM', target_weight=0.30, actual_weight=0.20, asset_class='commodity'
        )
        # Both are underweight; commodity is MORE underweight (-10% vs -5%)
        # but during drawdown equity should come first

        engine = ContributionEngine(concentration_cap=0.80)
        plan = engine.allocate(
            holdings=[h_equity, h_commodity],
            analyses=[a_equity, a_commodity],
            total_portfolio=10_000,
            monthly_contribution=500,
            drawdown_regime='modest_dip',
        )
        self.assertEqual(plan[0].symbol, 'EQ')  # equity prioritised

    def test_total_allocated_does_not_exceed_contribution(self):
        """Total allocation must not exceed the contribution amount."""
        holdings_pairs = [
            self._make_pair('A', 0.25, 0.10),
            self._make_pair('B', 0.25, 0.10),
            self._make_pair('C', 0.25, 0.10),
        ]
        holdings = [p[0] for p in holdings_pairs]
        analyses = [p[1] for p in holdings_pairs]

        engine = ContributionEngine(concentration_cap=0.60)
        plan = engine.allocate(
            holdings=holdings, analyses=analyses,
            total_portfolio=10_000, monthly_contribution=800,
        )
        total = sum(a.recommended_dollars for a in plan)
        self.assertLessEqual(total, 800.01)


# ---------------------------------------------------------------------------
# Structural violation tests
# ---------------------------------------------------------------------------

class TestStructuralViolations(unittest.TestCase):

    def _make_pair(
        self, symbol, target_weight, actual_weight,
        is_leveraged=False, leverage_factor=1.0, price=100.0,
    ):
        holding = _Holding(
            symbol=symbol,
            shares=100,
            target_weight=target_weight,
            asset_class='us_equity',
            is_leveraged=is_leveraged,
            leverage_factor=leverage_factor,
            current_price=price,
            actual_weight=actual_weight,
        )
        analysis = _Analysis(
            symbol=symbol,
            drift=actual_weight - target_weight,
            actual_weight=actual_weight,
            target_weight=target_weight,
        )
        return holding, analysis

    def test_no_violation_within_cap(self):
        h, a = self._make_pair('AAA', 0.40, 0.38)
        violations = detect_structural_violations(
            holdings=[h], analyses=[a],
            total_portfolio=10_000, concentration_cap=0.40,
        )
        self.assertEqual(violations, [])

    def test_concentration_violation_detected(self):
        """Holding above concentration cap triggers a violation."""
        h, a = self._make_pair('BIG', 0.40, 0.55)
        violations = detect_structural_violations(
            holdings=[h], analyses=[a],
            total_portfolio=10_000, concentration_cap=0.40,
        )
        self.assertEqual(len(violations), 1)
        v = violations[0]
        self.assertIn('CONCENTRATION', v.rec_key)
        self.assertEqual(v.symbol, 'BIG')

    def test_leverage_cap_violation_detected(self):
        """Total leveraged exposure above cap triggers a violation."""
        h, a = self._make_pair(
            'LEV', target_weight=0.05, actual_weight=0.10,
            is_leveraged=True, leverage_factor=2.0
        )
        # Effective exposure = 0.10 × 2 = 0.20 — exceeds 0.15 cap
        violations = detect_structural_violations(
            holdings=[h], analyses=[a],
            total_portfolio=10_000,
            concentration_cap=0.40,
            leverage_cap=0.15,
        )
        lev_violations = [v for v in violations if 'LEVERAGE' in v.rec_key]
        self.assertEqual(len(lev_violations), 1)

    def test_no_leverage_violation_within_cap(self):
        """Leverage exposure within cap — no violation."""
        h, a = self._make_pair(
            'LEV', target_weight=0.05, actual_weight=0.05,
            is_leveraged=True, leverage_factor=2.0
        )
        # Effective = 0.05 × 2 = 0.10 — under 0.15 cap
        violations = detect_structural_violations(
            holdings=[h], analyses=[a],
            total_portfolio=10_000, leverage_cap=0.15,
        )
        lev_violations = [v for v in violations if 'LEVERAGE' in v.rec_key]
        self.assertEqual(len(lev_violations), 0)

    def test_both_violations_detected(self):
        """Both concentration and leverage cap violations are returned."""
        h_big, a_big = self._make_pair('BIG', 0.40, 0.55)
        h_lev, a_lev = self._make_pair(
            'LEV', 0.05, 0.10, is_leveraged=True, leverage_factor=2.0
        )
        violations = detect_structural_violations(
            holdings=[h_big, h_lev],
            analyses=[a_big, a_lev],
            total_portfolio=10_000,
            concentration_cap=0.40,
            leverage_cap=0.15,
        )
        self.assertGreaterEqual(len(violations), 2)


# ---------------------------------------------------------------------------
# Projections tests
# ---------------------------------------------------------------------------

class TestProjections(unittest.TestCase):

    def test_project_future_value_no_contribution(self):
        """Without contributions, value should compound correctly."""
        fv = project_future_value(
            current_value=10_000, monthly_contribution=0,
            annual_cagr=0.10, years=10
        )
        # At 10% CAGR for 10 years: 10000 * 1.10^10 ≈ 25937
        self.assertAlmostEqual(fv, 10_000 * (1.10 ** 10), delta=200)

    def test_project_future_value_zero_years(self):
        fv = project_future_value(10_000, 500, 0.08, 0)
        self.assertEqual(fv, 10_000)

    def test_project_future_value_positive_contributions(self):
        """Contributions should push future value above pure growth."""
        fv_no_contrib = project_future_value(10_000, 0, 0.08, 10)
        fv_with_contrib = project_future_value(10_000, 500, 0.08, 10)
        self.assertGreater(fv_with_contrib, fv_no_contrib)

    def test_milestone_already_reached(self):
        result = estimate_milestone(100_001, 500, 0.08, 100_000)
        self.assertEqual(result, 0.0)

    def test_milestone_monotonically_increasing(self):
        """Higher milestone targets must require more time."""
        y_100k = estimate_milestone(5_000, 500, 0.08, 100_000)
        y_250k = estimate_milestone(5_000, 500, 0.08, 250_000)
        y_500k = estimate_milestone(5_000, 500, 0.08, 500_000)
        self.assertIsNotNone(y_100k)
        self.assertIsNotNone(y_250k)
        self.assertIsNotNone(y_500k)
        self.assertLess(y_100k, y_250k)
        self.assertLess(y_250k, y_500k)

    def test_milestone_unreachable_returns_none(self):
        """Zero CAGR and zero contribution — target unreachable."""
        result = estimate_milestone(1_000, 0, 0.0, 1_000_000)
        self.assertIsNone(result)

    def test_project_extra_200_higher_than_base(self):
        """Adding $200/month should always produce a higher 10-year value."""
        dashboard = compute_compounding_dashboard(
            current_value=10_000,
            monthly_contribution=500,
            expected_cagr=0.09,
            drawdown_pct=0.0,
        )
        self.assertGreater(dashboard.projected_value_10yr_extra_200, dashboard.projected_value_10yr)

    def test_dashboard_milestones_monotone(self):
        """Milestones must be strictly increasing in time."""
        dashboard = compute_compounding_dashboard(
            current_value=5_000,
            monthly_contribution=1_000,
            expected_cagr=0.09,
            drawdown_pct=0.05,
        )
        milestones = [
            dashboard.years_to_100k,
            dashboard.years_to_250k,
            dashboard.years_to_500k,
            dashboard.years_to_1m,
        ]
        # Filter out None (unreachable) and already-reached (0.0)
        reachable = [m for m in milestones if m is not None and m > 0]
        for i in range(len(reachable) - 1):
            self.assertLess(reachable[i], reachable[i + 1])

    def test_portfolio_cagr_weighted(self):
        """CAGR should be weighted sum of holding asset-class returns."""

        @dataclass
        class _H:
            asset_class: str
            actual_weight: float

        holdings = [
            _H('us_equity', 0.60),
            _H('bonds', 0.35),
        ]
        expected_returns = {'us_equity': 0.10, 'bonds': 0.04, 'cash': 0.04}
        cagr = compute_portfolio_cagr(
            holdings=holdings,
            total_portfolio=10_000,
            expected_returns=expected_returns,
            target_cash_weight=0.05,
        )
        # 0.60×0.10 + 0.35×0.04 + 0.05×0.04 = 0.06 + 0.014 + 0.002 = 0.076
        self.assertAlmostEqual(cagr, 0.076, places=4)


if __name__ == '__main__':
    unittest.main()
