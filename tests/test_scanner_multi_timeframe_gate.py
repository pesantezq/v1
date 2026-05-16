"""
P4.3 — Multi-timeframe trend gate in signal_score composite.

Adds an optional weekly + monthly trend gate to _compute_signal_score.
The gate is a multiplicative factor applied to `technical_score` only:

    both timeframes bullish     → gate = 1.00 (no penalty)
    exactly one bullish         → gate = 0.85
    both bearish                → gate = 0.70
    either missing/None         → gate = 1.00 (no penalty)

Reads from tech dict keys `weekly_trend_bullish` and
`monthly_trend_bullish`. When neither key is present (legacy callers),
signal_score is byte-identical to the previous implementation.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.scanner import _compute_signal_score  # noqa: E402


def _base_tech() -> dict:
    """A technical bundle that produces a non-zero technical_score."""
    return {
        "price_change_1d": 3.0,   # 60% of 1d momentum slot
        "price_change_5d": 6.0,   # 60% of 5d momentum slot
        "volume_spike": True,
        "above_sma20": True,
        "above_sma50": True,
    }


def _no_news() -> list[dict]:
    return []


def _no_theme() -> dict:
    return {}


class TestMultiTimeframeGate(unittest.TestCase):

    def _score(self, tech_overrides: dict | None = None) -> tuple[float, dict]:
        tech = _base_tech()
        if tech_overrides:
            tech.update(tech_overrides)
        return _compute_signal_score(
            tech=tech,
            theme_scores=_no_theme(),
            articles=_no_news(),
            fund_score=0.50,
        )

    # ---- Backward compatibility (no mtf inputs) ----------------------------

    def test_no_mtf_inputs_signal_score_unchanged(self):
        # Calling with the legacy tech dict (no weekly/monthly keys) must
        # produce the same signal_score as before the gate was added.
        score, breakdown = self._score()
        self.assertGreater(score, 0.0)
        # Gate applied factor of 1.0 → technical_score unchanged.
        self.assertEqual(breakdown["mtf_gate"], 1.00)

    # ---- Both timeframes present -------------------------------------------

    def test_both_bullish_no_penalty(self):
        _, breakdown = self._score(
            {"weekly_trend_bullish": True, "monthly_trend_bullish": True}
        )
        self.assertEqual(breakdown["mtf_gate"], 1.00)

    def test_both_bearish_full_penalty(self):
        _, bd_bearish = self._score(
            {"weekly_trend_bullish": False, "monthly_trend_bullish": False}
        )
        self.assertEqual(bd_bearish["mtf_gate"], 0.70)

    def test_only_weekly_bullish_partial_penalty(self):
        _, bd = self._score(
            {"weekly_trend_bullish": True, "monthly_trend_bullish": False}
        )
        self.assertEqual(bd["mtf_gate"], 0.85)

    def test_only_monthly_bullish_partial_penalty(self):
        _, bd = self._score(
            {"weekly_trend_bullish": False, "monthly_trend_bullish": True}
        )
        self.assertEqual(bd["mtf_gate"], 0.85)

    # ---- One or both missing -----------------------------------------------

    def test_weekly_missing_no_penalty(self):
        _, bd = self._score({"monthly_trend_bullish": False})
        self.assertEqual(bd["mtf_gate"], 1.00)

    def test_monthly_missing_no_penalty(self):
        _, bd = self._score({"weekly_trend_bullish": False})
        self.assertEqual(bd["mtf_gate"], 1.00)

    def test_weekly_none_no_penalty(self):
        _, bd = self._score(
            {"weekly_trend_bullish": None, "monthly_trend_bullish": False}
        )
        self.assertEqual(bd["mtf_gate"], 1.00)

    def test_monthly_none_no_penalty(self):
        _, bd = self._score(
            {"weekly_trend_bullish": True, "monthly_trend_bullish": None}
        )
        self.assertEqual(bd["mtf_gate"], 1.00)

    # ---- Gate is applied to technical only ---------------------------------

    def test_gate_reduces_signal_score_when_bearish(self):
        # With identical theme + fundamental inputs, both-bearish gate must
        # produce a strictly lower signal_score than both-bullish (or the
        # legacy no-mtf path).
        legacy_score, _ = self._score()
        bullish_score, _ = self._score(
            {"weekly_trend_bullish": True, "monthly_trend_bullish": True}
        )
        bearish_score, bd_bearish = self._score(
            {"weekly_trend_bullish": False, "monthly_trend_bullish": False}
        )
        self.assertAlmostEqual(legacy_score, bullish_score, places=4)
        self.assertLess(bearish_score, bullish_score)
        # The reduction equals technical_component × (1 - 0.70) × 0.30 weight
        # Sanity: signal scores differ by at most the technical contribution.
        delta = bullish_score - bearish_score
        self.assertLessEqual(delta, 0.30)
        # Breakdown reflects the gate
        self.assertEqual(bd_bearish["mtf_gate"], 0.70)

    def test_gate_does_not_affect_theme_or_fundamental(self):
        # technical_score breakdown reports gated value; theme/fundamental
        # unchanged.
        _, bd_legacy = self._score()
        _, bd_bearish = self._score(
            {"weekly_trend_bullish": False, "monthly_trend_bullish": False}
        )
        self.assertEqual(bd_legacy["theme_news_score"], bd_bearish["theme_news_score"])
        self.assertEqual(
            bd_legacy["fundamental_context_score"],
            bd_bearish["fundamental_context_score"],
        )
        self.assertGreater(bd_legacy["technical_score"], bd_bearish["technical_score"])

    # ---- Breakdown observability -------------------------------------------

    def test_breakdown_includes_mtf_gate(self):
        _, breakdown = self._score(
            {"weekly_trend_bullish": True, "monthly_trend_bullish": False}
        )
        self.assertIn("mtf_gate", breakdown)

    def test_breakdown_includes_mtf_inputs_when_present(self):
        _, breakdown = self._score(
            {"weekly_trend_bullish": True, "monthly_trend_bullish": False}
        )
        self.assertEqual(breakdown.get("weekly_trend_bullish"), True)
        self.assertEqual(breakdown.get("monthly_trend_bullish"), False)

    def test_breakdown_omits_mtf_inputs_when_absent(self):
        # Legacy callers must not see mtf input fields they didn't provide.
        _, breakdown = self._score()
        self.assertNotIn("weekly_trend_bullish", breakdown)
        self.assertNotIn("monthly_trend_bullish", breakdown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
