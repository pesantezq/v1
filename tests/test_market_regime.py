import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from market_regime import detect_market_regime
from watchlist_scanner.conviction import apply_conviction_layer
from watchlist_scanner.output_writers import _write_portfolio_snapshot_json
from watchlist_scanner.portfolio_construction import apply_portfolio_construction_layer


class TestMarketRegime(unittest.TestCase):

    def _scan_result(self) -> dict:
        return {
            "run_date": "2026-04-14",
            "generated_at": "2026-04-14T12:00:00",
            "data_mode": "live",
            "degraded_mode": False,
            "scan_summary": {},
            "results": [
                {
                    "ticker": "SPY",
                    "signal_score": 0.80,
                    "confidence_score": 0.90,
                    "effective_score": 0.72,
                    "price_change_pct": 1.8,
                    "above_sma20": True,
                    "above_sma50": True,
                    "themes": ["Broad Market"],
                    "historical_performance_score": 0.75,
                    "signal_reliability": "strong",
                    "fundamentals": {"sector": "Index", "market_cap": 500_000_000_000},
                },
                {
                    "ticker": "NVDA",
                    "signal_score": 0.92,
                    "confidence_score": 0.93,
                    "effective_score": 0.86,
                    "price_change_pct": 2.4,
                    "above_sma20": True,
                    "above_sma50": True,
                    "themes": ["AI"],
                    "historical_performance_score": 0.82,
                    "signal_reliability": "strong",
                    "fundamentals": {"sector": "Technology", "market_cap": 2_400_000_000_000},
                },
                {
                    "ticker": "MSFT",
                    "signal_score": 0.87,
                    "confidence_score": 0.91,
                    "effective_score": 0.79,
                    "price_change_pct": 1.2,
                    "above_sma20": True,
                    "above_sma50": True,
                    "themes": ["AI", "Cloud"],
                    "historical_performance_score": 0.80,
                    "signal_reliability": "strong",
                    "fundamentals": {"sector": "Technology", "market_cap": 2_100_000_000_000},
                },
            ],
            "alerts": [],
        }

    def test_deterministic_regime_labeling_for_mocked_inputs(self):
        regime = detect_market_regime(
            regime_inputs={
                "index_trend_state": "up",
                "breadth_sma50": 0.80,
                "breadth_sma20": 0.78,
                "avg_price_change_pct": 1.4,
                "volatility_proxy": 1.5,
                "sector_leadership_concentration": 0.35,
            }
        )
        self.assertEqual(regime["regime_label"], "risk_on")
        self.assertGreater(regime["regime_confidence"], 0.70)

    def test_graceful_behavior_when_inputs_are_missing(self):
        regime = detect_market_regime(results=[])
        self.assertIn(regime["regime_label"], {"neutral", "high_volatility", "risk_off", "risk_on"})
        self.assertLessEqual(regime["regime_confidence"], 0.60)
        self.assertEqual(regime["regime_data_quality"], "limited")

    def test_regime_fields_appear_in_portfolio_output(self):
        result = apply_portfolio_construction_layer(apply_conviction_layer(self._scan_result()))
        regime = detect_market_regime(
            results=result["results"],
            portfolio_construction=result["portfolio_construction"],
            data_health={"degraded_mode": False},
        )
        result["market_regime"] = regime
        result["portfolio_construction"]["market_regime"] = regime
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            _write_portfolio_snapshot_json(out_dir, result["portfolio_construction"])
            snapshot = json.loads((out_dir / "portfolio_snapshot.json").read_text(encoding="utf-8"))
        self.assertIn("market_regime", snapshot)
        self.assertIn("regime_label", snapshot["market_regime"])

    def test_regime_layer_does_not_change_conviction_or_normalization(self):
        base = apply_portfolio_construction_layer(apply_conviction_layer(self._scan_result()))
        before = deepcopy(base["portfolio_construction"]["rows"])
        regime = detect_market_regime(
            results=base["results"],
            portfolio_construction=base["portfolio_construction"],
            data_health={"degraded_mode": True, "degraded_reason": "fallback_watchlist"},
        )
        base["market_regime"] = regime
        base["portfolio_construction"]["market_regime"] = regime
        after = base["portfolio_construction"]["rows"]
        self.assertEqual(
            [row["normalized_allocation"] for row in before],
            [row["normalized_allocation"] for row in after],
        )
        self.assertEqual(
            [row["conviction_score"] for row in before],
            [row["conviction_score"] for row in after],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
