"""Tests for portfolio_automation.discovery.approval_workflow."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from portfolio_automation.discovery.approval_workflow import (
    ApprovalDecision,
    DiscoveryApprovalDecision,
    _FORBIDDEN_DECISIONS,
    _validate_decision,
    _validate_governance_flags,
    build_approval_summary,
    load_approval_decisions,
    make_approval_decision,
    record_approval_decision,
)
from portfolio_automation.data_governance import DataGovernanceError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)


def _base_decision(**overrides) -> DiscoveryApprovalDecision:
    defaults = dict(
        generated_at=_NOW.isoformat(),
        symbol="NVDA",
        company_name="NVIDIA",
        candidate_status="watch",
        corroboration_score=0.72,
        corroboration_level="strong",
        decision=ApprovalDecision.KEEP_WATCHING,
        decision_reason="Watching this week",
        operator="operator",
        source_artifact="outputs/sandbox/discovery/emerging_candidates.json",
        run_id="2026-05-02_discovery",
    )
    defaults.update(overrides)
    return DiscoveryApprovalDecision(**defaults)


# ---------------------------------------------------------------------------
# 1. ApprovalDecision enum — no forbidden values
# ---------------------------------------------------------------------------

class TestApprovalDecisionEnum(unittest.TestCase):
    def test_no_buy_value(self):
        values = {d.value for d in ApprovalDecision}
        self.assertNotIn("buy", values)

    def test_no_sell_value(self):
        values = {d.value for d in ApprovalDecision}
        self.assertNotIn("sell", values)

    def test_no_actionable_value(self):
        values = {d.value for d in ApprovalDecision}
        self.assertNotIn("actionable", values)

    def test_no_promoted_value(self):
        values = {d.value for d in ApprovalDecision}
        self.assertNotIn("promoted", values)

    def test_no_validated_value(self):
        values = {d.value for d in ApprovalDecision}
        self.assertNotIn("validated", values)

    def test_only_allowed_values_exist(self):
        allowed = {
            "approve_for_research_review",
            "keep_watching",
            "reject_candidate",
            "needs_more_evidence",
        }
        self.assertEqual({d.value for d in ApprovalDecision}, allowed)

    def test_four_decisions_defined(self):
        self.assertEqual(len(ApprovalDecision), 4)


# ---------------------------------------------------------------------------
# 2. _validate_decision
# ---------------------------------------------------------------------------

class TestValidateDecision(unittest.TestCase):
    def test_valid_string_returns_enum(self):
        result = _validate_decision("keep_watching")
        self.assertEqual(result, ApprovalDecision.KEEP_WATCHING)

    def test_valid_enum_returns_same(self):
        result = _validate_decision(ApprovalDecision.APPROVE_FOR_RESEARCH_REVIEW)
        self.assertEqual(result, ApprovalDecision.APPROVE_FOR_RESEARCH_REVIEW)

    def test_uppercase_string_accepted(self):
        result = _validate_decision("KEEP_WATCHING")
        self.assertEqual(result, ApprovalDecision.KEEP_WATCHING)

    def test_buy_is_forbidden(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_decision("buy")
        self.assertIn("Forbidden", str(ctx.exception))

    def test_sell_is_forbidden(self):
        with self.assertRaises(ValueError):
            _validate_decision("sell")

    def test_actionable_is_forbidden(self):
        with self.assertRaises(ValueError):
            _validate_decision("actionable")

    def test_promoted_is_forbidden(self):
        with self.assertRaises(ValueError):
            _validate_decision("promoted")

    def test_validated_is_forbidden(self):
        with self.assertRaises(ValueError):
            _validate_decision("validated")

    def test_unknown_value_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_decision("unknown_decision_xyz")
        self.assertIn("Unknown decision", str(ctx.exception))

    def test_all_four_valid_values_accepted(self):
        for d in ApprovalDecision:
            result = _validate_decision(d.value)
            self.assertEqual(result, d)


# ---------------------------------------------------------------------------
# 3. Governance flags on DiscoveryApprovalDecision
# ---------------------------------------------------------------------------

class TestGovernanceFlags(unittest.TestCase):
    def test_observe_only_defaults_true(self):
        d = _base_decision()
        self.assertTrue(d.observe_only)

    def test_sandbox_only_defaults_true(self):
        d = _base_decision()
        self.assertTrue(d.sandbox_only)

    def test_no_trade_defaults_true(self):
        d = _base_decision()
        self.assertTrue(d.no_trade)

    def test_no_official_promotion_defaults_true(self):
        d = _base_decision()
        self.assertTrue(d.no_official_promotion)

    def test_validate_governance_flags_passes_for_valid(self):
        d = _base_decision()
        _validate_governance_flags(d)  # should not raise

    def test_validate_governance_flags_rejects_sandbox_only_false(self):
        d = _base_decision(sandbox_only=False)
        with self.assertRaises(ValueError):
            _validate_governance_flags(d)

    def test_validate_governance_flags_rejects_no_trade_false(self):
        d = _base_decision(no_trade=False)
        with self.assertRaises(ValueError):
            _validate_governance_flags(d)

    def test_validate_governance_flags_rejects_no_official_promotion_false(self):
        d = _base_decision(no_official_promotion=False)
        with self.assertRaises(ValueError):
            _validate_governance_flags(d)


# ---------------------------------------------------------------------------
# 4. to_dict serialization
# ---------------------------------------------------------------------------

class TestToDict(unittest.TestCase):
    def test_decision_serialized_as_string(self):
        d = _base_decision()
        result = d.to_dict()
        self.assertIsInstance(result["decision"], str)
        self.assertEqual(result["decision"], "keep_watching")

    def test_all_required_keys_present(self):
        d = _base_decision()
        result = d.to_dict()
        for key in (
            "generated_at", "symbol", "company_name", "candidate_status",
            "corroboration_score", "corroboration_level", "decision",
            "decision_reason", "operator", "source_artifact", "run_id",
            "observe_only", "sandbox_only", "no_trade", "no_official_promotion",
        ):
            self.assertIn(key, result, f"Missing key: {key}")

    def test_symbol_uppercase(self):
        d = _base_decision(symbol="nvda")
        # make_approval_decision uppercases, but direct construction doesn't
        result = d.to_dict()
        self.assertEqual(result["symbol"], "nvda")

    def test_json_serializable(self):
        d = _base_decision()
        json_str = json.dumps(d.to_dict())
        self.assertIsInstance(json_str, str)


# ---------------------------------------------------------------------------
# 5. make_approval_decision factory
# ---------------------------------------------------------------------------

class TestMakeApprovalDecision(unittest.TestCase):
    def test_creates_valid_decision(self):
        d = make_approval_decision(symbol="aapl", decision="keep_watching")
        self.assertEqual(d.symbol, "AAPL")
        self.assertEqual(d.decision, ApprovalDecision.KEEP_WATCHING)

    def test_governance_flags_hardcoded(self):
        d = make_approval_decision(symbol="AAPL", decision="reject_candidate")
        self.assertTrue(d.observe_only)
        self.assertTrue(d.sandbox_only)
        self.assertTrue(d.no_trade)
        self.assertTrue(d.no_official_promotion)

    def test_buy_decision_raises(self):
        with self.assertRaises(ValueError):
            make_approval_decision(symbol="AAPL", decision="buy")

    def test_sell_decision_raises(self):
        with self.assertRaises(ValueError):
            make_approval_decision(symbol="AAPL", decision="sell")

    def test_symbol_uppercased(self):
        d = make_approval_decision(symbol="nvda", decision="keep_watching")
        self.assertEqual(d.symbol, "NVDA")

    def test_custom_timestamp(self):
        d = make_approval_decision(symbol="NVDA", decision="keep_watching", now=_NOW)
        self.assertIn("2026-05-02", d.generated_at)

    def test_default_operator(self):
        d = make_approval_decision(symbol="NVDA", decision="keep_watching")
        self.assertEqual(d.operator, "operator")


# ---------------------------------------------------------------------------
# 6. record_approval_decision — JSONL append write
# ---------------------------------------------------------------------------

class TestRecordApprovalDecision(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "outputs"

    def tearDown(self):
        self.tmp.cleanup()

    def _make(self, **kw) -> DiscoveryApprovalDecision:
        return make_approval_decision(symbol="NVDA", decision="keep_watching", **kw)

    def test_creates_file_on_first_write(self):
        d = self._make()
        path = record_approval_decision(d, base_dir=self.base)
        self.assertTrue(path.exists())

    def test_file_in_sandbox_subdir(self):
        d = self._make()
        path = record_approval_decision(d, base_dir=self.base)
        self.assertIn("sandbox", str(path))
        self.assertIn("discovery", str(path))

    def test_file_ends_with_jsonl(self):
        d = self._make()
        path = record_approval_decision(d, base_dir=self.base)
        self.assertTrue(str(path).endswith(".jsonl"))

    def test_written_line_is_valid_json(self):
        d = self._make()
        path = record_approval_decision(d, base_dir=self.base)
        lines = [l for l in path.read_text().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        obj = json.loads(lines[0])
        self.assertIsInstance(obj, dict)

    def test_append_only_second_write_adds_line(self):
        d1 = self._make()
        d2 = make_approval_decision(symbol="AAPL", decision="needs_more_evidence")
        record_approval_decision(d1, base_dir=self.base)
        record_approval_decision(d2, base_dir=self.base)
        path = self.base / "sandbox" / "discovery" / "approval_decisions.jsonl"
        lines = [l for l in path.read_text().splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)

    def test_append_does_not_overwrite_prior_decisions(self):
        d1 = make_approval_decision(symbol="NVDA", decision="keep_watching")
        d2 = make_approval_decision(symbol="NVDA", decision="reject_candidate")
        record_approval_decision(d1, base_dir=self.base)
        record_approval_decision(d2, base_dir=self.base)
        path = self.base / "sandbox" / "discovery" / "approval_decisions.jsonl"
        lines = [l for l in path.read_text().splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        self.assertEqual(first["decision"], "keep_watching")
        self.assertEqual(second["decision"], "reject_candidate")

    def test_governance_flags_in_written_payload(self):
        d = self._make()
        path = record_approval_decision(d, base_dir=self.base)
        obj = json.loads(path.read_text().strip())
        self.assertTrue(obj["sandbox_only"])
        self.assertTrue(obj["no_trade"])
        self.assertTrue(obj["no_official_promotion"])

    def test_tampered_sandbox_only_raises(self):
        d = _base_decision(sandbox_only=False)
        with self.assertRaises(ValueError):
            record_approval_decision(d, base_dir=self.base)

    def test_tampered_no_trade_raises(self):
        d = _base_decision(no_trade=False)
        with self.assertRaises(ValueError):
            record_approval_decision(d, base_dir=self.base)

    def test_no_writes_outside_sandbox(self):
        """record_approval_decision must never write to latest or policy."""
        d = self._make()
        record_approval_decision(d, base_dir=self.base)
        latest_dir = self.base / "latest"
        policy_dir = self.base / "policy"
        portfolio_dir = self.base / "portfolio"
        self.assertFalse(latest_dir.exists())
        self.assertFalse(policy_dir.exists())
        self.assertFalse(portfolio_dir.exists())


# ---------------------------------------------------------------------------
# 7. load_approval_decisions
# ---------------------------------------------------------------------------

class TestLoadApprovalDecisions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "outputs"

    def tearDown(self):
        self.tmp.cleanup()

    def _jsonl_path(self) -> Path:
        p = self.base / "sandbox" / "discovery" / "approval_decisions.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def test_missing_file_returns_empty_list(self):
        result = load_approval_decisions(self.base)
        self.assertEqual(result, [])

    def test_empty_file_returns_empty_list(self):
        self._jsonl_path().write_text("", encoding="utf-8")
        result = load_approval_decisions(self.base)
        self.assertEqual(result, [])

    def test_valid_lines_loaded(self):
        p = self._jsonl_path()
        d = make_approval_decision(symbol="NVDA", decision="keep_watching")
        p.write_text(json.dumps(d.to_dict()) + "\n", encoding="utf-8")
        result = load_approval_decisions(self.base)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["symbol"], "NVDA")

    def test_malformed_line_skipped(self):
        p = self._jsonl_path()
        good = json.dumps(make_approval_decision(symbol="NVDA", decision="keep_watching").to_dict())
        p.write_text("not-json\n" + good + "\n", encoding="utf-8")
        result = load_approval_decisions(self.base)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["symbol"], "NVDA")

    def test_blank_lines_skipped(self):
        p = self._jsonl_path()
        good = json.dumps(make_approval_decision(symbol="AAPL", decision="reject_candidate").to_dict())
        p.write_text("\n\n" + good + "\n\n", encoding="utf-8")
        result = load_approval_decisions(self.base)
        self.assertEqual(len(result), 1)

    def test_non_dict_json_lines_skipped(self):
        p = self._jsonl_path()
        p.write_text('["list", "not", "a", "dict"]\n', encoding="utf-8")
        result = load_approval_decisions(self.base)
        self.assertEqual(result, [])

    def test_multiple_decisions_all_loaded(self):
        p = self._jsonl_path()
        lines = [
            json.dumps(make_approval_decision(symbol="NVDA", decision="keep_watching").to_dict()),
            json.dumps(make_approval_decision(symbol="AAPL", decision="reject_candidate").to_dict()),
        ]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = load_approval_decisions(self.base)
        self.assertEqual(len(result), 2)


# ---------------------------------------------------------------------------
# 8. build_approval_summary
# ---------------------------------------------------------------------------

class TestBuildApprovalSummary(unittest.TestCase):
    def test_empty_decisions_returns_safe_defaults(self):
        summary = build_approval_summary([])
        self.assertEqual(summary["total_decisions"], 0)
        self.assertEqual(summary["unique_symbols_reviewed"], 0)
        self.assertEqual(summary["decision_counts"], {})

    def test_governance_flags_always_true(self):
        summary = build_approval_summary([])
        self.assertTrue(summary["observe_only"])
        self.assertTrue(summary["sandbox_only"])
        self.assertTrue(summary["no_trade"])
        self.assertTrue(summary["no_official_promotion"])

    def test_counts_decisions_correctly(self):
        decisions = [
            make_approval_decision(symbol="NVDA", decision="keep_watching").to_dict(),
            make_approval_decision(symbol="AAPL", decision="keep_watching").to_dict(),
            make_approval_decision(symbol="MSFT", decision="reject_candidate").to_dict(),
        ]
        summary = build_approval_summary(decisions)
        self.assertEqual(summary["total_decisions"], 3)
        self.assertEqual(summary["decision_counts"]["keep_watching"], 2)
        self.assertEqual(summary["decision_counts"]["reject_candidate"], 1)

    def test_unique_symbols_counted(self):
        decisions = [
            make_approval_decision(symbol="NVDA", decision="keep_watching").to_dict(),
            make_approval_decision(symbol="AAPL", decision="keep_watching").to_dict(),
        ]
        summary = build_approval_summary(decisions)
        self.assertEqual(summary["unique_symbols_reviewed"], 2)

    def test_latest_per_symbol_last_wins(self):
        decisions = [
            make_approval_decision(symbol="NVDA", decision="keep_watching").to_dict(),
            make_approval_decision(symbol="NVDA", decision="reject_candidate").to_dict(),
        ]
        summary = build_approval_summary(decisions)
        self.assertEqual(summary["latest_per_symbol"]["NVDA"]["decision"], "reject_candidate")

    def test_disclaimer_present(self):
        summary = build_approval_summary([])
        self.assertIn("sandbox research notes", summary["disclaimer"])

    def test_no_buy_sell_in_summary_keys(self):
        summary = build_approval_summary([])
        summary_str = json.dumps(summary)
        # governance keys should not mention buy/sell
        self.assertNotIn('"buy"', summary_str)
        self.assertNotIn('"sell"', summary_str)


# ---------------------------------------------------------------------------
# 9. Safety regression tests
# ---------------------------------------------------------------------------

class TestSafetyConstraints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "outputs"

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_forbidden_statuses_in_enum(self):
        for forbidden in ("BUY", "SELL", "ACTIONABLE", "PROMOTED", "VALIDATED"):
            self.assertFalse(
                hasattr(ApprovalDecision, forbidden),
                f"ApprovalDecision must not have {forbidden}",
            )

    def test_record_buy_decision_always_fails(self):
        for bad in ("buy", "BUY", "sell", "SELL", "actionable", "promoted", "validated"):
            with self.assertRaises((ValueError, AttributeError)):
                d = _base_decision(decision=bad)  # type: ignore
                record_approval_decision(d, base_dir=self.base)

    def test_no_outputs_latest_written(self):
        d = make_approval_decision(symbol="NVDA", decision="keep_watching")
        record_approval_decision(d, base_dir=self.base)
        self.assertFalse((self.base / "latest").exists())

    def test_no_outputs_policy_written(self):
        d = make_approval_decision(symbol="NVDA", decision="keep_watching")
        record_approval_decision(d, base_dir=self.base)
        self.assertFalse((self.base / "policy").exists())

    def test_no_outputs_portfolio_written(self):
        d = make_approval_decision(symbol="NVDA", decision="keep_watching")
        record_approval_decision(d, base_dir=self.base)
        self.assertFalse((self.base / "portfolio").exists())

    def test_risk_flagged_candidate_can_be_reviewed(self):
        # review is a research note — risk_flag does not block review
        d = make_approval_decision(
            symbol="BADCO",
            decision="reject_candidate",
            candidate_status="discovered",
            corroboration_score=0.3,
            corroboration_level="weak",
        )
        path = record_approval_decision(d, base_dir=self.base)
        self.assertTrue(path.exists())
        obj = json.loads(path.read_text().strip())
        self.assertEqual(obj["decision"], "reject_candidate")
        self.assertTrue(obj["no_trade"])
        self.assertTrue(obj["no_official_promotion"])
