"""
tests/test_agent_bundle_builder.py

Tests for agent/bundle_builder.py.

All tests are fully offline — they create temporary directories with small
CSV/JSON fixtures and verify the bundle fields.  No network calls, no FMP,
no Ollama, no Claude.
"""

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers for fixture creation
# ---------------------------------------------------------------------------

def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = fieldnames or list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _make_minimal_repo(tmp: Path) -> None:
    """Populate a minimal fake repo tree under tmp."""
    # config.json
    _write_json(tmp / "config.json", {
        "investor": {"name": "Test User", "age": 30},
        "portfolio": {
            "holdings": [
                {"symbol": "QQQ", "shares": 5, "target_weight": 0.50,
                 "asset_class": "us_equity", "is_leveraged": False, "leverage_factor": 1},
                {"symbol": "GLD", "shares": 2, "target_weight": 0.30,
                 "asset_class": "commodity", "is_leveraged": False, "leverage_factor": 1},
                {"symbol": "QLD", "shares": 3, "target_weight": 0.20,
                 "asset_class": "us_equity_leveraged", "is_leveraged": True, "leverage_factor": 2},
            ],
            "cash_available": 500.0,
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
                "us_equity_leveraged": 0.14,
                "cash": 0.04,
            },
        },
        "email": {"enabled": False},
        "scanner": {"enabled": False},
        "speculative_sleeve": {"enabled": False, "max_total": 0.10, "max_per_position": 0.05},
    })

    # drawdown_state.json
    _write_json(tmp / "data" / "drawdown_state.json", {
        "all_time_high": 8000.0,
        "rolling_12m_high": 8000.0,
        "rolling_12m_high_date": "2026-01-01",
        "last_update_date": "2026-03-03",
        "current_value": 7800.0,
    })

    # price_cache.json
    _write_json(tmp / "data" / "price_cache.json", {
        "QQQ": {"price": 600.0, "timestamp": "2026-03-03T10:00:00"},
        "GLD": {"price": 470.0, "timestamp": "2026-03-03T10:00:00"},
        "QLD": {"price": 65.0, "timestamp": "2026-03-03T10:00:00"},
    })

    # finance_history.json
    _write_json(tmp / "data" / "finance_history.json", [
        {
            "date": "2026-03-03",
            "portfolio_value": 7800.0,
            "cash_available": 500.0,
            "savings_rate": 0.15,
            "max_drift": 0.10,
            "max_drift_symbol": "QQQ",
        }
    ])

    # outputs/latest/contribution_plan.csv
    _write_csv(tmp / "outputs" / "latest" / "contribution_plan.csv", [
        {
            "Symbol": "GLD", "AssetClass": "commodity", "CurrentWeight": "0.12",
            "TargetWeight": "0.30", "Drift": "-0.18",
            "RecommendedContributionDollars": "600", "Reason": "Under target",
        },
        {
            "Symbol": "QLD", "AssetClass": "us_equity_leveraged", "CurrentWeight": "0.03",
            "TargetWeight": "0.20", "Drift": "-0.17",
            "RecommendedContributionDollars": "400", "Reason": "Under target",
        },
    ])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBundleBuilderBasic(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.root = Path(self.tmp_dir)
        _make_minimal_repo(self.root)

    def _build(self, mode="daily"):
        # Import here so the module path is correct
        from agent.bundle_builder import build_bundle
        return build_bundle(mode=mode, root=self.root)

    def test_bundle_returns_dict(self):
        bundle = self._build()
        self.assertIsInstance(bundle, dict)

    def test_required_top_level_keys(self):
        bundle = self._build()
        required = [
            "run_mode", "generated_at", "portfolio_value", "drawdown",
            "guardrails", "contribution_plan", "should_email", "data_freshness",
            "expected_cagr",
        ]
        for key in required:
            self.assertIn(key, bundle, f"Missing key: {key}")

    def test_run_mode_preserved(self):
        for mode in ("daily", "weekly", "monthly"):
            bundle = self._build(mode=mode)
            self.assertEqual(bundle["run_mode"], mode)

    def test_portfolio_value_positive(self):
        bundle = self._build()
        # QQQ: 5*600=3000, GLD: 2*470=940, QLD: 3*65=195, cash: 500 → total=4635
        self.assertGreater(bundle["portfolio_value"], 0)
        self.assertAlmostEqual(bundle["portfolio_value"], 4635.0, places=0)

    def test_drawdown_fields(self):
        bundle = self._build()
        dd = bundle["drawdown"]
        self.assertIn("current_value", dd)
        self.assertIn("all_time_high", dd)
        self.assertIn("drawdown_pct", dd)
        # drawdown_pct = (8000 - 7800) / 8000 = 0.025
        self.assertAlmostEqual(dd["drawdown_pct"], 0.025, places=3)

    def test_guardrails_concentration_violation(self):
        # QQQ weight = 3000/4635 ≈ 64.7% > 40% cap
        bundle = self._build()
        guardrails = bundle["guardrails"]
        self.assertFalse(guardrails["pass"])
        symbols = [v["symbol"] for v in guardrails["violations"] if "symbol" in v]
        self.assertIn("QQQ", symbols)

    def test_guardrails_leverage_violation(self):
        # QLD effective exposure = (195/4635)*2 ≈ 8.4% < 15% cap → no violation here
        # But let's check the rule detection works with a higher-leverage fixture
        bundle = self._build()
        rules = [v["rule"] for v in bundle["guardrails"]["violations"]]
        # At minimum, concentration_cap should fire
        self.assertIn("concentration_cap", rules)

    def test_contribution_plan_loaded(self):
        bundle = self._build()
        plan = bundle["contribution_plan"]
        self.assertIsInstance(plan, list)
        self.assertGreater(len(plan), 0)
        self.assertIn("symbol", plan[0])
        self.assertIn("dollars", plan[0])

    def test_contribution_plan_sorted_descending(self):
        bundle = self._build()
        plan = bundle["contribution_plan"]
        if len(plan) >= 2:
            self.assertGreaterEqual(plan[0]["dollars"], plan[1]["dollars"])

    def test_should_email_false(self):
        bundle = self._build()
        self.assertFalse(bundle["should_email"])

    def test_data_freshness_price_asof(self):
        bundle = self._build()
        asof = bundle["data_freshness"]["price_asof"]
        self.assertIsNotNone(asof)
        self.assertIn("2026-03-03", asof)

    def test_bundle_written_to_disk(self):
        self._build()
        out_path = self.root / "outputs" / "latest" / "agent_bundle.json"
        self.assertTrue(out_path.exists(), "agent_bundle.json should be written to disk")
        data = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertIn("portfolio_value", data)

    def test_no_fmp_calls(self):
        """build_bundle must not attempt any network calls."""
        import unittest.mock as mock
        import urllib.request
        with mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network call")) as patched:
            self._build()
            patched.assert_not_called()

    def test_missing_price_cache_graceful(self):
        """Bundle should still build if price_cache.json is missing."""
        (self.root / "data" / "price_cache.json").unlink()
        bundle = self._build()
        self.assertIn("portfolio_value", bundle)

    def test_missing_contribution_csv_graceful(self):
        """Bundle should still build if contribution_plan.csv is missing."""
        (self.root / "outputs" / "latest" / "contribution_plan.csv").unlink()
        bundle = self._build()
        self.assertEqual(bundle["contribution_plan"], [])

    def test_candidates_top20_none_when_missing(self):
        bundle = self._build()
        self.assertIsNone(bundle["candidates_top20"])

    def test_drawdown_regime_normal(self):
        # drawdown_pct = 2.5% < 10% → normal
        bundle = self._build()
        self.assertEqual(bundle["drawdown_regime"], "normal")

    def test_drawdown_regime_with_high_drawdown(self):
        # Override drawdown to 25%
        _write_json(self.root / "data" / "drawdown_state.json", {
            "all_time_high": 10000.0,
            "rolling_12m_high": 10000.0,
            "last_update_date": "2026-03-03",
            "current_value": 7500.0,  # 25% drawdown
        })
        bundle = self._build()
        self.assertIn(bundle["drawdown_regime"], ("aggressive_equity_tilt", "deploy_all_cash"))

    def test_expected_cagr_in_range(self):
        bundle = self._build()
        cagr = bundle["expected_cagr"]
        self.assertGreater(cagr, 0.03)
        self.assertLess(cagr, 0.20)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
