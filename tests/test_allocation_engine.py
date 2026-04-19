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

        self.assertAlmostEqual(suggestion.suggested_pct, 0.05, places=4)
        self.assertAlmostEqual(suggestion.suggested_amount, 5_000.0, places=2)

    def test_momentum_and_lower_confidence_size_smaller(self):
        suggestion = suggest_allocation(
            opportunity=self._opportunity(score=70.0, confidence=0.64),
            strategy_type="momentum",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
        )

        self.assertLess(suggestion.suggested_pct, 0.03)
        self.assertLess(suggestion.suggested_amount, 3_000.0)

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

        self.assertAlmostEqual(suggestion.confidence, 0.60, places=3)
        self.assertAlmostEqual(suggestion.suggested_pct, 0.0375, places=4)


class TestSectorCapDefault(unittest.TestCase):
    def _opportunity(self, **overrides):
        payload = {"symbol": "AAPL", "score": 82.0, "confidence": 0.80}
        payload.update(overrides)
        return payload

    def test_default_sector_cap_is_20_pct(self):
        self.assertAlmostEqual(DEFAULT_CONFIG["sector_cap"], 0.20, places=4)

    def test_no_sector_exposure_allows_full_base_size(self):
        # sector_cap=0.20, no existing exposure → headroom=0.20 > base 5% → no cap applied
        suggestion = suggest_allocation(
            opportunity=self._opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            current_sector_exposure=0.0,
        )
        self.assertAlmostEqual(suggestion.suggested_pct, 0.05, places=4)
        self.assertNotIn("sector_cap", suggestion.capped_by)

    def test_sector_nearly_full_limits_allocation(self):
        # 18% already in sector, default cap 20% → only 2% headroom
        suggestion = suggest_allocation(
            opportunity=self._opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            current_sector_exposure=0.18,
        )
        self.assertAlmostEqual(suggestion.suggested_pct, 0.02, places=4)
        self.assertIn("sector_cap", suggestion.capped_by)

    def test_sector_at_cap_zeroes_allocation(self):
        # 20% already in sector, cap is 20% → 0% headroom → no allocation
        suggestion = suggest_allocation(
            opportunity=self._opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            current_sector_exposure=0.20,
        )
        self.assertAlmostEqual(suggestion.suggested_pct, 0.0, places=4)
        self.assertAlmostEqual(suggestion.suggested_amount, 0.0, places=2)

    def test_sector_cap_can_be_disabled_with_none(self):
        # Passing sector_cap=None explicitly overrides the default and removes the cap
        suggestion = suggest_allocation(
            opportunity=self._opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            current_sector_exposure=0.18,
            config={"sector_cap": None},
        )
        self.assertAlmostEqual(suggestion.suggested_pct, 0.05, places=4)
        self.assertNotIn("sector_cap", suggestion.capped_by)


class TestMinPositionPct(unittest.TestCase):
    def _opportunity(self, **overrides):
        payload = {"symbol": "AAPL", "score": 50.0, "confidence": 0.50}
        payload.update(overrides)
        return payload

    def test_default_min_position_pct_is_1_pct(self):
        self.assertAlmostEqual(DEFAULT_CONFIG["min_position_pct"], 0.01, places=4)

    def test_normal_size_above_floor_unaffected(self):
        # 3% momentum at medium confidence → 2.25%, well above 1% floor
        suggestion = suggest_allocation(
            opportunity=self._opportunity(confidence=0.64),
            strategy_type="momentum",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
        )
        self.assertGreater(suggestion.suggested_pct, 0.01)

    def test_extreme_penalties_zero_out_tiny_position(self):
        # degraded + risk_off + low confidence:
        # momentum base 3% × 0.50 (low-conf) × 0.55 (risk_off) × 0.65 (degraded) ≈ 0.54%
        # 0.54% < min_position_pct (1%) → zeroed out
        suggestion = suggest_allocation(
            opportunity=self._opportunity(confidence=0.40),
            strategy_type="momentum",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            context={"degraded_mode": True, "regime_label": "risk_off"},
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
        # Set floor to 3% — a 2.25% medium-confidence momentum should be zeroed
        suggestion = suggest_allocation(
            opportunity=self._opportunity(confidence=0.64),
            strategy_type="momentum",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            config={"min_position_pct": 0.03},
        )
        # 3% × 0.75 = 2.25%, which is below the 3% floor → zeroed
        self.assertAlmostEqual(suggestion.suggested_pct, 0.0, places=4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
