import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.policy_recommender import (
    build_policy_recommendation,
    render_policy_recommendation_markdown,
    run_policy_recommendation,
)


class TestPolicyRecommender(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.context_path = self.root / "outputs" / "latest" / "watchlist_signals.json"
        self.regime_path = self.root / "outputs" / "regime" / "regime_performance.json"
        self.simulation_path = self.root / "outputs" / "simulations" / "policy_simulation.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _context(self, *, regime: str = "risk_on", confidence: float = 0.72, degraded: bool = False) -> dict:
        return {
            "degraded_mode": degraded,
            "degraded_reason": "cache_only" if degraded else None,
            "market_regime": {
                "regime_label": regime,
                "regime_confidence": confidence,
                "regime_data_quality": "partial",
                "regime_reasoning": "test regime context",
            },
        }

    def _strong_regime_performance(self, regime: str = "risk_on") -> dict:
        return {
            "by_regime": {
                regime: {
                    "total_signals": 12,
                    "win_rate": 0.67,
                    "avg_return_pct": 3.1,
                    "best_conviction_band": "high_conviction",
                    "worst_conviction_band": "starter",
                }
            }
        }

    def _strong_simulation(self) -> dict:
        return {
            "comparison": {
                "best_by_win_rate": "quality_growth",
                "best_by_drawdown": "defensive_rotation",
                "best_policy_by_regime": {"risk_on": "quality_growth"},
                "best_degraded_mode_policy": "degraded_safe_mode",
            },
            "policies": [
                {
                    "policy": "quality_growth",
                    "total_trades": 10,
                    "win_rate": 0.72,
                    "avg_return_pct": 4.0,
                    "max_drawdown_pct": 4.0,
                    "performance_by_regime": {
                        "risk_on": {"total_trades": 6, "win_rate": 0.75, "avg_return_pct": 4.3}
                    },
                },
                {
                    "policy": "regime_aligned",
                    "total_trades": 8,
                    "win_rate": 0.68,
                    "avg_return_pct": 3.0,
                    "max_drawdown_pct": 5.0,
                    "performance_by_regime": {
                        "risk_on": {"total_trades": 5, "win_rate": 0.70, "avg_return_pct": 3.4}
                    },
                },
                {
                    "policy": "high_quality_concentrated",
                    "total_trades": 7,
                    "win_rate": 0.64,
                    "avg_return_pct": 3.8,
                    "max_drawdown_pct": 7.0,
                    "performance_by_regime": {
                        "risk_on": {"total_trades": 4, "win_rate": 0.66, "avg_return_pct": 4.0}
                    },
                },
                {
                    "policy": "defensive_rotation",
                    "total_trades": 6,
                    "win_rate": 0.55,
                    "avg_return_pct": 1.1,
                    "max_drawdown_pct": 2.5,
                    "performance_by_regime": {
                        "risk_on": {"total_trades": 2, "win_rate": 0.50, "avg_return_pct": 0.8}
                    },
                },
            ],
        }

    def _sparse_simulation(self) -> dict:
        return {
            "comparison": {},
            "policies": [
                {
                    "policy": "quality_growth",
                    "total_trades": 2,
                    "win_rate": 1.0,
                    "avg_return_pct": 5.0,
                    "max_drawdown_pct": 1.0,
                    "performance_by_regime": {
                        "risk_on": {"total_trades": 1, "win_rate": 1.0, "avg_return_pct": 5.0}
                    },
                }
            ],
        }

    def test_regime_based_recommendation_mapping(self):
        summary = build_policy_recommendation(
            current_context=self._context(regime="risk_on", confidence=0.70, degraded=False)["market_regime"] | {
                "degraded_mode": False,
                "degraded_reason": None,
            },
            regime_performance=self._strong_regime_performance(),
            policy_simulation=self._strong_simulation(),
        )

        self.assertEqual(summary["recommendation"]["recommended_policy"], "quality_growth")
        self.assertEqual(summary["recommendation"]["recommended_profile"], "balanced_growth")

    def test_degraded_mode_override_behavior(self):
        summary = build_policy_recommendation(
            current_context={
                "regime_label": "risk_on",
                "regime_confidence": 0.71,
                "degraded_mode": True,
                "degraded_reason": "cache_only",
                "regime_data_quality": "degraded",
                "regime_reasoning": "test",
            },
            regime_performance=self._strong_regime_performance(),
            policy_simulation=self._strong_simulation(),
        )

        self.assertEqual(summary["recommendation"]["recommendation_source"], "degraded_mode_override")
        self.assertIn(summary["recommendation"]["recommended_profile"], {"conservative_observe", "defensive_quality"})
        self.assertIn(summary["recommendation"]["recommended_policy"], {"quality_growth", "conservative_size_cap", "degraded_safe_mode", "defensive_rotation"})

    def test_sparse_data_fallback_behavior(self):
        summary = build_policy_recommendation(
            current_context={
                "regime_label": "risk_on",
                "regime_confidence": 0.63,
                "degraded_mode": False,
                "degraded_reason": None,
                "regime_data_quality": "limited",
                "regime_reasoning": "test",
            },
            regime_performance={"by_regime": {"risk_on": {"total_signals": 1}}},
            policy_simulation=self._sparse_simulation(),
        )

        self.assertEqual(summary["recommendation"]["recommendation_source"], "rule_based_fallback")
        self.assertEqual(summary["recommendation"]["recommendation_data_quality"], "sparse_simulation_history")
        self.assertIn("limited", (summary["recommendation"]["recommendation_quality_note"] or "").lower())

    def test_recommendation_confidence_decreases_when_data_is_weak(self):
        strong = build_policy_recommendation(
            current_context={
                "regime_label": "risk_on",
                "regime_confidence": 0.72,
                "degraded_mode": False,
                "degraded_reason": None,
                "regime_data_quality": "partial",
                "regime_reasoning": "test",
            },
            regime_performance=self._strong_regime_performance(),
            policy_simulation=self._strong_simulation(),
        )
        weak = build_policy_recommendation(
            current_context={
                "regime_label": "risk_on",
                "regime_confidence": 0.72,
                "degraded_mode": False,
                "degraded_reason": None,
                "regime_data_quality": "limited",
                "regime_reasoning": "test",
            },
            regime_performance={"by_regime": {"risk_on": {"total_signals": 1}}},
            policy_simulation=self._sparse_simulation(),
        )

        self.assertGreater(
            strong["recommendation"]["recommendation_confidence"],
            weak["recommendation"]["recommendation_confidence"],
        )

    def test_output_artifact_creation(self):
        self._write_json(self.context_path, self._context())
        self._write_json(self.regime_path, self._strong_regime_performance())
        self._write_json(self.simulation_path, self._strong_simulation())

        summary = run_policy_recommendation(
            input_context_json=self.context_path,
            input_regime_json=self.regime_path,
            input_simulation_json=self.simulation_path,
            output_dir=self.root / "outputs" / "policy",
        )

        json_path = Path(summary["paths"]["json_path"])
        md_path = Path(summary["paths"]["markdown_path"])
        self.assertTrue(json_path.exists())
        self.assertTrue(md_path.exists())
        saved = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertIn("recommendation", saved)
        self.assertIn("alternatives", saved)
        self.assertIn("quality_growth", md_path.read_text(encoding="utf-8"))

    def test_no_mutation_of_underlying_simulation_data(self):
        self._write_json(self.context_path, self._context())
        self._write_json(self.regime_path, self._strong_regime_performance())
        self._write_json(self.simulation_path, self._strong_simulation())
        before_regime = self.regime_path.read_text(encoding="utf-8")
        before_simulation = self.simulation_path.read_text(encoding="utf-8")

        run_policy_recommendation(
            input_context_json=self.context_path,
            input_regime_json=self.regime_path,
            input_simulation_json=self.simulation_path,
            output_dir=self.root / "outputs" / "policy",
        )

        self.assertEqual(before_regime, self.regime_path.read_text(encoding="utf-8"))
        self.assertEqual(before_simulation, self.simulation_path.read_text(encoding="utf-8"))

    def test_cli_path_works(self):
        self._write_json(self.context_path, self._context())
        self._write_json(self.regime_path, self._strong_regime_performance())
        self._write_json(self.simulation_path, self._strong_simulation())
        out_dir = self.root / "outputs" / "policy"

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.policy_recommender",
                "--input-context-json",
                str(self.context_path),
                "--input-regime-json",
                str(self.regime_path),
                "--input-simulation-json",
                str(self.simulation_path),
                "--output-dir",
                str(out_dir),
            ],
            cwd=Path(__file__).parent.parent,
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertIn("policy_recommendation.json", result.stdout)
        saved = json.loads((out_dir / "policy_recommendation.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["recommendation"]["recommended_profile"], "balanced_growth")

    def test_markdown_rendering_includes_formula_and_support(self):
        summary = build_policy_recommendation(
            current_context={
                "regime_label": "risk_on",
                "regime_confidence": 0.72,
                "degraded_mode": False,
                "degraded_reason": None,
                "regime_data_quality": "partial",
                "regime_reasoning": "test",
            },
            regime_performance=self._strong_regime_performance(),
            policy_simulation=self._strong_simulation(),
        )
        markdown = render_policy_recommendation_markdown(summary)
        self.assertIn("## Formula", markdown)
        self.assertIn("Best recent policy by win rate", markdown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
