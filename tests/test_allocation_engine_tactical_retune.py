"""
Lock-in tests for the 2026-05-18 tactical retune of allocation_engine.DEFAULT_CONFIG.

Operator approved increasing per-signal sizing and relaxing diversification
guards to favor profit maximization over conservative compounding:

    compounder_base_pct       0.05 -> 0.10
    momentum_base_pct         0.03 -> 0.06
    max_position_cap          0.08 -> 0.15
    sector_cap                0.20 -> 0.35
    low_confidence_multiplier 0.50 -> 0.65

These tests pin the post-retune defaults so a silent revert can't slip
through without an explicit test edit. They DO NOT validate the broader
band/Kelly/regime machinery — existing dedicated suites cover that.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from allocation_engine import DEFAULT_CONFIG, suggest_allocation
from decision_engine import _ABSOLUTE_MAX_ALLOCATION_PCT
from portfolio_automation.cash_deployment_plan import _MAX_POSITION_PCT
from watchlist_scanner.allocation_preview import (
    _DEFAULT_MAX_TICKER_PCT,
    _DEFAULT_MAX_SECTOR_PCT,
)


class TestRetuneDefaults(unittest.TestCase):
    def test_compounder_base_is_10pct(self):
        self.assertAlmostEqual(DEFAULT_CONFIG["compounder_base_pct"], 0.10, places=4)

    def test_momentum_base_is_6pct(self):
        self.assertAlmostEqual(DEFAULT_CONFIG["momentum_base_pct"], 0.06, places=4)

    def test_max_position_cap_is_12pct(self):
        # 2026-06-26 targeted partial revert: max_position_cap 0.15 -> 0.12
        self.assertAlmostEqual(DEFAULT_CONFIG["max_position_cap"], 0.12, places=4)

    def test_sector_cap_is_25pct(self):
        # 2026-06-26 targeted partial revert: sector_cap 0.35 -> 0.25
        self.assertAlmostEqual(DEFAULT_CONFIG["sector_cap"], 0.25, places=4)

    def test_low_confidence_multiplier_is_065(self):
        self.assertAlmostEqual(DEFAULT_CONFIG["low_confidence_multiplier"], 0.65, places=4)


class TestRetuneMirrors(unittest.TestCase):
    """The cap value is mirrored in three places; they must all agree."""

    def test_decision_engine_mirror_matches_max_position_cap(self):
        self.assertAlmostEqual(
            _ABSOLUTE_MAX_ALLOCATION_PCT,
            DEFAULT_CONFIG["max_position_cap"],
            places=4,
        )

    def test_cash_deployment_plan_mirror_matches_max_position_cap(self):
        self.assertAlmostEqual(
            _MAX_POSITION_PCT,
            DEFAULT_CONFIG["max_position_cap"],
            places=4,
        )

    def test_allocation_preview_mirror_matches_max_position_cap(self):
        self.assertAlmostEqual(
            _DEFAULT_MAX_TICKER_PCT,
            DEFAULT_CONFIG["max_position_cap"],
            places=4,
        )

    def test_allocation_preview_sector_mirror_matches_sector_cap(self):
        self.assertAlmostEqual(
            _DEFAULT_MAX_SECTOR_PCT,
            DEFAULT_CONFIG["sector_cap"],
            places=4,
        )


class TestRetuneEndToEnd(unittest.TestCase):
    """Behavioral sanity checks: post-retune sizing yields ~2x the prior allocation."""

    def _opp(self, **overrides):
        payload = {"symbol": "ABC", "score": 82.0, "confidence": 0.80, "sector": "Tech"}
        payload.update(overrides)
        return payload

    def test_high_conviction_compounder_doubles_vs_prior_default(self):
        s = suggest_allocation(
            opportunity=self._opp(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=50_000.0,
        )
        # Prior default would have produced 5%; new default produces 10%.
        self.assertAlmostEqual(s.suggested_pct, 0.10, places=4)

    def test_low_confidence_multiplier_is_more_generous(self):
        # Confidence 0.40 routes through the low-confidence bucket.
        # Prior: 10% × 0.50 = 5%. Post-retune: 10% × 0.65 = 6.5%.
        s = suggest_allocation(
            opportunity=self._opp(confidence=0.40),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=50_000.0,
        )
        self.assertAlmostEqual(s.suggested_pct, 0.065, places=4)

    def test_max_position_cap_binds_at_12pct(self):
        # Drive raw sizing above the cap with a high vol_regime multiplier.
        s = suggest_allocation(
            opportunity=self._opp(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=50_000.0,
            vol_regime_plan={
                "status": "ok",
                "sizing_multiplier_suggested": 5.0,
                "regime": "extreme_low_vol",
            },
        )
        self.assertAlmostEqual(s.suggested_pct, 0.12, places=4)
        self.assertIn("max_position_cap", s.capped_by)


if __name__ == "__main__":
    unittest.main(verbosity=2)
