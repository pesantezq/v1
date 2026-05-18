"""
P4.4 — Regime-aware allocation.

When the Volatility Regime Advisor reports a usable sizing multiplier
(status="ok", positive non-unity multiplier), `suggest_allocation`
applies it as an aggregate scaling factor *after* the risk-off and
degraded-mode adjustments and *before* the position/sector/cash caps —
so caps still bind on the regime-adjusted size.

When the advisor is missing, insufficient, or reports multiplier=1.0,
behavior is byte-identical to legacy.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from allocation_engine import suggest_allocation  # noqa: E402


def _opportunity(**overrides) -> dict:
    payload = {
        "symbol": "AAPL",
        "score": 82.0,
        "confidence": 0.80,
        "sector": "Technology",
    }
    payload.update(overrides)
    return payload


def _vol_plan(*, status: str = "ok", multiplier=1.00, regime: str = "normal") -> dict:
    # multiplier may be None / negative for negative-path tests; keep summary_line tolerant.
    try:
        mult_str = f"{float(multiplier):.2f}"
    except (TypeError, ValueError):
        mult_str = str(multiplier)
    return {
        "observe_only": True,
        "schema_version": "1",
        "status": status,
        "regime": regime,
        "sizing_multiplier_suggested": multiplier,
        "summary_line": f"Vol regime: {regime}, suggested sizing x{mult_str}",
    }


class TestAllocationEngineVolRegime(unittest.TestCase):

    # ---- Fallback paths (legacy preserved) ---------------------------------

    def test_no_change_when_vol_regime_plan_is_none(self):
        suggestion = suggest_allocation(
            opportunity=_opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            vol_regime_plan=None,
        )
        self.assertAlmostEqual(suggestion.suggested_pct, 0.10, places=4)
        self.assertAlmostEqual(suggestion.vol_regime_multiplier, 1.00)
        self.assertEqual(suggestion.vol_regime_source, "default")

    def test_no_change_when_vol_regime_status_insufficient(self):
        suggestion = suggest_allocation(
            opportunity=_opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            vol_regime_plan=_vol_plan(status="insufficient_data", multiplier=0.50),
        )
        # Even with a non-unity multiplier in the plan, insufficient status
        # means we ignore it.
        self.assertAlmostEqual(suggestion.suggested_pct, 0.10, places=4)
        self.assertEqual(suggestion.vol_regime_source, "default")

    def test_no_change_when_multiplier_is_unity(self):
        suggestion = suggest_allocation(
            opportunity=_opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            vol_regime_plan=_vol_plan(multiplier=1.00, regime="normal"),
        )
        self.assertAlmostEqual(suggestion.suggested_pct, 0.10, places=4)
        # Source is still "advisor" because we DID consult it — just no scaling
        self.assertEqual(suggestion.vol_regime_source, "advisor")
        self.assertAlmostEqual(suggestion.vol_regime_multiplier, 1.00)

    def test_no_change_when_multiplier_invalid(self):
        for bad in (None, 0.0, -0.5):
            suggestion = suggest_allocation(
                opportunity=_opportunity(),
                strategy_type="compounder",
                portfolio_value=100_000.0,
                cash_available=20_000.0,
                vol_regime_plan=_vol_plan(multiplier=bad),
            )
            self.assertAlmostEqual(suggestion.suggested_pct, 0.10, places=4)
            self.assertEqual(suggestion.vol_regime_source, "default")

    def test_no_change_when_plan_malformed(self):
        suggestion = suggest_allocation(
            opportunity=_opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            vol_regime_plan={"observe_only": True},  # no status, no multiplier
        )
        self.assertAlmostEqual(suggestion.suggested_pct, 0.10, places=4)
        self.assertEqual(suggestion.vol_regime_source, "default")

    # ---- Outcome-driven path -----------------------------------------------

    def test_high_vol_regime_shrinks_position(self):
        # Multiplier 0.50 → compounder base 10% × 0.50 = 5% (post-retune)
        suggestion = suggest_allocation(
            opportunity=_opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            vol_regime_plan=_vol_plan(multiplier=0.50, regime="high_vol"),
        )
        self.assertAlmostEqual(suggestion.suggested_pct, 0.05, places=4)
        self.assertAlmostEqual(suggestion.vol_regime_multiplier, 0.50)
        self.assertEqual(suggestion.vol_regime_source, "advisor")

    def test_low_vol_regime_grows_position(self):
        # Multiplier 1.20 → 10% × 1.20 = 12%, under max_position_cap (15%)
        suggestion = suggest_allocation(
            opportunity=_opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            vol_regime_plan=_vol_plan(multiplier=1.20, regime="low_vol"),
        )
        self.assertAlmostEqual(suggestion.suggested_pct, 0.12, places=4)

    def test_regime_multiplier_bounded_by_position_cap(self):
        # Multiplier 2.0 → 10% × 2.0 = 20%, but max_position_cap is 15%.
        suggestion = suggest_allocation(
            opportunity=_opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            vol_regime_plan=_vol_plan(multiplier=2.0, regime="low_vol"),
        )
        self.assertAlmostEqual(suggestion.suggested_pct, 0.15, places=4)
        self.assertIn("max_position_cap", suggestion.capped_by)

    def test_regime_multiplier_combines_with_risk_off(self):
        # Risk-off compounder multiplier 0.85 + vol regime 0.50
        # 10% × 0.85 × 0.50 = 4.25% (rounded to 4 places by allocation_engine)
        suggestion = suggest_allocation(
            opportunity=_opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            context={"regime_label": "risk_off"},
            vol_regime_plan=_vol_plan(multiplier=0.50, regime="high_vol"),
        )
        self.assertAlmostEqual(suggestion.suggested_pct, 0.0425, places=4)

    def test_regime_multiplier_combines_with_degraded_mode(self):
        # Degraded 0.65 + vol regime 0.50
        # 10% × 0.65 × 0.50 = 3.25% → 0.0325 at 4 places
        suggestion = suggest_allocation(
            opportunity=_opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            context={"degraded_mode": True},
            vol_regime_plan=_vol_plan(multiplier=0.50, regime="high_vol"),
        )
        self.assertAlmostEqual(suggestion.suggested_pct, 0.0325, places=4)

    # ---- Observability -----------------------------------------------------

    def test_rationale_mentions_regime_when_applied(self):
        suggestion = suggest_allocation(
            opportunity=_opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            vol_regime_plan=_vol_plan(multiplier=0.50, regime="high_vol"),
        )
        rationale_str = " ".join(suggestion.rationale).lower()
        self.assertIn("vol regime", rationale_str)
        self.assertIn("high_vol", rationale_str)

    def test_rationale_does_not_mention_regime_when_unity(self):
        suggestion = suggest_allocation(
            opportunity=_opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            vol_regime_plan=_vol_plan(multiplier=1.00, regime="normal"),
        )
        rationale_str = " ".join(suggestion.rationale).lower()
        self.assertNotIn("vol regime", rationale_str)

    def test_to_dict_includes_vol_regime_metadata(self):
        suggestion = suggest_allocation(
            opportunity=_opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
            vol_regime_plan=_vol_plan(multiplier=0.50, regime="high_vol"),
        )
        d = suggestion.to_dict()
        self.assertEqual(d["vol_regime_source"], "advisor")
        self.assertAlmostEqual(d["vol_regime_multiplier"], 0.50)
        self.assertEqual(d["vol_regime_label"], "high_vol")

    # ---- Backward compatibility --------------------------------------------

    def test_existing_call_signature_unchanged(self):
        # Calling without vol_regime_plan must behave exactly as before.
        suggestion = suggest_allocation(
            opportunity=_opportunity(),
            strategy_type="compounder",
            portfolio_value=100_000.0,
            cash_available=20_000.0,
        )
        self.assertAlmostEqual(suggestion.suggested_pct, 0.10, places=4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
