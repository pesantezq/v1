"""
tests/test_decision_engine.py

Unit tests for portfolio_automation.decision_engine.

Each test is self-contained and uses plain-dict inputs — no external dependencies,
no file I/O, no mocking required.
"""

import unittest

from portfolio_automation.decision_engine import (
    DECISION_AVOID,
    DECISION_BUY,
    DECISION_HOLD,
    DECISION_SCALE,
    DECISION_SELL,
    DECISION_WAIT,
    SOURCE_MARKET,
    SOURCE_STRUCTURAL,
    SOURCE_WATCHLIST,
    URGENCY_CRITICAL,
    URGENCY_HIGH,
    URGENCY_LOW,
    URGENCY_MEDIUM,
    apply_decision_overrides,
    build_decision_plan,
    decision_from_market_opportunity,
    decision_from_structural_violation,
    decision_from_watchlist_signal,
    rank_decisions,
    summarize_decision_plan,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_BASE_CONTEXT = {
    "total_portfolio_value": 50_000,
    "cash": 5_000,
    "current_holdings": {},
    "degraded_mode": False,
    "data_mode": "live",
    "drawdown_regime": "neutral",
    "active_structural_violations": [],
}


def _ctx(**overrides) -> dict:
    """Return a copy of the base context with selected fields overridden."""
    return {**_BASE_CONTEXT, **overrides}


def _concentration_violation(symbol: str = "QQQ") -> dict:
    return {
        "symbol": symbol,
        "violation_type": "concentration",
        "current_pct": 0.45,
        "cap_pct": 0.40,
        "required_action": "trim",
    }


def _leverage_violation(symbol: str = "TQQQ") -> dict:
    return {
        "symbol": symbol,
        "violation_type": "leverage",
        "current_pct": 0.18,
        "cap_pct": 0.15,
        "required_action": "trim",
    }


def _high_conviction_signal(ticker: str = "NVDA") -> dict:
    return {
        "ticker": ticker,
        "conviction_band": "high_conviction",
        "conviction_score": 0.88,
        "signal_score": 0.82,
        "confidence_score": 0.91,
        "effective_score": 0.85,
        "sizing_multiplier": 1.0,
        "suggested_allocation": 0.04,
        "suggested_amount": 2_000,
        "data_mode": "live",
    }


def _low_confidence_signal(ticker: str = "XYZ") -> dict:
    return {
        "ticker": ticker,
        "conviction_band": "normal",
        "conviction_score": 0.55,
        "signal_score": 0.70,
        "confidence_score": 0.45,   # below floor
        "effective_score": 0.60,
        "data_mode": "live",
    }


def _weak_conviction_signal(ticker: str = "JUNK") -> dict:
    return {
        "ticker": ticker,
        "conviction_band": "observe",
        "conviction_score": 0.22,
        "signal_score": 0.60,
        "confidence_score": 0.70,
        "effective_score": 0.40,
        "data_mode": "live",
    }


def _underweight_opportunity(symbol: str = "VFH") -> dict:
    return {
        "symbol": symbol,
        "opportunity_type": "underweight_target",
        "suggested_pct": 0.03,
        "suggested_amount": 1_500,
        "reason": "Financial sector underweight vs target.",
    }


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestStructuralViolationOutranksWatchlist(unittest.TestCase):
    """
    Case 1: Structural QQQ concentration breach must outrank a high-conviction
    watchlist BUY in the ranked plan.
    """

    def test_structural_outranks_watchlist_buy(self):
        plan = build_decision_plan(
            structural_violations=[_concentration_violation("QQQ")],
            watchlist_signals=[_high_conviction_signal("NVDA")],
            portfolio_context=_ctx(),
        )

        self.assertGreater(len(plan), 1)
        first = plan[0]
        self.assertEqual(first["source"], SOURCE_STRUCTURAL)
        self.assertEqual(first["symbol"], "QQQ")
        self.assertEqual(first["decision"], DECISION_SELL)

        watchlist_priorities = [
            d["priority"] for d in plan if d["source"] == SOURCE_WATCHLIST
        ]
        self.assertTrue(all(first["priority"] > p for p in watchlist_priorities))


class TestLeverageViolationIsCriticalSell(unittest.TestCase):
    """
    Case 2: A leveraged exposure breach must produce a SELL decision with
    CRITICAL urgency and a priority score above all non-structural sources.
    """

    def test_leverage_produces_critical_sell(self):
        plan = build_decision_plan(
            structural_violations=[_leverage_violation("TQQQ")],
            portfolio_context=_ctx(),
        )

        self.assertEqual(len(plan), 1)
        d = plan[0]
        self.assertEqual(d["decision"], DECISION_SELL)
        self.assertEqual(d["urgency"], URGENCY_CRITICAL)
        self.assertEqual(d["source"], SOURCE_STRUCTURAL)
        self.assertGreater(d["priority"], 0.90)

    def test_leverage_has_higher_priority_than_concentration(self):
        plan = build_decision_plan(
            structural_violations=[
                _leverage_violation("TQQQ"),
                _concentration_violation("QQQ"),
            ],
            portfolio_context=_ctx(),
        )

        symbols = [d["symbol"] for d in plan]
        self.assertEqual(symbols[0], "TQQQ")


class TestUnderweightContributionBecomesBuy(unittest.TestCase):
    """
    Case 3: An underweight portfolio target funded by contribution must become
    a BUY decision from the market source.
    """

    def test_underweight_target_is_buy(self):
        plan = build_decision_plan(
            market_opportunities=[_underweight_opportunity("VFH")],
            portfolio_context=_ctx(),
        )

        self.assertEqual(len(plan), 1)
        d = plan[0]
        self.assertEqual(d["symbol"], "VFH")
        self.assertEqual(d["decision"], DECISION_BUY)
        self.assertEqual(d["source"], SOURCE_MARKET)

    def test_underweight_recommended_amount_populated(self):
        plan = build_decision_plan(
            market_opportunities=[_underweight_opportunity("VFH")],
            portfolio_context=_ctx(),
        )
        d = plan[0]
        self.assertIsNotNone(d["recommended_amount"])
        self.assertGreater(d["recommended_amount"], 0)


class TestLowConfidenceBecomesWait(unittest.TestCase):
    """
    Case 4: A scanner alert with confidence below the floor must be capped at
    WAIT (new position) or HOLD (existing position), never BUY/SCALE.
    """

    def test_low_confidence_new_position_is_wait(self):
        plan = build_decision_plan(
            watchlist_signals=[_low_confidence_signal("XYZ")],
            portfolio_context=_ctx(),
        )

        d = plan[0]
        self.assertEqual(d["decision"], DECISION_WAIT)
        self.assertIn("low_confidence", d["risk_flags"])

    def test_low_confidence_existing_position_is_hold(self):
        ctx = _ctx(current_holdings={"XYZ": {"value": 2_000, "pct": 0.04}})
        plan = build_decision_plan(
            watchlist_signals=[_low_confidence_signal("XYZ")],
            portfolio_context=ctx,
        )

        d = plan[0]
        self.assertEqual(d["decision"], DECISION_HOLD)
        self.assertIn("low_confidence", d["risk_flags"])


class TestDegradedDataDowngradesBuyToWait(unittest.TestCase):
    """
    Case 5: A high-conviction watchlist signal must be downgraded from BUY to
    WAIT when the portfolio context signals degraded data mode.
    """

    def test_degraded_mode_flag_downgrades_buy(self):
        ctx = _ctx(degraded_mode=True)
        plan = build_decision_plan(
            watchlist_signals=[_high_conviction_signal("ABC")],
            portfolio_context=ctx,
        )

        d = plan[0]
        self.assertEqual(d["decision"], DECISION_WAIT)
        self.assertIn("degraded_data", d["risk_flags"])

    def test_fallback_data_mode_also_downgrades(self):
        ctx = _ctx(data_mode="fallback")
        plan = build_decision_plan(
            watchlist_signals=[_high_conviction_signal("DEF")],
            portfolio_context=ctx,
        )

        d = plan[0]
        self.assertEqual(d["decision"], DECISION_WAIT)
        self.assertIn("degraded_data", d["risk_flags"])

    def test_degraded_does_not_downgrade_structural_sell(self):
        ctx = _ctx(degraded_mode=True)
        plan = build_decision_plan(
            structural_violations=[_leverage_violation("TQQQ")],
            portfolio_context=ctx,
        )

        d = plan[0]
        # SELL is authoritative — degraded mode must not touch it.
        self.assertEqual(d["decision"], DECISION_SELL)


class TestExistingHoldingWithStrongConvictionIsScale(unittest.TestCase):
    """
    Case 6: A high-conviction watchlist signal for a symbol already held must
    produce SCALE, not BUY.
    """

    def test_existing_holding_high_conviction_is_scale(self):
        ctx = _ctx(current_holdings={"MSFT": {"value": 4_000, "pct": 0.08}})
        plan = build_decision_plan(
            watchlist_signals=[_high_conviction_signal("MSFT")],
            portfolio_context=ctx,
        )

        d = plan[0]
        self.assertEqual(d["symbol"], "MSFT")
        self.assertEqual(d["decision"], DECISION_SCALE)
        self.assertEqual(d["source"], SOURCE_WATCHLIST)

    def test_new_position_high_conviction_is_buy(self):
        plan = build_decision_plan(
            watchlist_signals=[_high_conviction_signal("TSLA")],
            portfolio_context=_ctx(),
        )

        d = plan[0]
        self.assertEqual(d["decision"], DECISION_BUY)


class TestWeakConvictionNewOpportunityIsAvoid(unittest.TestCase):
    """
    Case 7: A watchlist signal with a sub-starter conviction band (observe/defer)
    must produce AVOID for a new position.
    """

    def test_observe_band_is_avoid(self):
        plan = build_decision_plan(
            watchlist_signals=[_weak_conviction_signal("JUNK")],
            portfolio_context=_ctx(),
        )

        d = plan[0]
        self.assertEqual(d["decision"], DECISION_AVOID)
        self.assertIn("weak_conviction", d["risk_flags"])

    def test_defer_band_is_also_avoid(self):
        signal = {**_weak_conviction_signal("TINY"), "conviction_band": "defer"}
        plan = build_decision_plan(
            watchlist_signals=[signal],
            portfolio_context=_ctx(),
        )

        d = plan[0]
        self.assertEqual(d["decision"], DECISION_AVOID)


class TestFinalDecisionsSortByPriorityDescending(unittest.TestCase):
    """
    Case 8: rank_decisions must return records sorted by priority descending,
    with AVOID decisions always trailing regardless of score.
    """

    def test_priority_is_descending(self):
        plan = build_decision_plan(
            structural_violations=[_leverage_violation("TQQQ")],      # ~0.95
            market_opportunities=[_underweight_opportunity("VTI")],   # ~0.62
            watchlist_signals=[_weak_conviction_signal("JUNK")],      # low, AVOID
            portfolio_context=_ctx(),
        )

        priorities = [d["priority"] for d in plan if d["decision"] != DECISION_AVOID]
        self.assertEqual(priorities, sorted(priorities, reverse=True))

    def test_avoid_decisions_trail_actionable_ones(self):
        plan = build_decision_plan(
            structural_violations=[_leverage_violation("TQQQ")],
            watchlist_signals=[_weak_conviction_signal("JUNK")],
            portfolio_context=_ctx(),
        )

        decisions = [d["decision"] for d in plan]
        avoid_indices = [i for i, d in enumerate(decisions) if d == DECISION_AVOID]
        non_avoid_indices = [i for i, d in enumerate(decisions) if d != DECISION_AVOID]

        if avoid_indices and non_avoid_indices:
            self.assertGreater(min(avoid_indices), max(non_avoid_indices))

    def test_rank_decisions_stable_with_equal_priorities(self):
        d1 = decision_from_structural_violation(_concentration_violation("A"))
        d2 = decision_from_structural_violation(_concentration_violation("B"))
        d1["priority"] = 0.88
        d2["priority"] = 0.88
        ranked = rank_decisions([d1, d2])
        self.assertEqual(len(ranked), 2)


class TestMissingOptionalFieldsDoNotCrash(unittest.TestCase):
    """
    Case 9: All converter functions must handle dicts with only a subset of
    fields and never raise an exception on missing optional data.
    """

    def test_structural_violation_minimal_fields(self):
        plan = build_decision_plan(
            structural_violations=[{"symbol": "QQQ"}],
            portfolio_context={},
        )
        self.assertIsInstance(plan, list)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["decision"], DECISION_SELL)

    def test_watchlist_signal_minimal_fields(self):
        plan = build_decision_plan(
            watchlist_signals=[{"ticker": "XYZ"}],
            portfolio_context={},
        )
        self.assertIsInstance(plan, list)
        self.assertEqual(len(plan), 1)

    def test_market_opportunity_minimal_fields(self):
        plan = build_decision_plan(
            market_opportunities=[{"symbol": "VTI"}],
            portfolio_context={},
        )
        self.assertIsInstance(plan, list)
        self.assertEqual(len(plan), 1)

    def test_empty_portfolio_context(self):
        plan = build_decision_plan(
            structural_violations=[_leverage_violation()],
            watchlist_signals=[_high_conviction_signal()],
            portfolio_context=None,
        )
        self.assertIsInstance(plan, list)

    def test_all_sources_empty(self):
        plan = build_decision_plan(portfolio_context=_ctx())
        self.assertEqual(plan, [])

    def test_missing_conviction_fields_in_watchlist(self):
        signal = {"ticker": "ABC", "signal_score": 0.70}
        d = decision_from_watchlist_signal(signal)
        self.assertIn(d["decision"], (
            DECISION_BUY, DECISION_SELL, DECISION_SCALE,
            DECISION_HOLD, DECISION_WAIT, DECISION_AVOID,
        ))

    def test_None_values_in_signal_do_not_crash(self):
        signal = {
            "ticker": "NIL",
            "conviction_score": None,
            "signal_score": None,
            "confidence_score": None,
            "conviction_band": None,
        }
        d = decision_from_watchlist_signal(signal)
        self.assertIsInstance(d["priority"], float)
        self.assertIsInstance(d["risk_flags"], list)


class TestSummarizeDecisionPlan(unittest.TestCase):
    """
    Case 10: summarize_decision_plan must return a non-empty, human-readable
    string that surfaces key symbols, decision types, and context.
    """

    def test_summary_is_non_empty_string(self):
        plan = build_decision_plan(
            structural_violations=[_concentration_violation("QQQ")],
            portfolio_context=_ctx(),
        )
        summary = summarize_decision_plan(plan, _ctx())
        self.assertIsInstance(summary, str)
        self.assertGreater(len(summary), 50)

    def test_summary_contains_symbol(self):
        plan = build_decision_plan(
            structural_violations=[_concentration_violation("QQQ")],
            portfolio_context=_ctx(),
        )
        summary = summarize_decision_plan(plan, _ctx())
        self.assertIn("QQQ", summary)

    def test_summary_contains_decision_type(self):
        plan = build_decision_plan(
            structural_violations=[_leverage_violation("TQQQ")],
            portfolio_context=_ctx(),
        )
        summary = summarize_decision_plan(plan, _ctx())
        self.assertIn("SELL", summary)

    def test_summary_flags_degraded_mode(self):
        ctx = _ctx(degraded_mode=True)
        plan = build_decision_plan(
            watchlist_signals=[_high_conviction_signal("NVDA")],
            portfolio_context=ctx,
        )
        summary = summarize_decision_plan(plan, ctx)
        self.assertIn("DEGRADED", summary)

    def test_summary_with_empty_plan(self):
        summary = summarize_decision_plan([], {})
        self.assertIsInstance(summary, str)
        self.assertIn("No decisions", summary)

    def test_summary_shows_risk_flags(self):
        ctx = _ctx(degraded_mode=True)
        plan = build_decision_plan(
            watchlist_signals=[_high_conviction_signal()],
            portfolio_context=ctx,
        )
        summary = summarize_decision_plan(plan, ctx)
        self.assertIn("degraded_data", summary)

    def test_summary_includes_urgency_breakdown(self):
        plan = build_decision_plan(
            structural_violations=[_leverage_violation()],
            portfolio_context=_ctx(),
        )
        summary = summarize_decision_plan(plan, _ctx())
        self.assertIn("critical", summary)


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------


class TestApplyDecisionOverridesIsolated(unittest.TestCase):
    """Direct tests of the override layer in isolation."""

    def _watchlist_buy(self, symbol: str = "NVDA") -> dict:
        return decision_from_watchlist_signal(_high_conviction_signal(symbol))

    def test_degraded_mode_caps_buy_at_wait(self):
        record = self._watchlist_buy()
        self.assertEqual(record["decision"], DECISION_BUY)

        overridden = apply_decision_overrides(record, {"degraded_mode": True})
        self.assertEqual(overridden["decision"], DECISION_WAIT)
        self.assertIn("degraded_data", overridden["risk_flags"])

    def test_sell_is_not_downgraded_by_degraded_mode(self):
        record = decision_from_structural_violation(_leverage_violation())
        self.assertEqual(record["decision"], DECISION_SELL)

        overridden = apply_decision_overrides(record, {"degraded_mode": True})
        self.assertEqual(overridden["decision"], DECISION_SELL)

    def test_guardrail_conflict_caps_buy_at_hold(self):
        record = self._watchlist_buy("QQQ")
        overridden = apply_decision_overrides(
            record,
            {"active_structural_violations": [{"symbol": "QQQ"}]},
        )
        self.assertEqual(overridden["decision"], DECISION_HOLD)
        self.assertIn("guardrail_conflict", overridden["risk_flags"])

    def test_drawdown_bear_caps_buy_at_hold(self):
        record = self._watchlist_buy()
        overridden = apply_decision_overrides(
            record, {"drawdown_regime": "bear"}
        )
        self.assertEqual(overridden["decision"], DECISION_HOLD)
        self.assertIn("drawdown_regime", overridden["risk_flags"])

    def test_overrides_do_not_mutate_original(self):
        record = self._watchlist_buy()
        original_decision = record["decision"]
        _ = apply_decision_overrides(record, {"degraded_mode": True})
        self.assertEqual(record["decision"], original_decision)


class TestPriorityOrdering(unittest.TestCase):
    """Verify the absolute priority ordering between source types."""

    def test_leverage_beats_concentration(self):
        lev = decision_from_structural_violation(_leverage_violation())
        conc = decision_from_structural_violation(_concentration_violation())
        self.assertGreater(lev["priority"], conc["priority"])

    def test_structural_beats_market(self):
        struct = decision_from_structural_violation(_concentration_violation())
        market = decision_from_market_opportunity(_underweight_opportunity())
        self.assertGreater(struct["priority"], market["priority"])

    def test_market_beats_low_conviction_watchlist(self):
        market = decision_from_market_opportunity(_underweight_opportunity())
        watch = decision_from_watchlist_signal(_weak_conviction_signal())
        self.assertGreater(market["priority"], watch["priority"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
