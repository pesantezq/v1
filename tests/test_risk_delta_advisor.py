"""
Tests for portfolio_automation/risk_delta_advisor.py.

The advisor is a thin compose-of-pure-functions producer. Tests focus on:

  - VaR math is correct on a known sigma + portfolio value pair
  - Concentration classification respects the breach/near_cap/ok ladder
  - Leverage aggregation handles factor + weight correctly
  - Degraded modes return safe dicts (no exceptions, available=False)
  - observe_only invariant is hardcoded
  - run_risk_delta_advisor never mutates decision/score artifacts
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.risk_delta_advisor import (
    _classify_headroom,
    build_risk_delta,
    compute_concentration,
    compute_leverage,
    compute_var,
    render_risk_delta_md,
    run_risk_delta_advisor,
    _VAR_95_Z,
    _TRADING_DAYS_PER_YEAR,
)


class TestClassifyHeadroom(unittest.TestCase):
    def test_breach_when_headroom_zero_or_negative(self):
        self.assertEqual(_classify_headroom(0.0), "breach")
        self.assertEqual(_classify_headroom(-0.05), "breach")

    def test_near_cap_inside_5pp(self):
        self.assertEqual(_classify_headroom(0.01), "near_cap")
        self.assertEqual(_classify_headroom(0.05), "near_cap")

    def test_ok_when_headroom_more_than_5pp(self):
        self.assertEqual(_classify_headroom(0.06), "ok")
        self.assertEqual(_classify_headroom(0.20), "ok")


class TestComputeConcentration(unittest.TestCase):
    def test_top_position_ranked_first(self):
        holdings = [
            {"symbol": "A", "shares": 1.0, "target_weight": 0.10},
            {"symbol": "B", "shares": 1.0, "target_weight": 0.50},
            {"symbol": "C", "shares": 1.0, "target_weight": 0.30},
        ]
        result = compute_concentration(holdings, portfolio_value=100.0, cap=0.60)
        self.assertTrue(result["available"])
        self.assertEqual(result["top_position"]["symbol"], "B")
        self.assertAlmostEqual(result["top_position"]["weight"], 0.50, places=4)

    def test_quotes_override_target_weight(self):
        holdings = [{"symbol": "X", "shares": 5.0, "target_weight": 0.20}]
        # 5 shares × $20 = $100 → 100% of a $100 portfolio (overrides 20% target).
        result = compute_concentration(
            holdings, portfolio_value=100.0, cap=0.60, quotes={"X": 20.0}
        )
        self.assertAlmostEqual(result["top_position"]["weight"], 1.00, places=4)
        self.assertEqual(result["top_position"]["status"], "breach")

    def test_target_weight_fallback_when_no_quote(self):
        holdings = [{"symbol": "Y", "shares": 0.0, "target_weight": 0.30}]
        result = compute_concentration(holdings, portfolio_value=100.0, cap=0.60)
        # No shares, no quote → falls back to target_weight 30%
        self.assertAlmostEqual(result["top_position"]["weight"], 0.30, places=4)
        self.assertEqual(result["top_position"]["price_source"], "target_weight_fallback")

    def test_breach_count_and_near_cap_count(self):
        holdings = [
            {"symbol": "A", "shares": 1.0, "target_weight": 0.65},  # breach (cap 0.60)
            {"symbol": "B", "shares": 1.0, "target_weight": 0.58},  # near_cap
            {"symbol": "C", "shares": 1.0, "target_weight": 0.10},  # ok
        ]
        result = compute_concentration(holdings, portfolio_value=100.0, cap=0.60)
        self.assertEqual(result["breach_count"], 1)
        self.assertEqual(result["near_cap_count"], 1)

    def test_no_holdings_unavailable(self):
        self.assertFalse(compute_concentration([], 100.0, 0.60)["available"])

    def test_zero_portfolio_value_unavailable(self):
        self.assertFalse(
            compute_concentration(
                [{"symbol": "X", "shares": 1.0}], 0.0, 0.60
            )["available"]
        )


class TestComputeLeverage(unittest.TestCase):
    def test_aggregates_leveraged_positions_only(self):
        holdings = [
            {"symbol": "QQQ", "shares": 1.0, "target_weight": 0.40, "is_leveraged": False},
            {"symbol": "QLD", "shares": 1.0, "target_weight": 0.05,
             "is_leveraged": True, "leverage_factor": 2},
            {"symbol": "TQQQ", "shares": 1.0, "target_weight": 0.05,
             "is_leveraged": True, "leverage_factor": 3},
        ]
        result = compute_leverage(holdings, portfolio_value=100.0, cap=0.25)
        # 0.05 × 2 + 0.05 × 3 = 0.25 exposure → exactly at cap → breach (<=0)
        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["total_exposure"], 0.25, places=4)
        self.assertAlmostEqual(result["headroom"], 0.0, places=4)
        self.assertEqual(result["status"], "breach")
        self.assertEqual(len(result["leveraged_positions"]), 2)

    def test_no_leveraged_holdings_zero_exposure(self):
        holdings = [{"symbol": "QQQ", "shares": 1.0, "target_weight": 0.40}]
        result = compute_leverage(holdings, portfolio_value=100.0, cap=0.25)
        self.assertAlmostEqual(result["total_exposure"], 0.0, places=4)
        self.assertEqual(result["leveraged_positions"], [])
        self.assertEqual(result["status"], "ok")

    def test_zero_portfolio_unavailable(self):
        self.assertFalse(
            compute_leverage(
                [{"symbol": "X", "is_leveraged": True}], 0.0, 0.25
            )["available"]
        )


class TestComputeVaR(unittest.TestCase):
    def test_known_value_at_15pct_annual_vol(self):
        # Sanity check the math: 15% annual vol, $10k portfolio, 1-day 95% VaR
        # daily_sigma = 0.15 / sqrt(252) ≈ 0.00945
        # var_pct = 1.645 × 0.00945 ≈ 0.01554
        # var_dollar ≈ $155.40
        result = compute_var(portfolio_value=10_000.0, sigma_annual=0.15)
        expected_daily_sigma = 0.15 / math.sqrt(_TRADING_DAYS_PER_YEAR)
        expected_var_pct = _VAR_95_Z * expected_daily_sigma
        self.assertAlmostEqual(result["sigma_daily"], expected_daily_sigma, places=4)
        self.assertAlmostEqual(result["var_pct"], expected_var_pct, places=4)
        self.assertAlmostEqual(
            result["var_dollar"], expected_var_pct * 10_000.0, places=1
        )

    def test_horizon_scaling_uses_square_root_of_time(self):
        # VaR over 4 days = 2 × 1-day VaR (sqrt(4) = 2). Artifact rounds to
        # 4 decimal places, so we allow ~1% slack on the ratio.
        one_day = compute_var(10_000.0, 0.15, horizon_days=1)
        four_day = compute_var(10_000.0, 0.15, horizon_days=4)
        ratio = four_day["var_pct"] / one_day["var_pct"]
        self.assertGreater(ratio, 1.98)
        self.assertLess(ratio, 2.02)

    def test_zero_sigma_unavailable(self):
        self.assertFalse(compute_var(10_000.0, 0.0)["available"])

    def test_none_sigma_unavailable(self):
        self.assertFalse(compute_var(10_000.0, None)["available"])

    def test_zero_portfolio_unavailable(self):
        self.assertFalse(compute_var(0.0, 0.15)["available"])


class TestBuildRiskDelta(unittest.TestCase):
    def _holdings(self):
        return [
            {"symbol": "QQQ", "shares": 1.0, "target_weight": 0.55},
            {"symbol": "QLD", "shares": 1.0, "target_weight": 0.10,
             "is_leveraged": True, "leverage_factor": 2},
            {"symbol": "GLD", "shares": 1.0, "target_weight": 0.20},
        ]

    def test_full_artifact_shape(self):
        payload = build_risk_delta(
            holdings=self._holdings(),
            portfolio_value=10_000.0,
            concentration_cap=0.60,
            leverage_cap=0.25,
            sigma_annual=0.15,
        )
        self.assertTrue(payload["observe_only"])
        self.assertEqual(payload["schema_version"], "1")
        self.assertEqual(payload["source"], "risk_delta_advisor")
        self.assertIn("concentration", payload)
        self.assertIn("leverage", payload)
        self.assertIn("var", payload)

    def test_overall_status_picks_worst_subsection(self):
        # Force a breach: concentration cap 0.50, QQQ at 0.55 → breach
        payload = build_risk_delta(
            holdings=self._holdings(),
            portfolio_value=10_000.0,
            concentration_cap=0.50,
            leverage_cap=0.25,
            sigma_annual=0.15,
        )
        self.assertEqual(payload["overall_status"], "breach")

    def test_observe_only_invariant_in_artifact(self):
        payload = build_risk_delta(
            holdings=self._holdings(),
            portfolio_value=10_000.0,
            concentration_cap=0.60,
            leverage_cap=0.25,
            sigma_annual=0.15,
        )
        self.assertIs(payload["observe_only"], True)


class TestRenderRiskDeltaMd(unittest.TestCase):
    def test_renders_three_sections(self):
        payload = build_risk_delta(
            holdings=[{"symbol": "QQQ", "shares": 1.0, "target_weight": 0.55}],
            portfolio_value=10_000.0,
            concentration_cap=0.60,
            leverage_cap=0.25,
            sigma_annual=0.15,
        )
        md = render_risk_delta_md(payload)
        self.assertIn("Concentration vs 60% cap", md)
        self.assertIn("Leverage vs 25% cap", md)
        self.assertIn("1-day 95% Value-at-Risk", md)
        self.assertIn("Risk Delta Panel", md)

    def test_renders_disclaimer(self):
        payload = build_risk_delta(
            holdings=[{"symbol": "QQQ", "shares": 1.0, "target_weight": 0.55}],
            portfolio_value=10_000.0,
            concentration_cap=0.60,
            leverage_cap=0.25,
            sigma_annual=0.15,
        )
        md = render_risk_delta_md(payload)
        self.assertIn("Observe-only", md)


class TestRunRiskDeltaAdvisor(unittest.TestCase):
    """Integration test: feed a temp repo, ensure the advisor writes valid artifacts."""

    def test_writes_both_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config.json").write_text(json.dumps({
                "portfolio": {
                    "holdings": [
                        {"symbol": "QQQ", "shares": 1, "target_weight": 0.50,
                         "is_leveraged": False},
                    ]
                },
                "growth_mode": {
                    "concentration_cap": 0.60,
                    "leverage_cap": 0.25,
                },
            }))
            (root / "outputs" / "latest").mkdir(parents=True)
            (root / "outputs" / "latest" / "decision_plan.json").write_text(json.dumps({
                "portfolio_context": {"total_portfolio_value": 10_000.0},
            }))
            (root / "outputs" / "latest" / "vol_regime_advisor.json").write_text(json.dumps({
                "sigma_annual": 0.15,
            }))

            result = run_risk_delta_advisor(root=root)
            self.assertEqual(result["status"], "ok")
            self.assertTrue((root / "outputs" / "latest" / "risk_delta.json").exists())
            self.assertTrue((root / "outputs" / "latest" / "risk_delta.md").exists())

            payload = json.loads(
                (root / "outputs" / "latest" / "risk_delta.json").read_text()
            )
            self.assertTrue(payload["observe_only"])
            self.assertEqual(payload["source"], "risk_delta_advisor")

    def test_does_not_mutate_decision_plan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config.json").write_text(json.dumps({"portfolio": {"holdings": []}}))
            (root / "outputs" / "latest").mkdir(parents=True)
            plan_path = root / "outputs" / "latest" / "decision_plan.json"
            original = {"portfolio_context": {"total_portfolio_value": 5000.0},
                        "decisions": [{"symbol": "X", "decision": "BUY"}]}
            plan_path.write_text(json.dumps(original))

            run_risk_delta_advisor(root=root)

            after = json.loads(plan_path.read_text())
            self.assertEqual(after, original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
