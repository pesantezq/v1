from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from portfolio_automation.decision_explainer import (
    AI_VALIDATION_BOOST,
    AI_VALIDATION_CAUTION,
    AI_VALIDATION_NEUTRAL,
    build_decision_explanations,
    generate_decision_explanations,
)


_DECISION_PLAN_REL = Path("outputs/latest/decision_plan.json")
_SYSTEM_SUMMARY_REL = Path("outputs/latest/system_decision_summary.json")
_EXPLANATIONS_JSON_REL = Path("outputs/latest/decision_explanations.json")
_EXPLANATIONS_MD_REL = Path("outputs/latest/decision_explanations.md")


def _plan(decisions: list[dict] | None = None) -> dict:
    return {
        "generated_at": "2026-04-29T09:00:00",
        "run_mode": "daily",
        "observe_only": True,
        "total_decisions": len(decisions or []),
        "decisions": decisions or [],
    }


def _system_summary(*, degraded: bool = False) -> dict:
    return {
        "generated_at": "2026-04-29T09:00:00",
        "top_theme": {"name": "AI Infrastructure", "persistence": 0.72},
        "top_opportunity": {"ticker": "NVDA", "conviction_band": "high_conviction"},
        "data_health": {
            "degraded_mode": degraded,
            "data_mode": "fallback" if degraded else "live",
            "missing_artifact_count": 1 if degraded else 0,
            "fallback_alerts_used": degraded,
        },
        "changes": {
            "previous_available": True,
            "changes": ["Top decision set changed."],
            "summary_line": "1 change detected.",
        },
    }


def _sample_decisions() -> list[dict]:
    return [
        {
            "symbol": "QLD",
            "decision": "SELL",
            "priority": 0.95,
            "urgency": "critical",
            "source": "structural",
            "reason": "STRUCTURAL: Reduce total leveraged exposure 17.8% to below 15% cap. More detail.",
            "risk_flags": ["leverage_breach", "degraded_mode", "drawdown_override", "extra_flag"],
            "confidence": 0.91,
            "current_pct": 0.178,
            "cap_pct": 0.15,
            "inputs_used": {"violation_type": "leverage"},
        },
        {
            "symbol": "QQQ",
            "decision": "SELL",
            "priority": 0.88,
            "urgency": "high",
            "source": "structural",
            "reason": "STRUCTURAL: Current concentration is 55.2% vs 40% cap. Extra detail.",
            "risk_flags": ["concentration_breach"],
            "confidence": 0.90,
            "inputs_used": {"violation_type": "concentration"},
        },
        {
            "symbol": "VFH",
            "decision": "SCALE",
            "priority": 0.55,
            "urgency": "low",
            "source": "portfolio",
            "reason": "Underweight contribution target. Rebalance needed.",
            "risk_flags": [],
            "confidence": 0.82,
            "inputs_used": {},
        },
        {
            "symbol": "FANG",
            "decision": "WAIT",
            "priority": 0.55,
            "urgency": "medium",
            "source": "market",
            "reason": "Relative strength is strong and relative strength remains near highs.",
            "risk_flags": [],
            "confidence": 0.61,
            "inputs_used": {},
        },
        {
            "symbol": "SMH",
            "decision": "BUY",
            "priority": 0.62,
            "urgency": "high",
            "source": "watchlist",
            "reason": "Momentum breakout setup near highs with improving breadth.",
            "risk_flags": [],
            "confidence": 0.86,
            "inputs_used": {"conviction_band": "high_conviction"},
        },
        {
            "symbol": "XLE",
            "decision": "WAIT",
            "priority": 0.20,
            "urgency": "low",
            "source": "market",
            "reason": "Tail item should not appear in explanations.",
            "risk_flags": [],
            "confidence": 0.50,
            "inputs_used": {},
        },
    ]


class TestDecisionExplainer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_json(self, rel_path: Path, payload: dict) -> None:
        path = self.root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def test_missing_decision_plan_handled_gracefully(self):
        payload, markdown = generate_decision_explanations(self.root)

        self.assertFalse(payload["available"])
        self.assertEqual("Decision plan unavailable.", payload["summary_line"])
        self.assertEqual([], payload["explanations"])
        self.assertIn("Decision plan unavailable.", markdown)
        self.assertTrue((self.root / _EXPLANATIONS_JSON_REL).exists())
        self.assertTrue((self.root / _EXPLANATIONS_MD_REL).exists())

    def test_malformed_decision_plan_handled_gracefully(self):
        path = self.root / _DECISION_PLAN_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{bad json", encoding="utf-8")

        payload, markdown = generate_decision_explanations(self.root)

        self.assertFalse(payload["available"])
        self.assertEqual("Decision plan malformed.", payload["summary_line"])
        self.assertEqual([], payload["explanations"])
        self.assertIn("Decision plan malformed.", markdown)

    def test_valid_explanation_output_written(self):
        self._write_json(_DECISION_PLAN_REL, _plan(_sample_decisions()))
        self._write_json(_SYSTEM_SUMMARY_REL, _system_summary())

        payload, markdown = generate_decision_explanations(self.root)

        self.assertTrue(payload["available"])
        self.assertEqual(5, len(payload["explanations"]))
        self.assertIn("Decision Explanations", markdown)
        self.assertIn("Leverage exceeds cap (17.8% vs 15%).", markdown)
        self.assertTrue((self.root / _EXPLANATIONS_JSON_REL).exists())
        self.assertTrue((self.root / _EXPLANATIONS_MD_REL).exists())

        qld = payload["explanations"][0]
        self.assertEqual("QLD", qld["symbol"])
        self.assertEqual("SELL", qld["action"])
        self.assertEqual("structural", qld["source"])
        self.assertEqual("Leverage exceeds cap (17.8% vs 15%).", qld["concise_explanation"])
        self.assertEqual(["leverage_breach", "degraded_mode", "drawdown_override"], qld["risks"])
        self.assertEqual(AI_VALIDATION_CAUTION, qld["ai_validation"])

    def test_compact_limits_enforced(self):
        self._write_json(_DECISION_PLAN_REL, _plan(_sample_decisions()))
        self._write_json(_SYSTEM_SUMMARY_REL, _system_summary(degraded=True))

        payload, _ = generate_decision_explanations(self.root)

        self.assertEqual(5, len(payload["explanations"]))
        for row in payload["explanations"]:
            self.assertLessEqual(len(row["risks"]), 3)
            self.assertLessEqual(len(row["what_to_watch_next"]), 3)
            self.assertLessEqual(len(row["explanation_basis"]), 5)
            self.assertLessEqual(len(row["concise_explanation"]), 100)

    def test_input_plan_not_mutated(self):
        plan = _plan(_sample_decisions())
        original = copy.deepcopy(plan)

        payload = build_decision_explanations(plan, _system_summary())

        self.assertEqual(original, plan)
        self.assertEqual(5, len(payload["explanations"]))
        self.assertEqual(0.95, payload["explanations"][0]["priority"])
        self.assertEqual("SELL", payload["explanations"][0]["action"])

    def test_deterministic_validation_labels(self):
        plan = _plan(_sample_decisions())

        normal_payload = build_decision_explanations(plan, _system_summary())
        labels = {row["symbol"]: row["ai_validation"] for row in normal_payload["explanations"]}
        self.assertEqual(AI_VALIDATION_CAUTION, labels["QLD"])
        self.assertEqual(AI_VALIDATION_NEUTRAL, labels["FANG"])
        self.assertEqual(AI_VALIDATION_BOOST, labels["SMH"])

        degraded_payload = build_decision_explanations(plan, _system_summary(degraded=True))
        degraded_labels = {row["symbol"]: row["ai_validation"] for row in degraded_payload["explanations"]}
        self.assertEqual(AI_VALIDATION_CAUTION, degraded_labels["SMH"])


if __name__ == "__main__":
    unittest.main()
