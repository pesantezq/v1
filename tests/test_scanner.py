"""
Unit tests for scanner/candidate_scanner.py.

Fully offline — no API calls, no file I/O (watchlist paths not used).
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scanner.candidate_scanner import CandidateScanner


def _scanner(**kw) -> CandidateScanner:
    defaults = dict(min_mkt_cap=5e9, min_rev_growth=0.15,
                    trend_filter_200dma=True, top_k=10)
    defaults.update(kw)
    return CandidateScanner(**defaults)


def _profile(mkt_cap=10e9, sector='Technology'):
    return {'mktCap': mkt_cap, 'sector': sector}


def _metrics(rev_growth=0.20, fcf_yield=0.05, roe=0.25, pe=20):
    return {
        'revenueGrowth': rev_growth,
        'freeCashFlowYield': fcf_yield,
        'roe': roe,
        'peRatio': pe,
    }


def _quote(price=110, avg200=100):
    return {'price': price, 'priceAvg200': avg200, 'marketCap': 10e9}


# ---------------------------------------------------------------------------
# Hard filter tests
# ---------------------------------------------------------------------------

class TestHardFilters(unittest.TestCase):

    def test_passes_all_criteria(self):
        s = _scanner()
        passes, failures = s._passes_hard_filters('AAPL', _profile(), _metrics(), _quote())
        self.assertTrue(passes)
        self.assertEqual(failures, [])

    def test_fails_below_min_mkt_cap(self):
        s = _scanner()
        passes, failures = s._passes_hard_filters(
            'TINY', _profile(mkt_cap=1e9), _metrics(), _quote())
        self.assertFalse(passes)
        self.assertTrue(any('mkt_cap' in f for f in failures))

    def test_fails_below_min_rev_growth(self):
        s = _scanner()
        passes, failures = s._passes_hard_filters(
            'SLOW', _profile(), _metrics(rev_growth=0.05), _quote())
        self.assertFalse(passes)
        self.assertTrue(any('rev_growth' in f for f in failures))

    def test_fails_below_200dma(self):
        s = _scanner()
        passes, failures = s._passes_hard_filters(
            'DOWN', _profile(), _metrics(), _quote(price=80, avg200=100))
        self.assertFalse(passes)
        self.assertTrue(any('200dma' in f for f in failures))

    def test_fails_pe_too_high(self):
        s = _scanner()
        passes, failures = s._passes_hard_filters(
            'BUBL', _profile(), _metrics(pe=60), _quote())
        self.assertFalse(passes)
        self.assertTrue(any('pe' in f for f in failures))

    def test_fails_negative_fcf_yield(self):
        s = _scanner()
        passes, failures = s._passes_hard_filters(
            'BURN', _profile(), _metrics(fcf_yield=-0.02), _quote())
        self.assertFalse(passes)
        self.assertTrue(any('fcf' in f.lower() for f in failures))

    def test_trend_filter_disabled_ignores_200dma(self):
        """When trend_filter_200dma=False, price < 200 DMA should not block."""
        s = _scanner(trend_filter_200dma=False)
        passes, _ = s._passes_hard_filters(
            'OK', _profile(), _metrics(), _quote(price=80, avg200=100))
        self.assertTrue(passes)

    def test_missing_rev_growth_fails(self):
        """None revenueGrowth should be treated as a filter failure."""
        s = _scanner()
        passes, failures = s._passes_hard_filters(
            'NA', _profile(), {}, _quote())
        self.assertFalse(passes)
        self.assertTrue(any('rev_growth' in f for f in failures))


# ---------------------------------------------------------------------------
# Scoring tests
# ---------------------------------------------------------------------------

class TestScoring(unittest.TestCase):

    def test_high_quality_scores_above_70(self):
        s = _scanner()
        score = s._score(
            _profile(),
            _metrics(rev_growth=0.35, fcf_yield=0.04, roe=0.50, pe=25),
            _quote(price=100, avg200=80),
        )
        self.assertGreater(score, 70)

    def test_low_quality_scores_below_30(self):
        s = _scanner()
        score = s._score(
            _profile(),
            _metrics(rev_growth=0.16, fcf_yield=0.005, roe=0.05, pe=48),
            _quote(price=50, avg200=60),   # below 200 DMA → no trend pts
        )
        self.assertLess(score, 30)

    def test_above_200dma_adds_10_pts(self):
        s = _scanner()
        m = _metrics()
        score_above = s._score(_profile(), m, _quote(price=110, avg200=100))
        score_below = s._score(_profile(), m, _quote(price=90, avg200=100))
        self.assertAlmostEqual(score_above - score_below, 10.0, places=1)

    def test_score_capped_at_100(self):
        s = _scanner()
        score = s._score(
            {'mktCap': 3e12},
            {'revenueGrowth': 1.0, 'freeCashFlowYield': 0.20, 'roe': 2.0, 'peRatio': 10},
            {'price': 200, 'priceAvg200': 100},
        )
        self.assertLessEqual(score, 100.0)


# ---------------------------------------------------------------------------
# Full-scan and daily-refresh integration tests
# ---------------------------------------------------------------------------

class TestFullScan(unittest.TestCase):

    def _bulk_data(self):
        profiles = [
            {'symbol': 'AAPL', 'mktCap': 3e12, 'sector': 'Technology'},
            {'symbol': 'SLOW', 'mktCap': 10e9, 'sector': 'Energy'},    # low rev growth
            {'symbol': 'TINY', 'mktCap': 1e9, 'sector': 'Health'},     # below mkt cap
        ]
        metrics = [
            {'symbol': 'AAPL', 'revenueGrowth': 0.25, 'freeCashFlowYield': 0.03,
             'roe': 0.30, 'peRatio': 28},
            {'symbol': 'SLOW', 'revenueGrowth': 0.05, 'freeCashFlowYield': 0.02,
             'roe': 0.10, 'peRatio': 15},
            {'symbol': 'TINY', 'revenueGrowth': 0.20, 'freeCashFlowYield': 0.03,
             'roe': 0.20, 'peRatio': 20},
        ]
        quotes = {
            'AAPL': {'price': 190, 'priceAvg200': 170},
            'SLOW': {'price': 50, 'priceAvg200': 45},
            'TINY': {'price': 20, 'priceAvg200': 18},
        }
        return profiles, metrics, quotes

    def test_full_scan_filters_correctly(self):
        """AAPL passes; SLOW fails rev_growth; TINY fails mkt_cap."""
        s = _scanner()
        profiles, metrics, quotes = self._bulk_data()
        candidates, debug = s.full_scan(
            ['AAPL', 'SLOW', 'TINY'], profiles, metrics, quotes
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['symbol'], 'AAPL')
        self.assertEqual(len(debug), 3)

    def test_full_scan_debug_records_all_symbols(self):
        s = _scanner()
        profiles, metrics, quotes = self._bulk_data()
        _, debug = s.full_scan(['AAPL', 'SLOW', 'TINY'], profiles, metrics, quotes)
        debug_symbols = {d['symbol'] for d in debug}
        self.assertEqual(debug_symbols, {'AAPL', 'SLOW', 'TINY'})

    def test_daily_refresh_trend_drop_reduces_score(self):
        """Score drops by 10 when stock falls below 200 DMA."""
        s = _scanner()
        watchlist = [{
            'symbol': 'AAPL', 'score': 75.0, 'above_200dma': True,
            'price': 180, 'price_200dma': 170, 'rev_growth': 0.25,
            'sector': 'Tech', 'mkt_cap': 3e12, 'fcf_yield': 0.03,
            'roe': 0.30, 'pe': 28,
        }]
        new_quotes = {'AAPL': {'price': 160, 'priceAvg200': 170}}  # below 200 DMA
        refreshed, _ = s.daily_refresh(watchlist, new_quotes)
        self.assertFalse(refreshed[0]['above_200dma'])
        self.assertAlmostEqual(refreshed[0]['score'], 65.0, places=1)

    def test_daily_refresh_trend_gain_raises_score(self):
        """Score rises by 10 when stock crosses above 200 DMA."""
        s = _scanner()
        watchlist = [{
            'symbol': 'NVDA', 'score': 60.0, 'above_200dma': False,
            'price': 90, 'price_200dma': 100, 'rev_growth': 0.35,
            'sector': 'Tech', 'mkt_cap': 2e12, 'fcf_yield': 0.02,
            'roe': 0.40, 'pe': 30,
        }]
        new_quotes = {'NVDA': {'price': 105, 'priceAvg200': 100}}  # now above 200 DMA
        refreshed, _ = s.daily_refresh(watchlist, new_quotes)
        self.assertTrue(refreshed[0]['above_200dma'])
        self.assertAlmostEqual(refreshed[0]['score'], 70.0, places=1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
