import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from allocation_engine import suggest_allocation, DEFAULT_CONFIG


class TestAllocationEngine(unittest.TestCase):
    def _opportunity(self, **overrides):
        payload = {
            "symbol": "AAPL",
            "score": 82.0,
            "confidence": 0.80,
            "sector": "Technology",
        }
        payload.update(overrides)
        return payload

    def test_high_confidence_compounder_gets_base_size(self):
        suggestion = suggest_allocation(
            opportunity=self._opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
        )

        # Compounder base = 10% post-retune (operator-approved 2026-05-18)
        self.assertAlmostEqual(suggestion.suggested_pct, 0.10, places=4)
        self.assertAlmostEqual(suggestion.suggested_amount, 10_000.0, places=2)

    def test_momentum_and_lower_confidence_size_smaller(self):
        suggestion = suggest_allocation(
            opportunity=self._opportunity(score=70.0, confidence=0.64),
            strategy_type="momentum",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
        )

        # Momentum base = 6% post-retune; 0.64 confidence is medium (×0.75) → ~4.5%
        self.assertLess(suggestion.suggested_pct, 0.06)
        self.assertLess(suggestion.suggested_amount, 6_000.0)

    def test_sector_cap_limits_size(self):
        suggestion = suggest_allocation(
            opportunity=self._opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            current_sector_exposure=0.09,
            config={"sector_cap": 0.10},
        )

        self.assertAlmostEqual(suggestion.suggested_pct, 0.01, places=4)
        self.assertIn("sector_cap", suggestion.capped_by)

    def test_cash_reserve_blocks_new_allocation(self):
        suggestion = suggest_allocation(
            opportunity=self._opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=3_000.0,
        )

        self.assertEqual(suggestion.suggested_amount, 0.0)
        self.assertEqual(suggestion.deployable_cash, 0.0)

    def test_degraded_mode_reduces_size(self):
        normal = suggest_allocation(
            opportunity=self._opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
        )
        degraded = suggest_allocation(
            opportunity=self._opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            context={"degraded_mode": True},
        )

        self.assertLess(degraded.suggested_amount, normal.suggested_amount)

    def test_confidence_percent_scale_is_normalized(self):
        suggestion = suggest_allocation(
            opportunity=self._opportunity(confidence=60),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
        )

        # Confidence 60% is the medium threshold → 0.75× of 10% base = 7.5%
        self.assertAlmostEqual(suggestion.confidence, 0.60, places=3)
        self.assertAlmostEqual(suggestion.suggested_pct, 0.075, places=4)


class TestSectorCapDefault(unittest.TestCase):
    def _opportunity(self, **overrides):
        payload = {"symbol": "AAPL", "score": 82.0, "confidence": 0.80}
        payload.update(overrides)
        return payload

    def test_default_sector_cap_is_25_pct(self):
        # 2026-06-26 targeted partial revert: sector_cap 0.35 -> 0.25
        self.assertAlmostEqual(DEFAULT_CONFIG["sector_cap"], 0.25, places=4)

    def test_no_sector_exposure_allows_full_base_size(self):
        # sector_cap=0.25, no existing exposure → headroom=0.25 > base 10% → no cap applied
        suggestion = suggest_allocation(
            opportunity=self._opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            current_sector_exposure=0.0,
        )
        self.assertAlmostEqual(suggestion.suggested_pct, 0.10, places=4)
        self.assertNotIn("sector_cap", suggestion.capped_by)

    def test_sector_nearly_full_limits_allocation(self):
        # 22% already in sector, default cap 25% → 3% headroom < 10% base → cap kicks in
        suggestion = suggest_allocation(
            opportunity=self._opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            current_sector_exposure=0.22,
        )
        self.assertAlmostEqual(suggestion.suggested_pct, 0.03, places=4)
        self.assertIn("sector_cap", suggestion.capped_by)

    def test_sector_at_cap_zeroes_allocation(self):
        # 25% already in sector, cap is 25% → 0% headroom → no allocation
        suggestion = suggest_allocation(
            opportunity=self._opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            current_sector_exposure=0.25,
        )
        self.assertAlmostEqual(suggestion.suggested_pct, 0.0, places=4)
        self.assertAlmostEqual(suggestion.suggested_amount, 0.0, places=2)

    def test_sector_cap_can_be_disabled_with_none(self):
        # 20% exposure with default cap 25% → 5% headroom < 10% base → capped at 5%.
        # Passing sector_cap=None removes the cap entirely → full 10% comes through.
        capped = suggest_allocation(
            opportunity=self._opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            current_sector_exposure=0.20,
        )
        uncapped = suggest_allocation(
            opportunity=self._opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            current_sector_exposure=0.20,
            config={"sector_cap": None},
        )
        self.assertAlmostEqual(capped.suggested_pct, 0.05, places=4)
        self.assertIn("sector_cap", capped.capped_by)
        self.assertAlmostEqual(uncapped.suggested_pct, 0.10, places=4)
        self.assertNotIn("sector_cap", uncapped.capped_by)


class TestMinPositionPct(unittest.TestCase):
    def _opportunity(self, **overrides):
        payload = {"symbol": "AAPL", "score": 50.0, "confidence": 0.50}
        payload.update(overrides)
        return payload

    def test_default_min_position_pct_is_1_pct(self):
        self.assertAlmostEqual(DEFAULT_CONFIG["min_position_pct"], 0.01, places=4)

    def test_normal_size_above_floor_unaffected(self):
        # 6% momentum at medium confidence → 4.5%, well above 1% floor (post-retune)
        suggestion = suggest_allocation(
            opportunity=self._opportunity(confidence=0.64),
            strategy_type="momentum",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
        )
        self.assertGreater(suggestion.suggested_pct, 0.01)

    def test_extreme_penalties_zero_out_tiny_position(self):
        # After tactical retune, momentum base is 6% and low_confidence_multiplier
        # is 0.65; the stacked-penalty product is roughly:
        #   6% × 0.65 × 0.55 × 0.65 ≈ 1.39%.
        # Raise min_position_pct to 0.02 so the same "zero out tiny positions"
        # invariant is still exercised on the new baseline.
        suggestion = suggest_allocation(
            opportunity=self._opportunity(confidence=0.40),
            strategy_type="momentum",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            context={"degraded_mode": True, "regime_label": "risk_off"},
            config={"min_position_pct": 0.02},
        )
        self.assertAlmostEqual(suggestion.suggested_pct, 0.0, places=4)
        self.assertAlmostEqual(suggestion.suggested_amount, 0.0, places=2)

    def test_min_position_pct_zero_disables_floor(self):
        # With min_position_pct=0.0, even tiny positions are allowed through
        suggestion = suggest_allocation(
            opportunity=self._opportunity(confidence=0.40),
            strategy_type="momentum",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            context={"degraded_mode": True, "regime_label": "risk_off"},
            config={"min_position_pct": 0.0},
        )
        # Should be non-zero (the ~0.54% passes through)
        self.assertGreater(suggestion.suggested_pct, 0.0)

    def test_min_position_pct_custom_threshold(self):
        # Post-retune: momentum 6% × medium 0.75 = 4.5%. Set the floor to 5% so
        # the same intent (floor zeros a position that would otherwise pass) is preserved.
        suggestion = suggest_allocation(
            opportunity=self._opportunity(confidence=0.64),
            strategy_type="momentum",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            config={"min_position_pct": 0.05},
        )
        # 6% × 0.75 = 4.5%, which is below the 5% floor → zeroed
        self.assertAlmostEqual(suggestion.suggested_pct, 0.0, places=4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
