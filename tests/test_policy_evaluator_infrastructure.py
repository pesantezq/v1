from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from policy_evaluator.history_writer import load_history
from policy_evaluator.infrastructure import (
    align_row_to_forward_window,
    append_jsonl_records,
    build_forward_return_inputs,
    build_forward_window_boundaries,
    build_mfe_mae_inputs,
    load_recommendation_history,
    normalize_history_row,
    parse_timestamp,
    read_policy_recommendation,
    read_policy_simulation,
    read_regime_performance,
)


class TestPolicyEvaluatorInfrastructure(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.history_path = self.root / "outputs" / "policy" / "recommendation_history.jsonl"
        self.policy_recommendation_path = self.root / "outputs" / "policy" / "policy_recommendation.json"
        self.regime_path = self.root / "outputs" / "regime" / "regime_performance.json"
        self.simulation_path = self.root / "outputs" / "simulations" / "policy_simulation.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def test_safe_artifact_readers_handle_missing_files(self):
        self.assertIsNone(read_policy_recommendation(self.policy_recommendation_path))
        self.assertIsNone(read_regime_performance(self.regime_path))
        self.assertIsNone(read_policy_simulation(self.simulation_path))

    def test_load_recommendation_history_handles_empty_history(self):
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.write_text("", encoding="utf-8")
        self.assertEqual(load_recommendation_history(self.history_path), [])
        self.assertEqual(load_history(self.history_path), [])

    def test_load_recommendation_history_skips_malformed_rows(self):
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.write_text(
            json.dumps({"run_id": "r1", "timestamp": "2026-04-16T10:00:00", "rec_id": "a"}) + "\n"
            "{bad json\n"
            + json.dumps(["not", "an", "object"]) + "\n",
            encoding="utf-8",
        )
        rows = load_recommendation_history(self.history_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rec_id"], "a")

    def test_normalize_history_row_handles_old_schema_rows(self):
        row = normalize_history_row(
            {
                "run_id": "r1",
                "timestamp": "2026-04-16T10:00:00",
                "rec_id": "emergency_fund_2026-04-16",
                "score": 88,
                "action_level": "Action Required",
            }
        )
        self.assertEqual(row["rec_base_id"], "emergency_fund")
        self.assertEqual(row["confidence"], 100)
        self.assertEqual(row["regime"], "unknown")
        self.assertIsNone(row["recommended_policy"])
        self.assertIsNone(row["recommendation_confidence"])

    def test_normalize_history_row_extracts_nested_recommendation_fields(self):
        row = normalize_history_row(
            {
                "run_id": "r2",
                "generated_at": "2026-04-16T11:00:00Z",
                "recommendation": {
                    "recommended_policy": "quality_growth",
                    "recommended_profile": "balanced_growth",
                    "recommendation_confidence": 0.71,
                    "recommendation_score": 0.88,
                    "recommendation_source": "performance_backed_logic",
                },
                "current_context": {
                    "regime_label": "risk_on",
                    "degraded_mode": False,
                },
            }
        )
        self.assertEqual(row["recommended_policy"], "quality_growth")
        self.assertEqual(row["recommended_profile"], "balanced_growth")
        self.assertEqual(row["regime"], "risk_on")
        self.assertAlmostEqual(row["recommendation_confidence"], 0.71)
        self.assertEqual(row["timestamp"], "2026-04-16T11:00:00")

    def test_parse_timestamp_and_window_alignment_are_consistent(self):
        parsed = parse_timestamp("2026-04-16T15:30:00Z")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.isoformat(), "2026-04-16T15:30:00")

        windows = build_forward_window_boundaries("2026-04-16T15:30:00Z")
        self.assertEqual(windows["1d"]["window_end"], "2026-04-17T15:30:00")
        self.assertEqual(windows["10d"]["window_end"], "2026-04-26T15:30:00")

        row = normalize_history_row(
            {
                "run_id": "r3",
                "timestamp": "2026-04-16T15:30:00Z",
                "rec_id": "rec_1",
                "recommendation": {"recommended_policy": "quality_growth"},
            }
        )
        aligned = align_row_to_forward_window(row, window_days=5)
        self.assertIsNotNone(aligned)
        self.assertEqual(aligned["window"]["window_start"], "2026-04-16T15:30:00")
        self.assertEqual(aligned["window"]["window_end"], "2026-04-21T15:30:00")

    def test_forward_return_and_mfe_mae_helpers_share_window_inputs(self):
        row = normalize_history_row(
            {
                "run_id": "r4",
                "timestamp": "2026-04-16",
                "rec_id": "rec_2",
                "recommendation": {
                    "recommended_policy": "quality_growth",
                    "recommended_profile": "balanced_growth",
                },
                "current_context": {"regime_label": "risk_on"},
            }
        )
        forward_inputs = build_forward_return_inputs(row, window_days=3)
        mfe_mae_inputs = build_mfe_mae_inputs(row, window_days=3)
        self.assertEqual(forward_inputs["window_end"], "2026-04-19T00:00:00")
        self.assertEqual(mfe_mae_inputs["window_end"], "2026-04-19T00:00:00")
        self.assertEqual(forward_inputs["recommended_policy"], "quality_growth")
        self.assertEqual(mfe_mae_inputs["recommended_profile"], "balanced_growth")

    def test_append_only_behavior_preserves_old_rows_and_creates_directories(self):
        count_one = append_jsonl_records([{"run_id": "r1", "timestamp": "2026-04-16T10:00:00"}], self.history_path)
        count_two = append_jsonl_records([{"run_id": "r2", "timestamp": "2026-04-17T10:00:00"}], self.history_path)
        self.assertEqual(count_one, 1)
        self.assertEqual(count_two, 1)

        lines = self.history_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["run_id"], "r1")
        self.assertEqual(json.loads(lines[1])["run_id"], "r2")

    def test_safe_readers_return_payloads_without_mutating_unrelated_outputs(self):
        policy_payload = {"recommendation": {"recommended_policy": "quality_growth"}}
        regime_payload = {"by_regime": {"risk_on": {"total_signals": 3}}}
        simulation_payload = {"policies": [{"policy": "quality_growth"}]}
        unrelated_path = self.root / "outputs" / "latest" / "watchlist_signals.json"
        unrelated_payload = {"results": [{"ticker": "NVDA"}]}

        self._write_json(self.policy_recommendation_path, policy_payload)
        self._write_json(self.regime_path, regime_payload)
        self._write_json(self.simulation_path, simulation_payload)
        self._write_json(unrelated_path, unrelated_payload)
        before_unrelated = unrelated_path.read_text(encoding="utf-8")

        self.assertEqual(read_policy_recommendation(self.policy_recommendation_path), policy_payload)
        self.assertEqual(read_regime_performance(self.regime_path), regime_payload)
        self.assertEqual(read_policy_simulation(self.simulation_path), simulation_payload)
        self.assertEqual(before_unrelated, unrelated_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
