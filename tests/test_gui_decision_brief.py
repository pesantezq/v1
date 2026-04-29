from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gui_operator_data import load_operator_dashboard_data


class TestGuiDecisionBrief(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_json(self, rel_path: str, payload: dict) -> None:
        path = self.root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _system_summary(self, **overrides) -> dict:
        payload = {
            "generated_at": "2026-04-28T09:15:23",
            "top_theme": {
                "name": "AI",
                "persistence": 0.72,
            },
            "top_opportunity": {
                "ticker": "NVDA",
                "conviction_band": "high_conviction",
                "portfolio_fit_label": "strong",
            },
            "data_health": {
                "degraded_mode": False,
                "data_mode": "live",
                "missing_artifact_count": 0,
                "fallback_alerts_used": False,
            },
            "changes": {
                "previous_available": True,
                "changes": [
                    "Structural leverage risk was elevated.",
                    "Top opportunity shifted toward AI leaders.",
                    "Capital deployment stayed selective.",
                    "This fourth item should not surface.",
                ],
                "summary_line": "4 changes detected.",
            },
        }
        payload.update(overrides)
        return payload

    def _decision_plan(self) -> dict:
        return {
            "generated_at": "2026-04-28T09:15:23",
            "run_mode": "daily",
            "observe_only": True,
            "total_decisions": 6,
            "decisions": [
                {
                    "symbol": "QLD",
                    "decision": "SELL",
                    "priority": 0.95,
                    "urgency": "critical",
                    "source": "structural",
                    "reason": "Structural leverage violation on QLD.",
                    "risk_flags": ["leverage_breach"],
                    "inputs_used": {"violation_type": "leverage"},
                },
                {
                    "symbol": "QQQ",
                    "decision": "SELL",
                    "priority": 0.88,
                    "urgency": "high",
                    "source": "structural",
                    "reason": "Structural concentration violation on QQQ.",
                    "risk_flags": ["concentration_breach"],
                    "inputs_used": {"violation_type": "concentration"},
                },
                {
                    "symbol": "VFH",
                    "decision": "SCALE",
                    "priority": 0.55,
                    "urgency": "low",
                    "source": "portfolio",
                    "reason": "Underweight contribution target.",
                    "risk_flags": [],
                    "recommended_amount": 500.0,
                    "inputs_used": {},
                },
                {
                    "symbol": "FANG",
                    "decision": "WAIT",
                    "priority": 0.55,
                    "urgency": "medium",
                    "source": "market",
                    "reason": "Opportunity exists but confidence is not yet strong enough.",
                    "risk_flags": ["low_confidence"],
                    "inputs_used": {},
                },
                {
                    "symbol": "XLRE",
                    "decision": "WAIT",
                    "priority": 0.54,
                    "urgency": "medium",
                    "source": "market",
                    "reason": "Opportunity exists but evidence is still building.",
                    "risk_flags": [],
                    "inputs_used": {},
                },
                {
                    "symbol": "XLE",
                    "decision": "BUY",
                    "priority": 0.10,
                    "urgency": "low",
                    "source": "market",
                    "reason": "Tail item that should be trimmed from the compact summary.",
                    "risk_flags": [],
                    "inputs_used": {},
                },
            ],
        }

    def test_top_decisions_capped_at_five(self):
        self._write_json("outputs/latest/system_decision_summary.json", self._system_summary())
        self._write_json("outputs/latest/decision_plan.json", self._decision_plan())

        bundle = load_operator_dashboard_data(self.root)
        rows = bundle["decision_brief"]["top_decisions"]

        self.assertEqual(5, len(rows))
        self.assertEqual("QLD", rows[0]["symbol"])
        self.assertNotIn("XLE", [row["symbol"] for row in rows])

    def test_risk_focus_capped_at_three(self):
        degraded_summary = self._system_summary(
            data_health={
                "degraded_mode": True,
                "data_mode": "fallback",
                "missing_artifact_count": 2,
                "fallback_alerts_used": True,
            }
        )
        self._write_json("outputs/latest/system_decision_summary.json", degraded_summary)
        self._write_json("outputs/latest/decision_plan.json", self._decision_plan())

        bundle = load_operator_dashboard_data(self.root)
        risk_focus = bundle["decision_brief"]["risk_focus"]

        self.assertLessEqual(len(risk_focus), 3)
        self.assertTrue(any("Concentration risk" in item for item in risk_focus))
        self.assertTrue(any("Leverage risk" in item for item in risk_focus))

    def test_what_changed_capped_at_three(self):
        self._write_json("outputs/latest/system_decision_summary.json", self._system_summary())
        self._write_json("outputs/latest/decision_plan.json", self._decision_plan())

        bundle = load_operator_dashboard_data(self.root)
        changes = bundle["decision_brief"]["what_changed"]

        self.assertEqual(3, len(changes))
        self.assertNotIn("This fourth item should not surface.", changes)

    def test_system_health_hidden_when_normal(self):
        self._write_json("outputs/latest/system_decision_summary.json", self._system_summary())
        self._write_json("outputs/latest/decision_plan.json", self._decision_plan())

        bundle = load_operator_dashboard_data(self.root)

        self.assertEqual([], bundle["decision_brief"]["system_data_health"])

    def test_missing_decision_plan_handled_cleanly(self):
        self._write_json("outputs/latest/system_decision_summary.json", self._system_summary())

        bundle = load_operator_dashboard_data(self.root)
        brief = bundle["decision_brief"]

        self.assertFalse(brief["available"])
        self.assertEqual("Decision plan unavailable.", brief["summary_line"])
        self.assertEqual([], brief["top_decisions"])

