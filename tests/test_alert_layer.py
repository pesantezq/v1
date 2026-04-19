import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from alert_layer import (
    Alert,
    AlertBundle,
    _alert_from_execution_action,
    _deduplicate,
    _format_headline,
    _format_replacement_headline,
    _replacement_alert_from_raw,
    _resolve_severity,
    build_alert_bundle,
    print_alert_bundle,
)
from execution_layer import ExecutionAction, ExecutionSummary, build_execution_summary

_TS = "2026-01-01T00:00:00+00:00"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ea(
    symbol="NVDA",
    action="BUY",
    priority="HIGH",
    strategy="momentum",
    allocation=0.08,
    allocation_amount=8_000.0,
    score=82.0,
    confidence=0.80,
    reason="Strong breakout. Above all key MAs.",
    group="immediate",
) -> ExecutionAction:
    return ExecutionAction(
        symbol=symbol,
        action=action,
        priority=priority,
        group=group,
        strategy=strategy,
        allocation=allocation,
        allocation_amount=allocation_amount,
        reason=reason,
        score=score,
        confidence=confidence,
    )


def _raw(
    symbol="NVDA",
    action="BUY",
    score=82.0,
    confidence=0.80,
    strategy_type="momentum",
    allocation_pct=0.08,
    allocation_amount=8_000.0,
    rationale=None,
    related_symbol=None,
) -> dict:
    return {
        "symbol": symbol,
        "action": action,
        "score": score,
        "confidence": confidence,
        "strategy_type": strategy_type,
        "suggested_allocation_pct": allocation_pct,
        "suggested_allocation_amount": allocation_amount,
        "rationale": rationale or ["Strong breakout with volume", "Above all key MAs"],
        "related_symbol": related_symbol,
        "exit_plan": None,
    }


def _exec_summary_from_raws(raws: list[dict]) -> ExecutionSummary:
    return build_execution_summary({"actions": raws, "summary_line": ""})


# ─── Severity Resolution ──────────────────────────────────────────────────────

class TestResolveSeverity(unittest.TestCase):
    def test_sell_always_high(self):
        self.assertEqual(_resolve_severity("SELL", "LOW"), "HIGH")

    def test_trim_always_high(self):
        self.assertEqual(_resolve_severity("TRIM", "LOW"), "HIGH")

    def test_buy_inherits_priority(self):
        self.assertEqual(_resolve_severity("BUY", "HIGH"), "HIGH")
        self.assertEqual(_resolve_severity("BUY", "MEDIUM"), "MEDIUM")
        self.assertEqual(_resolve_severity("BUY", "LOW"), "LOW")

    def test_hold_always_low(self):
        self.assertEqual(_resolve_severity("HOLD", "LOW"), "LOW")

    def test_watch_always_low(self):
        self.assertEqual(_resolve_severity("WATCH", "LOW"), "LOW")

    def test_unknown_priority_defaults_low(self):
        self.assertEqual(_resolve_severity("BUY", "UNKNOWN"), "LOW")


# ─── Headline Formatting ──────────────────────────────────────────────────────

class TestFormatHeadline(unittest.TestCase):
    def test_buy_headline_contains_symbol(self):
        ea = _ea("NVDA", "BUY", allocation=0.08, score=82.0, confidence=0.80)
        h = _format_headline(ea)
        self.assertIn("BUY", h)
        self.assertIn("NVDA", h)

    def test_buy_headline_contains_alloc_pct(self):
        ea = _ea("NVDA", "BUY", allocation=0.08, allocation_amount=None)
        h = _format_headline(ea)
        self.assertIn("8.0%", h)

    def test_buy_headline_uses_dollar_amount_when_no_pct(self):
        ea = _ea("NVDA", "BUY", allocation=None, allocation_amount=8_000.0)
        h = _format_headline(ea)
        self.assertIn("$8,000", h)

    def test_sell_headline_mentions_exit(self):
        ea = _ea("MSFT", "SELL", allocation=None, allocation_amount=None)
        h = _format_headline(ea)
        self.assertIn("SELL", h)
        self.assertIn("exit", h.lower())

    def test_trim_headline_mentions_partial(self):
        ea = _ea("SPY", "TRIM", allocation=None, allocation_amount=None)
        h = _format_headline(ea)
        self.assertIn("TRIM", h)
        self.assertIn("partial", h.lower())

    def test_watch_headline_mentions_monitor(self):
        ea = _ea("TSLA", "WATCH", allocation=None, allocation_amount=None, score=None, confidence=None)
        h = _format_headline(ea)
        self.assertIn("WATCH", h)
        self.assertIn("monitor", h.lower())

    def test_strategy_appears_in_brackets(self):
        ea = _ea("NVDA", "BUY", strategy="compounder")
        h = _format_headline(ea)
        self.assertIn("[compounder]", h)

    def test_no_strategy_no_brackets(self):
        ea = _ea("NVDA", "BUY", strategy=None)
        h = _format_headline(ea)
        self.assertNotIn("[", h)

    def test_replacement_headline_contains_both_symbols(self):
        h = _format_replacement_headline("NVDA", "INTC", "momentum")
        self.assertIn("NVDA", h)
        self.assertIn("INTC", h)
        self.assertIn("REPLACEMENT", h)


# ─── Alert From ExecutionAction ───────────────────────────────────────────────

class TestAlertFromExecutionAction(unittest.TestCase):
    def test_buy_alert_fields(self):
        ea = _ea("NVDA", "BUY", priority="HIGH")
        alert = _alert_from_execution_action(ea, _TS)
        self.assertEqual(alert.symbol, "NVDA")
        self.assertEqual(alert.alert_type, "BUY")
        self.assertEqual(alert.severity, "HIGH")
        self.assertEqual(alert.group, "immediate")
        self.assertIsNotNone(alert.alert_id)
        self.assertEqual(alert.timestamp, _TS)

    def test_sell_alert_is_always_high_severity(self):
        ea = _ea("MSFT", "SELL", priority="LOW", allocation=None, allocation_amount=None)
        alert = _alert_from_execution_action(ea, _TS)
        self.assertEqual(alert.severity, "HIGH")
        self.assertEqual(alert.group, "immediate")

    def test_trim_alert_is_always_high_severity(self):
        ea = _ea("SPY", "TRIM", priority="LOW", allocation=None, allocation_amount=None)
        alert = _alert_from_execution_action(ea, _TS)
        self.assertEqual(alert.severity, "HIGH")

    def test_medium_buy_goes_to_monitor_group(self):
        ea = _ea("AMD", "BUY", priority="MEDIUM", group="immediate")
        alert = _alert_from_execution_action(ea, _TS)
        self.assertEqual(alert.severity, "MEDIUM")
        self.assertEqual(alert.group, "monitor")

    def test_watch_goes_to_informational(self):
        ea = _ea("TSLA", "WATCH", priority="LOW", group="watchlist",
                 allocation=None, allocation_amount=None, score=None, confidence=None)
        alert = _alert_from_execution_action(ea, _TS)
        self.assertEqual(alert.severity, "LOW")
        self.assertEqual(alert.group, "informational")

    def test_alert_id_is_unique_per_call(self):
        ea = _ea()
        a1 = _alert_from_execution_action(ea, _TS)
        a2 = _alert_from_execution_action(ea, _TS)
        self.assertNotEqual(a1.alert_id, a2.alert_id)

    def test_alert_id_contains_symbol_and_type(self):
        ea = _ea("NVDA", "BUY")
        alert = _alert_from_execution_action(ea, _TS)
        self.assertIn("NVDA", alert.alert_id)
        self.assertIn("BUY", alert.alert_id)

    def test_detail_populated_from_reason(self):
        ea = _ea(reason="Breakout confirmed on volume.")
        alert = _alert_from_execution_action(ea, _TS)
        self.assertIn("Breakout confirmed", alert.detail)

    def test_empty_reason_produces_fallback_detail(self):
        ea = _ea(reason="")
        alert = _alert_from_execution_action(ea, _TS)
        self.assertNotEqual(alert.detail, "")


# ─── Replacement Alert ────────────────────────────────────────────────────────

class TestReplacementAlert(unittest.TestCase):
    def test_replacement_alert_emitted_when_related_symbol_present(self):
        raw = _raw("NVDA", related_symbol="INTC")
        alert = _replacement_alert_from_raw(raw, _TS)
        self.assertIsNotNone(alert)
        self.assertEqual(alert.alert_type, "REPLACEMENT")
        self.assertEqual(alert.symbol, "NVDA")
        self.assertEqual(alert.metadata["replaces"], "INTC")

    def test_no_replacement_alert_when_related_symbol_absent(self):
        raw = _raw("NVDA", related_symbol=None)
        self.assertIsNone(_replacement_alert_from_raw(raw, _TS))

    def test_no_replacement_alert_when_symbol_empty(self):
        raw = _raw("", related_symbol="INTC")
        self.assertIsNone(_replacement_alert_from_raw(raw, _TS))

    def test_replacement_severity_is_medium(self):
        raw = _raw("NVDA", related_symbol="INTC")
        alert = _replacement_alert_from_raw(raw, _TS)
        self.assertEqual(alert.severity, "MEDIUM")

    def test_replacement_group_is_monitor(self):
        raw = _raw("NVDA", related_symbol="INTC")
        alert = _replacement_alert_from_raw(raw, _TS)
        self.assertEqual(alert.group, "monitor")

    def test_replacement_headline_contains_both_symbols(self):
        raw = _raw("NVDA", related_symbol="INTC")
        alert = _replacement_alert_from_raw(raw, _TS)
        self.assertIn("NVDA", alert.headline)
        self.assertIn("INTC", alert.headline)

    def test_replacement_detail_uses_first_rationale(self):
        raw = _raw("NVDA", related_symbol="INTC", rationale=["RS confirms rotation"])
        alert = _replacement_alert_from_raw(raw, _TS)
        self.assertIn("RS confirms rotation", alert.detail)


# ─── Deduplication ────────────────────────────────────────────────────────────

class TestDeduplicate(unittest.TestCase):
    def _make_alert(self, symbol, alert_type, severity) -> Alert:
        return Alert(
            alert_id=f"{symbol}_{alert_type}_test",
            symbol=symbol,
            alert_type=alert_type,
            severity=severity,
            group="immediate",
            headline="test",
            detail="test",
            strategy=None,
            allocation=None,
            allocation_amount=None,
            score=None,
            confidence=None,
            timestamp=_TS,
        )

    def test_duplicate_same_symbol_and_type_keeps_higher_severity(self):
        a_low = self._make_alert("NVDA", "BUY", "LOW")
        a_high = self._make_alert("NVDA", "BUY", "HIGH")
        result = _deduplicate([a_low, a_high])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].severity, "HIGH")

    def test_duplicate_reversed_order_still_keeps_higher(self):
        a_high = self._make_alert("NVDA", "BUY", "HIGH")
        a_low = self._make_alert("NVDA", "BUY", "LOW")
        result = _deduplicate([a_high, a_low])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].severity, "HIGH")

    def test_different_types_same_symbol_kept_separately(self):
        buy = self._make_alert("NVDA", "BUY", "HIGH")
        sell = self._make_alert("NVDA", "SELL", "HIGH")
        result = _deduplicate([buy, sell])
        self.assertEqual(len(result), 2)

    def test_different_symbols_all_kept(self):
        alerts = [self._make_alert(sym, "BUY", "HIGH") for sym in ("A", "B", "C")]
        result = _deduplicate(alerts)
        self.assertEqual(len(result), 3)

    def test_empty_list_returns_empty(self):
        self.assertEqual(_deduplicate([]), [])

    def test_single_alert_unchanged(self):
        a = self._make_alert("NVDA", "BUY", "HIGH")
        result = _deduplicate([a])
        self.assertEqual(len(result), 1)


# ─── Full Bundle Builder ──────────────────────────────────────────────────────

class TestBuildAlertBundle(unittest.TestCase):
    def _make_summary(self) -> tuple[ExecutionSummary, list[dict]]:
        raws = [
            _raw("NVDA", "BUY", 82, 0.80, "momentum", 0.08, 8_000),
            _raw("MSFT", "SELL", None, None, "compounder", None, None,
                 ["Trend break below 200DMA"]),
            _raw("AAPL", "HOLD", 60, 0.55, "compounder", None, None,
                 ["Thesis intact"]),
            _raw("TSLA", "ADD_TO_WATCHLIST", 50, 0.50, "momentum", None, None,
                 ["Setup forming"]),
            _raw("AMD", "BUY", 65, 0.63, "momentum", 0.04, 4_000),
        ]
        return _exec_summary_from_raws(raws), raws

    def test_sell_lands_in_immediate(self):
        summary, raws = self._make_summary()
        bundle = build_alert_bundle(summary, raws)
        symbols = {a.symbol for a in bundle.immediate}
        self.assertIn("MSFT", symbols)

    def test_high_buy_lands_in_immediate(self):
        summary, raws = self._make_summary()
        bundle = build_alert_bundle(summary, raws)
        symbols = {a.symbol for a in bundle.immediate}
        self.assertIn("NVDA", symbols)

    def test_medium_buy_lands_in_monitor(self):
        summary, raws = self._make_summary()
        bundle = build_alert_bundle(summary, raws)
        symbols = {a.symbol for a in bundle.monitor}
        self.assertIn("AMD", symbols)

    def test_watch_lands_in_informational(self):
        summary, raws = self._make_summary()
        bundle = build_alert_bundle(summary, raws)
        symbols = {a.symbol for a in bundle.informational}
        self.assertIn("TSLA", symbols)

    def test_hold_lands_in_informational(self):
        summary, raws = self._make_summary()
        bundle = build_alert_bundle(summary, raws)
        symbols = {a.symbol for a in bundle.informational}
        self.assertIn("AAPL", symbols)

    def test_has_urgent_true_when_sell_present(self):
        summary, raws = self._make_summary()
        bundle = build_alert_bundle(summary, raws)
        self.assertTrue(bundle.has_urgent)

    def test_all_alerts_count(self):
        summary, raws = self._make_summary()
        bundle = build_alert_bundle(summary, raws)
        self.assertEqual(len(bundle.all_alerts), 5)

    def test_immediate_sells_sorted_before_buys(self):
        summary, raws = self._make_summary()
        bundle = build_alert_bundle(summary, raws)
        types = [a.alert_type for a in bundle.immediate]
        if "SELL" in types and "BUY" in types:
            self.assertLess(types.index("SELL"), types.index("BUY"))

    def test_replacement_alert_emitted_when_raw_has_related_symbol(self):
        raws = [_raw("NVDA", "BUY", 82, 0.80, related_symbol="INTC")]
        summary = _exec_summary_from_raws(raws)
        bundle = build_alert_bundle(summary, raws)
        replacement_alerts = [a for a in bundle.all_alerts if a.alert_type == "REPLACEMENT"]
        self.assertEqual(len(replacement_alerts), 1)
        self.assertEqual(replacement_alerts[0].metadata["replaces"], "INTC")

    def test_no_replacement_alert_without_raw_actions(self):
        raws = [_raw("NVDA", "BUY", 82, 0.80)]
        summary = _exec_summary_from_raws(raws)
        bundle = build_alert_bundle(summary)  # no raw_actions passed
        replacement_alerts = [a for a in bundle.all_alerts if a.alert_type == "REPLACEMENT"]
        self.assertEqual(len(replacement_alerts), 0)

    def test_dedup_off_allows_duplicates(self):
        # Two identical raws produce duplicate alerts when dedup is disabled
        raws = [_raw("NVDA", "BUY", 82, 0.80)] * 2
        summary = _exec_summary_from_raws(raws)
        bundle = build_alert_bundle(summary, raws, deduplicate=False)
        nvda_alerts = [a for a in bundle.all_alerts if a.symbol == "NVDA" and a.alert_type == "BUY"]
        self.assertGreater(len(nvda_alerts), 1)

    def test_dedup_on_collapses_duplicates(self):
        raws = [_raw("NVDA", "BUY", 82, 0.80)] * 3
        summary = _exec_summary_from_raws(raws)
        bundle = build_alert_bundle(summary, raws, deduplicate=True)
        nvda_alerts = [a for a in bundle.all_alerts if a.symbol == "NVDA" and a.alert_type == "BUY"]
        self.assertEqual(len(nvda_alerts), 1)


# ─── Empty and Edge Cases ─────────────────────────────────────────────────────

class TestEmptyAndEdgeCases(unittest.TestCase):
    def test_empty_summary_produces_empty_bundle(self):
        bundle = build_alert_bundle(ExecutionSummary())
        self.assertEqual(bundle.all_alerts, [])
        self.assertFalse(bundle.has_urgent)

    def test_empty_portfolio_output(self):
        summary = build_execution_summary({"actions": [], "summary_line": ""})
        bundle = build_alert_bundle(summary)
        self.assertEqual(bundle.all_alerts, [])

    def test_all_hold_actions_go_to_informational(self):
        raws = [_raw(sym, "HOLD", 50, 0.50, None, None, None, ["Holding"]) for sym in ("A", "B")]
        summary = _exec_summary_from_raws(raws)
        bundle = build_alert_bundle(summary)
        self.assertEqual(len(bundle.informational), 2)
        self.assertEqual(len(bundle.immediate), 0)

    def test_all_sell_actions_go_to_immediate(self):
        raws = [_raw(sym, "SELL", None, None, None, None, None, ["Exit"]) for sym in ("X", "Y")]
        summary = _exec_summary_from_raws(raws)
        bundle = build_alert_bundle(summary)
        self.assertEqual(len(bundle.immediate), 2)

    def test_missing_score_and_confidence_handled(self):
        ea = ExecutionAction(
            symbol="FOO", action="BUY", priority="HIGH", group="immediate",
            strategy=None, allocation=None, allocation_amount=None,
            reason="", score=None, confidence=None,
        )
        alert = _alert_from_execution_action(ea, _TS)
        self.assertIsNone(alert.score)
        self.assertIsNone(alert.confidence)
        self.assertNotEqual(alert.detail, "")

    def test_generated_at_is_iso_string(self):
        bundle = build_alert_bundle(ExecutionSummary())
        from datetime import datetime, timezone
        # Should parse without raising
        dt = datetime.fromisoformat(bundle.generated_at)
        self.assertIsNotNone(dt)


# ─── Serialisation ────────────────────────────────────────────────────────────

class TestSerialisation(unittest.TestCase):
    def setUp(self):
        raws = [
            _raw("NVDA", "BUY", 82, 0.80, "momentum", 0.08, 8_000),
            _raw("MSFT", "SELL", None, None, "compounder", None, None, ["Trend break"]),
        ]
        summary = _exec_summary_from_raws(raws)
        self.bundle = build_alert_bundle(summary, raws)

    def test_to_dict_structure(self):
        d = self.bundle.to_dict()
        self.assertIn("generated_at", d)
        self.assertIn("has_urgent", d)
        self.assertIn("counts", d)
        self.assertIn("immediate", d)
        self.assertIn("monitor", d)
        self.assertIn("informational", d)

    def test_to_dict_counts_are_correct(self):
        d = self.bundle.to_dict()
        total = d["counts"]["total"]
        self.assertEqual(total, len(self.bundle.all_alerts))

    def test_to_json_is_valid(self):
        parsed = json.loads(self.bundle.to_json())
        self.assertIsInstance(parsed["immediate"], list)

    def test_to_webhook_payload_structure(self):
        payload = self.bundle.to_webhook_payload()
        self.assertIn("has_urgent", payload)
        self.assertIn("urgent_count", payload)
        self.assertIn("total_alerts", payload)
        self.assertIn("headlines", payload)
        self.assertIn("urgent_alerts", payload)

    def test_webhook_payload_urgent_count_matches(self):
        payload = self.bundle.to_webhook_payload()
        urgent_from_payload = payload["urgent_count"]
        urgent_from_bundle = sum(1 for a in self.bundle.all_alerts if a.severity == "HIGH")
        self.assertEqual(urgent_from_payload, urgent_from_bundle)

    def test_webhook_headlines_is_list_of_strings(self):
        payload = self.bundle.to_webhook_payload()
        for h in payload["headlines"]:
            self.assertIsInstance(h, str)

    def test_alert_to_dict_has_all_fields(self):
        alert = self.bundle.immediate[0]
        d = alert.to_dict()
        for key in ("alert_id", "symbol", "alert_type", "severity", "group",
                    "headline", "detail", "timestamp", "metadata"):
            self.assertIn(key, d)


# ─── Console Printer ──────────────────────────────────────────────────────────

class TestPrintAlertBundle(unittest.TestCase):
    def _capture(self, bundle: AlertBundle) -> str:
        captured = io.StringIO()
        sys.stdout = captured
        try:
            print_alert_bundle(bundle)
        finally:
            sys.stdout = sys.__stdout__
        return captured.getvalue()

    def test_prints_without_error(self):
        raws = [
            _raw("NVDA", "BUY", 82, 0.80),
            _raw("MSFT", "SELL", None, None, "compounder", None, None, ["Exit"]),
        ]
        summary = _exec_summary_from_raws(raws)
        bundle = build_alert_bundle(summary, raws)
        output = self._capture(bundle)
        self.assertIn("ALERTS", output)
        self.assertIn("IMMEDIATE", output)

    def test_empty_bundle_prints_without_error(self):
        output = self._capture(AlertBundle())
        self.assertIn("ALERTS", output)

    def test_all_groups_shown_when_populated(self):
        raws = [
            _raw("NVDA", "BUY", 82, 0.80),
            _raw("AMD", "BUY", 65, 0.63),
            _raw("TSLA", "ADD_TO_WATCHLIST", 50, 0.50, "momentum", None, None, ["Watch"]),
        ]
        summary = _exec_summary_from_raws(raws)
        bundle = build_alert_bundle(summary, raws)
        output = self._capture(bundle)
        self.assertIn("IMMEDIATE", output)
        self.assertIn("MONITOR", output)
        self.assertIn("INFORMATIONAL", output)

    def test_urgent_count_in_footer(self):
        raws = [_raw("MSFT", "SELL", None, None, None, None, None, ["Exit"])]
        summary = _exec_summary_from_raws(raws)
        bundle = build_alert_bundle(summary, raws)
        output = self._capture(bundle)
        self.assertIn("urgent", output.lower())


if __name__ == "__main__":
    unittest.main()
