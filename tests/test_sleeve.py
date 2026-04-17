"""
Unit tests for sleeve/spec_sleeve_allocator.py.

Fully offline — no API calls, no file I/O.
Uses a lightweight _Holding stub matching the Holding dataclass in utils.py.
"""

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from sleeve.spec_sleeve_allocator import SpecSleeveAllocator, SleeveRecommendation


@dataclass
class _Holding:
    symbol: str
    market_value: Optional[float]
    asset_class: str = 'us_equity'


def _alloc(**kw) -> SpecSleeveAllocator:
    defaults = dict(
        sleeve_total_max=0.10,
        max_per_stock=0.05,
        max_new_positions_per_month=1,
        min_position_dollars=200.0,
    )
    defaults.update(kw)
    return SpecSleeveAllocator(**defaults)


def _candidate(symbol='NVDA', score=80.0, sector='Technology'):
    return {
        'symbol': symbol, 'score': score, 'sector': sector,
        'rev_growth': 0.25, 'above_200dma': True,
    }


def _holding(symbol, mv=1000.0, asset_class='us_equity'):
    return _Holding(symbol=symbol, market_value=mv, asset_class=asset_class)


class TestSleeveCaps(unittest.TestCase):

    def test_sleeve_total_cap_respected(self):
        """Total recommended buy must never exceed sleeve_total_max."""
        a = _alloc()
        candidates = [
            _candidate('A', 90), _candidate('B', 85), _candidate('C', 80),
        ]
        result = a.allocate(candidates, [], total_portfolio=10_000, available_cash=5_000)
        total = sum(r.max_add_dollars for r in result)
        self.assertLessEqual(total, 10_000 * 0.10 + 0.01)

    def test_per_position_cap_respected(self):
        """Each recommendation must not exceed max_per_stock * total_portfolio."""
        a = _alloc()
        result = a.allocate(
            [_candidate('NVDA', 95)],
            holdings=[],
            total_portfolio=10_000,
            available_cash=5_000,
        )
        for rec in result:
            self.assertLessEqual(rec.max_add_dollars, 10_000 * 0.05 + 0.01)

    def test_max_one_new_position_per_month(self):
        """With max_new_positions_per_month=1, only one new symbol is recommended."""
        a = _alloc(max_new_positions_per_month=1)
        candidates = [_candidate('A', 90), _candidate('B', 85), _candidate('C', 80)]
        result = a.allocate(candidates, [], total_portfolio=10_000, available_cash=5_000)
        new_positions = [r for r in result if r.is_new_position]
        self.assertLessEqual(len(new_positions), 1)


class TestHoldingRules(unittest.TestCase):

    def test_core_holding_blocked_as_new_buy(self):
        """A symbol in the core portfolio (non-speculative) must not be a new buy."""
        a = _alloc()
        holdings = [_holding('AAPL', mv=500, asset_class='us_equity')]
        result = a.allocate(
            [_candidate('AAPL')], holdings,
            total_portfolio=10_000, available_cash=5_000,
        )
        new = [r for r in result if r.is_new_position]
        self.assertEqual(len(new), 0)

    def test_spec_topup_marked_not_new(self):
        """A symbol already in the speculative sleeve receives a top-up, not new."""
        a = _alloc()
        holdings = [_holding('NVDA', mv=200, asset_class='speculative')]
        result = a.allocate(
            [_candidate('NVDA')], holdings,
            total_portfolio=10_000, available_cash=5_000,
        )
        topups = [r for r in result if not r.is_new_position and r.symbol == 'NVDA']
        self.assertGreater(len(topups), 0)

    def test_insufficient_cash_skipped(self):
        """Candidate skipped when available_cash * 0.5 < min_position_dollars."""
        a = _alloc(min_position_dollars=500)
        result = a.allocate(
            [_candidate('NVDA')], [],
            total_portfolio=10_000,
            available_cash=50,  # 50 * 0.5 = 25 < 500
        )
        self.assertEqual(len(result), 0)


class TestEdgeCases(unittest.TestCase):

    def test_empty_candidates_returns_empty(self):
        a = _alloc()
        self.assertEqual(a.allocate([], [], total_portfolio=10_000, available_cash=5_000), [])

    def test_zero_portfolio_returns_empty(self):
        a = _alloc()
        self.assertEqual(
            a.allocate([_candidate()], [], total_portfolio=0, available_cash=1_000), []
        )

    def test_to_dict_has_required_keys(self):
        rec = SleeveRecommendation(
            symbol='NVDA', score=85.0, sector='Technology',
            max_add_dollars=400.0, is_new_position=True,
            current_position_dollars=0.0, reason='Score 85.0/100',
        )
        d = rec.to_dict()
        for key in ('Symbol', 'Score', 'Sector', 'MaxAddDollars',
                    'IsNewPosition', 'CurrentPositionDollars', 'Reason'):
            self.assertIn(key, d)


if __name__ == '__main__':
    unittest.main(verbosity=2)
