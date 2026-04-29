from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gui_operator_data import (
    load_operator_dashboard_data,
    _normalize_decision_brief,
    _compact_decision_reason,
    load_decision_explanations,
    _ai_validation_badge,
    _get_insight_cards,
)


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

    def test_system_health_reports_missing_artifact_paths_and_producers(self):
        self._write(_SYSTEM_SUMMARY_REL, _make_summary(
            data_health={
                "degraded_mode": True,
                "data_mode": "fallback",
                "missing_artifact_count": 2,
                "fallback_alerts_used": False,
                "missing_artifact_details": [
                    {
                        "artifact": "watchlist_signals",
                        "path": "outputs/latest/watchlist_signals.json",
                        "producer_step": "watchlist scanner",
                    },
                    {
                        "artifact": "theme_signals",
                        "path": "outputs/latest/theme_signals.json",
                        "producer_step": "theme engine",
                    },
                ],
            }
        ))
        self._write(_DECISION_PLAN_REL, _make_plan(_six_decisions()))
        bundle = load_operator_dashboard_data(self.root)
        joined = " ".join(bundle["decision_brief"]["system_data_health"])
        self.assertIn("outputs/latest/watchlist_signals.json (watchlist scanner)", joined)
        self.assertIn("outputs/latest/theme_signals.json (theme engine)", joined)

    def test_system_health_reports_defaulting_and_optional_artifacts(self):
        self._write(_SYSTEM_SUMMARY_REL, _make_summary(
            data_health={
                "degraded_mode": True,
                "data_mode": "live",
                "missing_artifact_count": 0,
                "fallback_alerts_used": False,
                "missing_artifact_details": [],
                "defaulting_artifact_details": [
                    {
                        "artifact": "approved_ranking_config",
                        "path": "outputs/performance/approved_ranking_config.json",
                        "producer_step": "ranking config promotion",
                    }
                ],
                "optional_artifact_details": [
                    {
                        "artifact": "theme_opportunities",
                        "path": "outputs/latest/theme_opportunities.json",
                        "producer_step": "theme discovery",
                    }
                ],
            }
        ))
        self._write(_DECISION_PLAN_REL, _make_plan(_six_decisions()))
        bundle = load_operator_dashboard_data(self.root)
        joined = " ".join(bundle["decision_brief"]["system_data_health"])
        self.assertIn("Defaulting because artifacts are not present", joined)
        self.assertIn("outputs/performance/approved_ranking_config.json (ranking config promotion)", joined)
        self.assertIn("Optional artifacts not present", joined)
        self.assertIn("outputs/latest/theme_opportunities.json (theme discovery)", joined)

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

    def test_top_decision_summary_uses_compact_reason_not_raw_structural_text(self):
        decisions = _six_decisions()
        decisions[0]["reason"] = (
            "STRUCTURAL: Reduce total leveraged exposure 18.0% to below 15% cap. "
            "This second sentence should never appear in the summary."
        )
        decisions[0]["current_pct"] = 0.18
        decisions[0]["cap_pct"] = 0.15
        decisions[1]["reason"] = (
            "STRUCTURAL: Current concentration is 48% vs 40% cap. "
            "This detail should also stay out of the compact summary."
        )
        self._write(_DECISION_PLAN_REL, _make_plan(decisions))
        bundle = load_operator_dashboard_data(self.root)
        top = bundle["decision_brief"]["top_decisions"]

        qld_row = next(r for r in top if r["symbol"] == "QLD")
        qqq_row = next(r for r in top if r["symbol"] == "QQQ")
        vfh_row = next(r for r in top if r["symbol"] == "VFH")

        self.assertEqual("Leverage exceeds cap (18% vs 15%).", qld_row["compact_reason"])
        self.assertEqual("Concentration exceeds cap (48% vs 40%).", qqq_row["compact_reason"])
        self.assertEqual("Drift exceeds rebalance threshold.", vfh_row["compact_reason"])
        self.assertNotIn("Structural leverage violation", qld_row["compact_reason"])
        self.assertNotIn("Reduce total leveraged exposure", qld_row["compact_reason"])
        self.assertNotIn("This second sentence", qld_row["compact_reason"])

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


class TestCompactDecisionReason(unittest.TestCase):
    """Unit tests for _compact_decision_reason — pure function, no Streamlit."""

    # ------------------------------------------------------------------
    # short_reason preferred
    # ------------------------------------------------------------------

    def test_short_reason_preferred_over_reason(self):
        row = {"short_reason": "Leverage exceeds cap.", "reason": "STRUCTURAL: Long text. | More text."}
        self.assertEqual("Leverage exceeds cap.", _compact_decision_reason(row))

    def test_short_reason_capped_at_80(self):
        row = {"short_reason": "This is a very long custom reason that should be capped cleanly at a word boundary without broken formatting"}
        result = _compact_decision_reason(row)
        self.assertLessEqual(len(result), 80)
        self.assertTrue(result.endswith("."))

    # ------------------------------------------------------------------
    # mapped structural reasons
    # ------------------------------------------------------------------

    def test_leverage_case_formats_correctly(self):
        row = {
            "source": "structural",
            "reason": "STRUCTURAL: Reduce total leveraged exposure 18.0% to below 15% cap.",
            "risk_flags": ["leverage_breach"],
            "inputs_used": {"violation_type": "leverage", "current_pct": 0.18, "cap_pct": 0.15},
        }
        self.assertEqual("Leverage exceeds cap (18% vs 15%).", _compact_decision_reason(row))

    def test_concentration_case_formats_correctly(self):
        row = {
            "source": "structural",
            "reason": "structural: Current concentration is 48% vs 40% cap. Detail.",
            "risk_flags": ["concentration_breach"],
            "inputs_used": {"violation_type": "concentration"},
        }
        self.assertEqual("Concentration exceeds cap (48% vs 40%).", _compact_decision_reason(row))

    # ------------------------------------------------------------------
    # portfolio / market mappings
    # ------------------------------------------------------------------

    def test_rebalance_case_formats_correctly(self):
        row = {"reason": "Underweight position. Rebalance needed. | Extra detail."}
        self.assertEqual("Drift exceeds rebalance threshold.", _compact_decision_reason(row))

    def test_momentum_case_formats_correctly(self):
        row = {"reason": "Momentum breakout setup near highs with improving breadth."}
        self.assertEqual("Momentum breakout near highs.", _compact_decision_reason(row))

    def test_relative_strength_case_formats_correctly(self):
        row = {"reason": "RS signal is strong and relative strength remains near highs."}
        self.assertEqual("Relative strength near highs.", _compact_decision_reason(row))

    # ------------------------------------------------------------------
    # compact safety rules
    # ------------------------------------------------------------------

    def test_result_never_exceeds_80_chars(self):
        row = {"reason": ("Leverage very high. " * 20)}
        self.assertLessEqual(len(_compact_decision_reason(row)), 80)

    def test_result_never_exceeds_80_chars_no_period(self):
        row = {"reason": "A" * 200}
        result = _compact_decision_reason(row)
        self.assertLessEqual(len(result), 80)
        self.assertTrue(result.endswith("."))

    def test_word_boundary_cap_no_mid_word_cut(self):
        long_sentence = "Leverage exceeds cap and requires immediate action to " + "reduce " * 12 + "exposure."
        row = {"reason": long_sentence}
        result = _compact_decision_reason(row)
        self.assertLessEqual(len(result), 80)
        self.assertNotIn("...", result)
        self.assertTrue(result.endswith("."))

    # ------------------------------------------------------------------
    # fallback cleanup
    # ------------------------------------------------------------------

    def test_first_sentence_returned(self):
        row = {"reason": "Leverage exceeds cap. This second sentence should not appear."}
        result = _compact_decision_reason(row)
        self.assertEqual("Leverage exceeds cap.", result)

    def test_first_sentence_question_mark(self):
        row = {"reason": "Is leverage too high? Yes it is."}
        result = _compact_decision_reason(row)
        self.assertEqual("Is leverage too high?", result)

    def test_pipe_segment_only_first_used(self):
        row = {"reason": "First segment info. | STRUCTURAL: Second segment should be hidden."}
        result = _compact_decision_reason(row)
        self.assertEqual("First segment info.", result)

    def test_no_broken_suffix_marker(self):
        row = {"reason": "STRUCTURAL: Leverage exceeds cap ...(+2 more details) and should be cleaned."}
        result = _compact_decision_reason(row)
        self.assertNotIn("...(+2", result)

    # ------------------------------------------------------------------
    # empty / missing reason
    # ------------------------------------------------------------------

    def test_empty_reason_returns_placeholder(self):
        self.assertEqual("No rationale provided.", _compact_decision_reason({}))

    def test_none_reason_returns_placeholder(self):
        self.assertEqual("No rationale provided.", _compact_decision_reason({"reason": None}))

    # ------------------------------------------------------------------
    # top decision header fields present (data layer)
    # ------------------------------------------------------------------

    def test_top_decision_rows_have_required_header_fields(self):
        """Every field needed for ACTION SYMBOL | source | urgency | pri is present."""
        import tempfile, json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "outputs" / "latest" / "decision_plan.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(json.dumps(_make_plan(_six_decisions())), encoding="utf-8")
            bundle = load_operator_dashboard_data(root)
        for row in bundle["decision_brief"]["top_decisions"]:
            self.assertIn("decision", row)
            self.assertIn("symbol", row)
            self.assertIn("source", row)
            self.assertIn("urgency", row)
            self.assertIn("priority", row)

    def test_top_decisions_still_capped_at_five(self):
        """Compact reason changes must not affect the 5-decision cap."""
        import tempfile, json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "outputs" / "latest" / "decision_plan.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(json.dumps(_make_plan(_six_decisions())), encoding="utf-8")
            bundle = load_operator_dashboard_data(root)
        self.assertEqual(5, len(bundle["decision_brief"]["top_decisions"]))

    def test_full_decisions_contain_untruncated_reason(self):
        """Long reasons must survive intact in the full queue."""
        long_reason = "Leverage exceeds cap. " * 10
        decisions = _six_decisions()
        decisions[0]["reason"] = long_reason
        brief = _normalize_decision_brief(
            decision_plan=_make_plan(decisions),
            system_summary={},
        )
        full_reason = next(
            d["reason"] for d in brief["full_decisions"] if d.get("symbol") == "QLD"
        )
        self.assertEqual(long_reason, full_reason)
        # compact version must be shorter and cleanly capped
        compact = _compact_decision_reason({"reason": long_reason})
        self.assertLess(len(compact), len(long_reason))
        self.assertLessEqual(len(compact), 80)


_EXPLANATIONS_REL = "outputs/latest/decision_explanations.json"


def _make_expl(symbol="QLD", action="SELL", validation="caution", risks=None, watch=None) -> dict:
    return {
        "decision_id": f"1-{symbol}-{action}-structural",
        "symbol": symbol,
        "action": action,
        "priority": 0.95,
        "urgency": "critical",
        "source": "structural",
        "concise_explanation": "Leverage exceeds cap.",
        "risks": risks if risks is not None else ["leverage_breach"],
        "what_to_watch_next": watch if watch is not None else ["Leverage after trim."],
        "explanation_basis": ["source:structural"],
        "ai_validation": validation,
    }


def _make_explanations_payload(explanations=None, available=True) -> dict:
    rows = explanations or []
    return {
        "generated_at": "2026-04-29T08:00:00",
        "available": available,
        "observe_only": True,
        "summary_line": f"{len(rows)} explanations generated.",
        "explanations": rows,
    }


class TestAIInsightCards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_explanations(self, payload: dict) -> None:
        path = self.root / _EXPLANATIONS_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # load correctly
    # ------------------------------------------------------------------

    def test_explanations_load_correctly(self):
        payload = _make_explanations_payload([_make_expl()])
        self._write_explanations(payload)
        result = load_decision_explanations(self.root)
        self.assertTrue(result["available"])
        self.assertEqual(1, len(result["explanations"]))
        self.assertEqual("QLD", result["explanations"][0]["symbol"])

    def test_explanations_in_bundle(self):
        payload = _make_explanations_payload([_make_expl()])
        self._write_explanations(payload)
        bundle = load_operator_dashboard_data(self.root)
        self.assertIn("decision_explanations", bundle)
        self.assertTrue(bundle["decision_explanations"]["available"])

    # ------------------------------------------------------------------
    # missing file handled
    # ------------------------------------------------------------------

    def test_missing_file_returns_unavailable(self):
        result = load_decision_explanations(self.root)
        self.assertFalse(result["available"])
        self.assertIn("No AI explanations available", result["summary_line"])
        self.assertEqual([], result["explanations"])

    def test_missing_file_does_not_raise(self):
        try:
            load_decision_explanations(self.root)
        except Exception as exc:
            self.fail(f"load_decision_explanations raised unexpectedly: {exc}")

    # ------------------------------------------------------------------
    # malformed file handled
    # ------------------------------------------------------------------

    def test_malformed_json_returns_unavailable(self):
        path = self.root / _EXPLANATIONS_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{bad json", encoding="utf-8")
        result = load_decision_explanations(self.root)
        self.assertFalse(result["available"])
        self.assertEqual([], result["explanations"])

    def test_malformed_non_dict_returns_unavailable(self):
        path = self.root / _EXPLANATIONS_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2, 3]", encoding="utf-8")
        result = load_decision_explanations(self.root)
        self.assertFalse(result["available"])

    # ------------------------------------------------------------------
    # available=False in payload passes through
    # ------------------------------------------------------------------

    def test_available_false_in_payload_returns_unavailable(self):
        payload = _make_explanations_payload([_make_expl()], available=False)
        self._write_explanations(payload)
        result = load_decision_explanations(self.root)
        self.assertFalse(result["available"])
        self.assertEqual([], result["explanations"])

    # ------------------------------------------------------------------
    # max 5 cards enforced
    # ------------------------------------------------------------------

    def test_max_5_cards_enforced(self):
        rows = [_make_expl(symbol=str(i)) for i in range(7)]
        self._write_explanations(_make_explanations_payload(rows))
        result = load_decision_explanations(self.root)
        cards = _get_insight_cards(result)
        self.assertEqual(5, len(cards))

    def test_get_insight_cards_returns_empty_when_unavailable(self):
        result = load_decision_explanations(self.root)  # file missing → unavailable
        self.assertEqual([], _get_insight_cards(result))

    # ------------------------------------------------------------------
    # risks capped at 3 in render slice
    # ------------------------------------------------------------------

    def test_risks_capped_at_3(self):
        expl = _make_expl(risks=["a", "b", "c", "d", "e"])
        self._write_explanations(_make_explanations_payload([expl]))
        result = load_decision_explanations(self.root)
        cards = _get_insight_cards(result)
        rendered_risks = (cards[0].get("risks") or [])[:3]
        self.assertEqual(3, len(rendered_risks))
        self.assertEqual(["a", "b", "c"], rendered_risks)

    # ------------------------------------------------------------------
    # watch_next capped at 3 in render slice
    # ------------------------------------------------------------------

    def test_watch_next_capped_at_3(self):
        expl = _make_expl(watch=["w1", "w2", "w3", "w4"])
        self._write_explanations(_make_explanations_payload([expl]))
        result = load_decision_explanations(self.root)
        cards = _get_insight_cards(result)
        rendered_watch = (cards[0].get("what_to_watch_next") or [])[:3]
        self.assertEqual(3, len(rendered_watch))

    # ------------------------------------------------------------------
    # badge mapping
    # ------------------------------------------------------------------

    def test_badge_boost(self):
        self.assertEqual("↑ boost", _ai_validation_badge("boost"))

    def test_badge_neutral(self):
        self.assertEqual("• neutral", _ai_validation_badge("neutral"))

    def test_badge_caution(self):
        self.assertEqual("⚠ caution", _ai_validation_badge("caution"))

    def test_badge_unknown_defaults_to_neutral(self):
        self.assertEqual("• neutral", _ai_validation_badge("unknown_label"))

    def test_badge_case_insensitive(self):
        self.assertEqual("↑ boost", _ai_validation_badge("BOOST"))
        self.assertEqual("⚠ caution", _ai_validation_badge("Caution"))


if __name__ == "__main__":
    unittest.main()
