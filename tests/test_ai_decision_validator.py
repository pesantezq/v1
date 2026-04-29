from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from portfolio_automation.ai_decision_validator import (
    STATUS_ALIGNED,
    STATUS_CAUTION,
    STATUS_CONTRADICTION,
    STATUS_INSUFFICIENT,
    build_ai_validation,
    render_ai_validation_md,
    run_ai_validation,
    validate_single_decision,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _structural_sell(risk_flags=None, violation_type="leverage") -> dict:
    return {
        "symbol": "QLD",
        "decision": "SELL",
        "source": "structural",
        "urgency": "critical",
        "priority": 0.95,
        "confidence": 1.0,
        "capital_action": "SELL all",
        "decision_reason": "Leverage violation.",
        "risk_flags": risk_flags if risk_flags is not None else ["leverage_breach"],
        "inputs_used": {"violation_type": violation_type},
        "decision_reason_structured": {
            "decision": "SELL",
            "band": "structural",
            "strategy": "risk_management",
            "drivers": ["leverage breach"],
            "why": ["Structural leverage violation requires immediate action"],
            "what_would_change": ["Decision downgraded if leverage resolves"],
            "watch_next": ["Monitor portfolio leverage"],
        },
    }


def _wait_row(capital_action="WAIT for better entry", risk_flags=None) -> dict:
    return {
        "symbol": "FANG",
        "decision": "WAIT",
        "source": "market",
        "urgency": "medium",
        "priority": 0.50,
        "confidence": 0.6,
        "capital_action": capital_action,
        "decision_reason": "Confidence building.",
        "risk_flags": risk_flags or [],
        "inputs_used": {},
        "decision_reason_structured": {
            "decision": "WAIT",
            "band": "market",
            "strategy": "observation",
            "drivers": [],
            "why": ["Evidence is accumulating"],
            "what_would_change": ["Conviction improves"],
            "watch_next": ["Signal strength"],
        },
    }


def _buy_row(confidence=0.8, risk_flags=None) -> dict:
    return {
        "symbol": "XLE",
        "decision": "BUY",
        "source": "market",
        "urgency": "low",
        "priority": 0.40,
        "confidence": confidence,
        "capital_action": "BUY $500",
        "decision_reason": "Momentum breakout.",
        "risk_flags": risk_flags or [],
        "inputs_used": {},
        "decision_reason_structured": {
            "decision": "BUY",
            "band": "market",
            "strategy": "momentum",
            "drivers": ["breakout"],
            "why": ["Price near highs"],
            "what_would_change": ["Breakout fails"],
            "watch_next": ["Price action"],
        },
    }


def _scale_row(confidence=0.75, risk_flags=None) -> dict:
    return {
        "symbol": "VFH",
        "decision": "SCALE",
        "source": "portfolio",
        "urgency": "low",
        "priority": 0.55,
        "confidence": confidence,
        "capital_action": "BUY $500",
        "decision_reason": "Underweight.",
        "risk_flags": risk_flags or [],
        "inputs_used": {},
        "decision_reason_structured": {
            "decision": "SCALE",
            "band": "portfolio",
            "strategy": "rebalance",
            "drivers": ["underweight"],
            "why": ["Position below target"],
            "what_would_change": ["Position reaches target weight"],
            "watch_next": ["Position weight"],
        },
    }


def _no_structured_reason_row() -> dict:
    return {
        "symbol": "NVDA",
        "decision": "BUY",
        "source": "market",
        "urgency": "low",
        "priority": 0.30,
        "confidence": 0.5,
        "capital_action": "BUY $200",
        "decision_reason": "Strong momentum.",
        "risk_flags": [],
        "inputs_used": {},
        # deliberately omit decision_reason_structured
    }


def _no_reason_row() -> dict:
    return {
        "symbol": "SPY",
        "decision": "WAIT",
        "source": "market",
        "urgency": "low",
        "priority": 0.20,
        "confidence": 0.5,
        "capital_action": "",
        "decision_reason": "",
        "risk_flags": [],
        "inputs_used": {},
    }


def _make_plan(decisions: list[dict]) -> dict:
    return {
        "generated_at": "2026-04-29T08:00:00",
        "run_mode": "daily",
        "observe_only": True,
        "total_decisions": len(decisions),
        "decisions": decisions,
    }


# ---------------------------------------------------------------------------
# TestDeterministicStatusRules
# ---------------------------------------------------------------------------

class TestDeterministicStatusRules(unittest.TestCase):
    def test_structural_sell_leverage_flag_aligned(self):
        row = _structural_sell(risk_flags=["leverage_breach"])
        result = validate_single_decision(row)
        self.assertEqual(STATUS_ALIGNED, result["validation_status"])

    def test_structural_sell_concentration_flag_aligned(self):
        row = _structural_sell(risk_flags=["concentration_breach"], violation_type="concentration")
        row["decision_reason_structured"]["drivers"] = ["concentration breach"]
        result = validate_single_decision(row)
        self.assertEqual(STATUS_ALIGNED, result["validation_status"])

    def test_structural_sell_violation_type_leverage_aligned(self):
        row = _structural_sell(risk_flags=[], violation_type="leverage")
        result = validate_single_decision(row)
        self.assertEqual(STATUS_ALIGNED, result["validation_status"])

    def test_structural_sell_violation_type_concentration_aligned(self):
        row = _structural_sell(risk_flags=[], violation_type="concentration")
        result = validate_single_decision(row)
        self.assertEqual(STATUS_ALIGNED, result["validation_status"])

    def test_wait_with_degraded_data_flag_caution(self):
        row = _wait_row(risk_flags=["degraded_data"])
        result = validate_single_decision(row)
        self.assertEqual(STATUS_CAUTION, result["validation_status"])

    def test_buy_with_degraded_data_flag_caution(self):
        row = _buy_row(risk_flags=["degraded_data"])
        result = validate_single_decision(row)
        self.assertEqual(STATUS_CAUTION, result["validation_status"])

    def test_scale_with_degraded_data_flag_caution(self):
        row = _scale_row(risk_flags=["degraded_data"])
        result = validate_single_decision(row)
        self.assertEqual(STATUS_CAUTION, result["validation_status"])

    def test_wait_deploy_capital_action_contradiction(self):
        row = _wait_row(capital_action="deploy capital now")
        result = validate_single_decision(row)
        self.assertEqual(STATUS_CONTRADICTION, result["validation_status"])
        self.assertTrue(len(result["contradictions"]) > 0)
        self.assertIn("WAIT", result["contradictions"][0])

    def test_wait_buy_capital_action_contradiction(self):
        row = _wait_row(capital_action="buy 500 shares")
        result = validate_single_decision(row)
        self.assertEqual(STATUS_CONTRADICTION, result["validation_status"])

    def test_hold_deploy_capital_action_contradiction(self):
        row = _wait_row(capital_action="invest proceeds")
        row["decision"] = "HOLD"
        result = validate_single_decision(row)
        self.assertEqual(STATUS_CONTRADICTION, result["validation_status"])

    def test_missing_structured_reason_insufficient_context(self):
        row = _no_structured_reason_row()
        result = validate_single_decision(row)
        self.assertEqual(STATUS_INSUFFICIENT, result["validation_status"])

    def test_no_reason_at_all_insufficient_context(self):
        row = _no_reason_row()
        result = validate_single_decision(row)
        self.assertEqual(STATUS_INSUFFICIENT, result["validation_status"])

    def test_low_confidence_buy_caution(self):
        row = _buy_row(confidence=0.5)
        result = validate_single_decision(row)
        self.assertEqual(STATUS_CAUTION, result["validation_status"])

    def test_sufficient_confidence_buy_caution_default(self):
        row = _buy_row(confidence=0.85)
        result = validate_single_decision(row)
        # no specific rule → default conservative caution
        self.assertEqual(STATUS_CAUTION, result["validation_status"])

    def test_degraded_mode_in_inputs_used_caution(self):
        row = _wait_row()
        row["inputs_used"]["degraded_mode"] = True
        result = validate_single_decision(row)
        self.assertEqual(STATUS_CAUTION, result["validation_status"])

    def test_fallback_data_mode_in_inputs_used_caution(self):
        row = _buy_row()
        row["inputs_used"]["data_mode"] = "fallback"
        result = validate_single_decision(row)
        self.assertEqual(STATUS_CAUTION, result["validation_status"])


# ---------------------------------------------------------------------------
# TestOutputSchema
# ---------------------------------------------------------------------------

class TestOutputSchema(unittest.TestCase):
    def test_record_has_all_required_fields(self):
        result = validate_single_decision(_structural_sell())
        required = {
            "symbol", "decision", "validation_status", "plain_english_summary",
            "rule_alignment", "narrative_context", "contradictions", "watch_next",
            "ai_used", "model", "generated_at",
        }
        for field in required:
            self.assertIn(field, result, msg=f"Missing field: {field}")

    def test_contradictions_is_list(self):
        result = validate_single_decision(_structural_sell())
        self.assertIsInstance(result["contradictions"], list)

    def test_watch_next_is_list(self):
        result = validate_single_decision(_structural_sell())
        self.assertIsInstance(result["watch_next"], list)

    def test_ai_used_false_by_default(self):
        result = validate_single_decision(_structural_sell())
        self.assertFalse(result["ai_used"])

    def test_model_none_by_default(self):
        result = validate_single_decision(_structural_sell())
        self.assertIsNone(result["model"])

    def test_watch_next_capped_at_three(self):
        row = _structural_sell()
        row["decision_reason_structured"]["watch_next"] = ["w1", "w2", "w3", "w4", "w5"]
        result = validate_single_decision(row)
        self.assertLessEqual(len(result["watch_next"]), 3)

    def test_watch_next_deduplicates(self):
        row = _structural_sell()
        row["decision_reason_structured"]["watch_next"] = ["Monitor leverage.", "Monitor leverage.", "Check cap."]
        row["decision_reason_structured"]["what_would_change"] = []
        result = validate_single_decision(row)
        self.assertEqual(2, len(result["watch_next"]))

    def test_plain_english_summary_non_empty(self):
        for row in [_structural_sell(), _wait_row(), _no_structured_reason_row()]:
            result = validate_single_decision(row)
            self.assertTrue(len(result["plain_english_summary"]) > 0)

    def test_narrative_context_non_empty(self):
        result = validate_single_decision(_structural_sell())
        self.assertIn("source=structural", result["narrative_context"])
        self.assertIn("urgency=critical", result["narrative_context"])

    def test_narrative_context_includes_band_and_strategy(self):
        result = validate_single_decision(_structural_sell())
        self.assertIn("band=structural", result["narrative_context"])
        self.assertIn("strategy=risk_management", result["narrative_context"])


# ---------------------------------------------------------------------------
# TestBuildAiValidation
# ---------------------------------------------------------------------------

class TestBuildAiValidation(unittest.TestCase):
    def test_summary_counts_correct(self):
        decisions = [
            _structural_sell(),                         # aligned
            _wait_row(capital_action="deploy now"),     # contradiction
            _no_structured_reason_row(),                # insufficient
            _wait_row(risk_flags=["degraded_data"]),    # caution
            _buy_row(confidence=0.5),                   # caution
        ]
        plan = _make_plan(decisions)
        result = build_ai_validation(plan)
        self.assertEqual(5, result["total_validated"])
        self.assertEqual(1, result["aligned_count"])
        self.assertEqual(1, result["contradiction_count"])
        self.assertEqual(1, result["insufficient_context_count"])
        self.assertEqual(2, result["caution_count"])

    def test_capped_at_five_decisions(self):
        decisions = [_structural_sell()] * 8
        plan = _make_plan(decisions)
        result = build_ai_validation(plan)
        self.assertEqual(5, result["total_validated"])
        self.assertEqual(5, len(result["validations"]))

    def test_empty_plan_returns_zero_counts(self):
        result = build_ai_validation(_make_plan([]))
        self.assertEqual(0, result["total_validated"])
        self.assertEqual([], result["validations"])

    def test_observe_only_always_true(self):
        result = build_ai_validation(_make_plan([_structural_sell()]))
        self.assertTrue(result["observe_only"])

    def test_ai_used_false_when_not_requested(self):
        result = build_ai_validation(_make_plan([_structural_sell()]), use_llm=False)
        self.assertFalse(result["ai_used"])


# ---------------------------------------------------------------------------
# TestMarkdownOutput
# ---------------------------------------------------------------------------

class TestMarkdownOutput(unittest.TestCase):
    def test_markdown_generated(self):
        plan = _make_plan([_structural_sell()])
        payload = build_ai_validation(plan)
        md = render_ai_validation_md(payload)
        self.assertIn("# AI Decision Validation", md)
        self.assertIn("Observe-only", md)

    def test_markdown_contains_symbol_and_decision(self):
        plan = _make_plan([_structural_sell()])
        payload = build_ai_validation(plan)
        md = render_ai_validation_md(payload)
        self.assertIn("SELL", md)
        self.assertIn("QLD", md)

    def test_markdown_contains_status(self):
        plan = _make_plan([_structural_sell()])
        payload = build_ai_validation(plan)
        md = render_ai_validation_md(payload)
        self.assertIn("aligned", md)

    def test_markdown_contains_contradiction_text(self):
        plan = _make_plan([_wait_row(capital_action="deploy capital now")])
        payload = build_ai_validation(plan)
        md = render_ai_validation_md(payload)
        self.assertIn("Contradictions", md)

    def test_markdown_summary_line(self):
        plan = _make_plan([_structural_sell(), _buy_row()])
        payload = build_ai_validation(plan)
        payload["available"] = True
        payload["summary_line"] = "2 decisions validated."
        md = render_ai_validation_md(payload)
        self.assertIn("Validated: 2", md)


# ---------------------------------------------------------------------------
# TestLLMFallback
# ---------------------------------------------------------------------------

class TestLLMFallback(unittest.TestCase):
    def test_llm_failure_leaves_record_unchanged(self):
        row = _structural_sell()

        with patch(
            "portfolio_automation.ai_decision_validator._try_llm_enhance",
            side_effect=RuntimeError("LLM is down"),
        ):
            result = build_ai_validation(_make_plan([row]), use_llm=True)

        # _try_llm_enhance raised but build_ai_validation must not propagate
        self.assertEqual(1, result["total_validated"])
        self.assertFalse(result["ai_used"])

    def test_build_does_not_fail_when_llm_import_missing(self):
        with patch.dict("sys.modules", {"agent.llm_adapters": None}):
            result = build_ai_validation(_make_plan([_structural_sell()]), use_llm=True)
        self.assertEqual(1, result["total_validated"])
        self.assertFalse(result["ai_used"])

    def test_llm_enhance_fallback_on_exception(self):
        from portfolio_automation.ai_decision_validator import _try_llm_enhance

        row = _structural_sell()
        record = validate_single_decision(row)
        original_summary = record["plain_english_summary"]

        with patch("portfolio_automation.ai_decision_validator.call_provider" if False else
                   "portfolio_automation.ai_decision_validator._try_llm_enhance",
                   return_value=record):
            enhanced = _try_llm_enhance(record, row, provider="ollama", model="gemma3:4b")

        self.assertEqual(original_summary, enhanced["plain_english_summary"])
        self.assertFalse(enhanced["ai_used"])


# ---------------------------------------------------------------------------
# TestRunAiValidation (file I/O integration)
# ---------------------------------------------------------------------------

class TestRunAiValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_plan(self, decisions: list[dict]) -> None:
        path = self.root / "outputs" / "latest" / "decision_plan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_make_plan(decisions)), encoding="utf-8")

    def test_writes_json_and_md(self):
        self._write_plan([_structural_sell()])
        payload, md = run_ai_validation(self.root)
        json_path = self.root / "outputs" / "latest" / "ai_decision_validation.json"
        md_path = self.root / "outputs" / "latest" / "ai_decision_validation.md"
        self.assertTrue(json_path.exists())
        self.assertTrue(md_path.exists())

    def test_json_artifact_is_valid(self):
        self._write_plan([_structural_sell()])
        run_ai_validation(self.root)
        json_path = self.root / "outputs" / "latest" / "ai_decision_validation.json"
        loaded = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertTrue(loaded.get("available"))
        self.assertIn("validations", loaded)
        self.assertIn("total_validated", loaded)

    def test_missing_decision_plan_returns_available_false(self):
        payload, _ = run_ai_validation(self.root, write_files=False)
        self.assertFalse(payload.get("available"))
        self.assertEqual(0, payload["total_validated"])

    def test_malformed_decision_plan_returns_available_false(self):
        path = self.root / "outputs" / "latest" / "decision_plan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{bad json", encoding="utf-8")
        payload, _ = run_ai_validation(self.root, write_files=False)
        self.assertFalse(payload.get("available"))

    def test_non_fatal_no_raise_on_missing_plan(self):
        try:
            run_ai_validation(self.root, write_files=False)
        except Exception as exc:
            self.fail(f"run_ai_validation raised unexpectedly: {exc}")

    def test_summary_line_present_when_available(self):
        self._write_plan([_structural_sell(), _wait_row()])
        payload, _ = run_ai_validation(self.root, write_files=False)
        self.assertTrue(payload.get("available"))
        self.assertIn("2", payload.get("summary_line", ""))

    def test_observe_only_in_output(self):
        self._write_plan([_structural_sell()])
        payload, _ = run_ai_validation(self.root, write_files=False)
        self.assertTrue(payload.get("observe_only"))

    def test_write_false_does_not_create_files(self):
        self._write_plan([_structural_sell()])
        run_ai_validation(self.root, write_files=False)
        json_path = self.root / "outputs" / "latest" / "ai_decision_validation.json"
        self.assertFalse(json_path.exists())


# ---------------------------------------------------------------------------
# TestGuiDataLayer
# ---------------------------------------------------------------------------

class TestGuiDataLayer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_validation(self, payload: dict) -> None:
        path = self.root / "outputs" / "latest" / "ai_decision_validation.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _make_validation_payload(self, validations: list[dict] | None = None) -> dict:
        rows = validations or []
        return {
            "generated_at": "2026-04-29T08:00:00",
            "observe_only": True,
            "available": True,
            "total_validated": len(rows),
            "aligned_count": sum(1 for r in rows if r.get("validation_status") == STATUS_ALIGNED),
            "caution_count": sum(1 for r in rows if r.get("validation_status") == STATUS_CAUTION),
            "contradiction_count": sum(1 for r in rows if r.get("validation_status") == STATUS_CONTRADICTION),
            "insufficient_context_count": sum(1 for r in rows if r.get("validation_status") == STATUS_INSUFFICIENT),
            "ai_used": False,
            "summary_line": f"{len(rows)} decisions validated.",
            "validations": rows,
        }

    def _make_record(self, symbol="QLD", decision="SELL", status=STATUS_ALIGNED) -> dict:
        return {
            "symbol": symbol,
            "decision": decision,
            "validation_status": status,
            "plain_english_summary": "Test summary.",
            "rule_alignment": "Test rule.",
            "narrative_context": "source=structural; urgency=critical",
            "contradictions": [],
            "watch_next": ["Watch leverage."],
            "ai_used": False,
            "model": None,
            "generated_at": "2026-04-29T08:00:00",
        }

    def test_load_returns_available_when_file_exists(self):
        from gui_operator_data import load_ai_decision_validation
        payload = self._make_validation_payload([self._make_record()])
        self._write_validation(payload)
        result = load_ai_decision_validation(self.root)
        self.assertTrue(result["available"])

    def test_load_returns_unavailable_when_file_missing(self):
        from gui_operator_data import load_ai_decision_validation
        result = load_ai_decision_validation(self.root)
        self.assertFalse(result["available"])

    def test_load_returns_unavailable_on_malformed_json(self):
        from gui_operator_data import load_ai_decision_validation
        path = self.root / "outputs" / "latest" / "ai_decision_validation.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{bad json", encoding="utf-8")
        result = load_ai_decision_validation(self.root)
        self.assertFalse(result["available"])

    def test_load_returns_unavailable_when_available_false(self):
        from gui_operator_data import load_ai_decision_validation
        payload = {"available": False, "validations": [], "summary_line": "Not ready."}
        self._write_validation(payload)
        result = load_ai_decision_validation(self.root)
        self.assertFalse(result["available"])

    def test_load_in_bundle(self):
        from gui_operator_data import load_operator_dashboard_data
        payload = self._make_validation_payload([self._make_record()])
        self._write_validation(payload)
        bundle = load_operator_dashboard_data(self.root)
        self.assertIn("ai_decision_validation", bundle)
        self.assertTrue(bundle["ai_decision_validation"]["available"])

    def test_missing_artifact_does_not_raise(self):
        from gui_operator_data import load_ai_decision_validation
        try:
            load_ai_decision_validation(self.root)
        except Exception as exc:
            self.fail(f"load_ai_decision_validation raised unexpectedly: {exc}")

    def test_summary_fields_present_when_missing(self):
        from gui_operator_data import load_ai_decision_validation
        result = load_ai_decision_validation(self.root)
        self.assertIn("total_validated", result)
        self.assertIn("aligned_count", result)
        self.assertIn("caution_count", result)
        self.assertIn("contradiction_count", result)
        self.assertIn("insufficient_context_count", result)


if __name__ == "__main__":
    unittest.main()
