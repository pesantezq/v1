"""
Tests for the explicit theme_support field throughout the pipeline.

Covers:
  - compute_theme_support() computation logic
  - detect_events() injecting theme_support into ScanResults
  - scan() accepting theme_signals override
  - theme_support propagation: ScanResult → RankedOpportunity → PromotedCandidate
  - strategy_router using numeric threshold (no string parsing)
  - exit_engine using numeric threshold (no string parsing)
  - Backward compatibility: missing theme_support → None → safe neutral behavior
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from universal_scanner import ScanResult, UniversalScanner
from event_detection import detect_events, compute_theme_support, EventType
from opportunity_ranker import rank_opportunities, RankedOpportunity, FactorBreakdown
from promotion_engine import promote_candidates
from strategy_router import route_opportunity
from exit_engine import evaluate_exit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sr(symbol="AAPL", **overrides):
    defaults = dict(
        price=100.0,
        pct_change_1d=2.0,
        rel_volume=1.5,
        day_range_pct=2.0,
        pct_from_year_high=-5.0,
        pct_from_200dma=4.0,
    )
    defaults.update(overrides)
    return ScanResult(symbol=symbol, **defaults)


def _ranked_opp(symbol="AAPL", theme_support=None, **overrides):
    fb = FactorBreakdown(
        momentum=60.0,
        relative_strength=80.0,
        volume_confirmation=60.0,
        volatility_sanity=80.0,
    )
    return RankedOpportunity(
        symbol=symbol,
        total_score=overrides.pop("total_score", 70.0),
        factor_breakdown=fb,
        reasons=overrides.pop("reasons", []),
        events=overrides.pop("events", []),
        theme_support=theme_support,
    )


def _quote(symbol, **kw):
    q = dict(
        price=100.0, changesPercentage=2.0, volume=2_000_000, avgVolume=1_000_000,
        marketCap=10e9, priceAvg200=95.0, priceAvg50=98.0,
        dayHigh=102.0, dayLow=98.0, yearHigh=110.0, yearLow=75.0,
        timestamp="1713139200",
    )
    q.update(kw)
    return q


# ===========================================================================
# compute_theme_support
# ===========================================================================

class TestComputeThemeSupport(unittest.TestCase):

    def _make_results(self, n=10):
        return [
            ScanResult(symbol=f"S{i}", price=100.0 + i)
            for i in range(n)
        ]

    def test_no_events_gives_zero_scores(self):
        results = self._make_results(10)
        scores = compute_theme_support(results, [])
        for v in scores.values():
            self.assertAlmostEqual(v, 0.0, places=3)

    def test_single_positive_event_out_of_ten_stays_low(self):
        results = self._make_results(10)
        from event_detection import MarketEvent
        events = [MarketEvent(symbol="S0", event_type=EventType.STRONG_MOVE_UP, strength=0.8)]
        scores = compute_theme_support(results, events)
        # 1/10 = 10% positive; breadth_threshold=15% → broad_score = 10/15 ≈ 0.67
        # S0 also gets +0.10 STRONG_MOVE_UP bonus
        self.assertIn("S0", scores)
        self.assertGreater(scores["S0"], scores["S1"])

    def test_high_breadth_saturates_broad_score(self):
        results = self._make_results(10)
        from event_detection import MarketEvent
        events = [
            MarketEvent(symbol=f"S{i}", event_type=EventType.STRONG_MOVE_UP, strength=0.8)
            for i in range(8)  # 80% of symbols moving up
        ]
        scores = compute_theme_support(results, events)
        # broad_score should be 1.0; S0–S7 get +0.10 bonus → capped at 1.0
        for i in range(8):
            self.assertAlmostEqual(scores[f"S{i}"], 1.0, places=3)

    def test_breakout_proxy_gets_larger_bonus_than_strong_move(self):
        # Use 20 symbols so broad_score stays sub-1 and the per-symbol bonuses
        # are the differentiating factor.
        filler = [ScanResult(symbol=f"F{i}", price=100.0) for i in range(18)]
        results = [ScanResult(symbol="BP", price=100.0), ScanResult(symbol="SM", price=100.0)] + filler
        from event_detection import MarketEvent
        events = [
            MarketEvent(symbol="BP", event_type=EventType.BREAKOUT_PROXY, strength=0.9),
            MarketEvent(symbol="SM", event_type=EventType.STRONG_MOVE_UP, strength=0.8),
        ]
        scores = compute_theme_support(results, events)
        self.assertGreater(scores["BP"], scores["SM"])

    def test_all_three_events_bonus_stacks(self):
        results = [ScanResult(symbol="TRIPLE", price=100.0)]
        from event_detection import MarketEvent
        events = [
            MarketEvent(symbol="TRIPLE", event_type=EventType.BREAKOUT_PROXY, strength=1.0),
            MarketEvent(symbol="TRIPLE", event_type=EventType.STRONG_MOVE_UP, strength=1.0),
            MarketEvent(symbol="TRIPLE", event_type=EventType.VOLUME_SPIKE, strength=1.0),
        ]
        scores = compute_theme_support(results, events)
        # broad_score = min(1.0, 1/(1*0.15)) = 1.0; bonus = 0.35 → capped at 1.0
        self.assertAlmostEqual(scores["TRIPLE"], 1.0, places=3)

    def test_empty_scan_results_returns_empty_dict(self):
        self.assertEqual(compute_theme_support([], []), {})

    def test_symbols_without_price_excluded(self):
        results = [ScanResult(symbol="NOPRICE")]   # has_price = False
        scores = compute_theme_support(results, [])
        self.assertNotIn("NOPRICE", scores)

    def test_scores_clamped_between_0_and_1(self):
        results = self._make_results(5)
        from event_detection import MarketEvent
        events = [
            MarketEvent(symbol=f"S{i}", event_type=EventType.BREAKOUT_PROXY, strength=1.0)
            for i in range(5)
        ]
        scores = compute_theme_support(results, events)
        for v in scores.values():
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 1.0)

    def test_configurable_breadth_threshold(self):
        results = self._make_results(10)
        from event_detection import MarketEvent
        events = [
            MarketEvent(symbol="S0", event_type=EventType.STRONG_MOVE_UP, strength=0.8)
        ]
        # With breadth_threshold=0.05 (5%), 1/10=10% → broad_score = 10/5 = 2.0 → capped 1.0
        scores_loose = compute_theme_support(results, events, {"theme_breadth_threshold": 0.05})
        # With breadth_threshold=0.5 (50%), 1/10=10% → broad_score = 10/50 = 0.2
        scores_tight = compute_theme_support(results, events, {"theme_breadth_threshold": 0.50})
        self.assertGreater(scores_loose["S0"], scores_tight["S0"])


# ===========================================================================
# detect_events injects theme_support into ScanResults
# ===========================================================================

class TestDetectEventsInjectsThemeSupport(unittest.TestCase):

    def test_theme_support_set_after_detect_events(self):
        sr = _sr("AAPL", pct_change_1d=5.0, rel_volume=3.0, pct_from_year_high=-0.5)
        self.assertIsNone(sr.theme_support)  # None before detection
        detect_events([sr])
        self.assertIsNotNone(sr.theme_support)
        self.assertGreaterEqual(sr.theme_support, 0.0)
        self.assertLessEqual(sr.theme_support, 1.0)

    def test_external_theme_support_not_overwritten(self):
        sr = _sr("AAPL", pct_change_1d=5.0)
        sr.theme_support = 0.42  # set externally
        detect_events([sr])
        self.assertAlmostEqual(sr.theme_support, 0.42, places=3)

    def test_flat_stock_gets_low_theme_support(self):
        # Minimal move, low volume → no positive events → low theme_support
        sr = _sr("FLAT", pct_change_1d=0.2, rel_volume=0.8, pct_from_year_high=-15.0)
        detect_events([sr])
        self.assertIsNotNone(sr.theme_support)
        self.assertLessEqual(sr.theme_support, 0.3)

    def test_strong_mover_gets_higher_theme_than_flat(self):
        # Add enough filler symbols so the broad score doesn't saturate to 1.0,
        # ensuring the per-symbol bonus is the differentiating factor.
        filler = [_sr(f"F{i}", pct_change_1d=0.1) for i in range(18)]
        strong = _sr("STR", pct_change_1d=6.0, rel_volume=3.0, pct_from_year_high=-0.5)
        flat = _sr("FLAT", pct_change_1d=0.1, rel_volume=0.8, pct_from_year_high=-15.0)
        detect_events([strong, flat] + filler)
        self.assertGreater(strong.theme_support, flat.theme_support)

    def test_no_price_symbol_not_injected(self):
        sr = ScanResult(symbol="NOPRICE")  # has_price = False
        detect_events([sr])
        self.assertIsNone(sr.theme_support)


# ===========================================================================
# scan() theme_signals injection
# ===========================================================================

class TestScannerThemeSignals(unittest.TestCase):

    def test_theme_signals_populated_on_matching_symbol(self):
        scanner = UniversalScanner()
        results = scanner.scan({"AAPL": _quote("AAPL")}, theme_signals={"AAPL": 0.75})
        self.assertAlmostEqual(results[0].theme_support, 0.75, places=3)

    def test_theme_signals_not_present_leaves_none(self):
        scanner = UniversalScanner()
        results = scanner.scan({"AAPL": _quote("AAPL")})
        self.assertIsNone(results[0].theme_support)

    def test_theme_signals_clamps_above_1(self):
        scanner = UniversalScanner()
        results = scanner.scan({"AAPL": _quote("AAPL")}, theme_signals={"AAPL": 1.5})
        self.assertAlmostEqual(results[0].theme_support, 1.0, places=3)

    def test_theme_signals_clamps_below_0(self):
        scanner = UniversalScanner()
        results = scanner.scan({"AAPL": _quote("AAPL")}, theme_signals={"AAPL": -0.3})
        self.assertAlmostEqual(results[0].theme_support, 0.0, places=3)

    def test_theme_signals_no_match_leaves_none(self):
        scanner = UniversalScanner()
        results = scanner.scan({"AAPL": _quote("AAPL")}, theme_signals={"MSFT": 0.6})
        self.assertIsNone(results[0].theme_support)


# ===========================================================================
# theme_support propagation through the pipeline
# ===========================================================================

class TestThemeSupportPropagation(unittest.TestCase):

    def test_theme_support_reaches_ranked_opportunity(self):
        sr = _sr("AAPL")
        sr.theme_support = 0.65
        ranked = rank_opportunities([sr], events=[])
        self.assertAlmostEqual(ranked[0].theme_support, 0.65, places=3)

    def test_none_theme_support_in_scan_result_propagates_as_none(self):
        sr = _sr("AAPL")
        self.assertIsNone(sr.theme_support)
        ranked = rank_opportunities([sr], events=[])
        self.assertIsNone(ranked[0].theme_support)

    def test_theme_support_in_ranked_opp_reaches_promoted_candidate(self):
        opp = _ranked_opp("AAPL", theme_support=0.72)
        promoted = promote_candidates([opp], config={"min_score": 0.0})
        self.assertAlmostEqual(promoted[0].theme_support, 0.72, places=3)

    def test_none_theme_support_propagates_through_promotion(self):
        opp = _ranked_opp("AAPL", theme_support=None)
        promoted = promote_candidates([opp], config={"min_score": 0.0})
        self.assertIsNone(promoted[0].theme_support)

    def test_to_dict_includes_theme_support(self):
        sr = _sr("AAPL")
        sr.theme_support = 0.55
        ranked = rank_opportunities([sr], events=[])
        d = ranked[0].to_dict()
        self.assertIn("theme_support", d)
        self.assertAlmostEqual(d["theme_support"], 0.55, places=3)

    def test_promoted_candidate_to_dict_includes_theme_support(self):
        opp = _ranked_opp("AAPL", theme_support=0.88)
        promoted = promote_candidates([opp], config={"min_score": 0.0})
        d = promoted[0].to_dict()
        self.assertIn("theme_support", d)
        self.assertAlmostEqual(d["theme_support"], 0.88, places=3)


# ===========================================================================
# strategy_router — numeric threshold, no string parsing
# ===========================================================================

class TestStrategyRouterNumericTheme(unittest.TestCase):

    def _opp(self, theme_support, **overrides):
        payload = {
            "symbol": "TEST",
            "label": "watchlist",
            "events": [],
            "reasons": [],
            "factor_breakdown": {
                "momentum": 55.0,
                "relative_strength": 78.0,
                "volume_confirmation": 50.0,
                "volatility_sanity": 80.0,
            },
            "pct_from_200dma": 6.0,
            "theme_support": theme_support,
        }
        payload.update(overrides)
        return payload

    def test_strong_theme_support_votes_compounder(self):
        route = route_opportunity(self._opp(theme_support=0.65))
        # theme_support=0.65 >= 0.55 → +1 compounder vote
        self.assertIn("theme support is durable", " ".join(route.rationale))
        self.assertEqual(route.strategy_type, "compounder")

    def test_weak_theme_support_does_not_vote(self):
        route = route_opportunity(self._opp(theme_support=0.30))
        # theme_support=0.30 < 0.55 → no theme vote
        self.assertNotIn("theme support is durable", " ".join(route.rationale))

    def test_none_theme_support_does_not_crash_or_vote(self):
        route = route_opportunity(self._opp(theme_support=None))
        self.assertNotIn("theme support is durable", " ".join(route.rationale))
        self.assertIn(route.strategy_type, ("compounder", "momentum"))

    def test_exactly_at_threshold_votes(self):
        route = route_opportunity(self._opp(theme_support=0.55))
        self.assertIn("theme support is durable", " ".join(route.rationale))

    def test_strings_in_reasons_no_longer_affect_routing(self):
        # Even with "theme" keyword in reasons, it must NOT generate a vote if
        # the numeric theme_support field is missing/None.
        route = route_opportunity(
            self._opp(
                theme_support=None,
                reasons=["sector theme breakout", "52wk high forming", "theme momentum"],
            )
        )
        self.assertNotIn("theme support is durable", " ".join(route.rationale))


# ===========================================================================
# exit_engine — numeric threshold, no string parsing
# ===========================================================================

class TestExitEngineNumericTheme(unittest.TestCase):

    def _holding(self, theme_support, **overrides):
        payload = {
            "symbol": "AAPL",
            "pct_from_50dma": 2.0,
            "pct_from_200dma": 5.0,
            "signal_score": 0.70,
            "confidence_score": 0.72,
            "unrealized_return": 0.05,
            "theme_support": theme_support,
        }
        payload.update(overrides)
        return payload

    def test_theme_below_floor_triggers_thesis_weakening(self):
        suggestion = evaluate_exit(
            self._holding(theme_support=0.20),
            strategy_type="compounder",
        )
        self.assertIn("thesis_weakening", suggestion.triggers)
        self.assertEqual(suggestion.action, "SELL")

    def test_theme_above_floor_no_thesis_weakening(self):
        suggestion = evaluate_exit(
            self._holding(theme_support=0.65),
            strategy_type="compounder",
        )
        self.assertNotIn("thesis_weakening", suggestion.triggers)

    def test_theme_exactly_at_floor_does_not_trigger(self):
        # Default floor is 0.40; at 0.40 we are NOT below it
        suggestion = evaluate_exit(
            self._holding(theme_support=0.40),
            strategy_type="compounder",
        )
        self.assertNotIn("thesis_weakening", suggestion.triggers)

    def test_none_theme_support_does_not_trigger_weakening(self):
        holding = self._holding(theme_support=None)
        suggestion = evaluate_exit(holding, strategy_type="compounder")
        self.assertNotIn("thesis_weakening", suggestion.triggers)

    def test_strings_in_reasons_no_longer_affect_exit(self):
        # Even if reasons contain "theme" / "durable", exit should NOT fire
        # thesis_weakening unless the numeric field is below the floor.
        holding = {
            "symbol": "AAPL",
            "pct_from_50dma": 2.0,
            "pct_from_200dma": 5.0,
            "signal_score": 0.70,
            "confidence_score": 0.72,
            "unrealized_return": 0.05,
            "reasons": ["theme fading", "sector theme weakening", "durable thesis deteriorating"],
            # no theme_support field
        }
        suggestion = evaluate_exit(holding, strategy_type="compounder")
        self.assertNotIn("thesis_weakening", suggestion.triggers)

    def test_configurable_floor_respected(self):
        suggestion = evaluate_exit(
            self._holding(theme_support=0.45),
            strategy_type="compounder",
            config={"theme_support_floor": 0.50},  # raised floor
        )
        self.assertIn("thesis_weakening", suggestion.triggers)


# ===========================================================================
# Full pipeline integration
# ===========================================================================

class TestFullPipelineWithThemeSupport(unittest.TestCase):

    def test_end_to_end_theme_support_flows_to_promoted_candidate(self):
        scanner = UniversalScanner()
        # Include enough flat symbols so broad_score doesn't saturate, allowing
        # NVDA's per-symbol bonus to differentiate it from FLAT.
        quotes = {
            "NVDA": _quote("NVDA", changesPercentage=6.0, volume=4_000_000, avgVolume=1_000_000,
                           price=99.0, yearHigh=100.0),
            **{f"FLAT{i}": _quote(f"FLAT{i}", changesPercentage=0.1, volume=800_000,
                                   avgVolume=1_000_000)
               for i in range(19)},
        }
        results = scanner.scan(quotes)
        events = detect_events(results)

        sr_map = {sr.symbol: sr for sr in results}
        self.assertIsNotNone(sr_map["NVDA"].theme_support)
        self.assertIsNotNone(sr_map["FLAT0"].theme_support)
        self.assertGreater(sr_map["NVDA"].theme_support, sr_map["FLAT0"].theme_support)

        ranked = rank_opportunities(results, events)
        nvda_ranked = next(r for r in ranked if r.symbol == "NVDA")
        self.assertIsNotNone(nvda_ranked.theme_support)

        promoted = promote_candidates(ranked, config={"min_score": 0.0})
        nvda_promo = next((p for p in promoted if p.symbol == "NVDA"), None)
        if nvda_promo:
            self.assertIsNotNone(nvda_promo.theme_support)

    def test_external_theme_signals_override_computed_value(self):
        scanner = UniversalScanner()
        results = scanner.scan(
            {"AAPL": _quote("AAPL", changesPercentage=0.1)},
            theme_signals={"AAPL": 0.85},
        )
        events = detect_events(results)
        # External value must be preserved after detect_events injection
        self.assertAlmostEqual(results[0].theme_support, 0.85, places=3)

        ranked = rank_opportunities(results, events)
        self.assertAlmostEqual(ranked[0].theme_support, 0.85, places=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
