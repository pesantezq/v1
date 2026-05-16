"""
P4.1 — Outcome-driven conviction sizing.

These tests exercise the new `kelly_plan` kwarg on
`apply_conviction_layer`. When the Kelly Sizing Advisor reports a usable
fraction for the BUY decision, the band multipliers scale relative to a
nominal half-Kelly reference (0.20). When Kelly is missing or
insufficient, the layer falls back to the static 0.25 / 0.50 / 1.00
multipliers — preserving byte-identical legacy behavior.
"""
from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.conviction import apply_conviction_layer  # noqa: E402


def _scan_result_high_conviction() -> dict:
    """A scan_result whose first row reaches the high_conviction band."""
    return {
        "run_date": "2026-05-16",
        "generated_at": "2026-05-16T00:00:00",
        "data_mode": "live",
        "degraded_mode": False,
        "scan_summary": {},
        "results": [
            {
                "ticker": "NVDA",
                "signal_score": 0.90,
                "confidence_score": 0.92,
                "effective_score": 0.83,
                "priority_score": 0.80,
                "alert_tier": "high",
                "notification_status": "alerted",
                "watchlist_source": "static",
                "data_quality": "fresh",
                "historical_performance_score": 0.82,
                "signal_reliability": "strong",
            },
        ],
        "alerts": [],
    }


def _kelly_plan_ok(buy_fraction: float) -> dict:
    """Synthetic kelly_sizing_advisor plan with BUY=ok at the given fraction."""
    return {
        "observe_only": True,
        "schema_version": "1",
        "min_resolved_required": 20,
        "half_kelly": True,
        "hard_cap": 0.25,
        "summary_line": "Kelly sizing: 1/3 decision groups have sufficient data",
        "by_decision": [
            {"decision": "BUY", "status": "ok", "kelly_fraction_suggested": buy_fraction,
             "n_judgeable": 25, "hit_rate": 0.55, "avg_win_pct": 0.04, "avg_loss_pct": 0.03},
            {"decision": "SCALE", "status": "insufficient_data", "kelly_fraction_suggested": None},
            {"decision": "SELL", "status": "insufficient_data", "kelly_fraction_suggested": None},
        ],
    }


def _kelly_plan_all_insufficient() -> dict:
    return {
        "observe_only": True,
        "by_decision": [
            {"decision": "BUY", "status": "insufficient_data", "kelly_fraction_suggested": None},
            {"decision": "SCALE", "status": "insufficient_data", "kelly_fraction_suggested": None},
            {"decision": "SELL", "status": "insufficient_data", "kelly_fraction_suggested": None},
        ],
    }


class TestConvictionKellySizing(unittest.TestCase):

    # ---- Static fallback (preserves legacy behavior) ----------------------

    def test_static_multipliers_when_kelly_plan_is_none(self):
        enriched = apply_conviction_layer(_scan_result_high_conviction(), kelly_plan=None)
        self.assertAlmostEqual(enriched["results"][0]["sizing_multiplier"], 1.00)

    def test_static_multipliers_when_kelly_plan_is_empty_dict(self):
        enriched = apply_conviction_layer(_scan_result_high_conviction(), kelly_plan={})
        self.assertAlmostEqual(enriched["results"][0]["sizing_multiplier"], 1.00)

    def test_static_multipliers_when_kelly_buy_is_insufficient(self):
        enriched = apply_conviction_layer(
            _scan_result_high_conviction(), kelly_plan=_kelly_plan_all_insufficient()
        )
        self.assertAlmostEqual(enriched["results"][0]["sizing_multiplier"], 1.00)

    def test_static_multipliers_when_kelly_fraction_is_zero(self):
        enriched = apply_conviction_layer(
            _scan_result_high_conviction(), kelly_plan=_kelly_plan_ok(0.0)
        )
        # Kelly says "don't bet" → fall back to legacy multipliers, not zero
        # (zeroing here would silently kill the portfolio; static fallback is safer)
        self.assertAlmostEqual(enriched["results"][0]["sizing_multiplier"], 1.00)

    def test_static_multipliers_when_kelly_plan_malformed(self):
        # No "by_decision" key
        enriched = apply_conviction_layer(
            _scan_result_high_conviction(), kelly_plan={"observe_only": True}
        )
        self.assertAlmostEqual(enriched["results"][0]["sizing_multiplier"], 1.00)

    # ---- Outcome-driven path -----------------------------------------------

    def test_outcome_driven_kelly_at_nominal_matches_static(self):
        # Kelly fraction equal to NOMINAL (0.20) → scaling=1.0 → static-equivalent
        enriched = apply_conviction_layer(
            _scan_result_high_conviction(), kelly_plan=_kelly_plan_ok(0.20)
        )
        self.assertAlmostEqual(enriched["results"][0]["sizing_multiplier"], 1.00)

    def test_outcome_driven_kelly_below_nominal_shrinks_multiplier(self):
        # Kelly fraction 0.10 → scaling=0.5
        enriched = apply_conviction_layer(
            _scan_result_high_conviction(), kelly_plan=_kelly_plan_ok(0.10)
        )
        self.assertAlmostEqual(enriched["results"][0]["sizing_multiplier"], 0.50)

    def test_outcome_driven_kelly_above_nominal_grows_multiplier(self):
        # Kelly fraction 0.30 → scaling=1.5 (at upper clamp)
        enriched = apply_conviction_layer(
            _scan_result_high_conviction(), kelly_plan=_kelly_plan_ok(0.30)
        )
        self.assertAlmostEqual(enriched["results"][0]["sizing_multiplier"], 1.50)

    def test_outcome_driven_scaling_clamped_low(self):
        # Kelly fraction 0.02 → would be scaling=0.10, clamp to 0.50
        enriched = apply_conviction_layer(
            _scan_result_high_conviction(), kelly_plan=_kelly_plan_ok(0.02)
        )
        self.assertAlmostEqual(enriched["results"][0]["sizing_multiplier"], 0.50)

    def test_outcome_driven_scaling_clamped_high(self):
        # Kelly fraction 0.50 (above hard_cap, but pretend) → scaling=2.5, clamp 1.5
        enriched = apply_conviction_layer(
            _scan_result_high_conviction(), kelly_plan=_kelly_plan_ok(0.50)
        )
        self.assertAlmostEqual(enriched["results"][0]["sizing_multiplier"], 1.50)

    # ---- Cross-band scaling (starter / normal / high_conviction) -----------

    def _scan_result_three_bands(self) -> dict:
        # Build three rows whose conviction_score lands one in each band:
        # starter, normal, high_conviction. Tweak inputs to fall in each band.
        result = {
            "run_date": "2026-05-16",
            "generated_at": "2026-05-16T00:00:00",
            "data_mode": "live",
            "degraded_mode": False,
            "scan_summary": {},
            "results": [
                {  # starter band
                    "ticker": "STARTER_X",
                    "signal_score": 0.45,
                    "confidence_score": 0.55,
                    "effective_score": 0.40,
                    "historical_performance_score": 0.50,
                    "signal_reliability": "mixed",
                },
                {  # normal band
                    "ticker": "NORMAL_X",
                    "signal_score": 0.65,
                    "confidence_score": 0.75,
                    "effective_score": 0.60,
                    "historical_performance_score": 0.60,
                    "signal_reliability": "mixed",
                },
                {  # high_conviction band
                    "ticker": "HIGH_X",
                    "signal_score": 0.90,
                    "confidence_score": 0.92,
                    "effective_score": 0.83,
                    "historical_performance_score": 0.82,
                    "signal_reliability": "strong",
                },
            ],
            "alerts": [],
        }
        return result

    def test_outcome_driven_scales_all_bands_proportionally(self):
        # Kelly fraction 0.10 → scaling=0.5 → all bands halved vs static.
        enriched = apply_conviction_layer(
            self._scan_result_three_bands(), kelly_plan=_kelly_plan_ok(0.10)
        )
        rows = {row["ticker"]: row for row in enriched["results"]}
        bands_seen = {row["ticker"]: row["conviction_band"] for row in enriched["results"]}
        # Sanity: bands assigned correctly first
        self.assertEqual(bands_seen["STARTER_X"], "starter")
        self.assertEqual(bands_seen["NORMAL_X"], "normal")
        self.assertEqual(bands_seen["HIGH_X"], "high_conviction")
        # Static would be 0.25 / 0.50 / 1.00; scaled 0.5x = 0.125 / 0.25 / 0.50
        self.assertAlmostEqual(rows["STARTER_X"]["sizing_multiplier"], 0.125)
        self.assertAlmostEqual(rows["NORMAL_X"]["sizing_multiplier"], 0.25)
        self.assertAlmostEqual(rows["HIGH_X"]["sizing_multiplier"], 0.50)

    # ---- Metadata / observability ------------------------------------------

    def test_kelly_metadata_on_row_when_outcome_driven(self):
        enriched = apply_conviction_layer(
            _scan_result_high_conviction(), kelly_plan=_kelly_plan_ok(0.15)
        )
        row = enriched["results"][0]
        self.assertEqual(row["conviction_inputs"]["kelly_sizing_source"], "outcome-driven")
        self.assertAlmostEqual(row["conviction_inputs"]["kelly_fraction_buy"], 0.15)
        # 0.15 / 0.20 = 0.75, in [0.5, 1.5] so no clamp
        self.assertAlmostEqual(row["conviction_inputs"]["kelly_scaling"], 0.75)

    def test_kelly_metadata_on_row_when_static_fallback(self):
        enriched = apply_conviction_layer(_scan_result_high_conviction(), kelly_plan=None)
        row = enriched["results"][0]
        self.assertEqual(row["conviction_inputs"]["kelly_sizing_source"], "static-fallback")
        self.assertIsNone(row["conviction_inputs"]["kelly_fraction_buy"])
        self.assertAlmostEqual(row["conviction_inputs"]["kelly_scaling"], 1.0)

    def test_kelly_envelope_section_present(self):
        enriched = apply_conviction_layer(
            _scan_result_high_conviction(), kelly_plan=_kelly_plan_ok(0.15)
        )
        kelly_meta = enriched["conviction"]["kelly_sizing"]
        self.assertEqual(kelly_meta["source"], "outcome-driven")
        self.assertAlmostEqual(kelly_meta["kelly_fraction_buy"], 0.15)
        self.assertAlmostEqual(kelly_meta["scaling"], 0.75)
        self.assertEqual(kelly_meta["nominal_kelly_reference"], 0.20)
        self.assertEqual(kelly_meta["scaling_clamp"], [0.5, 1.5])

    def test_kelly_envelope_section_static_when_no_plan(self):
        enriched = apply_conviction_layer(_scan_result_high_conviction(), kelly_plan=None)
        kelly_meta = enriched["conviction"]["kelly_sizing"]
        self.assertEqual(kelly_meta["source"], "static-fallback")
        self.assertIsNone(kelly_meta["kelly_fraction_buy"])

    def test_sizing_reason_mentions_kelly_when_outcome_driven(self):
        enriched = apply_conviction_layer(
            _scan_result_high_conviction(), kelly_plan=_kelly_plan_ok(0.15)
        )
        self.assertIn("kelly_scaling", enriched["results"][0]["sizing_reason"])

    # ---- Determinism / non-regression --------------------------------------

    def test_existing_call_signature_unchanged(self):
        # Calling without the new kwarg must work exactly as before.
        enriched = apply_conviction_layer(_scan_result_high_conviction())
        self.assertAlmostEqual(enriched["results"][0]["sizing_multiplier"], 1.00)
        self.assertEqual(enriched["results"][0]["conviction_band"], "high_conviction")


if __name__ == "__main__":
    unittest.main(verbosity=2)
