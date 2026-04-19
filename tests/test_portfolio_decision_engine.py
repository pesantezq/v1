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


class TestBuyStarterMultiplier(unittest.TestCase):
    """BUY actions should produce a starter (70%) position; PROMOTE gets full size."""

    def _opportunity(self, symbol="NVDA", **overrides):
        payload = {
            "symbol": symbol,
            "score": 60.0,
            "confidence": 0.66,
            "label": "compounder",
            "events": ["BREAKOUT_PROXY"],
            "reasons": ["RS: near 52wk high"],
            "factor_breakdown": {
                "momentum": 60.0,
                "relative_strength": 78.0,
                "volume_confirmation": 60.0,
                "volatility_sanity": 80.0,
            },
            "sector": "Technology",
        }
        payload.update(overrides)
        return payload

    def test_buy_gets_starter_allocation_smaller_than_promote(self):
        # BUY scenario: score=60 (below promote threshold 72), confidence=0.66
        buy_result = generate_portfolio_actions(
            current_holdings=[],
            opportunities=[self._opportunity(score=60.0, confidence=0.66)],
            portfolio_value=100_000.0,
            cash_available=20_000.0,
        )
        # PROMOTE scenario: score=80 (above promote threshold 72), same confidence
        promote_result = generate_portfolio_actions(
            current_holdings=[],
            opportunities=[self._opportunity(score=80.0, confidence=0.80)],
            portfolio_value=100_000.0,
            cash_available=20_000.0,
        )

        buy_actions = buy_result["actions"]
        promote_actions = promote_result["actions"]
        self.assertEqual(buy_actions[0]["action"], "BUY")
        self.assertEqual(promote_actions[0]["action"], "PROMOTE_TO_PORTFOLIO")

        buy_amt = buy_actions[0]["suggested_allocation_amount"]
        promote_amt = promote_actions[0]["suggested_allocation_amount"]
        self.assertIsNotNone(buy_amt)
        self.assertIsNotNone(promote_amt)
        self.assertLess(buy_amt, promote_amt)

    def test_buy_amount_is_70_pct_of_full_allocation(self):
        # High-confidence BUY (confidence=0.80 → high multiplier → full base 5%),
        # score=65 → BUY not PROMOTE.  With buy_starter_multiplier=0.70:
        # expected = 100_000 × 0.05 × 1.0 × 0.70 = $3,500
        result = generate_portfolio_actions(
            current_holdings=[],
            opportunities=[self._opportunity(score=65.0, confidence=0.80)],
            portfolio_value=100_000.0,
            cash_available=20_000.0,
        )
        act = result["actions"][0]
        self.assertEqual(act["action"], "BUY")
        self.assertAlmostEqual(act["suggested_allocation_amount"], 3_500.0, delta=1.0)

    def test_promote_gets_full_allocation_not_reduced(self):
        # score=80, confidence=0.80 → PROMOTE, full compounder base 5% = $5,000
        result = generate_portfolio_actions(
            current_holdings=[],
            opportunities=[self._opportunity(score=80.0, confidence=0.80)],
            portfolio_value=100_000.0,
            cash_available=20_000.0,
        )
        act = result["actions"][0]
        self.assertEqual(act["action"], "PROMOTE_TO_PORTFOLIO")
        self.assertAlmostEqual(act["suggested_allocation_amount"], 5_000.0, delta=1.0)

    def test_buy_starter_multiplier_configurable(self):
        # With buy_starter_multiplier=1.0, BUY and PROMOTE get the same allocation
        result = generate_portfolio_actions(
            current_holdings=[],
            opportunities=[self._opportunity(score=65.0, confidence=0.80)],
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            config={"buy_starter_multiplier": 1.0},
        )
        act = result["actions"][0]
        self.assertEqual(act["action"], "BUY")
        # No multiplier applied → full base 5% = $5,000
        self.assertAlmostEqual(act["suggested_allocation_amount"], 5_000.0, delta=1.0)

    def test_existing_holding_add_not_affected_by_starter_multiplier(self):
        # Adding to an existing confirmed holding should use full allocation,
        # not the starter multiplier (existing-holding BUY is a conviction add).
        holding = {
            "symbol": "NVDA",
            "shares": 10,
            "current_price": 100.0,
            "market_value": 1_000.0,
            "sector": "Technology",
            "strategy_type": "compounder",
            "pct_from_200dma": 5.0,
            "theme_support": 0.70,
            "signal_score": 0.80,
            "confidence_score": 0.82,
            "unrealized_return": 0.05,
        }
        result = generate_portfolio_actions(
            current_holdings=[holding],
            opportunities=[self._opportunity(score=80.0, confidence=0.80)],
            portfolio_value=100_000.0,
            cash_available=20_000.0,
        )
        act = result["actions"][0]
        # Existing holding with strong signals → BUY to add
        self.assertEqual(act["action"], "BUY")
        # Should NOT be discounted — existing holding adds use full allocation
        self.assertAlmostEqual(act["suggested_allocation_amount"], 5_000.0, delta=1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
