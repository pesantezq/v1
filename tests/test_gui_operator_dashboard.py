import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gui_operator_data import classify_freshness, load_operator_dashboard_data


class TestGuiOperatorDashboard(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_json(self, rel_path: str, payload: dict) -> Path:
        path = self.root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def _write_text(self, rel_path: str, content: str) -> Path:
        path = self.root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _minimal_run_summary(self, **overrides) -> dict:
        payload = {
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
                "regime_portfolio_fit": "stretched",
                "regime_portfolio_commentary": "Concentration warning active.",
            },
            "scan_summary": {
                "scan_status": "complete",
            },
        }
        payload.update(overrides)
        return payload

    def _minimal_watchlist(self, **overrides) -> dict:
        payload = {
            "data_mode": "live",
            "degraded_mode": False,
            "degraded_reason": None,
            "results": [
                {
                    "ticker": "NVDA",
                    "conviction_band": "high_conviction",
                    "conviction_score": 0.88,
                    "effective_score": 0.81,
                    "normalized_allocation": 0.03,
                    "portfolio_sector": "TECHNOLOGY",
                    "cooldown_active": False,
                    "degraded_confidence_penalty": 0.0,
                    "signal_reliability": "strong",
                    "actionable_signal": True,
                    "conviction_inputs": {"theme_support": 0.8},
                    "conviction_caps_applied": [],
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
                "top_sector": {"name": "TECHNOLOGY", "allocation_pct": 0.6},
                "degraded_mode_impact": {"degraded_mode": False, "data_mode": "live"},
                "groupings": {"by_sector": [{"name": "TECHNOLOGY", "count": 1}]},
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
                "market_regime": {
                    "regime_portfolio_fit": "stretched",
                    "regime_portfolio_commentary": "Concentration warning active.",
                },
            },
        }
        payload.update(overrides)
        return payload

    def _minimal_policy(self, **overrides) -> dict:
        payload = {
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
            "alternatives": {
                "policies": [{"name": "regime_aligned", "recommendation_score": 0.88}],
                "profiles": [{"name": "aggressive_growth", "recommendation_score": 0.9}],
            },
        }
        payload.update(overrides)
        return payload

    def _minimal_recommendation_outcomes(self, **overrides) -> dict:
        payload = {
            "generated_at": "2026-04-16T10:30:00",
            "coverage": {
                "total_records": 24,
                "attributable_records": 18,
                "unevaluable_records": 6,
                "coverage_rate": 0.75,
                "date_range": {"first": "2026-04-01", "last": "2026-04-16"},
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
                "avg_forward_return_1d": 0.006,
                "avg_forward_return_3d": 0.012,
                "avg_forward_return_5d": 0.018,
                "avg_forward_return_10d": 0.024,
            },
            "by_confidence_tier": {
                "low": {
                    "count": 5,
                    "attributable_count": 4,
                    "hit_rate": 0.25,
                    "avg_forward_return_5d": -0.01,
                    "median_forward_return_5d": -0.005,
                    "strong_win_rate": 0.0,
                    "adverse_rate": 0.25,
                    "small_sample": True,
                },
                "medium": {
                    "count": 9,
                    "attributable_count": 8,
                    "hit_rate": 0.50,
                    "avg_forward_return_5d": 0.01,
                    "median_forward_return_5d": 0.008,
                    "strong_win_rate": 0.13,
                    "adverse_rate": 0.12,
                    "small_sample": False,
                },
                "high": {
                    "count": 10,
                    "attributable_count": 6,
                    "hit_rate": 0.83,
                    "avg_forward_return_5d": 0.03,
                    "median_forward_return_5d": 0.022,
                    "strong_win_rate": 0.50,
                    "adverse_rate": 0.0,
                    "small_sample": False,
                },
            },
            "confidence_calibration": {
                "notes": ["Confidence buckets with small samples: low."],
                "monotonicity": {
                    "hit_rate_monotonic": True,
                    "avg_return_5d_monotonic": True,
                    "overall": True,
                    "hit_rate_checks": [
                        {"pair": "low->medium", "monotonic": True},
                        {"pair": "medium->high", "monotonic": True},
                    ],
                    "avg_return_5d_checks": [
                        {"pair": "low->medium", "monotonic": True},
                        {"pair": "medium->high", "monotonic": True},
                    ],
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
                "Monitor": {
                    "count": 6,
                    "attributable_count": 5,
                    "hit_rate": 0.4,
                    "avg_forward_return_5d": 0.003,
                },
            },
            "by_impact_area": {
                "Portfolio Risk": {
                    "count": 7,
                    "attributable_count": 6,
                    "hit_rate": 0.67,
                    "avg_forward_return_5d": 0.018,
                },
                "Cash Safety": {
                    "count": 5,
                    "attributable_count": 4,
                    "hit_rate": 0.25,
                    "avg_forward_return_5d": -0.008,
                    "small_sample": True,
                },
            },
            "by_score_decile": [
                {
                    "label": "21-30",
                    "count": 3,
                    "attributable_count": 3,
                    "hit_rate": 0.33,
                    "avg_forward_return_5d": -0.01,
                    "median_forward_return_5d": -0.008,
                    "small_sample": True,
                },
                {
                    "label": "81-90",
                    "count": 5,
                    "attributable_count": 4,
                    "hit_rate": 0.75,
                    "avg_forward_return_5d": 0.026,
                    "median_forward_return_5d": 0.022,
                },
            ],
            "data_quality_notes": ["Some buckets remain small."],
        }
        payload.update(overrides)
        return payload

    def _minimal_recommendation_evaluation(self, **overrides) -> dict:
        payload = {
            "generated_at": "2026-04-16T10:31:00",
            "total_records": 24,
            "total_runs": 6,
            "date_range": {"first": "2026-04-01", "last": "2026-04-16"},
            "hit_rate_by_mode": {
                "normal": {"total": 12, "resolved": 8, "hit_rate": 0.67},
                "degraded": {"total": 6, "resolved": 2, "hit_rate": 0.33},
            },
            "confidence_calibration": {
                "calibration_score": 1.0,
                "tiers": {
                    "low": {"tier": "low", "count": 5, "resolution_rate": 0.2},
                    "medium": {"tier": "medium", "count": 8, "resolution_rate": 0.5},
                    "high": {"tier": "high", "count": 7, "resolution_rate": 0.8},
                },
            },
        }
        payload.update(overrides)
        return payload

    def _minimal_regime_performance(self, **overrides) -> dict:
        payload = {
            "generated_at": "2026-04-16T09:39:15",
            "primary_window_days": 3,
            "resolved_signals": 7,
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
            "observability": {},
        }
        payload.update(overrides)
        return payload

    def test_loader_handles_missing_files_gracefully(self):
        bundle = load_operator_dashboard_data(self.root)

        self.assertEqual(bundle["overview"]["latest_run_status"], "missing")
        self.assertFalse(bundle["memo"]["available"])
        self.assertFalse(bundle["weekly_review"]["available"])
        self.assertFalse(bundle["strategy_view"]["available"])
        self.assertFalse(bundle["signal_triage"]["available"])
        self.assertIn("missing_artifact", bundle["strategy_view"]["source"])
        self.assertEqual(bundle["overview"]["freshness_strip"][0]["freshness_status"], "missing")
        self.assertFalse(bundle["performance_view"]["available"])
        self.assertFalse(bundle["regime_analytics_view"]["available"])

    def test_freshness_classification_logic(self):
        now = datetime(2026, 4, 16, 12, 0, 0)

        self.assertEqual(classify_freshness(None, now), "missing")
        self.assertEqual(classify_freshness(datetime(2026, 4, 16, 10, 0, 0), now), "fresh")
        self.assertEqual(classify_freshness(datetime(2026, 4, 16, 2, 0, 0), now), "stale")
        self.assertEqual(classify_freshness(datetime(2026, 4, 15, 6, 0, 0), now), "old")

    def test_loader_normalizes_sparse_schema_safely(self):
        self._write_json(
            "outputs/latest/scraped_intel_run_summary.json",
            {
                "mode": "weekly",
                "generated_at": "2026-04-16T08:00:00",
                "data_mode": "fallback",
                "degraded_mode": False,
                "market_regime": {"regime_label": "neutral", "regime_confidence": 0.55},
            },
        )
        self._write_json(
            "outputs/latest/watchlist_signals.json",
            {
                "signals": [
                    {
                        "ticker": "MSFT",
                        "conviction_band": "starter",
                        "conviction_score": 0.52,
                        "effective_score": 0.41,
                        "normalized_allocation": 0.01,
                        "sector": "TECHNOLOGY",
                        "actionable_signal": True,
                    }
                ],
                "portfolio_construction": {
                    "summary_line": "Fallback portfolio snapshot.",
                    "total_normalized_allocation": 0.01,
                    "allocation_by_sector": {"TECHNOLOGY": 0.01},
                    "rows": [],
                },
            },
        )

        bundle = load_operator_dashboard_data(self.root)

        self.assertEqual(bundle["overview"]["run_mode"], "weekly")
        self.assertEqual(bundle["signal_triage"]["rows"][0]["ticker"], "MSFT")
        self.assertEqual(bundle["portfolio_view"]["summary_line"], "Fallback portfolio snapshot.")
        self.assertEqual(bundle["overview"]["market_regime"], "neutral")

    def test_missing_timestamps_fall_back_to_file_metadata(self):
        self._write_json(
            "outputs/latest/scraped_intel_run_summary.json",
            {
                "run_mode": "daily",
                "data_mode": "live",
                "scan_summary": {"scan_status": "complete"},
            },
        )
        self._write_json(
            "outputs/latest/watchlist_signals.json",
            {"results": []},
        )

        bundle = load_operator_dashboard_data(self.root)
        run_status = bundle["artifact_statuses"]["run_summary"]
        signals_status = bundle["artifact_statuses"]["watchlist_signals"]

        self.assertEqual(run_status["updated_source"], "file")
        self.assertNotEqual(run_status["updated_display"], "Unknown")
        self.assertEqual(signals_status["updated_source"], "file")
        self.assertIn(signals_status["freshness_status"], {"fresh", "stale", "old"})

    def test_dashboard_data_model_combines_primary_artifacts(self):
        self._write_json("outputs/latest/scraped_intel_run_summary.json", self._minimal_run_summary())
        self._write_json("outputs/latest/watchlist_signals.json", self._minimal_watchlist())
        self._write_json("outputs/portfolio/portfolio_snapshot.json", self._minimal_watchlist()["portfolio_construction"])
        self._write_json("outputs/policy/policy_recommendation.json", self._minimal_policy())
        self._write_json("outputs/policy/recommendation_outcomes.json", self._minimal_recommendation_outcomes())
        self._write_json("outputs/policy/recommendation_evaluation.json", self._minimal_recommendation_evaluation())
        self._write_json("outputs/regime/regime_performance.json", self._minimal_regime_performance())
        self._write_json(
            "outputs/latest/agent_llm_metadata.json",
            {
                "generated_at": "2026-04-16T09:40:00",
                "tasks": [
                    {
                        "completed_at": "2026-04-16T09:40:00",
                        "resolved_provider": "ollama",
                        "actual_provider": "offline_stub",
                        "actual_model": "(offline)",
                        "llm_fallback_triggered": True,
                        "data_fallback_triggered": True,
                    }
                ],
            },
        )
        self._write_text("outputs/latest/monthly_memo.md", "# Memo\n\nOperator note.")

        bundle = load_operator_dashboard_data(self.root)

        self.assertEqual(bundle["overview"]["policy"], "quality_growth")
        self.assertEqual(bundle["overview"]["profile"], "balanced_growth")
        self.assertTrue(bundle["run_status"]["fallback_occurred"])
        self.assertEqual(bundle["portfolio_view"]["capped_positions"], 1)
        self.assertTrue(bundle["memo"]["available"])
        self.assertEqual(bundle["signal_triage"]["rows"][0]["ticker"], "NVDA")
        self.assertTrue(bundle["performance_view"]["available"])
        self.assertTrue(bundle["regime_analytics_view"]["available"])
        self.assertEqual(bundle["recommendation_quality_view"]["monotonicity_label"], "monotonic")

    def test_loader_surfaces_weekly_review_when_present(self):
        self._write_text(
            "outputs/reports/weekly_summary.md",
            "# Weekly Operator Review\n\n## Executive Summary\n\n- Example.\n",
        )

        bundle = load_operator_dashboard_data(self.root)

        self.assertTrue(bundle["weekly_review"]["available"])
        self.assertEqual(bundle["weekly_review"]["output_target"]["scope"], "Reports")
        self.assertIn("Weekly Operator Review", bundle["weekly_review"]["markdown"])

    def test_recommendation_section_handles_missing_artifact(self):
        self._write_json("outputs/latest/scraped_intel_run_summary.json", self._minimal_run_summary())
        self._write_json("outputs/latest/watchlist_signals.json", self._minimal_watchlist())

        bundle = load_operator_dashboard_data(self.root)

        self.assertFalse(bundle["strategy_view"]["available"])
        self.assertEqual(bundle["strategy_view"]["recommended_policy"], "Unavailable")
        self.assertEqual(bundle["strategy_view"]["data_quality"], "missing")

    def test_memo_panel_handles_missing_memo_gracefully(self):
        self._write_json("outputs/latest/scraped_intel_run_summary.json", self._minimal_run_summary())
        bundle = load_operator_dashboard_data(self.root)

        self.assertFalse(bundle["memo"]["available"])
        self.assertIn("No memo artifact found", bundle["memo"]["simple_markdown"])

    def test_memo_sections_are_safe_when_expected_sections_are_missing(self):
        self._write_json("outputs/latest/scraped_intel_run_summary.json", self._minimal_run_summary())
        self._write_text(
            "outputs/latest/monthly_memo.md",
            "# Daily Note\n\nThis memo only covers a general note.\n\n## Closing\n\nNothing else today.",
        )

        bundle = load_operator_dashboard_data(self.root)
        memo = bundle["memo"]
        missing_sections = {section["key"]: section for section in memo["sections"]}

        self.assertTrue(memo["available"])
        self.assertIn("signals", missing_sections)
        self.assertFalse(missing_sections["signals"]["found"])
        self.assertIn("No signals section found", missing_sections["signals"]["content"])
        self.assertFalse(missing_sections["recommendation"]["found"])

    def test_loader_is_read_only_against_artifacts(self):
        run_path = self._write_json("outputs/latest/scraped_intel_run_summary.json", self._minimal_run_summary())
        watchlist_path = self._write_json("outputs/latest/watchlist_signals.json", self._minimal_watchlist())
        before = {
            run_path: run_path.read_text(encoding="utf-8"),
            watchlist_path: watchlist_path.read_text(encoding="utf-8"),
        }

        load_operator_dashboard_data(self.root)

        after = {
            run_path: run_path.read_text(encoding="utf-8"),
            watchlist_path: watchlist_path.read_text(encoding="utf-8"),
        }
        self.assertEqual(before, after)

    def test_empty_analytics_datasets_stay_safe(self):
        self._write_json("outputs/policy/recommendation_outcomes.json", {})
        self._write_json("outputs/policy/recommendation_evaluation.json", {})
        self._write_json("outputs/regime/regime_performance.json", {"by_regime": {}})

        bundle = load_operator_dashboard_data(self.root)

        self.assertFalse(bundle["performance_view"]["available"])
        self.assertEqual(bundle["performance_view"]["calibration_rows"], [])
        self.assertFalse(bundle["regime_analytics_view"]["available"])
        self.assertEqual(bundle["recommendation_quality_view"]["by_score_decile"], [])

    def test_analytics_grouping_logic_uses_existing_buckets(self):
        self._write_json("outputs/policy/recommendation_outcomes.json", self._minimal_recommendation_outcomes())
        self._write_json("outputs/policy/recommendation_evaluation.json", self._minimal_recommendation_evaluation())
        self._write_json("outputs/regime/regime_performance.json", self._minimal_regime_performance())

        bundle = load_operator_dashboard_data(self.root)

        performance = bundle["performance_view"]
        regime = bundle["regime_analytics_view"]
        quality = bundle["recommendation_quality_view"]

        self.assertEqual([row["bucket"] for row in performance["calibration_rows"]], ["low", "medium", "high"])
        self.assertEqual(performance["coverage_rows"][2]["count"], 16)
        self.assertEqual(regime["rows"][0]["regime"], "risk_off")
        self.assertEqual(regime["rows"][1]["regime"], "risk_on")
        self.assertEqual(quality["by_degraded_mode"][0]["bucket"], "normal")
        self.assertEqual(quality["by_score_decile"][1]["bucket"], "81-90")

    def test_existing_gui_includes_analytics_tabs(self):
        app_source = (Path(__file__).parent.parent / "gui" / "app.py").read_text(encoding="utf-8")

        self.assertIn('"Performance"', app_source)
        self.assertIn('"Regime"', app_source)
        self.assertIn('"Recommendation Quality"', app_source)
        self.assertIn('"Weekly Review"', app_source)
        self.assertIn('"Reports"', app_source)
        self.assertIn('def _render_performance_tab', app_source)
        self.assertIn('def _render_regime_analytics_tab', app_source)
        self.assertIn('def _render_recommendation_quality_tab', app_source)
        self.assertIn('def _render_weekly_review_tab', app_source)

    def test_existing_gui_extension_path_is_reused(self):
        app_source = (Path(__file__).parent.parent / "gui" / "app.py").read_text(encoding="utf-8")

        self.assertIn("from gui_operator_data import (", app_source)
        self.assertIn("load_operator_dashboard_data", app_source)
        self.assertIn('["Overview", "Advanced"]', app_source)
        self.assertIn('if   page == "Dashboard":    page_dashboard()', app_source)
        self.assertIn('st.title("Operator Dashboard")', app_source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
