from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from watchlist_scanner.config_report import (
    build_config_report,
    render_config_report_markdown,
    write_config_report,
)


class TestConfigReport(unittest.TestCase):

    def _summary(self) -> dict:
        return {
            "overall_win_rate": 0.42,
            "overall_follow_through_rate": 0.63,
            "overall_avg_return_pct": -0.15,
            "by_confidence_tier": {
                "high": {"count": 40, "win_rate": 0.65, "follow_through_rate": 0.80, "avg_return_pct": 1.9},
                "medium": {"count": 30, "win_rate": 0.42, "follow_through_rate": 0.58, "avg_return_pct": 0.4},
                "low": {"count": 12, "win_rate": 0.25, "follow_through_rate": 0.33, "avg_return_pct": -0.9},
            },
            "by_priority_bucket": {
                "<0.50": {"count": 12, "win_rate": 0.25, "follow_through_rate": 0.33, "avg_return_pct": -1.1},
                "0.50-0.64": {"count": 24, "win_rate": 0.50, "follow_through_rate": 0.66, "avg_return_pct": 0.2},
            },
            "by_evidence_count": {
                "1": {"count": 18, "win_rate": 0.33, "follow_through_rate": 0.50, "avg_return_pct": -0.8},
                "2": {"count": 22, "win_rate": 0.55, "follow_through_rate": 0.70, "avg_return_pct": 0.4},
            },
        }

    def _suggestions(self) -> list[dict]:
        return [
            {
                "field": "ranking.confidence_weight",
                "current": 0.30,
                "suggested": 0.35,
                "reason": "High-tier alerts are outperforming medium-tier alerts.",
                "sample_size": 30,
            },
            {
                "field": "signals.confidence_tiers.low",
                "current": 0.50,
                "suggested": 0.53,
                "reason": "Low-tier alerts are weak.",
                "sample_size": 12,
            },
            {
                "field": "signals.min_evidence_count",
                "current": 2,
                "suggested": 3,
                "reason": "Single-evidence medium alerts are underperforming.",
                "sample_size": 22,
            },
        ]

    def test_build_config_report_ranks_recommendations(self):
        report = build_config_report(
            self._suggestions(),
            self._summary(),
            profile="growth",
            generated_at="2026-04-13T12:00:00",
        )
        self.assertEqual(report["profile"], "growth")
        self.assertEqual(report["recommendations"][0]["priority_rank"], 1)
        self.assertGreaterEqual(
            report["recommendations"][0]["priority_value"],
            report["recommendations"][1]["priority_value"],
        )
        self.assertIn(report["recommendations"][0]["confidence_level"], {"low", "medium", "high"})

    def test_render_config_report_markdown_includes_summary_and_details(self):
        report = build_config_report(
            self._suggestions(),
            self._summary(),
            profile="growth",
            generated_at="2026-04-13T12:00:00",
        )
        markdown = render_config_report_markdown(report)
        self.assertIn("# Config Calibration Report", markdown)
        self.assertIn("## Top Recommendations", markdown)
        self.assertIn("ranking.confidence_weight", markdown)
        self.assertIn("Overall win rate", markdown)

    def test_write_config_report_writes_history_artifacts(self):
        report = build_config_report(
            self._suggestions(),
            self._summary(),
            profile="growth",
            generated_at="2026-04-13T12:00:00",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_dir = Path(tmp_dir) / "config"
            (config_dir / "history").mkdir(parents=True, exist_ok=True)
            paths = write_config_report(report, config_path=config_dir)

            self.assertIsNotNone(paths)
            self.assertTrue(Path(paths["markdown_path"]).exists())
            self.assertTrue(Path(paths["json_path"]).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
