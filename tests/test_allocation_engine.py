import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from allocation_engine import suggest_allocation


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
