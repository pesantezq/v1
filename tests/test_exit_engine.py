import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from exit_engine import evaluate_exit


class TestExitEngine(unittest.TestCase):
    def _holding(self, **overrides):
        payload = {
            "symbol": "AAPL",
            "pct_from_50dma": 1.0,
            "pct_from_200dma": 4.0,
            "theme_support": 0.70,
            "signal_score": 0.80,
            "confidence_score": 0.78,
            "unrealized_return": 0.05,
        }
        payload.update(overrides)
        return payload

    def _opportunity(self, **overrides):
        payload = {
            "symbol": "NVDA",
            "score": 88.0,
        }
        payload.update(overrides)
        return payload

    def test_momentum_trend_break_triggers_sell(self):
        suggestion = evaluate_exit(
            self._holding(pct_from_50dma=-3.5, theme_support=0.55),
            strategy_type="momentum",
        )

        self.assertEqual(suggestion.action, "SELL")
        self.assertIn("trend_break", suggestion.triggers)

    def test_compounder_thesis_weakening_triggers_sell(self):
        suggestion = evaluate_exit(
            self._holding(pct_from_200dma=-6.0, theme_support=0.25),
            strategy_type="compounder",
        )

        self.assertEqual(suggestion.action, "SELL")
        self.assertIn("thesis_weakening", suggestion.triggers)

    def test_profit_protection_can_trim_momentum(self):
        suggestion = evaluate_exit(
            self._holding(
                pct_from_50dma=0.5,
                theme_support=0.55,
                signal_score=0.45,
                confidence_score=0.55,
                unrealized_return=0.18,
            ),
            strategy_type="momentum",
        )

        self.assertEqual(suggestion.action, "TRIM")
        self.assertIn("profit_protection", suggestion.triggers)

    def test_stronger_replacement_can_trigger_rotation_sell(self):
        suggestion = evaluate_exit(
            self._holding(signal_score=0.40, confidence_score=0.50, theme_support=0.35),
            strategy_type="compounder",
            stronger_opportunity=self._opportunity(score=92.0),
        )

        self.assertEqual(suggestion.action, "SELL")
        self.assertIn("opportunity_rotation", suggestion.triggers)

    def test_degraded_mode_soft_signals_default_to_hold(self):
        suggestion = evaluate_exit(
            self._holding(theme_support=0.20, pct_from_200dma=2.0),
            strategy_type="compounder",
            context={"degraded_mode": True},
        )

        self.assertEqual(suggestion.action, "HOLD")

    def test_missing_theme_support_does_not_force_sell(self):
        holding = self._holding()
        holding.pop("theme_support")
        suggestion = evaluate_exit(
            holding,
            strategy_type="compounder",
        )

        self.assertEqual(suggestion.action, "HOLD")
        self.assertNotIn("thesis_weakening", suggestion.triggers)

    def test_compounder_hard_break_triggers_sell_despite_strong_theme(self):
        # -9% from 200dma breaches the hard-break floor (-8%) regardless of theme
        suggestion = evaluate_exit(
            self._holding(pct_from_200dma=-9.0, theme_support=0.75),
            strategy_type="compounder",
        )

        self.assertEqual(suggestion.action, "SELL")
        self.assertIn("trend_break", suggestion.triggers)

    def test_momentum_just_below_old_threshold_does_not_false_exit(self):
        # -2.5% from 50dma: breached the old -2.0% bar but NOT the new -3.0% bar
        suggestion = evaluate_exit(
            self._holding(pct_from_50dma=-2.5, theme_support=0.65),
            strategy_type="momentum",
        )

        self.assertEqual(suggestion.action, "HOLD")
        self.assertNotIn("trend_break", suggestion.triggers)

    def test_compounder_soft_break_with_intact_theme_does_not_exit(self):
        # -6% from 200dma (triggers soft-break threshold) but theme_support=0.60 >= 0.55
        # → soft-break condition fails → no trend_break
        suggestion = evaluate_exit(
            self._holding(pct_from_200dma=-6.0, theme_support=0.60),
            strategy_type="compounder",
        )

        self.assertNotIn("trend_break", suggestion.triggers)
        self.assertEqual(suggestion.action, "HOLD")

    def test_percentage_format_return_triggers_profit_protection(self):
        # unrealized_return=18.0 looks like percentage (18 > 2.0 threshold)
        # → divided by 100 → 0.18, which is >= profit_protect_momentum (0.12)
        suggestion = evaluate_exit(
            self._holding(
                pct_from_50dma=0.5,
                theme_support=0.55,
                signal_score=0.45,
                confidence_score=0.55,
                unrealized_return=18.0,
            ),
            strategy_type="momentum",
        )
        self.assertIn("profit_protection", suggestion.triggers)

    def test_rotation_gap_below_new_threshold_does_not_fire(self):
        # signal=0.75, confidence=0.82 → strength=61.5; opportunity=80 → gap=18.5
        # Old threshold (18): would have fired.  New threshold (25): must NOT fire.
        suggestion = evaluate_exit(
            self._holding(signal_score=0.75, confidence_score=0.82, theme_support=0.60),
            strategy_type="compounder",
            stronger_opportunity=self._opportunity(score=80.0),
        )
        self.assertNotIn("opportunity_rotation", suggestion.triggers)

    def test_rotation_gap_above_new_threshold_fires(self):
        # signal=0.75, confidence=0.82 → strength=61.5; opportunity=87 → gap=25.5 → fires
        suggestion = evaluate_exit(
            self._holding(signal_score=0.75, confidence_score=0.82, theme_support=0.60),
            strategy_type="compounder",
            stronger_opportunity=self._opportunity(score=87.0),
        )
        self.assertIn("opportunity_rotation", suggestion.triggers)

    def test_return_at_boundary_200pct_treated_as_decimal(self):
        # value=1.8 is <= 2.0 → kept as decimal (180% return) → does NOT divide by 100
        # 1.8 >= profit_protect_momentum (0.12) and strength < 60 → profit_protection fires
        suggestion = evaluate_exit(
            self._holding(
                pct_from_50dma=0.5,
                theme_support=0.55,
                signal_score=0.45,
                confidence_score=0.55,
                unrealized_return=1.8,
            ),
            strategy_type="momentum",
        )
        self.assertIn("profit_protection", suggestion.triggers)

    def test_theme_support_035_no_longer_triggers_thesis_weakening(self):
        # theme_support=0.35 is above the new floor (0.30) → thesis_weakening must NOT fire.
        # (Pre-fix floor was 0.40, so 0.35 would have triggered a false exit.)
        suggestion = evaluate_exit(
            self._holding(theme_support=0.35),
            strategy_type="momentum",
        )
        self.assertNotIn("thesis_weakening", suggestion.triggers)
        self.assertEqual(suggestion.action, "HOLD")

    def test_theme_support_029_still_triggers_thesis_weakening(self):
        # theme_support=0.29 is below the new floor (0.30) → thesis_weakening must fire.
        suggestion = evaluate_exit(
            self._holding(theme_support=0.29),
            strategy_type="compounder",
        )
        self.assertIn("thesis_weakening", suggestion.triggers)
        self.assertEqual(suggestion.action, "SELL")

    def test_isolated_breakout_with_low_theme_support_holds(self):
        # A freshly entered breakout position in a small universe gets theme_support≈0.35
        # (per-symbol-only score after fix to compute_theme_support).
        # With the corrected floor (0.30), thesis_weakening must NOT fire.
        suggestion = evaluate_exit(
            {
                "symbol": "BRKOUT",
                "pct_from_50dma": 3.0,
                "pct_from_200dma": 5.0,
                "theme_support": 0.35,
                "signal_score": 0.80,
                "confidence_score": 0.75,
                "unrealized_return": 0.02,
            },
            strategy_type="momentum",
        )
        self.assertNotIn("thesis_weakening", suggestion.triggers)
        self.assertEqual(suggestion.action, "HOLD")


class TestMomentumUrgency(unittest.TestCase):
    """Profit protection tightens when the gain arrived fast or in volatile conditions."""

    def _holding(self, **overrides):
        payload = {
            "symbol": "FAST",
            "pct_from_50dma": 2.0,
            "pct_from_200dma": 4.0,
            "theme_support": 0.55,
            "signal_score": 0.45,
            "confidence_score": 0.55,
            "unrealized_return": 0.10,
        }
        payload.update(overrides)
        return payload

    def test_high_urgency_fires_below_base_threshold(self):
        # urgency=1.0 → adjusted_pp = 0.12 × (1 − 1.0 × 0.40) = 0.072
        # 0.10 >= 0.072 → profit_protection fires even though return < 12% base
        suggestion = evaluate_exit(
            self._holding(day_range_pct=8.0, pct_change_1d=4.0),
            strategy_type="momentum",
        )
        self.assertIn("profit_protection", suggestion.triggers)

    def test_low_urgency_does_not_fire_below_base_threshold(self):
        # urgency ≈ 0.13 → adjusted_pp ≈ 0.114 → 0.10 < 0.114 → no fire
        suggestion = evaluate_exit(
            self._holding(day_range_pct=1.5, pct_change_1d=0.5),
            strategy_type="momentum",
        )
        self.assertNotIn("profit_protection", suggestion.triggers)

    def test_missing_urgency_data_falls_back_to_base_threshold(self):
        # no day_range_pct / pct_change_1d → urgency=0.0 → threshold=0.12
        # 0.10 < 0.12 → no fire
        suggestion = evaluate_exit(
            self._holding(),
            strategy_type="momentum",
        )
        self.assertNotIn("profit_protection", suggestion.triggers)

    def test_base_threshold_still_fires_without_urgency_data(self):
        # return=18% with no volatility data → urgency=0 → threshold=12% → fires
        suggestion = evaluate_exit(
            self._holding(unrealized_return=0.18),
            strategy_type="momentum",
        )
        self.assertIn("profit_protection", suggestion.triggers)

    def test_compounders_less_sensitive_to_urgency(self):
        # return=15%, urgency=1.0
        # momentum:   0.12 × (1 − 1.0 × 0.40) = 0.072 → 0.15 >= 0.072 → fires
        # compounder: 0.25 × (1 − 1.0 × 0.20) = 0.20  → 0.15 < 0.20  → does NOT fire
        fast_holding = self._holding(unrealized_return=0.15, day_range_pct=8.0, pct_change_1d=4.0)
        momentum_sug = evaluate_exit(fast_holding, strategy_type="momentum")
        compounder_sug = evaluate_exit(fast_holding, strategy_type="compounder")

        self.assertIn("profit_protection", momentum_sug.triggers)
        self.assertNotIn("profit_protection", compounder_sug.triggers)

    def test_high_urgency_reason_mentions_urgency(self):
        # urgency >= 0.6 → reason string should include "urgency"
        suggestion = evaluate_exit(
            self._holding(day_range_pct=8.0, pct_change_1d=4.0),
            strategy_type="momentum",
        )
        self.assertIn("profit_protection", suggestion.triggers)
        self.assertTrue(any("urgency" in r.lower() for r in suggestion.reasons))

    def test_urgency_sensitivity_configurable_to_zero(self):
        # urgency_sensitivity_momentum=0.0 disables urgency compression
        # return=10%, high urgency → threshold stays at 12% → no fire
        suggestion = evaluate_exit(
            self._holding(day_range_pct=8.0, pct_change_1d=4.0),
            strategy_type="momentum",
            config={"urgency_sensitivity_momentum": 0.0},
        )
        self.assertNotIn("profit_protection", suggestion.triggers)

    def test_compounder_high_urgency_large_gain_fires(self):
        # return=22%, urgency=1.0 → adjusted_pp = 0.25 × 0.80 = 0.20 → 0.22 >= 0.20 → fires
        suggestion = evaluate_exit(
            self._holding(unrealized_return=0.22, day_range_pct=8.0, pct_change_1d=4.0),
            strategy_type="compounder",
        )
        self.assertIn("profit_protection", suggestion.triggers)


class TestRotationExplainability(unittest.TestCase):
    """Rotation decisions are compared on a common 0–100 composite scale.
    rotation_detail exposes all values needed to diagnose why rotation fired or held."""

    def _holding(self, **overrides):
        payload = {
            "symbol": "HOLD",
            "pct_from_50dma": 1.0,
            "pct_from_200dma": 4.0,
            "theme_support": 0.70,
            "signal_score": 0.80,
            "confidence_score": 0.78,
            "unrealized_return": 0.05,
        }
        payload.update(overrides)
        return payload

    def _opp(self, score):
        return {"symbol": "CHAL", "score": score}

    # ── A: No rotation on small advantage ────────────────────────────────────

    def test_momentum_no_rotation_on_small_margin(self):
        # strength = 0.90 × 0.84 × 100 = 75.6; challenger = 86; gap = 10.4 < 12
        suggestion = evaluate_exit(
            self._holding(signal_score=0.90, confidence_score=0.84),
            strategy_type="momentum",
            stronger_opportunity=self._opp(86.0),
        )
        self.assertNotIn("opportunity_rotation", suggestion.triggers)
        self.assertFalse(suggestion.rotation_detail["rotation_triggered"])

    # ── B: Rotation on clear advantage ───────────────────────────────────────

    def test_momentum_rotation_on_clear_advantage(self):
        # strength = 0.80 × 0.75 × 100 = 60.0; challenger = 75; gap = 15 >= 12
        suggestion = evaluate_exit(
            self._holding(signal_score=0.80, confidence_score=0.75),
            strategy_type="momentum",
            stronger_opportunity=self._opp(75.0),
        )
        self.assertIn("opportunity_rotation", suggestion.triggers)
        self.assertEqual(suggestion.action, "SELL")
        self.assertTrue(suggestion.rotation_detail["rotation_triggered"])

    # ── C: Decimal challenger score normalises to 0–100 ──────────────────────

    def test_decimal_challenger_score_normalises_to_100_scale(self):
        # challenger score=0.75 → normalize_score(0.75) = 75.0 (×100 because in [-1,1])
        # incumbent strength = 0.80 × 0.75 × 100 = 60.0; gap = 15 >= 12 → fires
        # If normalize_score were skipped, 0.75 − 60 = −59.25 → would never fire.
        suggestion = evaluate_exit(
            self._holding(signal_score=0.80, confidence_score=0.75),
            strategy_type="momentum",
            stronger_opportunity=self._opp(0.75),
        )
        self.assertIn("opportunity_rotation", suggestion.triggers)
        self.assertAlmostEqual(suggestion.rotation_detail["challenger_score"], 75.0, places=1)
        self.assertAlmostEqual(suggestion.rotation_detail["actual_margin"], 15.0, places=1)

    # ── D: Established holding stickiness (compounder) ───────────────────────

    def test_compounder_high_quality_incumbent_holds_against_modest_challenger(self):
        # strength = 0.90 × 0.92 × 100 = 82.8; challenger = 95; gap = 12.2 < 25
        suggestion = evaluate_exit(
            self._holding(signal_score=0.90, confidence_score=0.92),
            strategy_type="compounder",
            stronger_opportunity=self._opp(95.0),
        )
        self.assertNotIn("opportunity_rotation", suggestion.triggers)
        self.assertFalse(suggestion.rotation_detail["rotation_triggered"])
        self.assertAlmostEqual(suggestion.rotation_detail["actual_margin"], 12.2, places=1)
        self.assertEqual(suggestion.rotation_detail["required_margin"], 25.0)

    # ── E: Fresh breakout challenger ─────────────────────────────────────────

    def test_fresh_breakout_challenger_insufficient_margin_no_rotation(self):
        # incumbent = 0.82 × 0.80 × 100 = 65.6; challenger = 73; gap = 7.4 < 12
        suggestion = evaluate_exit(
            self._holding(signal_score=0.82, confidence_score=0.80),
            strategy_type="momentum",
            stronger_opportunity=self._opp(73.0),
        )
        self.assertNotIn("opportunity_rotation", suggestion.triggers)
        self.assertLess(
            suggestion.rotation_detail["actual_margin"],
            suggestion.rotation_detail["required_margin"],
        )

    def test_fresh_breakout_challenger_sufficient_margin_rotates(self):
        # incumbent = 0.82 × 0.80 × 100 = 65.6; challenger = 82; gap = 16.4 >= 12
        suggestion = evaluate_exit(
            self._holding(signal_score=0.82, confidence_score=0.80),
            strategy_type="momentum",
            stronger_opportunity=self._opp(82.0),
        )
        self.assertIn("opportunity_rotation", suggestion.triggers)
        self.assertGreaterEqual(
            suggestion.rotation_detail["actual_margin"],
            suggestion.rotation_detail["required_margin"],
        )

    # ── Boundary: exact threshold fires (>=, not >) ───────────────────────────

    def test_momentum_rotation_at_exact_threshold_fires(self):
        # strength = 0.80 × 0.75 × 100 = 60.0; challenger = 72.0; gap = 12.0 = 12.0
        suggestion = evaluate_exit(
            self._holding(signal_score=0.80, confidence_score=0.75),
            strategy_type="momentum",
            stronger_opportunity=self._opp(72.0),
        )
        self.assertIn("opportunity_rotation", suggestion.triggers)
        self.assertAlmostEqual(suggestion.rotation_detail["actual_margin"], 12.0, places=1)

    # ── Explainability: all required keys present ─────────────────────────────

    def test_rotation_detail_fields_present_when_challenger_provided(self):
        suggestion = evaluate_exit(
            self._holding(signal_score=0.80, confidence_score=0.75),
            strategy_type="momentum",
            stronger_opportunity=self._opp(75.0),
        )
        required_keys = {
            "incumbent_score", "challenger_score", "actual_margin",
            "required_margin", "rotation_triggered", "score_basis",
        }
        self.assertTrue(required_keys.issubset(suggestion.rotation_detail.keys()))
        self.assertEqual(suggestion.rotation_detail["score_basis"], "composite_0_to_100")

    def test_rotation_detail_populated_even_when_not_triggered(self):
        # Challenger exists but gap is below threshold — detail still populated for diagnosis.
        suggestion = evaluate_exit(
            self._holding(signal_score=0.90, confidence_score=0.84),
            strategy_type="momentum",
            stronger_opportunity=self._opp(86.0),
        )
        self.assertIn("incumbent_score", suggestion.rotation_detail)
        self.assertFalse(suggestion.rotation_detail["rotation_triggered"])

    def test_no_challenger_rotation_detail_is_empty(self):
        suggestion = evaluate_exit(
            self._holding(),
            strategy_type="momentum",
        )
        self.assertEqual(suggestion.rotation_detail, {})

    def test_to_dict_includes_rotation_detail(self):
        suggestion = evaluate_exit(
            self._holding(signal_score=0.80, confidence_score=0.75),
            strategy_type="momentum",
            stronger_opportunity=self._opp(75.0),
        )
        d = suggestion.to_dict()
        self.assertIn("rotation_detail", d)
        self.assertTrue(d["rotation_detail"]["rotation_triggered"])

    def test_reason_string_includes_scores_and_margin(self):
        suggestion = evaluate_exit(
            self._holding(signal_score=0.80, confidence_score=0.75),
            strategy_type="momentum",
            stronger_opportunity=self._opp(75.0),
        )
        rotation_reasons = [r for r in suggestion.reasons if "rotation bar" in r]
        self.assertEqual(len(rotation_reasons), 1)
        # Reason must include both scores and the margin so operators can audit.
        self.assertIn("75.0", rotation_reasons[0])
        self.assertIn("60.0", rotation_reasons[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
