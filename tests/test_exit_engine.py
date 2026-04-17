import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from exit_engine import evaluate_exit


class TestExitEngine(unittest.TestCase):
    def _holding(self, **overrides):
        payload = {
            "symbol": "AAPL",
            "pct_from_50dma": 1.0,
            "pct_from_200dma": 4.0,
            "theme_support": 0.70,
            "signal_score": 0.80,
            "confidence_score": 0.78,
            "unrealized_return": 0.05,
        }
        payload.update(overrides)
        return payload

    def _opportunity(self, **overrides):
        payload = {
            "symbol": "NVDA",
            "score": 88.0,
        }
        payload.update(overrides)
        return payload

    def test_momentum_trend_break_triggers_sell(self):
        suggestion = evaluate_exit(
            self._holding(pct_from_50dma=-3.5, theme_support=0.55),
            strategy_type="momentum",
        )

        self.assertEqual(suggestion.action, "SELL")
        self.assertIn("trend_break", suggestion.triggers)

    def test_compounder_thesis_weakening_triggers_sell(self):
        suggestion = evaluate_exit(
            self._holding(pct_from_200dma=-6.0, theme_support=0.25),
            strategy_type="compounder",
        )

        self.assertEqual(suggestion.action, "SELL")
        self.assertIn("thesis_weakening", suggestion.triggers)

    def test_profit_protection_can_trim_momentum(self):
        suggestion = evaluate_exit(
            self._holding(
                pct_from_50dma=0.5,
                theme_support=0.55,
                signal_score=0.45,
                confidence_score=0.55,
                unrealized_return=0.18,
            ),
            strategy_type="momentum",
        )

        self.assertEqual(suggestion.action, "TRIM")
        self.assertIn("profit_protection", suggestion.triggers)

    def test_stronger_replacement_can_trigger_rotation_sell(self):
        suggestion = evaluate_exit(
            self._holding(signal_score=0.40, confidence_score=0.50, theme_support=0.35),
            strategy_type="compounder",
            stronger_opportunity=self._opportunity(score=92.0),
        )

        self.assertEqual(suggestion.action, "SELL")
        self.assertIn("opportunity_rotation", suggestion.triggers)

    def test_degraded_mode_soft_signals_default_to_hold(self):
        suggestion = evaluate_exit(
            self._holding(theme_support=0.20, pct_from_200dma=2.0),
            strategy_type="compounder",
            context={"degraded_mode": True},
        )

        self.assertEqual(suggestion.action, "HOLD")

    def test_missing_theme_support_does_not_force_sell(self):
        holding = self._holding()
        holding.pop("theme_support")
        suggestion = evaluate_exit(
            holding,
            strategy_type="compounder",
        )

        self.assertEqual(suggestion.action, "HOLD")
        self.assertNotIn("thesis_weakening", suggestion.triggers)

    def test_compounder_hard_break_triggers_sell_despite_strong_theme(self):
        # -9% from 200dma breaches the hard-break floor (-8%) regardless of theme
        suggestion = evaluate_exit(
            self._holding(pct_from_200dma=-9.0, theme_support=0.75),
            strategy_type="compounder",
        )

        self.assertEqual(suggestion.action, "SELL")
        self.assertIn("trend_break", suggestion.triggers)

    def test_momentum_just_below_old_threshold_does_not_false_exit(self):
        # -2.5% from 50dma: breached the old -2.0% bar but NOT the new -3.0% bar
        suggestion = evaluate_exit(
            self._holding(pct_from_50dma=-2.5, theme_support=0.65),
            strategy_type="momentum",
        )

        self.assertEqual(suggestion.action, "HOLD")
        self.assertNotIn("trend_break", suggestion.triggers)

    def test_compounder_soft_break_with_intact_theme_does_not_exit(self):
        # -6% from 200dma (triggers soft-break threshold) but theme_support=0.60 >= 0.55
        # → soft-break condition fails → no trend_break
        suggestion = evaluate_exit(
            self._holding(pct_from_200dma=-6.0, theme_support=0.60),
            strategy_type="compounder",
        )

        self.assertNotIn("trend_break", suggestion.triggers)
        self.assertEqual(suggestion.action, "HOLD")

    def test_percentage_format_return_triggers_profit_protection(self):
        # unrealized_return=18.0 looks like percentage (18 > 2.0 threshold)
        # → divided by 100 → 0.18, which is >= profit_protect_momentum (0.12)
        suggestion = evaluate_exit(
            self._holding(
                pct_from_50dma=0.5,
                theme_support=0.55,
                signal_score=0.45,
                confidence_score=0.55,
                unrealized_return=18.0,
            ),
            strategy_type="momentum",
        )
        self.assertIn("profit_protection", suggestion.triggers)

    def test_return_at_boundary_200pct_treated_as_decimal(self):
        # value=1.8 is <= 2.0 → kept as decimal (180% return) → does NOT divide by 100
        # 1.8 >= profit_protect_momentum (0.12) and strength < 60 → profit_protection fires
        suggestion = evaluate_exit(
            self._holding(
                pct_from_50dma=0.5,
                theme_support=0.55,
                signal_score=0.45,
                confidence_score=0.55,
                unrealized_return=1.8,
            ),
            strategy_type="momentum",
        )
        self.assertIn("profit_protection", suggestion.triggers)


if __name__ == "__main__":
    unittest.main(verbosity=2)
