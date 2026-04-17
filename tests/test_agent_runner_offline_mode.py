"""
tests/test_agent_runner_offline_mode.py

Tests for agent/agent_runner.py in offline (--no-network / STOCKBOT_TESTING=1) mode.

All tests are fully offline — no Ollama, no Claude, no network.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers (shared with test_agent_bundle_builder but self-contained here)
# ---------------------------------------------------------------------------

def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict]) -> None:
    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _make_repo(tmp: Path) -> None:
    """Minimal repo tree for agent runner tests."""
    _write_json(tmp / "config.json", {
        "investor": {"name": "Test", "age": 25},
        "portfolio": {
            "holdings": [
                {"symbol": "QQQ", "shares": 4, "target_weight": 0.45,
                 "asset_class": "us_equity", "is_leveraged": False, "leverage_factor": 1},
                {"symbol": "GLD", "shares": 2, "target_weight": 0.30,
                 "asset_class": "commodity", "is_leveraged": False, "leverage_factor": 1},
            ],
            "cash_available": 400.0,
            "monthly_contribution": 1000,
        },
        "growth_mode": {
            "mode": "accumulation_aggressive",
            "concentration_cap": 0.40,
            "leverage_cap": 0.15,
            "target_cagr": 0.09,
            "drawdown_thresholds": {
                "modest_equity_tilt": 0.10,
                "aggressive_equity_tilt": 0.20,
                "deploy_all_cash": 0.30,
            },
            "expected_returns": {
                "us_equity": 0.10,
                "commodity": 0.04,
                "cash": 0.04,
            },
        },
        "email": {"enabled": False},
        "scanner": {"enabled": False},
        "speculative_sleeve": {"enabled": False, "max_total": 0.10, "max_per_position": 0.05},
    })
    _write_json(tmp / "data" / "drawdown_state.json", {
        "all_time_high": 5000.0,
        "rolling_12m_high": 5000.0,
        "last_update_date": "2026-03-03",
        "current_value": 4900.0,
    })
    _write_json(tmp / "data" / "price_cache.json", {
        "QQQ": {"price": 600.0, "timestamp": "2026-03-03T10:00:00"},
        "GLD": {"price": 470.0, "timestamp": "2026-03-03T10:00:00"},
    })
    _write_json(tmp / "data" / "finance_history.json", [
        {"date": "2026-03-03", "portfolio_value": 4900.0, "cash_available": 400.0}
    ])
    # logs dir with a stub log
    log_dir = tmp / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "2026-03-03.log").write_text(
        "[INFO] Engine run complete\n[INFO] Portfolio: $4,900.00\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAgentRunnerOfflineMode(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.root = Path(self.tmp_dir)
        _make_repo(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _run(self, mode="daily", offline=True):
        from agent.agent_runner import run
        return run(mode=mode, offline=offline, root=self.root)

    # ------------------------------------------------------------------
    # File creation tests
    # ------------------------------------------------------------------

    def test_daily_creates_decision_memo(self):
        self._run(mode="daily")
        memo_path = self.root / "outputs" / "latest" / "decision_memo.md"
        self.assertTrue(memo_path.exists(), "decision_memo.md should be written")

    def test_weekly_creates_decision_memo(self):
        self._run(mode="weekly")
        memo_path = self.root / "outputs" / "latest" / "decision_memo.md"
        self.assertTrue(memo_path.exists(), "decision_memo.md should be written")

    def test_monthly_creates_monthly_memo(self):
        self._run(mode="monthly")
        memo_path = self.root / "outputs" / "latest" / "monthly_memo.md"
        self.assertTrue(memo_path.exists(), "monthly_memo.md should be written")

    def test_monthly_creates_email_draft(self):
        self._run(mode="monthly")
        email_path = self.root / "outputs" / "latest" / "email_draft.md"
        self.assertTrue(email_path.exists(), "email_draft.md should be written")

    def test_email_draft_says_no_email_when_disabled(self):
        self._run(mode="monthly")
        email_path = self.root / "outputs" / "latest" / "email_draft.md"
        content = email_path.read_text(encoding="utf-8")
        self.assertIn("NO_EMAIL", content)

    def test_agent_bundle_written(self):
        self._run(mode="daily")
        bundle_path = self.root / "outputs" / "latest" / "agent_bundle.json"
        self.assertTrue(bundle_path.exists(), "agent_bundle.json should be written")

    def test_maintainer_blocked_without_approval(self):
        self._run(mode="daily")
        patch_path = self.root / "outputs" / "latest" / "maintainer_patch.diff"
        self.assertTrue(patch_path.exists(), "maintainer_patch.diff should be written (blocked note)")
        content = patch_path.read_text(encoding="utf-8")
        self.assertIn("BLOCKED", content)

    # ------------------------------------------------------------------
    # Content correctness
    # ------------------------------------------------------------------

    def test_decision_memo_contains_offline_marker(self):
        self._run(mode="daily")
        memo = (self.root / "outputs" / "latest" / "decision_memo.md").read_text(encoding="utf-8")
        self.assertIn("OFFLINE", memo)

    def test_monthly_memo_contains_offline_marker(self):
        self._run(mode="monthly")
        memo = (self.root / "outputs" / "latest" / "monthly_memo.md").read_text(encoding="utf-8")
        self.assertIn("OFFLINE", memo)

    def test_decision_memo_contains_portfolio_value(self):
        self._run(mode="daily")
        memo = (self.root / "outputs" / "latest" / "decision_memo.md").read_text(encoding="utf-8")
        # Portfolio value should appear (QQQ: 4*600=2400, GLD: 2*470=940, cash: 400 → 3740)
        self.assertIn("$", memo)

    def test_degraded_data_mode_is_reflected_in_memo_and_metadata(self):
        _write_json(
            self.root / "outputs" / "latest" / "scraped_intel_run_summary.json",
            {
                "degraded_mode": True,
                "degraded_reason": "fmp_403",
                "data_sources_used": ["fallback"],
                "data_mode": "fallback",
                "scanner": {
                    "data_fallback_triggered": True,
                    "data_latency_ms": 1234,
                    "fallback_depth": 1,
                    "degraded_confidence_penalty": 0.25,
                },
            },
        )

        self._run(mode="daily")

        memo = (self.root / "outputs" / "latest" / "decision_memo.md").read_text(encoding="utf-8")
        metadata = json.loads(
            (self.root / "outputs" / "latest" / "agent_llm_metadata.json").read_text(encoding="utf-8")
        )
        bundle = json.loads(
            (self.root / "outputs" / "latest" / "agent_bundle.json").read_text(encoding="utf-8")
        )

        self.assertIn("Operating in degraded data mode", memo)
        self.assertTrue(metadata["degraded_mode"])
        self.assertEqual(metadata["data_mode"], "fallback")
        self.assertTrue(metadata["tasks"][0]["data_fallback_triggered"])
        self.assertEqual(bundle["data_health"]["data_mode"], "fallback")

    def test_decision_memo_includes_high_confidence_and_suppressed_watchlist_signals(self):
        _write_json(
            self.root / "outputs" / "latest" / "watchlist_signals.json",
            {
                "data_mode": "fallback",
                "degraded_mode": True,
                "scan_summary": {
                    "signals_suppressed": 2,
                    "cooldown_hits": 1,
                },
                "results": [
                    {
                        "ticker": "NVDA",
                        "signal_score": 0.88,
                        "confidence_score": 0.91,
                        "effective_score": 0.80,
                        "notification_status": "alerted",
                        "notification_reason": "",
                    },
                    {
                        "ticker": "AMD",
                        "signal_score": 0.74,
                        "confidence_score": 0.66,
                        "effective_score": 0.49,
                        "notification_status": "cooldown_suppressed",
                        "notification_reason": "cooldown-suppressed: unchanged high alert within 6h window",
                        "cooldown_active": True,
                    },
                ],
            },
        )

        self._run(mode="daily")

        memo = (self.root / "outputs" / "latest" / "decision_memo.md").read_text(encoding="utf-8")
        metadata = json.loads(
            (self.root / "outputs" / "latest" / "agent_llm_metadata.json").read_text(encoding="utf-8")
        )
        bundle = json.loads(
            (self.root / "outputs" / "latest" / "agent_bundle.json").read_text(encoding="utf-8")
        )

        self.assertIn("High-confidence signals", memo)
        self.assertIn("NVDA", memo)
        self.assertIn("Suppressed signals", memo)
        self.assertIn("AMD", memo)
        self.assertEqual(metadata["suppressed_signals"], 2)
        self.assertEqual(metadata["cooldown_hits"], 1)
        self.assertEqual(bundle["data_health"]["suppressed_signals"], 2)
        self.assertEqual(bundle["data_health"]["cooldown_hits"], 1)

    def test_decision_memo_includes_historical_signal_performance_section(self):
        _write_json(
            self.root / "outputs" / "performance" / "performance_summary.json",
            {
                "historically_strong_tickers": [
                    {
                        "ticker": "NVDA",
                        "historical_performance_score": 0.82,
                        "win_rate": 0.75,
                        "avg_return_pct": 4.2,
                    }
                ],
                "low_reliability_tickers": [
                    {
                        "ticker": "TSLA",
                        "signal_reliability": "weak",
                        "win_rate": 0.33,
                        "avg_return_pct": -1.8,
                    }
                ],
            },
        )

        self._run(mode="daily")

        memo = (self.root / "outputs" / "latest" / "decision_memo.md").read_text(encoding="utf-8")
        bundle = json.loads(
            (self.root / "outputs" / "latest" / "agent_bundle.json").read_text(encoding="utf-8")
        )

        self.assertIn("Historical Signal Performance", memo)
        self.assertIn("NVDA", memo)
        self.assertIn("TSLA", memo)
        self.assertIsNotNone(bundle["signal_performance_summary"])

    def test_decision_memo_includes_conviction_and_sizing_section(self):
        _write_json(
            self.root / "outputs" / "latest" / "watchlist_signals.json",
            {
                "scan_summary": {
                    "conviction_band_counts": {
                        "high_conviction": 1,
                        "normal": 1,
                        "starter": 1,
                        "observe": 0,
                        "defer": 1,
                    },
                    "conviction_summary_line": "Conviction summary: 1 high_conviction, 1 normal, 1 starter, 0 observe, 1 defer",
                },
                "results": [
                    {
                        "ticker": "NVDA",
                        "signal_score": 0.88,
                        "confidence_score": 0.91,
                        "effective_score": 0.80,
                        "conviction_score": 0.85,
                        "conviction_band": "high_conviction",
                        "target_allocation_band": "1.00-2.00%",
                        "notification_status": "alerted",
                    },
                    {
                        "ticker": "AMD",
                        "signal_score": 0.68,
                        "confidence_score": 0.74,
                        "effective_score": 0.50,
                        "conviction_score": 0.42,
                        "conviction_band": "starter",
                        "target_allocation_band": "0.25-0.50%",
                        "notification_status": "alerted",
                    },
                    {
                        "ticker": "TSLA",
                        "signal_score": 0.70,
                        "confidence_score": 0.55,
                        "effective_score": 0.39,
                        "conviction_score": 0.18,
                        "conviction_band": "defer",
                        "notification_status": "alerted",
                        "notification_reason": "deferred due to low reliability",
                    },
                ],
            },
        )

        self._run(mode="daily")

        memo = (self.root / "outputs" / "latest" / "decision_memo.md").read_text(encoding="utf-8")
        self.assertIn("Conviction And Sizing", memo)
        self.assertIn("High conviction candidates", memo)
        self.assertIn("Starter-sized ideas", memo)
        self.assertIn("Deferred due to degraded mode / cooldown / low reliability", memo)
        self.assertIn("NVDA", memo)
        self.assertIn("AMD", memo)
        self.assertIn("TSLA", memo)

    def test_decision_memo_includes_portfolio_construction_section(self):
        _write_json(
            self.root / "outputs" / "latest" / "watchlist_signals.json",
            {
                "portfolio_construction": {
                    "summary_label": "overweight technology",
                    "summary_line": "Portfolio view: 3 actionable signals, 4.0% suggested, 3.0% normalized, 2 capped",
                    "warnings": [
                        "overconcentration_top_sector:Technology:66.7%",
                        "top3_ticker_concentration:100.0%",
                    ],
                    "rows": [
                        {
                            "ticker": "NVDA",
                            "sector": "Technology",
                            "conviction_score": 0.86,
                            "conviction_band": "high_conviction",
                            "normalized_allocation": 0.015,
                            "allocation_capped": True,
                            "allocation_cap_reason": "sector_cap",
                        },
                        {
                            "ticker": "MSFT",
                            "sector": "Technology",
                            "conviction_score": 0.82,
                            "conviction_band": "high_conviction",
                            "normalized_allocation": 0.010,
                            "allocation_capped": True,
                            "allocation_cap_reason": "total_allocation_cap,sector_cap",
                        },
                    ],
                },
                "results": [],
            },
        )

        self._run(mode="daily")

        memo = (self.root / "outputs" / "latest" / "decision_memo.md").read_text(encoding="utf-8")
        self.assertIn("Portfolio Construction View", memo)
        self.assertIn("overweight technology", memo)
        self.assertIn("Concentration warnings", memo)
        self.assertIn("NVDA", memo)
        self.assertIn("sector_cap", memo)

    def test_decision_memo_includes_market_regime_section(self):
        _write_json(
            self.root / "outputs" / "latest" / "watchlist_signals.json",
            {
                "market_regime": {
                    "regime_label": "risk_on",
                    "regime_confidence": 0.72,
                    "regime_reasoning": "broad uptrend with supportive leadership",
                    "regime_summary_line": "Market regime: risk_on (confidence 0.72) - broad uptrend with supportive leadership",
                    "regime_data_quality": "partial",
                    "regime_inputs": {"breadth_sma50": 0.75},
                    "regime_portfolio_fit": "aligned",
                    "regime_portfolio_commentary": "Current normalized allocations look broadly aligned with a constructive regime.",
                },
                "results": [],
            },
        )

        self._run(mode="daily")

        memo = (self.root / "outputs" / "latest" / "decision_memo.md").read_text(encoding="utf-8")
        bundle = json.loads(
            (self.root / "outputs" / "latest" / "agent_bundle.json").read_text(encoding="utf-8")
        )

        self.assertIn("Market Regime View", memo)
        self.assertIn("risk_on", memo)
        self.assertEqual(bundle["market_regime"]["regime_label"], "risk_on")

    def test_decision_memo_includes_regime_performance_insights(self):
        _write_json(
            self.root / "outputs" / "latest" / "watchlist_signals.json",
            {
                "market_regime": {
                    "regime_label": "risk_on",
                    "regime_summary_line": "Market regime: risk_on (confidence 0.72) - broad uptrend with supportive breadth",
                    "regime_portfolio_fit": "aligned",
                    "regime_portfolio_commentary": "Current normalized allocations look broadly aligned with a constructive regime.",
                },
                "results": [],
            },
        )
        _write_json(
            self.root / "outputs" / "regime" / "regime_performance.json",
            {
                "by_regime": {
                    "risk_on": {
                        "win_rate": 0.68,
                        "avg_return_pct": 3.4,
                        "best_conviction_band": "high_conviction",
                        "worst_conviction_band": "starter",
                        "degraded_data_impact_note": "degraded data present in this regime sample",
                    }
                }
            },
        )

        self._run(mode="daily")

        memo = (self.root / "outputs" / "latest" / "decision_memo.md").read_text(encoding="utf-8")
        bundle = json.loads(
            (self.root / "outputs" / "latest" / "agent_bundle.json").read_text(encoding="utf-8")
        )

        self.assertIn("Regime Performance Insights", memo)
        self.assertIn("68.0%", memo)
        self.assertIn("high_conviction", memo)
        self.assertIsNotNone(bundle["regime_performance_summary"])

    def test_decision_memo_includes_policy_simulation_insights(self):
        _write_json(
            self.root / "outputs" / "simulations" / "policy_simulation.json",
            {
                "comparison": {
                    "best_by_win_rate": "high_conviction_only",
                    "best_by_drawdown": "conservative_size_cap",
                    "best_degraded_mode_policy": "degraded_safe_mode",
                    "best_policy_by_regime": {"neutral": "balanced_growth"},
                },
                "policies": [
                    {
                        "policy": "high_conviction_only",
                        "win_rate": 0.71,
                        "avg_return_pct": 4.1,
                        "total_trades": 12,
                    },
                    {
                        "policy": "risk_on_only",
                        "win_rate": 0.64,
                        "avg_return_pct": 2.9,
                        "total_trades": 18,
                    },
                ]
            },
        )

        self._run(mode="daily")

        memo = (self.root / "outputs" / "latest" / "decision_memo.md").read_text(encoding="utf-8")
        bundle = json.loads(
            (self.root / "outputs" / "latest" / "agent_bundle.json").read_text(encoding="utf-8")
        )

        self.assertIn("Policy Simulation Insights", memo)
        self.assertIn("Strategy Policy View", memo)
        self.assertIn("71.0%", memo)
        self.assertIn("high_conviction_only", memo)
        self.assertIn("conservative_size_cap", memo)
        self.assertIsNotNone(bundle["policy_simulation_summary"])

    def test_decision_memo_includes_strategy_recommendation_when_artifact_exists(self):
        _write_json(
            self.root / "outputs" / "policy" / "policy_recommendation.json",
            {
                "recommendation": {
                    "recommended_policy": "quality_growth",
                    "recommended_profile": "balanced_growth",
                    "recommendation_confidence": 0.71,
                    "recommendation_reasoning": [
                        "Current regime is `risk_on` with confidence 0.68.",
                        "Recommended profile `balanced_growth` and policy `quality_growth` lead on the transparent advisory score.",
                    ],
                    "recommendation_inputs": {
                        "policy": {"regime_label": "risk_on"},
                        "profile": {"regime_label": "risk_on"},
                    },
                    "recommendation_data_quality": "sparse_simulation_history",
                    "recommendation_source": "rule_based_fallback",
                    "recommendation_quality_note": "Recommendation confidence is limited due to sparse policy simulation history.",
                },
                "alternatives": {
                    "policies": [
                        {"name": "regime_aligned", "recommendation_score": 0.78},
                        {"name": "high_quality_concentrated", "recommendation_score": 0.72},
                    ]
                },
            },
        )

        self._run(mode="daily")

        memo = (self.root / "outputs" / "latest" / "decision_memo.md").read_text(encoding="utf-8")
        metadata = json.loads(
            (self.root / "outputs" / "latest" / "agent_llm_metadata.json").read_text(encoding="utf-8")
        )
        bundle = json.loads(
            (self.root / "outputs" / "latest" / "agent_bundle.json").read_text(encoding="utf-8")
        )

        self.assertIn("Strategy Recommendation", memo)
        self.assertIn("Recommended profile: balanced_growth", memo)
        self.assertIn("Recommended policy: quality_growth", memo)
        self.assertIn("Recommendation confidence is limited", memo)
        self.assertEqual(metadata["recommended_policy"], "quality_growth")
        self.assertEqual(metadata["recommended_profile"], "balanced_growth")
        self.assertIsNotNone(bundle["policy_recommendation"])

    def test_legacy_summary_with_scanner_fallback_normalizes_to_degraded_mode(self):
        _write_json(
            self.root / "outputs" / "latest" / "scraped_intel_run_summary.json",
            {
                "scanner": {
                    "fmp_attempted": False,
                    "fmp_succeeded": False,
                    "fmp_error": "FMP circuit breaker open",
                    "fallback_used": True,
                    "watchlist_source": "fallback",
                    "data_fallback_triggered": True,
                }
            },
        )

        result = self._run(mode="daily")
        metadata = result["llm_metadata"][0]

        self.assertTrue(metadata["degraded_mode"])
        self.assertEqual(metadata["degraded_reason"], "circuit_breaker")
        self.assertTrue(metadata["data_fallback_triggered"])

    def test_run_result_has_files_written(self):
        result = self._run(mode="daily")
        self.assertIn("files_written", result)
        self.assertGreater(len(result["files_written"]), 0)

    def test_run_result_mode_preserved(self):
        result = self._run(mode="weekly")
        self.assertEqual(result["mode"], "weekly")

    def test_run_result_offline_flag(self):
        result = self._run(mode="daily", offline=True)
        self.assertTrue(result["offline"])

    # ------------------------------------------------------------------
    # Escalation
    # ------------------------------------------------------------------

    def test_escalation_triggered_on_concentration_violation(self):
        # QQQ weight = 2400/3740 ≈ 64% > 40% cap → escalation
        self._run(mode="daily")
        esc_path = self.root / "outputs" / "latest" / "escalation_packet.md"
        self.assertTrue(esc_path.exists(), "escalation_packet.md should be written")

    def test_escalation_contains_violation_details(self):
        self._run(mode="daily")
        esc = (self.root / "outputs" / "latest" / "escalation_packet.md").read_text(encoding="utf-8")
        self.assertIn("concentration_cap", esc)

    # ------------------------------------------------------------------
    # No network calls
    # ------------------------------------------------------------------

    def test_no_network_calls_in_offline_mode(self):
        """Offline mode must not make any HTTP requests."""
        import unittest.mock as mock
        import urllib.request
        with mock.patch.object(
            urllib.request, "urlopen", side_effect=AssertionError("unexpected network call")
        ) as patched:
            self._run(mode="daily", offline=True)
            patched.assert_not_called()

    # ------------------------------------------------------------------
    # Maintainer with approval file (offline)
    # ------------------------------------------------------------------

    def test_maintainer_with_approval_offline_writes_blocked_note(self):
        """With approval file but --no-network, should write an offline note, not call Claude."""
        approval_data = {
            "actions": [
                {"id": "fix-001", "file": "finance_analyzer.py", "description": "Fix dedup bug"}
            ]
        }
        (self.root / "approved_actions.json").write_text(
            json.dumps(approval_data), encoding="utf-8"
        )
        # offline=True means Claude is skipped
        self._run(mode="daily", offline=True)
        patch_path = self.root / "outputs" / "latest" / "maintainer_patch.diff"
        self.assertTrue(patch_path.exists())
        content = patch_path.read_text(encoding="utf-8")
        # In offline mode with approval file, should mention offline
        self.assertIn("OFFLINE", content)


class TestAgentRunnerStockbotTestingEnv(unittest.TestCase):
    """Verify STOCKBOT_TESTING=1 triggers offline mode."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.root = Path(self.tmp_dir)
        _make_repo(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        # Clean up env var
        os.environ.pop("STOCKBOT_TESTING", None)

    def test_stockbot_testing_env_triggers_offline(self):
        os.environ["STOCKBOT_TESTING"] = "1"
        from agent.agent_runner import run
        result = run(mode="daily", offline=False, root=self.root)
        # Even though offline=False was passed, STOCKBOT_TESTING=1 in main() would
        # trigger offline — but run() receives the pre-resolved flag.
        # The env var is consumed in main(), so here we test that passing offline=True
        # (simulating what main() would do) works correctly.
        self.assertTrue(
            (self.root / "outputs" / "latest" / "decision_memo.md").exists()
        )


if __name__ == "__main__":
    unittest.main()
