"""
Unit tests for guardrails.py pre-flight structural checks.

All tests are fully offline — no network calls, no file I/O.
Uses lightweight _Holding stubs matching the pattern in test_run_mode.py.
"""

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from guardrails import run_guardrail_checks, GuardrailResult, GuardrailViolation


# ---------------------------------------------------------------------------
# Lightweight stub (mirrors the Holding dataclass in utils.py)
# ---------------------------------------------------------------------------

@dataclass
class _Holding:
    symbol: str
    market_value: Optional[float]
    is_leveraged: bool = False
    leverage_factor: float = 1.0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGuardrails(unittest.TestCase):

    def _h(self, symbol, market_value, is_leveraged=False, leverage_factor=1.0):
        return _Holding(
            symbol=symbol,
            market_value=market_value,
            is_leveraged=is_leveraged,
            leverage_factor=leverage_factor,
        )

    def test_all_ok_within_caps(self):
        """Holdings well within both caps should return status='ok'."""
        h1 = self._h('AAA', 3500)   # 35% of 10k
        h2 = self._h('BBB', 3000)   # 30%
        h3 = self._h('CCC', 3500)   # 35%
        result = run_guardrail_checks([h1, h2, h3], 10000, 0.40, 0.15)
        self.assertEqual(result.status, 'ok')
        self.assertFalse(result.has_violations)

    def test_concentration_violation_detected(self):
        """A holding at 50% of portfolio should breach the 40% cap."""
        h1 = self._h('BIG', 5000)   # 50% of 10k
        h2 = self._h('SML', 5000)   # 50%
        result = run_guardrail_checks([h1, h2], 10000, 0.40, 0.15)
        self.assertEqual(result.status, 'structural_violation')
        symbols = [v.symbol for v in result.violations]
        self.assertIn('BIG', symbols)
        self.assertIn('SML', symbols)

    def test_leverage_violation_detected(self):
        """Leveraged holding producing 30% effective exposure exceeds 15% cap."""
        h = self._h('LEV', 1000, is_leveraged=True, leverage_factor=3.0)
        # Effective = (1000/10000) × 3 = 30%
        result = run_guardrail_checks([h], 10000, 0.40, 0.15)
        lev = [v for v in result.violations if v.violation_type == 'leverage']
        self.assertEqual(len(lev), 1)
        self.assertEqual(lev[0].symbol, 'PORTFOLIO')

    def test_leverage_within_cap_no_violation(self):
        """Leveraged holding at 10% effective exposure is under 15% cap."""
        h = self._h('LEV', 500, is_leveraged=True, leverage_factor=2.0)
        # Effective = (500/10000) × 2 = 10%
        result = run_guardrail_checks([h], 10000, 0.40, 0.15)
        lev = [v for v in result.violations if v.violation_type == 'leverage']
        self.assertEqual(len(lev), 0)

    def test_both_violations_detected(self):
        """Concentration and leverage violations can coexist in one result."""
        h_big = self._h('BIG', 5000)                                 # 50% concentration
        h_lev = self._h('LEV', 1000, is_leveraged=True, leverage_factor=3.0)
        # Leverage exposure = (1000/10000) × 3 = 30%
        result = run_guardrail_checks([h_big, h_lev], 10000, 0.40, 0.15)
        self.assertEqual(result.status, 'structural_violation')
        types = {v.violation_type for v in result.violations}
        self.assertIn('concentration', types)
        self.assertIn('leverage', types)

    def test_skips_holdings_with_no_market_value(self):
        """Holdings with market_value=None should not cause violations."""
        h = self._h('UNPRICED', None)
        result = run_guardrail_checks([h], 10000, 0.40, 0.15)
        self.assertEqual(result.status, 'ok')
        self.assertFalse(result.has_violations)

    def test_zero_portfolio_returns_ok_with_skip_message(self):
        """Zero total_portfolio is a graceful no-op, not an error."""
        h = self._h('AAA', 1000)
        result = run_guardrail_checks([h], 0, 0.40, 0.15)
        self.assertEqual(result.status, 'ok')
        self.assertFalse(result.has_violations)

    def test_to_dict_structure(self):
        """to_dict() should include required keys for result dict storage."""
        h = self._h('BIG', 5000)
        result = run_guardrail_checks([h], 10000, 0.40, 0.15)
        d = result.to_dict()
        self.assertIn('status', d)
        self.assertIn('violation_count', d)
        self.assertIn('violations', d)
        self.assertIn('summary', d)
        self.assertEqual(d['violation_count'], len(d['violations']))

    def test_non_leveraged_holding_does_not_count_toward_leverage_cap(self):
        """Standard (non-leveraged) holdings must not affect the leverage tally."""
        h1 = self._h('QQQ', 3000, is_leveraged=False)
        h2 = self._h('LEV', 300, is_leveraged=True, leverage_factor=2.0)
        # Effective leverage = (300/10000) × 2 = 6% — under cap
        result = run_guardrail_checks([h1, h2], 10000, 0.40, 0.15)
        lev = [v for v in result.violations if v.violation_type == 'leverage']
        self.assertEqual(len(lev), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
