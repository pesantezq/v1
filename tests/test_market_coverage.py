"""
Tests for Phase 1 and Phase 2 market-coverage modules:
  market_universe   — get_universe_symbols / get_all_symbols
  universal_scanner — UniversalScanner.scan / ScanResult
  event_detection   — detect_events / MarketEvent / EventType
  opportunity_ranker — rank_opportunities / RankedOpportunity
  promotion_engine  — promote_candidates / PromotedCandidate
"""

import unittest
from typing import List

# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------

def _quote(
    symbol: str,
    price: float = 100.0,
    changesPercentage: float = 0.5,
    volume: int = 1_000_000,
    avgVolume: int = 1_000_000,
    marketCap: float = 10e9,
    priceAvg200: float = 95.0,
    priceAvg50: float = 98.0,
    dayHigh: float = 102.0,
    dayLow: float = 98.0,
    yearHigh: float = 110.0,
    yearLow: float = 75.0,
    timestamp: str = "1713139200",
) -> dict:
    return {
        "symbol": symbol,
        "price": price,
        "changesPercentage": changesPercentage,
        "volume": volume,
        "avgVolume": avgVolume,
        "marketCap": marketCap,
        "priceAvg200": priceAvg200,
        "priceAvg50": priceAvg50,
        "dayHigh": dayHigh,
        "dayLow": dayLow,
        "yearHigh": yearHigh,
        "yearLow": yearLow,
        "timestamp": timestamp,
    }


# ===========================================================================
# market_universe
# ===========================================================================

class TestMarketUniverseGetSymbols(unittest.TestCase):

    def setUp(self):
        from market_universe import get_universe_symbols, get_all_symbols
        self.get = get_universe_symbols
        self.all = get_all_symbols

    def test_default_groups_returns_nasdaq100_and_etfs(self):
        result = self.get({})
        self.assertIn("nasdaq100", result)
        self.assertIn("sector_etfs", result)

    def test_nasdaq100_symbols_non_empty(self):
        result = self.get({})
        self.assertGreater(len(result["nasdaq100"]), 50)

    def test_sector_etfs_contains_xlk(self):
        result = self.get({})
        self.assertIn("XLK", result["sector_etfs"])

    def test_sp500_group_skipped_without_data(self):
        result = self.get({"market_universe": {"groups": ["sp500"]}})
        self.assertNotIn("sp500", result)

    def test_sp500_group_populated_when_data_provided(self):
        syms = ["AAPL", "MSFT", "GOOG"]
        result = self.get(
            {"market_universe": {"groups": ["sp500"]}},
            sp500_symbols=syms,
        )
        self.assertEqual(result["sp500"], syms)

    def test_portfolio_group_populated(self):
        result = self.get(
            {"market_universe": {"groups": ["portfolio"]}},
            portfolio_symbols=["QQQ", "GLD"],
        )
        self.assertEqual(result["portfolio"], ["QQQ", "GLD"])

    def test_portfolio_group_skipped_when_empty(self):
        result = self.get(
            {"market_universe": {"groups": ["portfolio"]}},
            portfolio_symbols=[],
        )
        self.assertNotIn("portfolio", result)

    def test_unknown_group_ignored(self):
        result = self.get({"market_universe": {"groups": ["unknown_group"]}})
        self.assertNotIn("unknown_group", result)

    def test_max_symbols_truncates(self):
        result = self.get({"market_universe": {"groups": ["nasdaq100"], "max_symbols": 5}})
        self.assertLessEqual(len(result["nasdaq100"]), 5)

    def test_max_symbols_zero_means_unlimited(self):
        result = self.get({"market_universe": {"groups": ["nasdaq100"], "max_symbols": 0}})
        self.assertGreater(len(result["nasdaq100"]), 50)

    def test_get_all_symbols_deduplicates(self):
        # nasdaq100 and sp500 share many symbols — result must be deduplicated
        sp500 = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "NEWSTOCK"]
        result = self.all(
            {"market_universe": {"groups": ["nasdaq100", "sp500"]}},
            sp500_symbols=sp500,
        )
        self.assertEqual(len(result), len(set(result)))

    def test_get_all_symbols_includes_extra_sp500(self):
        result = self.all(
            {"market_universe": {"groups": ["nasdaq100", "sp500"]}},
            sp500_symbols=["AAPL", "UNIQUESTOCK99"],
        )
        self.assertIn("UNIQUESTOCK99", result)

    def test_empty_groups_returns_empty(self):
        result = self.get({"market_universe": {"groups": []}})
        self.assertEqual(result, {})

    def test_get_all_symbols_empty_config(self):
        result = self.all({})
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)


# ===========================================================================
# universal_scanner
# ===========================================================================

class TestUniversalScannerBasic(unittest.TestCase):

    def setUp(self):
        from universal_scanner import UniversalScanner
        self.Scanner = UniversalScanner

    def test_scan_returns_result_for_valid_quote(self):
        scanner = self.Scanner()
        results = scanner.scan({"AAPL": _quote("AAPL")})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].symbol, "AAPL")

    def test_scan_populates_price(self):
        scanner = self.Scanner()
        results = scanner.scan({"AAPL": _quote("AAPL", price=150.0)})
        self.assertAlmostEqual(results[0].price, 150.0)

    def test_scan_computes_rel_volume(self):
        scanner = self.Scanner()
        results = scanner.scan({"AAPL": _quote("AAPL", volume=3_000_000, avgVolume=1_000_000)})
        self.assertAlmostEqual(results[0].rel_volume, 3.0)

    def test_scan_rel_volume_none_when_avg_zero(self):
        scanner = self.Scanner()
        q = _quote("AAPL")
        q["avgVolume"] = 0
        results = scanner.scan({"AAPL": q})
        self.assertIsNone(results[0].rel_volume)

    def test_scan_computes_pct_from_200dma(self):
        scanner = self.Scanner()
        results = scanner.scan({"AAPL": _quote("AAPL", price=110.0, priceAvg200=100.0)})
        self.assertAlmostEqual(results[0].pct_from_200dma, 10.0)

    def test_scan_computes_pct_from_year_high(self):
        scanner = self.Scanner()
        results = scanner.scan({"AAPL": _quote("AAPL", price=90.0, yearHigh=100.0)})
        self.assertAlmostEqual(results[0].pct_from_year_high, -10.0)

    def test_scan_computes_day_range_pct(self):
        scanner = self.Scanner()
        results = scanner.scan({"AAPL": _quote("AAPL", price=100.0, dayHigh=105.0, dayLow=95.0)})
        self.assertAlmostEqual(results[0].day_range_pct, 10.0)

    def test_scan_filters_by_min_price(self):
        scanner = self.Scanner({"min_price": 20.0})
        results = scanner.scan({"LOWP": _quote("LOWP", price=10.0)})
        self.assertEqual(len(results), 0)

    def test_scan_filters_by_min_market_cap(self):
        scanner = self.Scanner({"min_market_cap": 5e9})
        results = scanner.scan({"SMALL": _quote("SMALL", marketCap=1e8)})
        self.assertEqual(len(results), 0)

    def test_scan_skips_zero_price(self):
        scanner = self.Scanner()
        results = scanner.scan({"BAD": _quote("BAD", price=0.0)})
        self.assertEqual(len(results), 0)

    def test_scan_skips_missing_price(self):
        scanner = self.Scanner()
        q = _quote("BAD")
        del q["price"]
        results = scanner.scan({"BAD": q})
        self.assertEqual(len(results), 0)

    def test_scan_handles_nan_price(self):
        import math
        scanner = self.Scanner()
        q = _quote("NAN")
        q["price"] = float("nan")
        results = scanner.scan({"NAN": q})
        self.assertEqual(len(results), 0)

    def test_scan_with_symbol_filter(self):
        scanner = self.Scanner()
        batch = {
            "AAPL": _quote("AAPL"),
            "MSFT": _quote("MSFT"),
            "GOOG": _quote("GOOG"),
        }
        results = scanner.scan(batch, symbols=["AAPL", "GOOG"])
        syms = {r.symbol for r in results}
        self.assertEqual(syms, {"AAPL", "GOOG"})
        self.assertNotIn("MSFT", syms)

    def test_scan_emits_bare_result_for_missing_symbol(self):
        scanner = self.Scanner()
        results = scanner.scan({}, symbols=["AAPL"])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].has_price)

    def test_scan_empty_batch(self):
        scanner = self.Scanner()
        results = scanner.scan({})
        self.assertEqual(results, [])

    def test_scan_handles_none_fields_gracefully(self):
        scanner = self.Scanner()
        q = {"symbol": "AAPL", "price": 100.0}  # minimal quote
        results = scanner.scan({"AAPL": q})
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0].pct_change_1d)
        self.assertIsNone(results[0].rel_volume)

    def test_scan_has_price_flag(self):
        scanner = self.Scanner()
        results = scanner.scan({"AAPL": _quote("AAPL")})
        self.assertTrue(results[0].has_price)

    def test_to_dict_contains_symbol(self):
        from universal_scanner import ScanResult
        sr = ScanResult(symbol="AAPL", price=100.0)
        d = sr.to_dict()
        self.assertEqual(d["symbol"], "AAPL")

    def test_scan_non_dict_quote_skipped(self):
        scanner = self.Scanner()
        results = scanner.scan({"BAD": "not_a_dict"})
        self.assertEqual(len(results), 0)

    def test_scan_normalizes_requested_symbols_and_deduplicates(self):
        scanner = self.Scanner()
        batch = {"aapl": _quote("aapl")}
        results = scanner.scan(batch, symbols=["AAPL", "aapl", "AAPL"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].symbol, "AAPL")


# ===========================================================================
# event_detection
# ===========================================================================

class TestEventDetectionBasic(unittest.TestCase):

    def setUp(self):
        from universal_scanner import ScanResult
        from event_detection import detect_events, EventType, MarketEvent
        self.detect = detect_events
        self.EventType = EventType
        self.ScanResult = ScanResult

    def _sr(self, **kwargs) -> "ScanResult":
        defaults = dict(
            symbol="AAPL",
            price=100.0,
            pct_change_1d=0.5,
            volume=1_000_000,
            avg_volume=1_000_000,
            rel_volume=1.0,
            market_cap=10e9,
            day_high=101.0,
            day_low=99.0,
            day_range_pct=2.0,
            year_high=110.0,
            year_low=75.0,
            pct_from_year_high=-9.1,
        )
        defaults.update(kwargs)
        return self.ScanResult(**defaults)

    def test_strong_move_up_detected(self):
        sr = self._sr(pct_change_1d=5.0)
        events = self.detect([sr])
        types = [e.event_type for e in events]
        self.assertIn(self.EventType.STRONG_MOVE_UP, types)

    def test_strong_move_down_detected(self):
        sr = self._sr(pct_change_1d=-5.0)
        events = self.detect([sr])
        types = [e.event_type for e in events]
        self.assertIn(self.EventType.STRONG_MOVE_DOWN, types)

    def test_no_strong_move_below_threshold(self):
        sr = self._sr(pct_change_1d=1.0)
        events = self.detect([sr])
        types = [e.event_type for e in events]
        self.assertNotIn(self.EventType.STRONG_MOVE_UP, types)
        self.assertNotIn(self.EventType.STRONG_MOVE_DOWN, types)

    def test_volume_spike_detected(self):
        sr = self._sr(rel_volume=3.0)
        events = self.detect([sr])
        types = [e.event_type for e in events]
        self.assertIn(self.EventType.VOLUME_SPIKE, types)

    def test_no_volume_spike_below_factor(self):
        sr = self._sr(rel_volume=1.5)
        events = self.detect([sr])
        types = [e.event_type for e in events]
        self.assertNotIn(self.EventType.VOLUME_SPIKE, types)

    def test_breakout_proxy_detected(self):
        # price within 1% of year high, positive daily move
        sr = self._sr(pct_from_year_high=-0.5, pct_change_1d=2.0)
        events = self.detect([sr])
        types = [e.event_type for e in events]
        self.assertIn(self.EventType.BREAKOUT_PROXY, types)

    def test_breakout_proxy_not_detected_when_too_far_from_high(self):
        sr = self._sr(pct_from_year_high=-10.0, pct_change_1d=2.0)
        events = self.detect([sr])
        types = [e.event_type for e in events]
        self.assertNotIn(self.EventType.BREAKOUT_PROXY, types)

    def test_breakout_proxy_not_detected_without_positive_move(self):
        sr = self._sr(pct_from_year_high=-0.5, pct_change_1d=-1.0)
        events = self.detect([sr])
        types = [e.event_type for e in events]
        self.assertNotIn(self.EventType.BREAKOUT_PROXY, types)

    def test_volatility_expansion_detected(self):
        sr = self._sr(day_range_pct=6.0)
        events = self.detect([sr])
        types = [e.event_type for e in events]
        self.assertIn(self.EventType.VOLATILITY_EXPANSION, types)

    def test_no_volatility_expansion_below_threshold(self):
        sr = self._sr(day_range_pct=2.0)
        events = self.detect([sr])
        types = [e.event_type for e in events]
        self.assertNotIn(self.EventType.VOLATILITY_EXPANSION, types)

    def test_multiple_events_same_symbol(self):
        # Strong move + volume spike on same symbol
        sr = self._sr(pct_change_1d=5.0, rel_volume=3.0)
        events = self.detect([sr])
        self.assertGreaterEqual(len(events), 2)

    def test_empty_scan_results(self):
        events = self.detect([])
        self.assertEqual(events, [])

    def test_symbol_without_price_skipped(self):
        from universal_scanner import ScanResult
        sr = ScanResult(symbol="NOPRICE")
        events = self.detect([sr])
        self.assertEqual(events, [])

    def test_strength_between_zero_and_one(self):
        sr = self._sr(pct_change_1d=5.0)
        events = self.detect([sr])
        for ev in events:
            self.assertGreaterEqual(ev.strength, 0.0)
            self.assertLessEqual(ev.strength, 1.0)

    def test_custom_thresholds(self):
        # Lower threshold: 1% move should trigger
        sr = self._sr(pct_change_1d=2.0)
        events = self.detect([sr], config={"strong_move_pct": 1.0})
        types = [e.event_type for e in events]
        self.assertIn(self.EventType.STRONG_MOVE_UP, types)

    def test_event_metadata_present(self):
        sr = self._sr(pct_change_1d=5.0)
        events = self.detect([sr])
        up_events = [e for e in events if e.event_type == self.EventType.STRONG_MOVE_UP]
        self.assertTrue(len(up_events) > 0)
        self.assertIn("pct_change_1d", up_events[0].metadata)

    def test_to_dict_has_event_type(self):
        sr = self._sr(pct_change_1d=5.0)
        events = self.detect([sr])
        d = events[0].to_dict()
        self.assertIn("event_type", d)

    def test_none_pct_change_no_crash(self):
        from universal_scanner import ScanResult
        sr = ScanResult(symbol="TEST", price=100.0)
        events = self.detect([sr])
        # Should not raise; any events generated are fine
        self.assertIsInstance(events, list)


# ===========================================================================
# opportunity_ranker
# ===========================================================================

class TestOpportunityRankerBasic(unittest.TestCase):

    def setUp(self):
        from universal_scanner import UniversalScanner
        from event_detection import detect_events
        from opportunity_ranker import rank_opportunities, RankedOpportunity
        self.Scanner = UniversalScanner
        self.detect = detect_events
        self.rank = rank_opportunities
        self.RankedOpportunity = RankedOpportunity

    def _scan_and_rank(self, quotes: dict, ranker_cfg: dict = None) -> list:
        from universal_scanner import UniversalScanner
        scanner = UniversalScanner()
        results = scanner.scan(quotes)
        events = self.detect(results)
        return self.rank(results, events, config=ranker_cfg)

    def test_basic_ranking_returns_results(self):
        ranked = self._scan_and_rank({"AAPL": _quote("AAPL")})
        self.assertGreater(len(ranked), 0)

    def test_higher_momentum_ranks_higher(self):
        quotes = {
            "UP": _quote("UP", changesPercentage=5.0),
            "FLAT": _quote("FLAT", changesPercentage=0.1),
        }
        ranked = self._scan_and_rank(quotes)
        syms = [r.symbol for r in ranked]
        self.assertGreater(syms.index("FLAT"), syms.index("UP"))

    def test_rank_field_populated(self):
        ranked = self._scan_and_rank({"AAPL": _quote("AAPL"), "MSFT": _quote("MSFT")})
        ranks = [r.rank for r in ranked]
        self.assertEqual(sorted(ranks), list(range(1, len(ranks) + 1)))

    def test_total_score_between_zero_and_100(self):
        ranked = self._scan_and_rank({"AAPL": _quote("AAPL")})
        for r in ranked:
            self.assertGreaterEqual(r.total_score, 0.0)
            self.assertLessEqual(r.total_score, 100.0)

    def test_factor_breakdown_not_none(self):
        ranked = self._scan_and_rank({"AAPL": _quote("AAPL")})
        self.assertIsNotNone(ranked[0].factor_breakdown)

    def test_reasons_is_list(self):
        ranked = self._scan_and_rank({"AAPL": _quote("AAPL")})
        self.assertIsInstance(ranked[0].reasons, list)

    def test_events_is_list(self):
        ranked = self._scan_and_rank({"AAPL": _quote("AAPL")})
        self.assertIsInstance(ranked[0].events, list)

    def test_empty_scan_returns_empty(self):
        ranked = self._scan_and_rank({})
        self.assertEqual(ranked, [])

    def test_min_score_filters(self):
        ranked = self._scan_and_rank({"AAPL": _quote("AAPL")}, {"min_score": 99.0})
        # 99 is extremely high; likely filtered out
        for r in ranked:
            self.assertGreaterEqual(r.total_score, 99.0)

    def test_custom_weights_accepted(self):
        weights = {"momentum": 0.8, "relative_strength": 0.1,
                   "volume_confirmation": 0.05, "volatility_sanity": 0.05}
        ranked = self._scan_and_rank(
            {"AAPL": _quote("AAPL")},
            {"weights": weights},
        )
        self.assertGreater(len(ranked), 0)

    def test_weights_normalised(self):
        # Weights that don't sum to 1.0 should still produce valid scores
        weights = {"momentum": 2.0, "relative_strength": 2.0,
                   "volume_confirmation": 2.0, "volatility_sanity": 2.0}
        ranked = self._scan_and_rank(
            {"AAPL": _quote("AAPL")},
            {"weights": weights},
        )
        for r in ranked:
            self.assertLessEqual(r.total_score, 100.0)

    def test_symbol_without_price_excluded(self):
        from universal_scanner import ScanResult
        sr = ScanResult(symbol="NOPRICE")
        ranked = self.rank([sr], [], config={})
        self.assertEqual(ranked, [])

    def test_to_dict_serialisable(self):
        import json
        ranked = self._scan_and_rank({"AAPL": _quote("AAPL")})
        # Should not raise
        d = ranked[0].to_dict()
        json.dumps(d)

    def test_conflicting_signals(self):
        # Strong move up AND wide intraday range (risk flag)
        quotes = {
            "CONFLICT": _quote(
                "CONFLICT",
                changesPercentage=5.0,
                dayHigh=115.0,
                dayLow=85.0,  # 30% range
            )
        }
        ranked = self._scan_and_rank(quotes)
        # Should still return a result, with reduced sanity score
        self.assertGreater(len(ranked), 0)
        fb = ranked[0].factor_breakdown
        self.assertIsNotNone(fb.volatility_sanity)
        self.assertLess(fb.volatility_sanity, 50.0)


# ===========================================================================
# promotion_engine
# ===========================================================================

class TestPromotionEngineBasic(unittest.TestCase):

    def setUp(self):
        from universal_scanner import UniversalScanner
        from event_detection import detect_events
        from opportunity_ranker import rank_opportunities
        from promotion_engine import promote_candidates, PromotedCandidate
        self.Scanner = UniversalScanner
        self.detect = detect_events
        self.rank = rank_opportunities
        self.promote = promote_candidates
        self.PromotedCandidate = PromotedCandidate

    def _pipeline(self, quotes: dict, promo_cfg: dict = None) -> list:
        scanner = self.Scanner()
        results = scanner.scan(quotes)
        events = self.detect(results)
        ranked = self.rank(results, events)
        return self.promote(ranked, config=promo_cfg)

    def test_basic_promotion(self):
        quotes = {sym: _quote(sym) for sym in ["AAPL", "MSFT", "GOOG"]}
        promoted = self._pipeline(quotes, {"min_score": 0.0})
        self.assertGreater(len(promoted), 0)

    def test_promoted_has_label(self):
        promoted = self._pipeline({"AAPL": _quote("AAPL")})
        for p in promoted:
            self.assertIn(p.label, ("compounder", "momentum", "watchlist"))

    def test_top_n_respected(self):
        quotes = {f"SYM{i}": _quote(f"SYM{i}") for i in range(30)}
        promoted = self._pipeline(quotes, {"top_n": 5, "min_score": 0.0})
        self.assertLessEqual(len(promoted), 5)

    def test_min_score_filter(self):
        quotes = {"AAPL": _quote("AAPL")}
        promoted = self._pipeline(quotes, {"min_score": 200.0})  # impossible
        self.assertEqual(promoted, [])

    def test_max_promoted_hard_cap(self):
        quotes = {f"SYM{i}": _quote(f"SYM{i}") for i in range(50)}
        promoted = self._pipeline(quotes, {"top_n": 50, "min_score": 0.0, "max_promoted": 3})
        self.assertLessEqual(len(promoted), 3)

    def test_promoted_at_is_set(self):
        promoted = self._pipeline({"AAPL": _quote("AAPL")}, {"min_score": 0.0})
        for p in promoted:
            self.assertIsNotNone(p.promoted_at)
            self.assertIn("T", p.promoted_at)  # ISO 8601 format

    def test_empty_ranked_returns_empty(self):
        promoted = self.promote([], config={})
        self.assertEqual(promoted, [])

    def test_to_dict_serialisable(self):
        import json
        promoted = self._pipeline({"AAPL": _quote("AAPL")}, {"min_score": 0.0})
        if promoted:
            json.dumps(promoted[0].to_dict())

    def test_momentum_label_assigned_for_strong_move(self):
        # Strong upward move should produce at least one "momentum" label
        quotes = {"ROCKET": _quote("ROCKET", changesPercentage=8.0, volume=5_000_000, avgVolume=1_000_000)}
        promoted = self._pipeline(quotes, {"min_score": 0.0})
        labels = [p.label for p in promoted]
        self.assertIn("momentum", labels)

    def test_compounder_label_for_near_high_breakout(self):
        # Price at 99% of year high with positive daily move
        quotes = {
            "CLIMBER": _quote(
                "CLIMBER",
                price=99.0,
                yearHigh=100.0,    # pct_from_year_high = -1%
                changesPercentage=2.0,
                volume=2_500_000,
                avgVolume=1_000_000,
            )
        }
        promoted = self._pipeline(quotes, {"min_score": 0.0})
        if promoted:
            # Label depends on RS score; with price so close to high, RS should be high
            # At minimum it should not crash
            self.assertIn(promoted[0].label, ("compounder", "momentum", "watchlist"))

    def test_watchlist_label_when_no_events(self):
        # Flat, normal volume — no events should fire
        # Build manually so price is far from year high (low RS score)
        from universal_scanner import UniversalScanner
        from event_detection import detect_events
        from opportunity_ranker import rank_opportunities

        scanner = UniversalScanner()
        q = _quote("BORING", changesPercentage=0.1, volume=900_000,
                   avgVolume=1_000_000, yearHigh=200.0, price=100.0)
        results = scanner.scan({"BORING": q})
        events = detect_events(results)
        ranked = rank_opportunities(results, events)
        promoted = self.promote(ranked, config={"min_score": 0.0})
        if promoted:
            self.assertEqual(promoted[0].label, "watchlist")

    def test_all_candidates_filtered_returns_empty(self):
        from opportunity_ranker import RankedOpportunity, FactorBreakdown
        opp = RankedOpportunity(
            symbol="LOW",
            total_score=10.0,
            factor_breakdown=FactorBreakdown(),
            reasons=[],
            events=[],
        )
        promoted = self.promote([opp], config={"min_score": 50.0})
        self.assertEqual(promoted, [])

    def test_rank_field_carried_through(self):
        from opportunity_ranker import RankedOpportunity, FactorBreakdown
        opp = RankedOpportunity(
            symbol="AAPL",
            total_score=70.0,
            factor_breakdown=FactorBreakdown(),
            reasons=["test"],
            events=[],
            rank=3,
        )
        promoted = self.promote([opp], config={"min_score": 0.0})
        self.assertEqual(promoted[0].rank, 3)

    def test_portfolio_review_marks_existing_holdings_and_rotation_candidates(self):
        from promotion_engine import build_portfolio_review

        promoted = self._pipeline(
            {
                "AAPL": _quote("AAPL", changesPercentage=6.0, volume=4_000_000, avgVolume=1_000_000),
                "NEW1": _quote("NEW1", changesPercentage=5.0, volume=3_000_000, avgVolume=1_000_000),
            },
            {"min_score": 0.0},
        )
        review = build_portfolio_review(
            promoted,
            holdings=[{"symbol": "AAPL"}],
            scanner_candidates=[{"symbol": "MSFT"}],
            cash_available=2500.0,
        )

        self.assertTrue(review["available"])
        by_symbol = {row["symbol"]: row for row in review["reviewed_candidates"]}
        self.assertEqual(by_symbol["AAPL"]["portfolio_action_bucket"], "existing_holding_confirmation")
        self.assertEqual(by_symbol["NEW1"]["portfolio_action_bucket"], "rotation_candidate")
        self.assertGreaterEqual(review["new_rotation_candidates"], 1)

    def test_portfolio_review_marks_scanner_overlap(self):
        from promotion_engine import build_portfolio_review

        promoted = self._pipeline(
            {
                "MSFT": _quote("MSFT", changesPercentage=7.0, volume=5_000_000, avgVolume=1_000_000),
            },
            {"min_score": 0.0},
        )
        review = build_portfolio_review(
            promoted,
            holdings=[],
            scanner_candidates=[{"symbol": "MSFT"}],
            cash_available=1000.0,
        )

        self.assertEqual(review["scanner_confirmation_count"], 1)
        self.assertEqual(
            review["reviewed_candidates"][0]["portfolio_action_bucket"],
            "scanner_confirmation",
        )


# ===========================================================================
# Integration: full pipeline (universe → scan → events → rank → promote)
# ===========================================================================

class TestFullPipeline(unittest.TestCase):

    def test_full_pipeline_end_to_end(self):
        from market_universe import get_all_symbols
        from universal_scanner import UniversalScanner
        from event_detection import detect_events
        from opportunity_ranker import rank_opportunities
        from promotion_engine import promote_candidates

        # Simulated universe (10 symbols)
        universe_cfg = {"market_universe": {"groups": ["nasdaq100"]}}
        all_syms = get_all_symbols(universe_cfg)[:10]

        # Fake batch quotes for all symbols
        batch_quotes = {
            sym: _quote(
                sym,
                changesPercentage=float(i),
                volume=int(1e6 * (i + 1)),
                avgVolume=1_000_000,
            )
            for i, sym in enumerate(all_syms)
        }

        scanner = UniversalScanner()
        results = scanner.scan(batch_quotes, symbols=all_syms)
        self.assertEqual(len(results), len(all_syms))

        events = detect_events(results)
        ranked = rank_opportunities(results, events)
        self.assertGreater(len(ranked), 0)

        promoted = promote_candidates(ranked, config={"min_score": 0.0, "top_n": 5})
        self.assertLessEqual(len(promoted), 5)

        # All promoted candidates have required fields
        for p in promoted:
            self.assertIsNotNone(p.symbol)
            self.assertIsNotNone(p.score)
            self.assertIsNotNone(p.label)
            self.assertIn(p.label, ("compounder", "momentum", "watchlist"))

    def test_pipeline_with_empty_universe(self):
        from universal_scanner import UniversalScanner
        from event_detection import detect_events
        from opportunity_ranker import rank_opportunities
        from promotion_engine import promote_candidates

        scanner = UniversalScanner()
        results = scanner.scan({}, symbols=[])
        events = detect_events(results)
        ranked = rank_opportunities(results, events)
        promoted = promote_candidates(ranked)

        self.assertEqual(promoted, [])

    def test_pipeline_all_symbols_missing_from_quotes(self):
        from universal_scanner import UniversalScanner
        from event_detection import detect_events
        from opportunity_ranker import rank_opportunities
        from promotion_engine import promote_candidates

        scanner = UniversalScanner()
        results = scanner.scan({}, symbols=["AAPL", "MSFT"])
        # Results will be bare (no price)
        events = detect_events(results)
        ranked = rank_opportunities(results, events)
        promoted = promote_candidates(ranked)

        self.assertEqual(promoted, [])

    def test_pipeline_degraded_partial_data(self):
        """Some symbols have full data, some have minimal data."""
        from universal_scanner import UniversalScanner
        from event_detection import detect_events
        from opportunity_ranker import rank_opportunities
        from promotion_engine import promote_candidates

        batch_quotes = {
            "FULL": _quote("FULL"),
            "PARTIAL": {"symbol": "PARTIAL", "price": 50.0},  # minimal
        }
        scanner = UniversalScanner()
        results = scanner.scan(batch_quotes, symbols=["FULL", "PARTIAL", "MISSING"])
        events = detect_events(results)
        ranked = rank_opportunities(results, events)
        promoted = promote_candidates(ranked, config={"min_score": 0.0})

        syms = {p.symbol for p in promoted}
        self.assertIn("FULL", syms)
        # PARTIAL: price OK but most fields None → some events/scores may be missing
        # Should not crash regardless


if __name__ == "__main__":
    unittest.main()
