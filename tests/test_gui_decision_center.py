from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gui_operator_data import load_operator_dashboard_data, _normalize_decision_brief


_DECISION_PLAN_REL = "outputs/latest/decision_plan.json"
_SYSTEM_SUMMARY_REL = "outputs/latest/system_decision_summary.json"


def _make_plan(decisions: list[dict] | None = None, observe_only: bool = True) -> dict:
    return {
        "generated_at": "2026-04-29T08:00:00",
        "run_mode": "daily",
        "observe_only": observe_only,
        "total_decisions": len(decisions or []),
        "decisions": decisions or [],
    }


def _make_summary(**overrides) -> dict:
    payload = {
        "generated_at": "2026-04-29T08:00:00",
        "top_theme": {"name": "AI", "persistence": 0.65},
        "top_opportunity": {"ticker": "NVDA", "conviction_band": "high_conviction"},
        "data_health": {
            "degraded_mode": False,
            "data_mode": "live",
            "missing_artifact_count": 0,
            "fallback_alerts_used": False,
        },
        "changes": {
            "previous_available": True,
            "changes": ["Signal A changed.", "Signal B changed.", "Signal C changed.", "Signal D changed."],
            "summary_line": "4 changes detected.",
        },
    }
    payload.update(overrides)
    return payload


def _six_decisions() -> list[dict]:
    return [
        {"symbol": "QLD", "decision": "SELL", "priority": 0.95, "urgency": "critical",
         "source": "structural", "reason": "Leverage violation.", "risk_flags": ["leverage_breach"],
         "inputs_used": {"violation_type": "leverage"}},
        {"symbol": "QQQ", "decision": "SELL", "priority": 0.88, "urgency": "high",
         "source": "structural", "reason": "Concentration violation.", "risk_flags": ["concentration_breach"],
         "inputs_used": {"violation_type": "concentration"}},
        {"symbol": "VFH", "decision": "SCALE", "priority": 0.55, "urgency": "low",
         "source": "portfolio", "reason": "Underweight.", "risk_flags": [], "recommended_amount": 500.0,
         "inputs_used": {}},
        {"symbol": "FANG", "decision": "WAIT", "priority": 0.50, "urgency": "medium",
         "source": "market", "reason": "Confidence building.", "risk_flags": [], "inputs_used": {}},
        {"symbol": "XLRE", "decision": "WAIT", "priority": 0.45, "urgency": "low",
         "source": "market", "reason": "Evidence accumulating.", "risk_flags": [], "inputs_used": {}},
        {"symbol": "XLE", "decision": "BUY", "priority": 0.10, "urgency": "low",
         "source": "market", "reason": "Low-priority tail.", "risk_flags": [], "inputs_used": {}},
    ]


class TestDecisionCenterDataLayer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel: str, payload: dict) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # decision plan loads correctly
    # ------------------------------------------------------------------

    def test_decision_plan_available_when_file_exists(self):
        self._write(_DECISION_PLAN_REL, _make_plan(_six_decisions()))
        bundle = load_operator_dashboard_data(self.root)
        self.assertTrue(bundle["decision_brief"]["available"])

    def test_full_decisions_contains_all_rows(self):
        decisions = _six_decisions()
        self._write(_DECISION_PLAN_REL, _make_plan(decisions))
        bundle = load_operator_dashboard_data(self.root)
        # full_decisions must be the complete set, not capped like top_decisions
        full = bundle["decision_brief"]["full_decisions"]
        self.assertEqual(len(decisions), len(full))

    def test_observe_only_flag_propagated(self):
        self._write(_DECISION_PLAN_REL, _make_plan(_six_decisions(), observe_only=True))
        bundle = load_operator_dashboard_data(self.root)
        self.assertTrue(bundle["decision_brief"]["observe_only"])

    # ------------------------------------------------------------------
    # summary limits enforced
    # ------------------------------------------------------------------

    def test_top_decisions_capped_at_five(self):
        self._write(_DECISION_PLAN_REL, _make_plan(_six_decisions()))
        bundle = load_operator_dashboard_data(self.root)
        self.assertEqual(5, len(bundle["decision_brief"]["top_decisions"]))
        symbols = [r["symbol"] for r in bundle["decision_brief"]["top_decisions"]]
        self.assertNotIn("XLE", symbols)

    def test_top_decisions_sorted_by_priority(self):
        self._write(_DECISION_PLAN_REL, _make_plan(_six_decisions()))
        bundle = load_operator_dashboard_data(self.root)
        rows = bundle["decision_brief"]["top_decisions"]
        priorities = [float(r["priority"]) for r in rows]
        self.assertEqual(sorted(priorities, reverse=True), priorities)

    def test_risk_focus_capped_at_three(self):
        self._write(_SYSTEM_SUMMARY_REL, _make_summary(
            data_health={
                "degraded_mode": True, "data_mode": "fallback",
                "missing_artifact_count": 2, "fallback_alerts_used": True,
            }
        ))
        self._write(_DECISION_PLAN_REL, _make_plan(_six_decisions()))
        bundle = load_operator_dashboard_data(self.root)
        self.assertLessEqual(len(bundle["decision_brief"]["risk_focus"]), 3)

    def test_what_changed_capped_at_three(self):
        self._write(_SYSTEM_SUMMARY_REL, _make_summary())
        self._write(_DECISION_PLAN_REL, _make_plan(_six_decisions()))
        bundle = load_operator_dashboard_data(self.root)
        self.assertEqual(3, len(bundle["decision_brief"]["what_changed"]))

    # ------------------------------------------------------------------
    # missing file handled
    # ------------------------------------------------------------------

    def test_missing_decision_plan_available_false(self):
        bundle = load_operator_dashboard_data(self.root)
        brief = bundle["decision_brief"]
        self.assertFalse(brief["available"])
        self.assertEqual("Decision plan unavailable.", brief["summary_line"])
        self.assertEqual([], brief["top_decisions"])
        self.assertEqual([], brief["full_decisions"])

    def test_missing_plan_path_reported(self):
        bundle = load_operator_dashboard_data(self.root)
        brief = bundle["decision_brief"]
        self.assertIn("decision_plan.json", brief["path"])

    # ------------------------------------------------------------------
    # malformed JSON handled
    # ------------------------------------------------------------------

    def test_malformed_decision_plan_treated_as_missing(self):
        path = self.root / _DECISION_PLAN_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        bundle = load_operator_dashboard_data(self.root)
        brief = bundle["decision_brief"]
        self.assertFalse(brief["available"])
        self.assertEqual([], brief["top_decisions"])

    def test_malformed_system_summary_treated_as_missing(self):
        self._write(_DECISION_PLAN_REL, _make_plan(_six_decisions()))
        path = self.root / _SYSTEM_SUMMARY_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<<<bad>>>", encoding="utf-8")
        bundle = load_operator_dashboard_data(self.root)
        # decision plan still works even when system summary is malformed
        self.assertTrue(bundle["decision_brief"]["available"])

    # ------------------------------------------------------------------
    # system health only shown when degraded
    # ------------------------------------------------------------------

    def test_system_health_empty_under_normal_conditions(self):
        self._write(_SYSTEM_SUMMARY_REL, _make_summary())
        self._write(_DECISION_PLAN_REL, _make_plan(_six_decisions()))
        bundle = load_operator_dashboard_data(self.root)
        self.assertEqual([], bundle["decision_brief"]["system_data_health"])

    def test_system_health_present_when_degraded(self):
        self._write(_SYSTEM_SUMMARY_REL, _make_summary(
            data_health={
                "degraded_mode": True, "data_mode": "fallback",
                "missing_artifact_count": 1, "fallback_alerts_used": False,
            }
        ))
        self._write(_DECISION_PLAN_REL, _make_plan(_six_decisions()))
        bundle = load_operator_dashboard_data(self.root)
        self.assertGreater(len(bundle["decision_brief"]["system_data_health"]), 0)

    # ------------------------------------------------------------------
    # GUI does NOT recompute decisions
    # ------------------------------------------------------------------

    def test_top_decisions_match_source_plan_not_recomputed(self):
        decisions = _six_decisions()
        self._write(_DECISION_PLAN_REL, _make_plan(decisions))
        bundle = load_operator_dashboard_data(self.root)
        top = bundle["decision_brief"]["top_decisions"]
        # The decision field in each row must come directly from the source plan
        source_symbols = {d["symbol"] for d in decisions}
        for row in top:
            self.assertIn(row["symbol"], source_symbols)

    def test_decision_reasons_not_modified(self):
        self._write(_DECISION_PLAN_REL, _make_plan(_six_decisions()))
        bundle = load_operator_dashboard_data(self.root)
        top = bundle["decision_brief"]["top_decisions"]
        qld_row = next((r for r in top if r["symbol"] == "QLD"), None)
        self.assertIsNotNone(qld_row)
        self.assertIn("Leverage violation.", qld_row["reason"])

    def test_capital_action_amounts_from_source(self):
        self._write(_DECISION_PLAN_REL, _make_plan(_six_decisions()))
        bundle = load_operator_dashboard_data(self.root)
        # VFH has recommended_amount=500.0 in source; capital summary total should reflect it
        capital = bundle["decision_brief"]["capital_actions"]
        self.assertEqual(1, capital["scale"])

    # ------------------------------------------------------------------
    # empty decision plan
    # ------------------------------------------------------------------

    def test_empty_decisions_list_produces_empty_top_decisions(self):
        self._write(_DECISION_PLAN_REL, _make_plan([]))
        bundle = load_operator_dashboard_data(self.root)
        brief = bundle["decision_brief"]
        # plan is technically present but has no decisions
        self.assertTrue(brief["available"])
        self.assertEqual([], brief["top_decisions"])

    # ------------------------------------------------------------------
    # _normalize_decision_brief directly
    # ------------------------------------------------------------------

    def test_normalize_decision_brief_suppressed_rows_excluded(self):
        decisions = _six_decisions()
        decisions[0]["suppressed"] = True
        plan = _make_plan(decisions)
        brief = _normalize_decision_brief(decision_plan=plan, system_summary={})
        symbols = [r["symbol"] for r in brief["top_decisions"]]
        self.assertNotIn("QLD", symbols)


if __name__ == "__main__":
    unittest.main()
