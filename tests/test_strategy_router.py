import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy_router import route_opportunity


class TestStrategyRouter(unittest.TestCase):
    def _opportunity(self, **overrides):
        payload = {
            "symbol": "AAPL",
            "label": "watchlist",
            "events": [],
            "reasons": [],
            "factor_breakdown": {
                "momentum": 55.0,
                "relative_strength": 60.0,
                "volume_confirmation": 50.0,
                "volatility_sanity": 70.0,
            },
            "theme_support": 0.0,
            "pct_from_200dma": 2.0,
        }
        payload.update(overrides)
        return payload

    def test_routes_breakout_strength_to_compounder(self):
        route = route_opportunity(
            self._opportunity(
                events=["BREAKOUT_PROXY"],
                factor_breakdown={
                    "momentum": 68.0,
                    "relative_strength": 84.0,
                    "volume_confirmation": 60.0,
                    "volatility_sanity": 72.0,
                },
                theme_support=0.68,
                pct_from_200dma=7.0,
            )
        )

        self.assertEqual(route.strategy_type, "compounder")
        self.assertIn("relative strength is strong", " ".join(route.rationale))

    def test_routes_fast_move_and_volume_to_momentum(self):
        route = route_opportunity(
            self._opportunity(
                label="momentum",
                events=["STRONG_MOVE_UP", "VOLUME_SPIKE"],
                factor_breakdown={
                    "momentum": 88.0,
                    "relative_strength": 62.0,
                    "volume_confirmation": 90.0,
                    "volatility_sanity": 35.0,
                },
            )
        )

        self.assertEqual(route.strategy_type, "momentum")
        self.assertGreater(route.signals["momentum_votes"], route.signals["compounder_votes"])

    def test_routes_durable_trend_without_explicit_label(self):
        route = route_opportunity(
            self._opportunity(
                factor_breakdown={
                    "momentum": 64.0,
                    "relative_strength": 78.0,
                    "volume_confirmation": 58.0,
                    "volatility_sanity": 78.0,
                },
                theme_support=0.60,
                pct_from_200dma=6.5,
            )
        )

        self.assertEqual(route.strategy_type, "compounder")
        self.assertTrue(route.rationale)

    def test_missing_volatility_metric_does_not_add_false_tactical_rationale(self):
        route = route_opportunity(
            self._opportunity(
                label="momentum",
                events=["STRONG_MOVE_UP"],
                factor_breakdown={
                    "momentum": 82.0,
                    "relative_strength": 60.0,
                    "volume_confirmation": 70.0,
                    "volatility_sanity": None,
                },
            )
        )

        self.assertEqual(route.strategy_type, "momentum")
        self.assertNotIn("setup is volatile", " ".join(route.rationale))

    def test_individual_signals_can_override_compounder_label(self):
        # label=compounder contributes only 1 vote now; three momentum signals
        # (STRONG_MOVE_UP + VOLUME_SPIKE + momentum score) must be able to override it.
        route = route_opportunity(
            self._opportunity(
                label="compounder",
                events=["STRONG_MOVE_UP", "VOLUME_SPIKE"],
                factor_breakdown={
                    "momentum": 88.0,
                    "relative_strength": 55.0,
                    "volume_confirmation": 85.0,
                    "volatility_sanity": 70.0,
                },
                theme_support=0.0,
                pct_from_200dma=1.0,
            )
        )

        self.assertEqual(route.strategy_type, "momentum")
        self.assertGreater(route.signals["momentum_votes"], route.signals["compounder_votes"])

    def test_label_still_tips_tie_toward_compounder(self):
        # label=compounder (+1) and exactly one compounder signal (RS >= 75):
        # total 2 compounder vs 0 momentum → compounder wins.
        route = route_opportunity(
            self._opportunity(
                label="compounder",
                events=[],
                factor_breakdown={
                    "momentum": 50.0,
                    "relative_strength": 78.0,
                    "volume_confirmation": 40.0,
                    "volatility_sanity": 80.0,
                },
                theme_support=0.0,
                pct_from_200dma=2.0,
            )
        )

        self.assertEqual(route.strategy_type, "compounder")


if __name__ == "__main__":
    unittest.main(verbosity=2)
