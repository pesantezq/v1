import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution_layer import (
    ExecutionSummary,
    _assign_group,
    _assign_priority,
    _build_execution_action,
    _normalise_action,
    build_execution_summary,
    print_execution_summary,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _raw(
    symbol="NVDA",
    action="BUY",
    score=80.0,
    confidence=0.80,
    strategy_type="momentum",
    allocation_pct=0.08,
    allocation_amount=8_000.0,
    rationale=None,
) -> dict:
    return {
        "symbol": symbol,
        "action": action,
        "score": score,
        "confidence": confidence,
        "strategy_type": strategy_type,
        "suggested_allocation_pct": allocation_pct,
        "suggested_allocation_amount": allocation_amount,
        "rationale": rationale if rationale is not None else [
            "Strong breakout with volume confirmation",
            "Above all key moving averages",
        ],
        "related_symbol": None,
        "exit_plan": None,
    }


_MIXED_OUTPUT = {
    "available": True,
    "summary_line": "5 decisions — 1 exit, 2 entries, 1 watch, 1 hold",
    "actions": [
        _raw("NVDA", "BUY", 82, 0.80, "momentum", 0.08, 8_000),
        _raw("MSFT", "SELL", None, None, "compounder", None, None, ["Trend break below 200DMA"]),
        _raw("AAPL", "HOLD", 60, 0.55, "compounder", None, None, ["Thesis intact, no new catalyst"]),
        _raw("TSLA", "ADD_TO_WATCHLIST", 50, 0.50, "momentum", None, None, ["Watching for breakout"]),
        _raw("AMD", "BUY", 65, 0.63, "momentum", 0.04, 4_000),
    ],
}


# ─── Priority Assignment ──────────────────────────────────────────────────────

class TestAssignPriority(unittest.TestCase):
    def test_sell_always_high(self):
        self.assertEqual(_assign_priority("SELL", 0, 0.0, None), "HIGH")

    def test_trim_always_high(self):
        self.assertEqual(_assign_priority("TRIM", 20, 0.3, None), "HIGH")

    def test_buy_high_when_both_thresholds_met(self):
        self.assertEqual(_assign_priority("BUY", 80, 0.80, "momentum"), "HIGH")

    def test_buy_medium_when_moderate_signal(self):
        self.assertEqual(_assign_priority("BUY", 65, 0.65, "momentum"), "MEDIUM")

    def test_buy_low_when_weak_signal(self):
        self.assertEqual(_assign_priority("BUY", 40, 0.45, "compounder"), "LOW")

    def test_promote_treated_as_buy(self):
        self.assertEqual(_assign_priority("PROMOTE_TO_PORTFOLIO", 75, 0.76, "compounder"), "HIGH")

    def test_hold_always_low(self):
        self.assertEqual(_assign_priority("HOLD", 90, 0.95, "compounder"), "LOW")

    def test_watchlist_always_low(self):
        self.assertEqual(_assign_priority("ADD_TO_WATCHLIST", 99, 1.0, "momentum"), "LOW")

    def test_none_score_and_conf_defaults_low(self):
        self.assertEqual(_assign_priority("BUY", None, None, None), "LOW")

    def test_exact_high_boundary(self):
        self.assertEqual(_assign_priority("BUY", 72.0, 0.75, "momentum"), "HIGH")

    def test_just_below_high_score_boundary(self):
        self.assertEqual(_assign_priority("BUY", 71.9, 0.75, "momentum"), "MEDIUM")

    def test_high_score_but_low_conf_is_medium(self):
        # Score meets HIGH but confidence only meets MEDIUM
        self.assertEqual(_assign_priority("BUY", 80.0, 0.65, "momentum"), "MEDIUM")


# ─── Action Normalisation ─────────────────────────────────────────────────────

class TestNormaliseAction(unittest.TestCase):
    def test_promote_maps_to_buy(self):
        self.assertEqual(_normalise_action("PROMOTE_TO_PORTFOLIO"), "BUY")

    def test_watchlist_maps_to_watch(self):
        self.assertEqual(_normalise_action("ADD_TO_WATCHLIST"), "WATCH")

    def test_sell_passthrough(self):
        self.assertEqual(_normalise_action("SELL"), "SELL")

    def test_trim_passthrough(self):
        self.assertEqual(_normalise_action("TRIM"), "TRIM")

    def test_hold_passthrough(self):
        self.assertEqual(_normalise_action("HOLD"), "HOLD")

    def test_case_insensitive(self):
        self.assertEqual(_normalise_action("sell"), "SELL")

    def test_unknown_uppercased_passthrough(self):
        self.assertEqual(_normalise_action("rebalance"), "REBALANCE")


# ─── Group Assignment ─────────────────────────────────────────────────────────

class TestAssignGroup(unittest.TestCase):
    def test_sell_is_immediate(self):
        self.assertEqual(_assign_group("SELL", "HIGH"), "immediate")

    def test_trim_is_immediate(self):
        self.assertEqual(_assign_group("TRIM", "HIGH"), "immediate")

    def test_high_buy_is_immediate(self):
        self.assertEqual(_assign_group("BUY", "HIGH"), "immediate")

    def test_medium_buy_is_immediate(self):
        self.assertEqual(_assign_group("BUY", "MEDIUM"), "immediate")

    def test_low_buy_is_conditional(self):
        self.assertEqual(_assign_group("BUY", "LOW"), "conditional")

    def test_watch_is_watchlist(self):
        self.assertEqual(_assign_group("WATCH", "LOW"), "watchlist")

    def test_hold_is_conditional(self):
        self.assertEqual(_assign_group("HOLD", "LOW"), "conditional")


# ─── ExecutionAction Building ─────────────────────────────────────────────────

class TestBuildExecutionAction(unittest.TestCase):
    def test_buy_fields_populated(self):
        ea = _build_execution_action(_raw("NVDA", "BUY", 82, 0.80, "momentum", 0.08, 8_000))
        self.assertEqual(ea.symbol, "NVDA")
        self.assertEqual(ea.action, "BUY")
        self.assertEqual(ea.priority, "HIGH")
        self.assertEqual(ea.group, "immediate")
        self.assertAlmostEqual(ea.allocation, 0.08)
        self.assertEqual(ea.strategy, "momentum")

    def test_sell_is_high_priority_immediate(self):
        ea = _build_execution_action(_raw("MSFT", "SELL", None, None, "compounder", None, None, ["Trend break"]))
        self.assertEqual(ea.action, "SELL")
        self.assertEqual(ea.priority, "HIGH")
        self.assertEqual(ea.group, "immediate")
        self.assertIsNone(ea.allocation)

    def test_watchlist_action_lands_in_watchlist(self):
        ea = _build_execution_action(_raw("TSLA", "ADD_TO_WATCHLIST", 50, 0.50, "momentum", None, None, ["Setup forming"]))
        self.assertEqual(ea.action, "WATCH")
        self.assertEqual(ea.priority, "LOW")
        self.assertEqual(ea.group, "watchlist")

    def test_hold_lands_in_conditional(self):
        ea = _build_execution_action(_raw("AAPL", "HOLD", 60, 0.55, "compounder", None, None, ["Thesis intact"]))
        self.assertEqual(ea.action, "HOLD")
        self.assertEqual(ea.priority, "LOW")
        self.assertEqual(ea.group, "conditional")

    def test_reason_uses_first_two_rationale_items(self):
        ea = _build_execution_action(_raw(rationale=["Reason A", "Reason B", "Reason C"]))
        self.assertIn("Reason A", ea.reason)
        self.assertIn("Reason B", ea.reason)
        self.assertNotIn("Reason C", ea.reason)

    def test_missing_symbol_defaults_to_unknown(self):
        ea = _build_execution_action({"action": "HOLD"})
        self.assertEqual(ea.symbol, "UNKNOWN")

    def test_empty_rationale_uses_action_as_fallback(self):
        ea = _build_execution_action(_raw(rationale=[]))
        self.assertNotEqual(ea.reason, "")

    def test_promote_normalises_to_buy(self):
        ea = _build_execution_action(_raw("XYZ", "PROMOTE_TO_PORTFOLIO", 75, 0.76, "compounder", 0.05, 5_000))
        self.assertEqual(ea.action, "BUY")

    def test_completely_empty_dict(self):
        ea = _build_execution_action({})
        self.assertEqual(ea.symbol, "UNKNOWN")
        self.assertEqual(ea.action, "HOLD")
        self.assertEqual(ea.priority, "LOW")
        self.assertIsNotNone(ea.reason)

    def test_trim_action(self):
        ea = _build_execution_action(_raw("SPY", "TRIM", 70, 0.70, "compounder", 0.03, 3_000, ["Profit protection at 30%"]))
        self.assertEqual(ea.action, "TRIM")
        self.assertEqual(ea.priority, "HIGH")
        self.assertEqual(ea.group, "immediate")


# ─── Full Summary Builder ─────────────────────────────────────────────────────

class TestBuildExecutionSummary(unittest.TestCase):
    def setUp(self):
        self.summary = build_execution_summary(_MIXED_OUTPUT)

    def test_sell_lands_in_immediate(self):
        symbols = {a.symbol for a in self.summary.immediate}
        self.assertIn("MSFT", symbols)

    def test_high_buy_lands_in_immediate(self):
        symbols = {a.symbol for a in self.summary.immediate}
        self.assertIn("NVDA", symbols)

    def test_medium_buy_lands_in_immediate(self):
        symbols = {a.symbol for a in self.summary.immediate}
        self.assertIn("AMD", symbols)

    def test_watchlist_symbol_lands_in_watchlist(self):
        symbols = {a.symbol for a in self.summary.watchlist}
        self.assertIn("TSLA", symbols)

    def test_hold_lands_in_conditional(self):
        symbols = {a.symbol for a in self.summary.conditional}
        self.assertIn("AAPL", symbols)

    def test_all_actions_contains_all_decisions(self):
        self.assertEqual(len(self.summary.all_actions), 5)

    def test_summary_line_preserved(self):
        self.assertIn("5 decisions", self.summary.summary_line)

    def test_immediate_sorted_sell_before_buy(self):
        actions_in_order = [a.action for a in self.summary.immediate]
        if "SELL" in actions_in_order and "BUY" in actions_in_order:
            self.assertLess(actions_in_order.index("SELL"), actions_in_order.index("BUY"))

    def test_to_dict_has_all_keys(self):
        d = self.summary.to_dict()
        self.assertIn("immediate", d)
        self.assertIn("watchlist", d)
        self.assertIn("conditional", d)
        self.assertIn("summary_line", d)

    def test_to_json_is_valid(self):
        import json
        parsed = json.loads(self.summary.to_json())
        self.assertIsInstance(parsed["immediate"], list)

    def test_to_csv_has_header_and_rows(self):
        csv_text = self.summary.to_csv()
        self.assertIn("symbol", csv_text)
        self.assertIn("action", csv_text)
        self.assertIn("priority", csv_text)
        lines = csv_text.strip().splitlines()
        self.assertGreater(len(lines), 1)

    def test_to_csv_contains_all_symbols(self):
        csv_text = self.summary.to_csv()
        for sym in ("NVDA", "MSFT", "AAPL", "TSLA", "AMD"):
            self.assertIn(sym, csv_text)


# ─── Empty / Edge Cases ───────────────────────────────────────────────────────

class TestEmptyAndEdgeCases(unittest.TestCase):
    def test_empty_actions_list(self):
        s = build_execution_summary({"available": False, "summary_line": "", "actions": []})
        self.assertEqual(s.immediate, [])
        self.assertEqual(s.watchlist, [])
        self.assertEqual(s.conditional, [])
        self.assertEqual(s.all_actions, [])

    def test_missing_actions_key(self):
        s = build_execution_summary({})
        self.assertEqual(s.all_actions, [])

    def test_missing_summary_line_defaults_empty(self):
        s = build_execution_summary({"actions": []})
        self.assertEqual(s.summary_line, "")

    def test_all_hold_actions(self):
        output = {
            "actions": [
                _raw(symbol, "HOLD", 50, 0.50, "compounder", None, None, ["Holding"])
                for symbol in ("A", "B", "C")
            ]
        }
        s = build_execution_summary(output)
        self.assertEqual(len(s.conditional), 3)
        self.assertEqual(len(s.immediate), 0)

    def test_all_sell_actions_are_immediate(self):
        output = {
            "actions": [
                _raw(symbol, "SELL", None, None, "compounder", None, None, ["Exit signal"])
                for symbol in ("X", "Y", "Z")
            ]
        }
        s = build_execution_summary(output)
        self.assertEqual(len(s.immediate), 3)

    def test_raw_with_no_score_but_high_confidence_buy_is_low(self):
        ea = _build_execution_action({"symbol": "FOO", "action": "BUY", "confidence": 0.90})
        self.assertEqual(ea.priority, "LOW")

    def test_raw_with_no_confidence_but_high_score_buy_is_low(self):
        ea = _build_execution_action({"symbol": "BAR", "action": "BUY", "score": 90.0})
        self.assertEqual(ea.priority, "LOW")

    def test_single_rationale_item(self):
        ea = _build_execution_action(_raw(rationale=["Only reason"]))
        self.assertEqual(ea.reason, "Only reason")


# ─── Console Output ───────────────────────────────────────────────────────────

class TestPrintExecutionSummary(unittest.TestCase):
    def test_prints_without_error(self):
        s = build_execution_summary(_MIXED_OUTPUT)
        # Should not raise
        import io as _io
        import sys as _sys
        captured = _io.StringIO()
        _sys.stdout = captured
        try:
            print_execution_summary(s)
        finally:
            _sys.stdout = _sys.__stdout__
        output = captured.getvalue()
        self.assertIn("EXECUTION SUMMARY", output)
        self.assertIn("IMMEDIATE", output)

    def test_empty_summary_prints_without_error(self):
        import io as _io
        import sys as _sys
        captured = _io.StringIO()
        _sys.stdout = captured
        try:
            print_execution_summary(ExecutionSummary())
        finally:
            _sys.stdout = _sys.__stdout__
        output = captured.getvalue()
        self.assertIn("EXECUTION SUMMARY", output)

    def test_all_group_headers_appear(self):
        s = build_execution_summary(_MIXED_OUTPUT)
        import io as _io
        import sys as _sys
        captured = _io.StringIO()
        _sys.stdout = captured
        try:
            print_execution_summary(s)
        finally:
            _sys.stdout = _sys.__stdout__
        output = captured.getvalue()
        self.assertIn("IMMEDIATE", output)
        self.assertIn("WATCHLIST", output)
        self.assertIn("CONDITIONAL", output)


if __name__ == "__main__":
    unittest.main()
