import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from portfolio_decision_engine import generate_portfolio_actions


class TestPortfolioDecisionEngine(unittest.TestCase):
    def _holding(self, symbol: str = "AAPL", **overrides):
        payload = {
            "symbol": symbol,
            "shares": 10,
            "current_price": 100.0,
            "market_value": 1_000.0,
            "sector": "Technology",
            "strategy_type": "compounder",
            "pct_from_50dma": 1.0,
            "pct_from_200dma": 5.0,
            "theme_support": 0.70,
            "signal_score": 0.75,
            "confidence_score": 0.78,
            "unrealized_return": 0.05,
        }
        payload.update(overrides)
        return payload

    def _opportunity(self, symbol: str = "NVDA", **overrides):
        payload = {
            "symbol": symbol,
            "score": 84.0,
            "confidence": 0.80,
            "label": "compounder",
            "events": ["BREAKOUT_PROXY"],
            "reasons": ["RS: near 52wk high (+0.0% vs high)", "theme support reinforced"],
            "factor_breakdown": {
                "momentum": 72.0,
                "relative_strength": 86.0,
                "volume_confirmation": 68.0,
                "volatility_sanity": 72.0,
            },
            "theme_support": 0.65,
            "sector": "Technology",
        }
        payload.update(overrides)
        return payload

    def test_empty_opportunity_set(self):
        result = generate_portfolio_actions(
            current_holdings=[self._holding()],
            opportunities=[],
            portfolio_value=100_000.0,
            cash_available=20_000.0,
        )

        self.assertFalse(result["available"])
        self.assertEqual(result["actions"], [])

    def test_no_available_capital_adds_to_watchlist(self):
        result = generate_portfolio_actions(
            current_holdings=[],
            opportunities=[self._opportunity()],
            portfolio_value=100_000.0,
            cash_available=2_000.0,
        )

        self.assertEqual(result["actions"][0]["action"], "ADD_TO_WATCHLIST")

    def test_degraded_data_requires_more_confidence(self):
        result = generate_portfolio_actions(
            current_holdings=[],
            opportunities=[self._opportunity(confidence=0.68, score=86.0)],
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            context={"degraded_mode": True, "regime_label": "neutral"},
        )

        self.assertEqual(result["actions"][0]["action"], "ADD_TO_WATCHLIST")

    def test_conflicting_signals_stay_watchlist(self):
        result = generate_portfolio_actions(
            current_holdings=[],
            opportunities=[self._opportunity(confidence=0.58, score=88.0, label="momentum", events=["STRONG_MOVE_UP"])],
            portfolio_value=100_000.0,
            cash_available=20_000.0,
        )

        self.assertEqual(result["actions"][0]["action"], "ADD_TO_WATCHLIST")

    def test_stronger_replacement_opportunity_can_drive_rotation(self):
        weak_holding = self._holding(
            symbol="OLD",
            pct_from_200dma=-7.0,
            theme_support=0.20,
            signal_score=0.35,
            confidence_score=0.45,
        )
        result = generate_portfolio_actions(
            current_holdings=[weak_holding],
            opportunities=[self._opportunity(symbol="NEW", score=90.0, confidence=0.84)],
            portfolio_value=100_000.0,
            cash_available=20_000.0,
        )

        actions = {(row["action"], row["symbol"]) for row in result["actions"]}
        self.assertIn(("SELL", "OLD"), actions)
        self.assertIn(("PROMOTE_TO_PORTFOLIO", "NEW"), actions)

    def test_existing_holding_can_remain_hold_when_no_better_replacement_exists(self):
        stable = self._holding(symbol="AAPL")
        result = generate_portfolio_actions(
            current_holdings=[stable],
            opportunities=[self._opportunity(symbol="AAPL", score=72.0, confidence=0.62)],
            portfolio_value=100_000.0,
            cash_available=3_000.0,
        )

        actions = {(row["action"], row["symbol"]) for row in result["actions"]}
        self.assertIn(("HOLD", "AAPL"), actions)

    def test_trim_exit_is_not_upgraded_to_sell(self):
        momentum_holding = self._holding(
            symbol="FAST",
            strategy_type="momentum",
            pct_from_50dma=0.5,
            theme_support=0.55,
            signal_score=0.45,
            confidence_score=0.55,
            unrealized_return=0.18,
        )
        result = generate_portfolio_actions(
            current_holdings=[momentum_holding],
            opportunities=[self._opportunity(symbol="FAST", label="momentum", events=["STRONG_MOVE_UP"])],
            portfolio_value=100_000.0,
            cash_available=20_000.0,
        )

        actions = {(row["action"], row["symbol"]) for row in result["actions"]}
        self.assertIn(("TRIM", "FAST"), actions)

    def test_zero_sized_allocation_falls_back_to_watchlist(self):
        result = generate_portfolio_actions(
            current_holdings=[],
            opportunities=[self._opportunity(symbol="NVDA", sector="Technology")],
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            config={"allocation_engine": {"sector_cap": 0.0}},
        )

        self.assertEqual(result["actions"][0]["action"], "ADD_TO_WATCHLIST")

    def test_score_inferred_confidence_cannot_trigger_promote(self):
        # Opportunity has no explicit confidence field.
        # score=85 would previously infer confidence=0.85 → PROMOTE_TO_PORTFOLIO.
        # Now inferred confidence is capped at 0.60, which is below the 0.65 promote bar.
        opp = {
            "symbol": "INFER",
            "score": 85.0,
            "label": "compounder",
            "events": ["BREAKOUT_PROXY"],
            "reasons": ["RS: near 52wk high (+0.0%)"],
            "factor_breakdown": {
                "momentum": 70.0,
                "relative_strength": 86.0,
                "volume_confirmation": 65.0,
                "volatility_sanity": 80.0,
            },
            "sector": "Technology",
        }
        result = generate_portfolio_actions(
            current_holdings=[],
            opportunities=[opp],
            portfolio_value=100_000.0,
            cash_available=20_000.0,
        )
        action_names = {row["action"] for row in result["actions"]}
        self.assertNotIn("PROMOTE_TO_PORTFOLIO", action_names)
        # Confidence (0.60) still meets the BUY bar (min_buy_confidence - 0.05 = 0.60)
        self.assertIn("BUY", action_names)

    def test_explicit_confidence_above_65_can_still_promote(self):
        result = generate_portfolio_actions(
            current_holdings=[],
            opportunities=[self._opportunity(score=80.0, confidence=0.80)],
            portfolio_value=100_000.0,
            cash_available=20_000.0,
        )
        action_names = {row["action"] for row in result["actions"]}
        self.assertIn("PROMOTE_TO_PORTFOLIO", action_names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
