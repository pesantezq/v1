import csv
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.conviction import apply_conviction_layer
from watchlist_scanner.output_writers import _write_alerts_csv, _write_signals_json
from watchlist_scanner.postprocess import _apply_output_ordering


class TestWatchlistConviction(unittest.TestCase):

    def _scan_result(self) -> dict:
        base = {
            "run_date": "2026-04-14",
            "generated_at": "2026-04-14T12:00:00",
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
                {
                    "ticker": "TSLA",
                    "signal_score": 0.70,
                    "confidence_score": 0.62,
                    "effective_score": 0.43,
                    "priority_score": 0.60,
                    "alert_tier": "medium",
                    "notification_status": "alerted",
                    "watchlist_source": "static",
                    "data_quality": "fresh",
                    "historical_performance_score": 0.30,
                    "signal_reliability": "weak",
                },
            ],
        }
        base["alerts"] = [deepcopy(base["results"][0]), deepcopy(base["results"][1])]
        return base

    def test_conviction_score_calculation_is_deterministic(self):
        first = apply_conviction_layer(self._scan_result())
        second = apply_conviction_layer(self._scan_result())
        self.assertEqual(first["results"][0]["conviction_score"], second["results"][0]["conviction_score"])
        self.assertEqual(first["results"][0]["conviction_band"], second["results"][0]["conviction_band"])

    def test_degraded_mode_lowers_conviction(self):
        live = apply_conviction_layer(self._scan_result())
        degraded = self._scan_result()
        degraded["degraded_mode"] = True
        degraded["data_mode"] = "fallback"
        degraded["results"][0]["data_mode"] = "fallback"
        degraded["results"][0]["degraded_confidence_penalty"] = 0.30
        degraded["alerts"][0]["data_mode"] = "fallback"
        degraded["alerts"][0]["degraded_confidence_penalty"] = 0.30
        degraded = apply_conviction_layer(degraded)
        self.assertLess(degraded["results"][0]["conviction_score"], live["results"][0]["conviction_score"])

    def test_cooldown_caps_conviction_band(self):
        scan_result = self._scan_result()
        scan_result["results"][0]["cooldown_active"] = True
        scan_result["alerts"][0]["cooldown_active"] = True
        enriched = apply_conviction_layer(scan_result)
        self.assertEqual(enriched["results"][0]["conviction_band"], "defer")
        self.assertIn("cooldown_band_cap", enriched["results"][0]["conviction_caps_applied"])

    def test_low_reliability_reduces_conviction(self):
        enriched = apply_conviction_layer(self._scan_result())
        self.assertLess(
            enriched["results"][1]["conviction_score"],
            enriched["results"][0]["conviction_score"],
        )
        self.assertIn(
            enriched["results"][1]["conviction_band"],
            {"observe", "defer"},
        )

    def test_strong_signal_confidence_and_history_can_reach_high_conviction(self):
        enriched = apply_conviction_layer(self._scan_result())
        self.assertEqual(enriched["results"][0]["conviction_band"], "high_conviction")
        self.assertEqual(enriched["results"][0]["sizing_recommendation"], "high_conviction")
        self.assertAlmostEqual(enriched["results"][0]["sizing_multiplier"], 1.0)

    def test_output_artifacts_contain_conviction_fields(self):
        enriched = apply_conviction_layer(self._scan_result())
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            _write_signals_json(out_dir, enriched)
            _write_alerts_csv(out_dir, enriched["alerts"])
            signals = json.loads((out_dir / "watchlist_signals.json").read_text(encoding="utf-8"))
            self.assertIn("conviction_score", signals["results"][0])
            self.assertIn("target_allocation_band", signals["results"][0])
            with open(out_dir / "watchlist_alerts.csv", newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            self.assertIn("conviction_band", rows[0])
            self.assertIn("sizing_multiplier", rows[0])

    def test_conviction_does_not_change_existing_ranking_or_base_fields(self):
        original = self._scan_result()
        before = _apply_output_ordering(deepcopy(original))
        enriched = apply_conviction_layer(self._scan_result())
        after = _apply_output_ordering(enriched)
        self.assertEqual(
            [row["ticker"] for row in before["results"]],
            [row["ticker"] for row in after["results"]],
        )
        self.assertEqual(original["results"][0]["signal_score"], after["results"][0]["signal_score"])
        self.assertEqual(original["results"][0]["confidence_score"], after["results"][0]["confidence_score"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
