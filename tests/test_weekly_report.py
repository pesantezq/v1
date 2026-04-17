import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.weekly_report import (
    build_weekly_summary_html,
    generate_weekly_summary,
    markdown_to_plain_text,
)


class TestWeeklyReport(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo_root = Path(__file__).parent.parent

    def tearDown(self):
        self.tmp.cleanup()

    def _write_json(self, rel_path: str, payload: dict) -> Path:
        path = self.root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def _minimal_run_summary(self) -> dict:
        return {
            "timestamp": "2026-04-16T09:39:15",
            "run_mode": "daily",
            "data_mode": "mixed",
            "degraded_mode": True,
            "degraded_reason": "fallback_watchlist",
            "data_sources_used": ["fmp", "fallback"],
            "scanner": {
                "watchlist_source": "fallback",
                "data_fallback_triggered": True,
            },
            "market_regime": {
                "regime_label": "risk_on",
                "regime_confidence": 0.84,
            },
            "scan_summary": {
                "scan_status": "complete",
            },
        }

    def _minimal_watchlist(self) -> dict:
        return {
            "data_mode": "live",
            "results": [
                {
                    "ticker": "NVDA",
                    "conviction_band": "high_conviction",
                    "conviction_score": 0.88,
                    "effective_score": 0.81,
                    "normalized_allocation": 0.03,
                    "portfolio_sector": "TECHNOLOGY",
                    "actionable_signal": True,
                }
            ],
            "portfolio_construction": {
                "summary_line": "Portfolio view available.",
                "total_suggested_allocation": 0.05,
                "total_normalized_allocation": 0.04,
                "allocation_by_sector": {"TECHNOLOGY": 0.04},
                "warnings": ["overconcentration_top_sector:TECHNOLOGY:60.0%"],
                "capped_positions": 1,
                "summary_label": "stretched",
                "rows": [
                    {
                        "ticker": "NVDA",
                        "conviction_band": "high_conviction",
                        "conviction_score": 0.88,
                        "normalized_allocation": 0.03,
                        "allocation_capped": True,
                        "allocation_cap_reason": "max_position_cap",
                    }
                ],
            },
        }

    def _minimal_policy(self) -> dict:
        return {
            "current_context": {
                "regime_label": "risk_on",
                "regime_confidence": 0.84,
                "degraded_mode": False,
                "degraded_reason": None,
            },
            "recommendation": {
                "recommended_policy": "quality_growth",
                "recommended_profile": "balanced_growth",
                "recommendation_score": 0.91,
                "recommendation_confidence": 0.43,
                "recommendation_reasoning": ["Rule-based fallback chose the best regime-aligned policy."],
                "recommendation_inputs": {"policy": {"regime_label": "risk_on"}},
                "recommendation_source": "rule_based_fallback",
                "recommendation_data_quality": "limited_regime_history",
                "recommendation_quality_note": "Confidence is limited by shallow regime history.",
            },
        }

    def _minimal_recommendation_outcomes(self) -> dict:
        return {
            "generated_at": "2026-04-16T10:30:00",
            "coverage": {
                "total_records": 24,
                "attributable_records": 18,
            },
            "coverage_by_horizon": {
                "count_1d": 18,
                "count_3d": 18,
                "count_5d": 16,
                "count_10d": 10,
            },
            "sample_quality": "moderate",
            "overall": {
                "hit_rate": 0.61,
                "strong_win_rate": 0.28,
                "adverse_rate": 0.11,
                "avg_forward_return_5d": 0.018,
            },
            "by_confidence_tier": {
                "low": {
                    "count": 5,
                    "attributable_count": 4,
                    "hit_rate": 0.25,
                    "avg_forward_return_5d": -0.01,
                },
                "medium": {
                    "count": 9,
                    "attributable_count": 8,
                    "hit_rate": 0.50,
                    "avg_forward_return_5d": 0.01,
                },
                "high": {
                    "count": 10,
                    "attributable_count": 6,
                    "hit_rate": 0.83,
                    "avg_forward_return_5d": 0.03,
                },
            },
            "confidence_calibration": {
                "notes": ["Confidence buckets with small samples: low."],
                "monotonicity": {
                    "hit_rate_monotonic": True,
                    "avg_return_5d_monotonic": True,
                    "overall": True,
                },
            },
            "by_degraded_mode": {
                "normal": {
                    "count": 12,
                    "attributable_count": 10,
                    "hit_rate": 0.7,
                    "avg_forward_return_5d": 0.02,
                },
                "degraded": {
                    "count": 6,
                    "attributable_count": 4,
                    "hit_rate": 0.25,
                    "avg_forward_return_5d": -0.01,
                    "small_sample": True,
                },
            },
            "by_action_level": {
                "Recommended": {
                    "count": 8,
                    "attributable_count": 7,
                    "hit_rate": 0.71,
                    "avg_forward_return_5d": 0.021,
                },
            },
            "by_impact_area": {
                "Portfolio Risk": {
                    "count": 7,
                    "attributable_count": 6,
                    "hit_rate": 0.67,
                    "avg_forward_return_5d": 0.018,
                },
            },
            "by_score_decile": [
                {
                    "label": "81-90",
                    "count": 5,
                    "attributable_count": 4,
                    "hit_rate": 0.75,
                    "avg_forward_return_5d": 0.026,
                },
            ],
        }

    def _minimal_regime_performance(self) -> dict:
        return {
            "generated_at": "2026-04-16T09:39:15",
            "by_regime": {
                "risk_on": {
                    "total_signals": 4,
                    "resolved_signals": 4,
                    "win_rate": 0.75,
                    "avg_return_pct": 3.4,
                    "best_conviction_band": "high_conviction",
                    "worst_conviction_band": "starter",
                    "degraded_data_impact_note": "No degraded penalty observed.",
                },
                "risk_off": {
                    "total_signals": 3,
                    "resolved_signals": 3,
                    "win_rate": 0.33,
                    "avg_return_pct": -1.2,
                    "best_conviction_band": "normal",
                    "worst_conviction_band": "observe",
                    "degraded_data_impact_note": "Fallback-heavy sample.",
                },
            },
        }

    def _write_full_artifacts(self) -> None:
        self._write_json("outputs/latest/scraped_intel_run_summary.json", self._minimal_run_summary())
        self._write_json("outputs/latest/watchlist_signals.json", self._minimal_watchlist())
        self._write_json("outputs/policy/policy_recommendation.json", self._minimal_policy())
        self._write_json("outputs/policy/recommendation_outcomes.json", self._minimal_recommendation_outcomes())
        self._write_json(
            "outputs/policy/recommendation_evaluation.json",
            {
                "confidence_calibration": {
                    "calibration_score": 1.0,
                    "tiers": {
                        "low": {"tier": "low", "count": 5},
                        "medium": {"tier": "medium", "count": 8},
                        "high": {"tier": "high", "count": 7},
                    },
                }
            },
        )
        self._write_json("outputs/regime/regime_performance.json", self._minimal_regime_performance())

    def test_generate_weekly_summary_handles_missing_artifacts(self):
        result = generate_weekly_summary(root=self.root)

        self.assertTrue(result.output_path.exists())
        self.assertIn("## Executive Summary", result.markdown)
        self.assertIn("## Signal Quality", result.markdown)
        self.assertIn("## Performance Highlights", result.markdown)
        self.assertIn("## Calibration Insights", result.markdown)
        self.assertIn("## Regime Insights", result.markdown)
        self.assertIn("## Recommendation Quality", result.markdown)
        self.assertIn("## Key Takeaways", result.markdown)
        self.assertIn("No performance analytics available.", result.markdown)

    def test_generate_weekly_summary_handles_sparse_data(self):
        self._write_json("outputs/latest/scraped_intel_run_summary.json", self._minimal_run_summary())
        self._write_json("outputs/latest/watchlist_signals.json", {"results": []})
        self._write_json("outputs/policy/recommendation_outcomes.json", {"overall": {}, "coverage_by_horizon": {}})

        result = generate_weekly_summary(root=self.root)

        self.assertIn("Current regime", result.markdown)
        self.assertIn("No regime analytics available.", result.markdown)
        self.assertIn("No recommendation quality analytics available.", result.markdown)
        self.assertNotIn("Traceback", result.markdown)

    def test_generate_weekly_summary_includes_expected_sections_with_data(self):
        self._write_full_artifacts()

        result = generate_weekly_summary(root=self.root)

        self.assertIn("quality_growth", result.markdown)
        self.assertIn("balanced_growth", result.markdown)
        self.assertIn("Higher-conviction signals", result.markdown)
        self.assertIn("Confidence monotonicity status: `monotonic`.", result.markdown)
        self.assertIn("Hit rate by degraded vs normal", result.markdown)
        self.assertEqual(result.sections[-1], "Key Takeaways")

    def test_markdown_export_helpers_are_safe(self):
        self._write_full_artifacts()

        result = generate_weekly_summary(root=self.root)
        plain_text = markdown_to_plain_text(result.markdown)
        html = build_weekly_summary_html(result.markdown)

        self.assertIn("Weekly Operator Review", plain_text)
        self.assertNotIn("None", plain_text)
        self.assertIn("<html>", html)
        self.assertIn("<h1>Weekly Operator Review</h1>", html)

    def test_weekly_summary_generation_does_not_mutate_source_artifacts(self):
        run_path = self._write_json("outputs/latest/scraped_intel_run_summary.json", self._minimal_run_summary())
        watchlist_path = self._write_json("outputs/latest/watchlist_signals.json", self._minimal_watchlist())
        before = {
            run_path: run_path.read_text(encoding="utf-8"),
            watchlist_path: watchlist_path.read_text(encoding="utf-8"),
        }

        generate_weekly_summary(root=self.root)

        after = {
            run_path: run_path.read_text(encoding="utf-8"),
            watchlist_path: watchlist_path.read_text(encoding="utf-8"),
        }
        self.assertEqual(before, after)

    def test_cli_execution_writes_report(self):
        self._write_full_artifacts()

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.weekly_report",
                "--root",
                str(self.root),
                "--output-only",
            ],
            cwd=str(self.repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            timeout=30,
        )

        output_path = self.root / "outputs" / "reports" / "weekly_summary.md"
        self.assertEqual(result.returncode, 0, msg=result.stdout)
        self.assertTrue(output_path.exists())
        self.assertIn("Weekly summary written:", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
