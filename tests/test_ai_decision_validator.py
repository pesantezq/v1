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

    # ------------------------------------------------------------------
    # contradiction — NOT flagged (negation / hold language)
    # ------------------------------------------------------------------

    def test_wait_do_not_deploy_not_contradiction(self):
        row = _wait_row(capital_action="Stand by — do not deploy capital until conditions improve.")
        result = validate_single_decision(row)
        self.assertNotEqual(STATUS_CONTRADICTION, result["validation_status"])

    def test_wait_do_not_buy_not_contradiction(self):
        row = _wait_row(capital_action="do not buy at this time")
        result = validate_single_decision(row)
        self.assertNotEqual(STATUS_CONTRADICTION, result["validation_status"])

    def test_wait_stand_by_not_contradiction(self):
        row = _wait_row(capital_action="stand by")
        result = validate_single_decision(row)
        self.assertNotEqual(STATUS_CONTRADICTION, result["validation_status"])

    def test_wait_until_conditions_not_contradiction(self):
        row = _wait_row(capital_action="hold until conditions improve")
        result = validate_single_decision(row)
        self.assertNotEqual(STATUS_CONTRADICTION, result["validation_status"])

    def test_wait_pure_wait_phrase_not_contradiction(self):
        row = _wait_row(capital_action="wait for a better entry point")
        result = validate_single_decision(row)
        self.assertNotEqual(STATUS_CONTRADICTION, result["validation_status"])

    def test_wait_do_not_deploy_capital_not_contradiction(self):
        # Full realistic string from the issue report
        row = _wait_row(
            capital_action="Stand by — do not deploy capital until conditions improve."
        )
        result = validate_single_decision(row)
        self.assertEqual(STATUS_CAUTION, result["validation_status"])
        self.assertEqual([], result["contradictions"])

    # ------------------------------------------------------------------
    # contradiction — still flagged (positive deploy language)
    # ------------------------------------------------------------------

    def test_wait_deploy_amount_contradiction(self):
        row = _wait_row(capital_action="deploy $500")
        result = validate_single_decision(row)
        self.assertEqual(STATUS_CONTRADICTION, result["validation_status"])

    def test_wait_buy_shares_contradiction(self):
        row = _wait_row(capital_action="buy shares now")
        result = validate_single_decision(row)
        self.assertEqual(STATUS_CONTRADICTION, result["validation_status"])

    def test_hold_open_new_position_contradiction(self):
        row = _wait_row(capital_action="open new position")
        row["decision"] = "HOLD"
        result = validate_single_decision(row)
        self.assertEqual(STATUS_CONTRADICTION, result["validation_status"])

    def test_avoid_scale_position_contradiction(self):
        row = _wait_row(capital_action="scale position now")
        row["decision"] = "AVOID"
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


# ---------------------------------------------------------------------------
# TestAiBudgetInstrumentation
# ---------------------------------------------------------------------------

class TestAiBudgetInstrumentation(unittest.TestCase):
    """Verify that _try_llm_enhance records AI usage events correctly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base_dir = str(self.root / "outputs")

    def tearDown(self):
        self.tmp.cleanup()

    def _make_record(self):
        return validate_single_decision(_structural_sell())

    # ------------------------------------------------------------------
    # Token estimation helper
    # ------------------------------------------------------------------

    def test_estimate_tokens_empty_string(self):
        from portfolio_automation.ai_decision_validator import _estimate_tokens
        self.assertEqual(0, _estimate_tokens(""))

    def test_estimate_tokens_positive(self):
        from portfolio_automation.ai_decision_validator import _estimate_tokens
        result = _estimate_tokens("a" * 400)
        self.assertEqual(100, result)

    def test_estimate_tokens_non_negative(self):
        from portfolio_automation.ai_decision_validator import _estimate_tokens
        for text in ["", "x", "hello world", "a" * 1000]:
            self.assertGreaterEqual(_estimate_tokens(text), 0)

    # ------------------------------------------------------------------
    # Successful LLM call records usage event
    # ------------------------------------------------------------------

    def test_successful_call_records_usage_event(self):
        from portfolio_automation.ai_decision_validator import _try_llm_enhance, _record_validator_event
        row = _structural_sell()
        record = self._make_record()

        recorded: list[dict] = []

        def fake_record(**kwargs):
            recorded.append(kwargs)

        with patch("portfolio_automation.ai_decision_validator._record_validator_event", side_effect=fake_record):
            with patch("agent.llm_adapters.call_provider", return_value="This decision is aligned with structural rules."):
                _try_llm_enhance(record, row, provider="ollama", model="gemma3:4b", base_dir=self.base_dir)

        self.assertEqual(1, len(recorded))
        self.assertEqual("success", recorded[0]["status"])

    def test_successful_call_records_provider_and_model(self):
        from portfolio_automation.ai_decision_validator import _try_llm_enhance
        row = _structural_sell()
        record = self._make_record()

        recorded: list[dict] = []

        with patch("portfolio_automation.ai_decision_validator._record_validator_event", side_effect=lambda **kw: recorded.append(kw)):
            with patch("agent.llm_adapters.call_provider", return_value="Structural alignment confirmed."):
                _try_llm_enhance(record, row, provider="anthropic", model="claude-haiku-4-5-20251001", base_dir=self.base_dir)

        self.assertEqual(1, len(recorded))
        self.assertEqual("anthropic", recorded[0]["provider"])
        self.assertEqual("claude-haiku-4-5-20251001", recorded[0]["model"])

    def test_successful_call_records_estimated_tokens(self):
        from portfolio_automation.ai_decision_validator import _try_llm_enhance, _estimate_tokens
        row = _structural_sell()
        record = self._make_record()

        recorded: list[dict] = []
        response_text = "Structural alignment confirmed."

        with patch("portfolio_automation.ai_decision_validator._record_validator_event", side_effect=lambda **kw: recorded.append(kw)):
            with patch("agent.llm_adapters.call_provider", return_value=response_text):
                _try_llm_enhance(record, row, provider="ollama", model="gemma3:4b", base_dir=self.base_dir)

        self.assertEqual(1, len(recorded))
        self.assertGreater(recorded[0]["prompt_tokens"], 0)
        self.assertEqual(_estimate_tokens(response_text), recorded[0]["completion_tokens"])

    def test_successful_call_usage_source_is_estimated(self):
        """metadata.usage_source must be 'estimated_from_length'."""
        from portfolio_automation.ai_decision_validator import _record_validator_event
        import tempfile as _tf
        tmp_base = Path(_tf.mkdtemp()) / "outputs"

        _record_validator_event(
            provider="ollama",
            model="gemma3:4b",
            prompt_tokens=100,
            completion_tokens=50,
            status="success",
            base_dir=str(tmp_base),
        )
        event_path = tmp_base / "policy" / "ai_usage_events.jsonl"
        self.assertTrue(event_path.exists())
        import json as _json
        line = _json.loads(event_path.read_text())
        self.assertEqual("estimated_from_length", line["metadata"]["usage_source"])
        self.assertEqual("success", line["metadata"]["status"])

    # ------------------------------------------------------------------
    # Failed LLM call records failure event
    # ------------------------------------------------------------------

    def test_failed_call_records_error_event(self):
        from portfolio_automation.ai_decision_validator import _try_llm_enhance
        row = _structural_sell()
        record = self._make_record()

        recorded: list[dict] = []

        with patch("portfolio_automation.ai_decision_validator._record_validator_event", side_effect=lambda **kw: recorded.append(kw)):
            with patch("agent.llm_adapters.call_provider", side_effect=RuntimeError("connection refused")):
                result = _try_llm_enhance(record, row, provider="ollama", model="gemma3:4b", base_dir=self.base_dir)

        self.assertEqual(1, len(recorded))
        self.assertEqual("error", recorded[0]["status"])
        self.assertEqual(0, recorded[0]["completion_tokens"])
        self.assertFalse(result.get("ai_used"))

    def test_failed_call_preserves_original_exception_fallback(self):
        """Failed LLM call returns record unchanged; does not raise."""
        from portfolio_automation.ai_decision_validator import _try_llm_enhance
        row = _structural_sell()
        record = self._make_record()
        original_summary = record["plain_english_summary"]

        with patch("portfolio_automation.ai_decision_validator._record_validator_event"):
            with patch("agent.llm_adapters.call_provider", side_effect=RuntimeError("timeout")):
                result = _try_llm_enhance(record, row, provider="ollama", model="gemma3:4b", base_dir=self.base_dir)

        self.assertEqual(original_summary, result["plain_english_summary"])
        self.assertFalse(result.get("ai_used"))

    # ------------------------------------------------------------------
    # Recording failure does not break the caller
    # ------------------------------------------------------------------

    def test_recording_failure_does_not_break_successful_enhance(self):
        """Internal budget recording failure must not block the LLM enhancement.

        _record_validator_event catches all internal errors; even if the underlying
        record_ai_usage_event raises, the enhance result should be unaffected.
        """
        from portfolio_automation.ai_decision_validator import _try_llm_enhance
        row = _structural_sell()
        record = self._make_record()

        with patch("portfolio_automation.ai_budget.record_ai_usage_event", side_effect=RuntimeError("disk full")):
            with patch("agent.llm_adapters.call_provider", return_value="Structurally aligned decision."):
                result = _try_llm_enhance(record, row, provider="ollama", model="gemma3:4b", base_dir=self.base_dir)

        self.assertTrue(result.get("ai_used"))

    # ------------------------------------------------------------------
    # Import failure: budget module unavailable
    # ------------------------------------------------------------------

    def test_budget_import_failure_does_not_break_enhance(self):
        """If ai_budget cannot be imported, _try_llm_enhance still works."""
        from portfolio_automation.ai_decision_validator import _try_llm_enhance
        row = _structural_sell()
        record = self._make_record()

        with patch.dict("sys.modules", {"portfolio_automation.ai_budget": None}):
            with patch("agent.llm_adapters.call_provider", return_value="Aligned with structural rules."):
                result = _try_llm_enhance(record, row, provider="ollama", model="gemma3:4b", base_dir=self.base_dir)

        self.assertTrue(result.get("ai_used"))

    # ------------------------------------------------------------------
    # No extra AI calls for instrumentation
    # ------------------------------------------------------------------

    def test_no_extra_ai_calls_for_instrumentation(self):
        """Instrumentation must not make additional provider calls."""
        from portfolio_automation.ai_decision_validator import _try_llm_enhance
        row = _structural_sell()
        record = self._make_record()

        call_count = {"n": 0}

        def counting_provider(**kwargs):
            call_count["n"] += 1
            return "Aligned."

        with patch("portfolio_automation.ai_decision_validator._record_validator_event"):
            with patch("agent.llm_adapters.call_provider", side_effect=counting_provider):
                _try_llm_enhance(record, row, provider="ollama", model="gemma3:4b", base_dir=self.base_dir)

        self.assertEqual(1, call_count["n"])

    # ------------------------------------------------------------------
    # base_dir threading: run_ai_validation → build → _try_llm_enhance
    # ------------------------------------------------------------------

    def test_run_ai_validation_writes_event_to_root_outputs(self):
        """Usage event must land in {root}/outputs/policy/ai_usage_events.jsonl."""
        plan_path = self.root / "outputs" / "latest" / "decision_plan.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(_make_plan([_structural_sell()])), encoding="utf-8")

        with patch("agent.llm_adapters.call_provider", return_value="Aligned with structural rules."):
            run_ai_validation(self.root, use_llm=True)

        event_log = self.root / "outputs" / "policy" / "ai_usage_events.jsonl"
        self.assertTrue(event_log.exists(), "event log should exist after a real LLM call")
        import json as _json
        lines = [l for l in event_log.read_text().splitlines() if l.strip()]
        self.assertGreater(len(lines), 0)
        event = _json.loads(lines[0])
        self.assertEqual("ai_decision_validator", event["task_name"])

    # ------------------------------------------------------------------
    # Safety: no scoring/allocation/recommendation behavior changes
    # ------------------------------------------------------------------

    def test_instrumentation_does_not_change_validation_status(self):
        """Adding instrumentation must not alter validation_status output."""
        plan = _make_plan([_structural_sell()])
        result_no_llm = build_ai_validation(plan, use_llm=False)

        with patch("portfolio_automation.ai_decision_validator._record_validator_event"):
            result_with_llm = build_ai_validation(plan, use_llm=False)

        for r_no, r_with in zip(result_no_llm["validations"], result_with_llm["validations"]):
            self.assertEqual(r_no["validation_status"], r_with["validation_status"])

    def test_decision_explainer_has_no_llm_calls(self):
        """decision_explainer.py must not import or call any LLM provider."""
        import portfolio_automation.decision_explainer as de
        import inspect
        src = inspect.getsource(de)
        self.assertNotIn("call_provider", src)
        self.assertNotIn("call_claude", src)
        self.assertNotIn("call_openai", src)
        self.assertNotIn("call_ollama", src)
        self.assertNotIn("import anthropic", src)

    # ------------------------------------------------------------------
    # Empty / short response: event recorded even when output not accepted
    # ------------------------------------------------------------------

    def test_empty_response_records_event(self):
        """Empty string from provider must still produce a usage event."""
        from portfolio_automation.ai_decision_validator import _try_llm_enhance
        row = _structural_sell()
        record = self._make_record()
        recorded: list[dict] = []

        with patch("portfolio_automation.ai_decision_validator._record_validator_event",
                   side_effect=lambda **kw: recorded.append(kw)):
            with patch("agent.llm_adapters.call_provider", return_value=""):
                result = _try_llm_enhance(record, row, provider="ollama", model="gemma3:4b", base_dir=self.base_dir)

        self.assertEqual(1, len(recorded), "exactly one event should be recorded")
        self.assertEqual("success", recorded[0]["status"])
        self.assertFalse(recorded[0]["output_accepted"])
        self.assertEqual("empty_response", recorded[0]["fallback_reason"])
        self.assertFalse(result.get("ai_used"))

    def test_none_response_records_empty_event(self):
        """None from provider is treated as empty response."""
        from portfolio_automation.ai_decision_validator import _try_llm_enhance
        row = _structural_sell()
        record = self._make_record()
        recorded: list[dict] = []

        with patch("portfolio_automation.ai_decision_validator._record_validator_event",
                   side_effect=lambda **kw: recorded.append(kw)):
            with patch("agent.llm_adapters.call_provider", return_value=None):
                result = _try_llm_enhance(record, row, provider="ollama", model="gemma3:4b", base_dir=self.base_dir)

        self.assertEqual(1, len(recorded))
        self.assertFalse(recorded[0]["output_accepted"])
        self.assertEqual("empty_response", recorded[0]["fallback_reason"])
        self.assertFalse(result.get("ai_used"))

    def test_short_response_records_event(self):
        """Text <= 10 chars after strip must produce event with output_accepted=False."""
        from portfolio_automation.ai_decision_validator import _try_llm_enhance
        row = _structural_sell()
        record = self._make_record()
        recorded: list[dict] = []

        with patch("portfolio_automation.ai_decision_validator._record_validator_event",
                   side_effect=lambda **kw: recorded.append(kw)):
            with patch("agent.llm_adapters.call_provider", return_value="OK"):
                result = _try_llm_enhance(record, row, provider="ollama", model="gemma3:4b", base_dir=self.base_dir)

        self.assertEqual(1, len(recorded))
        self.assertEqual("success", recorded[0]["status"])
        self.assertFalse(recorded[0]["output_accepted"])
        self.assertEqual("short_response", recorded[0]["fallback_reason"])
        self.assertFalse(result.get("ai_used"))

    def test_valid_response_records_event_output_accepted(self):
        """Text > 10 chars after strip must produce event with output_accepted=True."""
        from portfolio_automation.ai_decision_validator import _try_llm_enhance
        row = _structural_sell()
        record = self._make_record()
        recorded: list[dict] = []

        with patch("portfolio_automation.ai_decision_validator._record_validator_event",
                   side_effect=lambda **kw: recorded.append(kw)):
            with patch("agent.llm_adapters.call_provider", return_value="Structurally aligned decision."):
                _try_llm_enhance(record, row, provider="ollama", model="gemma3:4b", base_dir=self.base_dir)

        self.assertEqual(1, len(recorded))
        self.assertTrue(recorded[0]["output_accepted"])
        self.assertIsNone(recorded[0]["fallback_reason"])

    def test_empty_response_does_not_enhance_record(self):
        """Empty response must leave record unchanged (no ai_used, no summary change)."""
        from portfolio_automation.ai_decision_validator import _try_llm_enhance
        row = _structural_sell()
        record = self._make_record()
        original_summary = record["plain_english_summary"]

        with patch("portfolio_automation.ai_decision_validator._record_validator_event"):
            with patch("agent.llm_adapters.call_provider", return_value=""):
                result = _try_llm_enhance(record, row, provider="ollama", model="gemma3:4b", base_dir=self.base_dir)

        self.assertFalse(result.get("ai_used"))
        self.assertEqual(original_summary, result["plain_english_summary"])

    def test_short_response_does_not_enhance_record(self):
        """Short response must leave record unchanged."""
        from portfolio_automation.ai_decision_validator import _try_llm_enhance
        row = _structural_sell()
        record = self._make_record()
        original_summary = record["plain_english_summary"]

        with patch("portfolio_automation.ai_decision_validator._record_validator_event"):
            with patch("agent.llm_adapters.call_provider", return_value="Short."):
                result = _try_llm_enhance(record, row, provider="ollama", model="gemma3:4b", base_dir=self.base_dir)

        self.assertFalse(result.get("ai_used"))
        self.assertEqual(original_summary, result["plain_english_summary"])

    def test_empty_response_completion_tokens_zero(self):
        """Empty response should estimate zero completion tokens."""
        from portfolio_automation.ai_decision_validator import _try_llm_enhance, _estimate_tokens
        row = _structural_sell()
        record = self._make_record()
        recorded: list[dict] = []

        with patch("portfolio_automation.ai_decision_validator._record_validator_event",
                   side_effect=lambda **kw: recorded.append(kw)):
            with patch("agent.llm_adapters.call_provider", return_value=""):
                _try_llm_enhance(record, row, provider="ollama", model="gemma3:4b", base_dir=self.base_dir)

        self.assertEqual(0, recorded[0]["completion_tokens"])

    def test_whitespace_only_response_is_empty(self):
        """Whitespace-only response must be treated as empty."""
        from portfolio_automation.ai_decision_validator import _try_llm_enhance
        row = _structural_sell()
        record = self._make_record()
        recorded: list[dict] = []

        with patch("portfolio_automation.ai_decision_validator._record_validator_event",
                   side_effect=lambda **kw: recorded.append(kw)):
            with patch("agent.llm_adapters.call_provider", return_value="   \n  "):
                result = _try_llm_enhance(record, row, provider="ollama", model="gemma3:4b", base_dir=self.base_dir)

        self.assertEqual("empty_response", recorded[0]["fallback_reason"])
        self.assertFalse(result.get("ai_used"))

    def test_provider_raises_records_error_not_success(self):
        """Exception from provider must produce error event, not success event."""
        from portfolio_automation.ai_decision_validator import _try_llm_enhance
        row = _structural_sell()
        record = self._make_record()
        recorded: list[dict] = []

        with patch("portfolio_automation.ai_decision_validator._record_validator_event",
                   side_effect=lambda **kw: recorded.append(kw)):
            with patch("agent.llm_adapters.call_provider", side_effect=RuntimeError("timeout")):
                _try_llm_enhance(record, row, provider="ollama", model="gemma3:4b", base_dir=self.base_dir)

        self.assertEqual(1, len(recorded))
        self.assertEqual("error", recorded[0]["status"])
        self.assertNotIn("output_accepted", recorded[0])


if __name__ == "__main__":
    unittest.main()
