import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.conviction import apply_conviction_layer
from watchlist_scanner.output_writers import (
    _write_portfolio_snapshot_json,
    _write_portfolio_summary_md,
    _write_signals_json,
)
from watchlist_scanner.portfolio_construction import apply_portfolio_construction_layer
from watchlist_scanner.postprocess import _apply_output_ordering


class TestWatchlistPortfolioConstruction(unittest.TestCase):

    def _scan_result(self) -> dict:
        return {
            "run_date": "2026-04-14",
            "generated_at": "2026-04-14T12:00:00",
            "data_mode": "live",
            "degraded_mode": False,
            "scan_summary": {},
            "results": [
                {
                    "ticker": "NVDA",
                    "signal_score": 0.90,
                    "confidence_score": 0.93,
                    "effective_score": 0.84,
                    "priority_score": 0.82,
                    "alert_tier": "high",
                    "notification_status": "alerted",
                    "watchlist_source": "static",
                    "data_quality": "fresh",
                    "historical_performance_score": 0.85,
                    "signal_reliability": "strong",
                    "themes": ["AI"],
                    "fundamentals": {"sector": "Technology", "market_cap": 2_500_000_000_000},
                },
                {
                    "ticker": "MSFT",
                    "signal_score": 0.88,
                    "confidence_score": 0.91,
                    "effective_score": 0.80,
                    "priority_score": 0.78,
                    "alert_tier": "high",
                    "notification_status": "alerted",
                    "watchlist_source": "static",
                    "data_quality": "fresh",
                    "historical_performance_score": 0.80,
                    "signal_reliability": "strong",
                    "themes": ["AI", "Cloud"],
                    "fundamentals": {"sector": "Technology", "market_cap": 2_000_000_000_000},
                },
                {
                    "ticker": "XOM",
                    "signal_score": 0.71,
                    "confidence_score": 0.76,
                    "effective_score": 0.54,
                    "priority_score": 0.60,
                    "alert_tier": "medium",
                    "notification_status": "alerted",
                    "watchlist_source": "static",
                    "data_quality": "fresh",
                    "historical_performance_score": 0.58,
                    "signal_reliability": "mixed",
                    "themes": ["Energy"],
                    "fundamentals": {"sector": "Energy", "market_cap": 450_000_000_000},
                },
            ],
            "alerts": [],
        }

    def test_correct_grouping(self):
        result = apply_portfolio_construction_layer(apply_conviction_layer(self._scan_result()))
        snapshot = result["portfolio_construction"]
        sectors = {row["name"]: row for row in snapshot["groupings"]["by_sector"]}
        themes = {row["name"]: row for row in snapshot["groupings"]["by_theme"]}
        market_caps = {row["name"]: row for row in snapshot["groupings"]["by_market_cap"]}

        self.assertEqual(sectors["Technology"]["count"], 2)
        self.assertEqual(sectors["Energy"]["count"], 1)
        self.assertEqual(themes["AI"]["count"], 2)
        self.assertEqual(market_caps["mega"]["count"], 3)

    def test_correct_allocation_sums_and_normalization(self):
        result = apply_portfolio_construction_layer(
            apply_conviction_layer(self._scan_result()),
            portfolio_config={
                "max_total_allocation": 0.03,
                "max_sector_allocation": 0.02,
                "top_sector_warning_threshold": 0.30,
            },
        )
        snapshot = result["portfolio_construction"]

        self.assertLessEqual(snapshot["total_normalized_allocation"], 0.03)
        self.assertLessEqual(snapshot["allocation_by_sector"]["Technology"], 0.02)
        self.assertGreater(snapshot["capped_positions"], 0)

    def test_output_artifacts_are_written(self):
        result = apply_portfolio_construction_layer(apply_conviction_layer(self._scan_result()))
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            portfolio_dir = out_dir / "portfolio"
            portfolio_dir.mkdir(parents=True, exist_ok=True)
            _write_signals_json(out_dir, result)
            _write_portfolio_snapshot_json(portfolio_dir, result["portfolio_construction"])
            _write_portfolio_summary_md(portfolio_dir, result["portfolio_construction"])

            signals = json.loads((out_dir / "watchlist_signals.json").read_text(encoding="utf-8"))
            snapshot = json.loads((portfolio_dir / "portfolio_snapshot.json").read_text(encoding="utf-8"))
            summary = (portfolio_dir / "portfolio_summary.md").read_text(encoding="utf-8")

            self.assertIn("normalized_allocation", signals["results"][0])
            self.assertIn("summary_line", snapshot)
            self.assertIn("Portfolio Construction View", summary)

    def test_portfolio_layer_does_not_change_existing_pipeline_order_or_conviction(self):
        baseline = apply_conviction_layer(self._scan_result())
        before = _apply_output_ordering(deepcopy(baseline))
        after = apply_portfolio_construction_layer(apply_conviction_layer(self._scan_result()))
        after = _apply_output_ordering(after)

        self.assertEqual(
            [row["ticker"] for row in before["results"]],
            [row["ticker"] for row in after["results"]],
        )
        self.assertEqual(before["results"][0]["conviction_score"], after["results"][0]["conviction_score"])
        self.assertEqual(before["results"][0]["signal_score"], after["results"][0]["signal_score"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
