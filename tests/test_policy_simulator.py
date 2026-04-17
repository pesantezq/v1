import copy
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.policy_framework import (
    get_profile,
    list_policy_names,
    list_profile_names,
    resolve_requested_policies,
)
from tools.policy_simulator import (
    POLICIES,
    apply_policy,
    render_policy_simulation_markdown,
    run_policy_simulation,
    summarize_policy,
)


class TestPolicySimulator(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.csv_path = self.root / "signal_outcomes.csv"
        rows = [
            {
                "ticker": "NVDA",
                "signal_time": "2026-04-01T12:00:00",
                "signal_score": "0.90",
                "confidence_score": "0.92",
                "conviction_score": "0.85",
                "conviction_band": "high_conviction",
                "normalized_allocation": "0.020",
                "regime_label": "risk_on",
                "regime_confidence": "0.75",
                "regime_data_quality": "full",
                "sector": "Technology",
                "outcome_return_3d": "5.0",
                "outcome_success_3d": "1",
                "degraded_mode": "0",
            },
            {
                "ticker": "AMD",
                "signal_time": "2026-04-02T12:00:00",
                "signal_score": "0.78",
                "confidence_score": "0.81",
                "conviction_score": "0.62",
                "conviction_band": "normal",
                "normalized_allocation": "0.010",
                "regime_label": "risk_on",
                "regime_confidence": "0.70",
                "regime_data_quality": "partial",
                "sector": "Technology",
                "outcome_return_3d": "2.0",
                "outcome_success_3d": "1",
                "degraded_mode": "0",
            },
            {
                "ticker": "XLU",
                "signal_time": "2026-04-03T12:00:00",
                "signal_score": "0.62",
                "confidence_score": "0.66",
                "conviction_score": "0.28",
                "conviction_band": "observe",
                "normalized_allocation": "0.000",
                "regime_label": "risk_off",
                "regime_confidence": "0.68",
                "regime_data_quality": "degraded",
                "sector": "Utilities",
                "outcome_return_3d": "-3.0",
                "outcome_success_3d": "0",
                "degraded_mode": "1",
            },
        ]
        with open(self.csv_path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    def tearDown(self):
        self.tmp.cleanup()

    def test_correct_filtering_logic(self):
        rows = [
            {
                "conviction_band": "high_conviction",
                "regime_label": "risk_on",
                "normalized_allocation": 0.02,
                "return_pct": 5.0,
                "outcome_success": 1,
            },
            {
                "conviction_band": "normal",
                "regime_label": "risk_off",
                "normalized_allocation": 0.01,
                "return_pct": -2.0,
                "outcome_success": 0,
            },
        ]
        filtered = apply_policy(rows, POLICIES["combined"])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["regime_label"], "risk_on")

    def test_policy_registry_loading(self):
        self.assertIn("baseline", list_policy_names())
        self.assertIn("quality_growth", list_policy_names())
        self.assertIn("balanced_growth", list_profile_names())

    def test_profile_mapping(self):
        policies, profiles = resolve_requested_policies(profile_names=["balanced_growth"])
        self.assertEqual(profiles[0].name, "balanced_growth")
        self.assertIn("quality_growth", [policy.name for policy in policies])
        self.assertTrue(get_profile("balanced_growth").allow_starter_ideas)

    def test_accurate_aggregation(self):
        rows = [
            {
                "conviction_band": "high_conviction",
                "regime_label": "risk_on",
                "normalized_allocation": 0.02,
                "return_pct": 5.0,
                "outcome_success": 1,
            },
            {
                "conviction_band": "high_conviction",
                "regime_label": "risk_on",
                "normalized_allocation": 0.01,
                "return_pct": 3.0,
                "outcome_success": 1,
            },
        ]
        summary = summarize_policy(rows, policy=POLICIES["high_conviction_only"])
        self.assertEqual(summary["total_trades"], 2)
        self.assertEqual(summary["win_rate"], 1.0)
        self.assertAlmostEqual(summary["avg_return_pct"], 4.0)
        self.assertIn("risk_on", summary["performance_by_regime"])

    def test_existing_policies_still_produce_expected_results(self):
        rows = [
            {
                "conviction_band": "high_conviction",
                "regime_label": "risk_on",
                "normalized_allocation": 0.02,
                "confidence_score": 0.92,
                "signal_reliability": "strong",
                "regime_data_quality": "full",
                "degraded_mode": False,
                "return_pct": 5.0,
                "outcome_success": 1,
            },
            {
                "conviction_band": "normal",
                "regime_label": "risk_off",
                "normalized_allocation": 0.01,
                "confidence_score": 0.70,
                "signal_reliability": "mixed",
                "regime_data_quality": "partial",
                "degraded_mode": False,
                "return_pct": -2.0,
                "outcome_success": 0,
            },
        ]
        self.assertEqual(len(apply_policy(rows, POLICIES["baseline"])), 2)
        self.assertEqual(len(apply_policy(rows, POLICIES["high_conviction_only"])), 1)
        self.assertEqual(len(apply_policy(rows, POLICIES["risk_on_only"])), 1)
        self.assertEqual(len(apply_policy(rows, POLICIES["avoid_risk_off"])), 1)

    def test_new_policies_apply_intended_filters(self):
        rows = [
            {
                "ticker": "NVDA",
                "conviction_band": "high_conviction",
                "regime_label": "risk_on",
                "normalized_allocation": 0.02,
                "confidence_score": 0.92,
                "signal_reliability": "strong",
                "regime_data_quality": "full",
                "degraded_mode": False,
                "return_pct": 5.0,
                "outcome_success": 1,
            },
            {
                "ticker": "XLU",
                "conviction_band": "high_conviction",
                "regime_label": "risk_off",
                "normalized_allocation": 0.003,
                "confidence_score": 0.88,
                "signal_reliability": "strong",
                "regime_data_quality": "degraded",
                "degraded_mode": True,
                "return_pct": 1.0,
                "outcome_success": 1,
            },
            {
                "ticker": "TSLA",
                "conviction_band": "normal",
                "regime_label": "neutral",
                "normalized_allocation": 0.012,
                "confidence_score": 0.72,
                "signal_reliability": "weak",
                "regime_data_quality": "partial",
                "degraded_mode": False,
                "return_pct": -1.0,
                "outcome_success": 0,
            },
        ]
        quality = apply_policy(rows, POLICIES["quality_growth"])
        degraded = apply_policy(rows, POLICIES["degraded_safe_mode"])
        concentrated = apply_policy(rows, POLICIES["high_quality_concentrated"])

        self.assertEqual([row["ticker"] for row in quality], ["NVDA"])
        self.assertEqual([row["ticker"] for row in degraded], ["XLU"])
        self.assertEqual([row["ticker"] for row in concentrated], ["NVDA"])
        self.assertGreater(concentrated[0]["simulated_allocation"], rows[0]["normalized_allocation"])

    def test_run_policy_simulation_writes_outputs(self):
        summary = run_policy_simulation(
            policies=["baseline", "high_conviction_only", "combined"],
            input_csv=self.csv_path,
            output_dir=self.root / "outputs" / "simulations",
            primary_window_days=3,
        )
        self.assertEqual(len(summary["policies"]), 3)
        self.assertTrue(Path(summary["paths"]["json_path"]).exists())
        self.assertTrue(Path(summary["paths"]["markdown_path"]).exists())
        saved = json.loads(Path(summary["paths"]["json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["filtered_dataset_size"], 3)
        self.assertIn("comparison", saved)
        self.assertIn("category", saved["policies"][0])

    def test_no_mutation_of_original_data(self):
        original = [
            {
                "conviction_band": "high_conviction",
                "regime_label": "risk_on",
                "normalized_allocation": 0.02,
                "return_pct": 5.0,
                "outcome_success": 1,
            }
        ]
        before = copy.deepcopy(original)
        apply_policy(original, POLICIES["conservative_size_cap"])
        self.assertEqual(original, before)

    def test_markdown_contains_comparison_table(self):
        summary = run_policy_simulation(
            policies=["baseline", "risk_on_only"],
            input_csv=self.csv_path,
            output_dir=self.root / "outputs" / "simulations",
            primary_window_days=3,
        )
        markdown = render_policy_simulation_markdown(summary)
        self.assertIn("Comparison", markdown)
        self.assertIn("baseline", markdown)
        self.assertIn("risk_on_only", markdown)
        self.assertIn("Strategy View", markdown)

    def test_profile_cli_path_works(self):
        out_dir = self.root / "outputs" / "simulations"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.policy_simulator",
                "--profile",
                "balanced_growth",
                "--input-csv",
                str(self.csv_path),
                "--output-dir",
                str(out_dir),
            ],
            cwd=Path(__file__).parent.parent,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("policy_simulation.json", result.stdout)
        saved = json.loads((out_dir / "policy_simulation.json").read_text(encoding="utf-8"))
        self.assertIn("balanced_growth", saved["requested_profiles"])
        self.assertGreaterEqual(len(saved["policies"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
