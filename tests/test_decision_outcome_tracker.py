from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from portfolio_automation.decision_outcome_tracker import (
    WAIT_CORRECT_THRESHOLD,
    _extract_price_map,
    _get_validation_status,
    _is_direction_correct,
    _load_jsonl,
    _make_snapshot_row,
    aggregate_metrics,
    render_summary_md,
    resolve_outcomes,
    run_outcome_tracker,
    snapshot_decisions,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TODAY = date.today().isoformat()
_RUN_ID = f"{_TODAY}_daily"


def _make_plan(decisions: list[dict] | None = None) -> dict:
    rows = decisions or []
    return {
        "generated_at": f"{_TODAY}T08:00:00",
        "run_mode": "daily",
        "observe_only": True,
        "total_decisions": len(rows),
        "decisions": rows,
    }


def _make_decision(
    symbol="QQQ",
    decision="SELL",
    priority=0.88,
    source="structural",
    confidence=1.0,
    strategy="risk_management",
    band="structural",
) -> dict:
    return {
        "symbol": symbol,
        "decision": decision,
        "priority": priority,
        "source": source,
        "confidence": confidence,
        "decision_reason": f"{decision} {symbol}.",
        "risk_flags": [],
        "inputs_used": {},
        "decision_reason_structured": {
            "decision": decision,
            "band": band,
            "strategy": strategy,
            "drivers": [],
            "why": [],
            "what_would_change": [],
            "watch_next": [],
        },
    }


def _make_validation(symbol="QQQ", decision="SELL", status="aligned") -> dict:
    return {
        "generated_at": f"{_TODAY}T08:00:00",
        "observe_only": True,
        "available": True,
        "total_validated": 1,
        "aligned_count": 1 if status == "aligned" else 0,
        "caution_count": 1 if status == "caution" else 0,
        "contradiction_count": 0,
        "insufficient_context_count": 0,
        "ai_used": False,
        "validations": [
            {
                "symbol": symbol,
                "decision": decision,
                "validation_status": status,
                "plain_english_summary": "Test.",
                "rule_alignment": "Test rule.",
                "narrative_context": "",
                "contradictions": [],
                "watch_next": [],
                "ai_used": False,
                "model": None,
                "generated_at": f"{_TODAY}T08:00:00",
            }
        ],
    }


def _make_outcome_row(
    symbol="QQQ",
    decision="SELL",
    days_ago: int = 3,
    price_at_decision: float | None = 100.0,
    resolved: bool = False,
    return_pct: float | None = None,
    direction_correct: bool | None = None,
    validation_status: str = "aligned",
) -> dict:
    row_date = (date.today() - timedelta(days=days_ago)).isoformat()
    row = {
        "run_id": f"{row_date}_daily",
        "date": row_date,
        "symbol": symbol,
        "decision": decision,
        "priority": 0.8,
        "source": "structural",
        "strategy": "risk_management",
        "band": "structural",
        "confidence": 1.0,
        "validation_status": validation_status,
        "price_at_decision": price_at_decision,
        "timestamp": f"{row_date}T08:00:00",
        "resolved": resolved,
        "resolved_at": None,
        "days_elapsed": None,
        "price_at_resolution": None,
        "return_pct": return_pct,
        "direction_correct": direction_correct,
    }
    if resolved and return_pct is not None:
        row["resolved_at"] = f"{_TODAY}T10:00:00"
        row["days_elapsed"] = days_ago
        row["price_at_resolution"] = price_at_decision * (1 + return_pct) if price_at_decision else None
    return row


# ---------------------------------------------------------------------------
# TestSnapshotDecisions
# ---------------------------------------------------------------------------

class TestSnapshotDecisions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _jsonl_path(self) -> Path:
        return self.root / "outputs" / "policy" / "decision_outcomes.jsonl"

    def test_snapshot_creates_jsonl(self):
        plan = _make_plan([_make_decision()])
        snapshot_decisions(self.root, plan, {})
        self.assertTrue(self._jsonl_path().exists())

    def test_snapshot_appends_correct_row_count(self):
        decisions = [_make_decision(symbol=f"SYM{i}") for i in range(3)]
        plan = _make_plan(decisions)
        snapshot_decisions(self.root, plan, {})
        rows = _load_jsonl(self._jsonl_path())
        self.assertEqual(3, len(rows))

    def test_snapshot_idempotent_same_run_id(self):
        plan = _make_plan([_make_decision()])
        snapshot_decisions(self.root, plan, {}, run_id="2026-04-29_daily")
        snapshot_decisions(self.root, plan, {}, run_id="2026-04-29_daily")
        rows = _load_jsonl(self._jsonl_path())
        self.assertEqual(1, len(rows))

    def test_snapshot_different_run_ids_both_appended(self):
        plan = _make_plan([_make_decision()])
        snapshot_decisions(self.root, plan, {}, run_id="2026-04-28_daily")
        snapshot_decisions(self.root, plan, {}, run_id="2026-04-29_daily")
        rows = _load_jsonl(self._jsonl_path())
        self.assertEqual(2, len(rows))

    def test_snapshot_captures_validation_status(self):
        plan = _make_plan([_make_decision("QQQ", "SELL")])
        validation = _make_validation("QQQ", "SELL", "aligned")
        snapshot_decisions(self.root, plan, validation)
        rows = _load_jsonl(self._jsonl_path())
        self.assertEqual("aligned", rows[0]["validation_status"])

    def test_snapshot_unknown_validation_status_when_no_match(self):
        plan = _make_plan([_make_decision("QQQ", "SELL")])
        validation = _make_validation("NVDA", "BUY", "caution")
        snapshot_decisions(self.root, plan, validation)
        rows = _load_jsonl(self._jsonl_path())
        self.assertEqual("unknown", rows[0]["validation_status"])

    def test_snapshot_price_from_price_snapshot(self):
        plan = _make_plan([_make_decision("QQQ", "SELL")])
        snapshot_decisions(self.root, plan, {}, price_snapshot={"QQQ": 450.0})
        rows = _load_jsonl(self._jsonl_path())
        self.assertAlmostEqual(450.0, rows[0]["price_at_decision"])

    def test_snapshot_price_null_when_no_source(self):
        plan = _make_plan([_make_decision("QQQ", "SELL")])
        snapshot_decisions(self.root, plan, {})
        rows = _load_jsonl(self._jsonl_path())
        self.assertIsNone(rows[0]["price_at_decision"])

    def test_snapshot_resolved_false_initially(self):
        plan = _make_plan([_make_decision()])
        snapshot_decisions(self.root, plan, {})
        rows = _load_jsonl(self._jsonl_path())
        self.assertFalse(rows[0]["resolved"])

    def test_snapshot_skips_empty_decisions(self):
        plan = _make_plan([])
        snapshot_decisions(self.root, plan, {})
        self.assertFalse(self._jsonl_path().exists())

    def test_snapshot_capped_at_max(self):
        decisions = [_make_decision(symbol=f"S{i}") for i in range(15)]
        plan = _make_plan(decisions)
        snapshot_decisions(self.root, plan, {})
        rows = _load_jsonl(self._jsonl_path())
        self.assertLessEqual(len(rows), 10)

    def test_snapshot_captures_band_and_strategy(self):
        plan = _make_plan([_make_decision(strategy="momentum", band="high_conviction")])
        snapshot_decisions(self.root, plan, {})
        rows = _load_jsonl(self._jsonl_path())
        self.assertEqual("momentum", rows[0]["strategy"])
        self.assertEqual("high_conviction", rows[0]["band"])


# ---------------------------------------------------------------------------
# TestDirectionCorrect
# ---------------------------------------------------------------------------

class TestDirectionCorrect(unittest.TestCase):
    def test_sell_correct_when_price_drops(self):
        self.assertTrue(_is_direction_correct("SELL", -0.05))

    def test_sell_incorrect_when_price_rises(self):
        self.assertFalse(_is_direction_correct("SELL", 0.05))

    def test_sell_incorrect_when_flat(self):
        self.assertFalse(_is_direction_correct("SELL", 0.0))

    def test_buy_correct_when_price_rises(self):
        self.assertTrue(_is_direction_correct("BUY", 0.05))

    def test_buy_incorrect_when_price_drops(self):
        self.assertFalse(_is_direction_correct("BUY", -0.05))

    def test_scale_treated_like_buy_correct(self):
        self.assertTrue(_is_direction_correct("SCALE", 0.04))

    def test_scale_treated_like_buy_incorrect(self):
        self.assertFalse(_is_direction_correct("SCALE", -0.04))

    def test_wait_correct_when_small_move(self):
        self.assertTrue(_is_direction_correct("WAIT", 0.01))

    def test_wait_correct_when_small_negative(self):
        self.assertTrue(_is_direction_correct("WAIT", -0.01))

    def test_wait_incorrect_when_large_move(self):
        self.assertFalse(_is_direction_correct("WAIT", 0.05))

    def test_wait_incorrect_when_large_drop(self):
        self.assertFalse(_is_direction_correct("WAIT", -0.05))

    def test_wait_boundary_at_threshold(self):
        # At exactly threshold it's False (abs >= threshold is incorrect)
        self.assertFalse(_is_direction_correct("WAIT", WAIT_CORRECT_THRESHOLD))
        self.assertTrue(_is_direction_correct("WAIT", WAIT_CORRECT_THRESHOLD - 0.001))

    def test_hold_neutral(self):
        self.assertIsNone(_is_direction_correct("HOLD", 0.10))

    def test_avoid_correct_when_price_drops(self):
        self.assertTrue(_is_direction_correct("AVOID", -0.05))

    def test_avoid_incorrect_when_price_rises(self):
        self.assertFalse(_is_direction_correct("AVOID", 0.05))

    def test_unknown_decision_neutral(self):
        self.assertIsNone(_is_direction_correct("UNKNOWN", 0.05))

    def test_case_insensitive(self):
        self.assertTrue(_is_direction_correct("sell", -0.05))
        self.assertTrue(_is_direction_correct("Buy", 0.05))


# ---------------------------------------------------------------------------
# TestResolveOutcomes
# ---------------------------------------------------------------------------

class TestResolveOutcomes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_jsonl(self, rows: list[dict]) -> None:
        path = self.root / "outputs" / "policy" / "decision_outcomes.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def _make_fetcher(self, prices: dict[str, float]):
        def fetcher(symbols):
            return {s.upper(): prices[s.upper()] for s in symbols if s.upper() in prices}
        return fetcher

    def test_no_resolution_when_price_fetcher_none(self):
        row = _make_outcome_row(days_ago=3, price_at_decision=100.0)
        self._write_jsonl([row])
        result = resolve_outcomes(self.root, price_fetcher=None)
        self.assertFalse(result[0]["resolved"])

    def test_resolution_skips_null_price_at_decision(self):
        row = _make_outcome_row(days_ago=3, price_at_decision=None)
        self._write_jsonl([row])
        result = resolve_outcomes(self.root, price_fetcher=self._make_fetcher({"QQQ": 95.0}))
        self.assertFalse(result[0]["resolved"])

    def test_resolution_skips_already_resolved(self):
        row = _make_outcome_row(days_ago=3, price_at_decision=100.0, resolved=True, return_pct=-0.05)
        self._write_jsonl([row])
        result = resolve_outcomes(self.root, price_fetcher=self._make_fetcher({"QQQ": 110.0}))
        # Already resolved — return_pct must not be recalculated
        self.assertAlmostEqual(-0.05, result[0]["return_pct"])

    def test_sell_resolved_correctly(self):
        row = _make_outcome_row(symbol="QQQ", decision="SELL", days_ago=3, price_at_decision=100.0)
        self._write_jsonl([row])
        result = resolve_outcomes(self.root, price_fetcher=self._make_fetcher({"QQQ": 95.0}))
        resolved = result[0]
        self.assertTrue(resolved["resolved"])
        self.assertAlmostEqual(-0.05, resolved["return_pct"])
        self.assertTrue(resolved["direction_correct"])

    def test_buy_resolved_correctly(self):
        row = _make_outcome_row(symbol="SPY", decision="BUY", days_ago=2, price_at_decision=500.0)
        self._write_jsonl([row])
        result = resolve_outcomes(self.root, price_fetcher=self._make_fetcher({"SPY": 520.0}))
        resolved = result[0]
        self.assertTrue(resolved["resolved"])
        self.assertAlmostEqual(0.04, resolved["return_pct"])
        self.assertTrue(resolved["direction_correct"])

    def test_wait_resolved_small_move_correct(self):
        row = _make_outcome_row(symbol="XLE", decision="WAIT", days_ago=1, price_at_decision=80.0)
        self._write_jsonl([row])
        result = resolve_outcomes(self.root, price_fetcher=self._make_fetcher({"XLE": 81.0}))
        resolved = result[0]
        self.assertTrue(resolved["resolved"])
        self.assertTrue(resolved["direction_correct"])

    def test_hold_resolved_neutral(self):
        row = _make_outcome_row(symbol="GLD", decision="HOLD", days_ago=2, price_at_decision=200.0)
        self._write_jsonl([row])
        result = resolve_outcomes(self.root, price_fetcher=self._make_fetcher({"GLD": 210.0}))
        resolved = result[0]
        self.assertTrue(resolved["resolved"])
        self.assertIsNone(resolved["direction_correct"])

    def test_resolution_skips_row_too_recent(self):
        # 0 days ago → not old enough for default lookback (min 1 day)
        today_str = date.today().isoformat()
        row = {
            "run_id": f"{today_str}_daily",
            "date": today_str,
            "symbol": "QQQ",
            "decision": "SELL",
            "price_at_decision": 100.0,
            "resolved": False,
        }
        self._write_jsonl([row])
        result = resolve_outcomes(self.root, price_fetcher=self._make_fetcher({"QQQ": 95.0}))
        self.assertFalse(result[0]["resolved"])

    def test_fetcher_failure_leaves_rows_unchanged(self):
        row = _make_outcome_row(days_ago=3, price_at_decision=100.0)
        self._write_jsonl([row])

        def bad_fetcher(symbols):
            raise RuntimeError("API is down")

        result = resolve_outcomes(self.root, price_fetcher=bad_fetcher)
        self.assertFalse(result[0]["resolved"])

    def test_empty_jsonl_no_crash(self):
        path = self.root / "outputs" / "policy" / "decision_outcomes.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        result = resolve_outcomes(self.root, price_fetcher=self._make_fetcher({"QQQ": 95.0}))
        self.assertEqual([], result)


# ---------------------------------------------------------------------------
# TestAggregateMetrics
# ---------------------------------------------------------------------------

class TestAggregateMetrics(unittest.TestCase):
    def test_empty_history_no_crash(self):
        summary = aggregate_metrics([])
        self.assertEqual(0, summary["total_decisions"])
        self.assertIsNone(summary["hit_rate"])

    def test_total_count_correct(self):
        rows = [
            _make_outcome_row(symbol="A", resolved=True, return_pct=-0.05, direction_correct=True),
            _make_outcome_row(symbol="B", decision="BUY", resolved=True, return_pct=0.04, direction_correct=True),
            _make_outcome_row(symbol="C"),
        ]
        summary = aggregate_metrics(rows)
        self.assertEqual(3, summary["total_decisions"])
        self.assertEqual(2, summary["resolved"])
        self.assertEqual(1, summary["unresolved"])

    def test_hit_rate_correct(self):
        rows = [
            _make_outcome_row(symbol="A", resolved=True, return_pct=-0.05, direction_correct=True),
            _make_outcome_row(symbol="B", resolved=True, return_pct=0.04, direction_correct=False),
            _make_outcome_row(symbol="C", resolved=True, return_pct=0.02, direction_correct=True),
        ]
        summary = aggregate_metrics(rows)
        self.assertAlmostEqual(2 / 3, summary["hit_rate"])

    def test_hold_excluded_from_hit_rate(self):
        rows = [
            _make_outcome_row(symbol="A", resolved=True, return_pct=-0.05, direction_correct=True),
            _make_outcome_row(symbol="B", decision="HOLD", resolved=True, return_pct=0.10,
                              direction_correct=None),
        ]
        summary = aggregate_metrics(rows)
        # Only the SELL row is judgeable
        self.assertAlmostEqual(1.0, summary["hit_rate"])

    def test_avg_return_correct(self):
        rows = [
            _make_outcome_row(symbol="A", resolved=True, return_pct=0.10, direction_correct=True),
            _make_outcome_row(symbol="B", resolved=True, return_pct=-0.04, direction_correct=False),
        ]
        summary = aggregate_metrics(rows)
        self.assertAlmostEqual(0.03, summary["avg_return_pct"])

    def test_by_decision_breakdown(self):
        rows = [
            _make_outcome_row(symbol="A", decision="SELL", resolved=True,
                              return_pct=-0.05, direction_correct=True),
            _make_outcome_row(symbol="B", decision="SELL", resolved=True,
                              return_pct=0.02, direction_correct=False),
            _make_outcome_row(symbol="C", decision="BUY", resolved=True,
                              return_pct=0.03, direction_correct=True),
        ]
        summary = aggregate_metrics(rows)
        self.assertIn("SELL", summary["by_decision"])
        self.assertEqual(2, summary["by_decision"]["SELL"]["count"])
        self.assertAlmostEqual(0.5, summary["by_decision"]["SELL"]["hit_rate"])
        self.assertIn("BUY", summary["by_decision"])
        self.assertAlmostEqual(1.0, summary["by_decision"]["BUY"]["hit_rate"])

    def test_by_validation_status_breakdown(self):
        rows = [
            _make_outcome_row(symbol="A", resolved=True, return_pct=-0.05,
                              direction_correct=True, validation_status="aligned"),
            _make_outcome_row(symbol="B", resolved=True, return_pct=0.02,
                              direction_correct=False, validation_status="caution"),
        ]
        summary = aggregate_metrics(rows)
        self.assertIn("aligned", summary["by_validation_status"])
        self.assertIn("caution", summary["by_validation_status"])

    def test_last_10_resolved_capped(self):
        rows = [
            _make_outcome_row(symbol=f"S{i}", days_ago=i + 1,
                              resolved=True, return_pct=0.01, direction_correct=True)
            for i in range(15)
        ]
        summary = aggregate_metrics(rows)
        self.assertLessEqual(len(summary["last_10_resolved"]), 10)

    def test_best_and_worst_decision(self):
        rows = [
            _make_outcome_row(symbol="BEST", resolved=True, return_pct=0.20, direction_correct=True),
            _make_outcome_row(symbol="MID", resolved=True, return_pct=0.05, direction_correct=True),
            _make_outcome_row(symbol="WORST", resolved=True, return_pct=-0.15, direction_correct=True),
        ]
        summary = aggregate_metrics(rows)
        self.assertEqual("BEST", summary["best_decision"]["symbol"])
        self.assertEqual("WORST", summary["worst_decision"]["symbol"])

    def test_no_resolved_rows_hit_rate_none(self):
        rows = [_make_outcome_row(symbol="A"), _make_outcome_row(symbol="B")]
        summary = aggregate_metrics(rows)
        self.assertIsNone(summary["hit_rate"])
        self.assertIsNone(summary["avg_return_pct"])


# ---------------------------------------------------------------------------
# TestMarkdownOutput
# ---------------------------------------------------------------------------

class TestMarkdownOutput(unittest.TestCase):
    def test_markdown_generated(self):
        rows = [
            _make_outcome_row(resolved=True, return_pct=-0.05, direction_correct=True),
        ]
        summary = aggregate_metrics(rows)
        md = render_summary_md(summary)
        self.assertIn("# Decision Outcome Summary", md)
        self.assertIn("Observe-only", md)

    def test_markdown_has_hit_rate(self):
        rows = [
            _make_outcome_row(resolved=True, return_pct=-0.05, direction_correct=True),
            _make_outcome_row(symbol="B", resolved=True, return_pct=0.02, direction_correct=False),
        ]
        summary = aggregate_metrics(rows)
        md = render_summary_md(summary)
        self.assertIn("50%", md)

    def test_markdown_by_decision_table(self):
        rows = [_make_outcome_row(resolved=True, return_pct=-0.04, direction_correct=True)]
        summary = aggregate_metrics(rows)
        md = render_summary_md(summary)
        self.assertIn("SELL", md)

    def test_markdown_no_crash_empty(self):
        md = render_summary_md(aggregate_metrics([]))
        self.assertIn("# Decision Outcome Summary", md)

    def test_insight_high_hit_rate(self):
        rows = [
            _make_outcome_row(symbol=f"S{i}", resolved=True,
                              return_pct=-0.05, direction_correct=True)
            for i in range(7)
        ] + [
            _make_outcome_row(symbol="X", resolved=True, return_pct=0.02, direction_correct=False)
        ]
        summary = aggregate_metrics(rows)
        md = render_summary_md(summary)
        self.assertIn("65", md)  # above 65% threshold mention

    def test_insight_no_data(self):
        md = render_summary_md(aggregate_metrics([]))
        self.assertIn("No resolved", md)


# ---------------------------------------------------------------------------
# TestExtractPriceMap
# ---------------------------------------------------------------------------

class TestExtractPriceMap(unittest.TestCase):
    def test_extracts_price_by_symbol(self):
        signals = {"results": [{"symbol": "QQQ", "price": 450.0}]}
        pm = _extract_price_map(signals)
        self.assertAlmostEqual(450.0, pm["QQQ"])

    def test_normalizes_symbol_to_uppercase(self):
        signals = {"results": [{"symbol": "qqq", "price": 450.0}]}
        pm = _extract_price_map(signals)
        self.assertIn("QQQ", pm)

    def test_skips_zero_price(self):
        signals = {"results": [{"symbol": "QQQ", "price": 0}]}
        pm = _extract_price_map(signals)
        self.assertNotIn("QQQ", pm)

    def test_empty_signals_returns_empty(self):
        self.assertEqual({}, _extract_price_map({}))

    def test_missing_results_key_returns_empty(self):
        self.assertEqual({}, _extract_price_map({"alerts": []}))


# ---------------------------------------------------------------------------
# TestRunOutcomeTracker (integration)
# ---------------------------------------------------------------------------

class TestRunOutcomeTracker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel: str, payload: dict) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_non_fatal_missing_plan(self):
        try:
            summary, md = run_outcome_tracker(self.root, write_files=False)
        except Exception as exc:
            self.fail(f"run_outcome_tracker raised: {exc}")
        self.assertEqual(0, summary["total_decisions"])

    def test_writes_summary_json_and_md(self):
        plan = _make_plan([_make_decision("QQQ", "SELL")])
        self._write("outputs/latest/decision_plan.json", plan)
        run_outcome_tracker(self.root, write_files=True)
        self.assertTrue((self.root / "outputs" / "policy" / "decision_outcome_summary.json").exists())
        self.assertTrue((self.root / "outputs" / "policy" / "decision_outcome_summary.md").exists())

    def test_snapshot_appended_to_jsonl(self):
        plan = _make_plan([_make_decision("QQQ", "SELL")])
        self._write("outputs/latest/decision_plan.json", plan)
        run_outcome_tracker(self.root, write_files=True)
        jsonl_path = self.root / "outputs" / "policy" / "decision_outcomes.jsonl"
        self.assertTrue(jsonl_path.exists())
        rows = _load_jsonl(jsonl_path)
        self.assertEqual(1, len(rows))

    def test_no_crash_on_malformed_plan(self):
        path = self.root / "outputs" / "latest" / "decision_plan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{bad json", encoding="utf-8")
        try:
            run_outcome_tracker(self.root, write_files=False)
        except Exception as exc:
            self.fail(f"run_outcome_tracker raised: {exc}")

    def test_price_fetcher_used_for_resolution(self):
        # Pre-populate a 3-day-old unresolved row
        old_row = _make_outcome_row(symbol="QQQ", decision="SELL",
                                    days_ago=3, price_at_decision=100.0)
        jsonl_path = self.root / "outputs" / "policy" / "decision_outcomes.jsonl"
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        jsonl_path.write_text(json.dumps(old_row) + "\n", encoding="utf-8")

        plan = _make_plan([])  # no new decisions
        self._write("outputs/latest/decision_plan.json", plan)

        fetcher = lambda symbols: {"QQQ": 95.0}
        run_outcome_tracker(self.root, write_files=True, price_fetcher=fetcher)

        rows = _load_jsonl(jsonl_path)
        resolved = [r for r in rows if r.get("resolved")]
        self.assertEqual(1, len(resolved))
        self.assertAlmostEqual(-0.05, resolved[0]["return_pct"])

    def test_write_false_no_summary_file(self):
        plan = _make_plan([_make_decision()])
        self._write("outputs/latest/decision_plan.json", plan)
        run_outcome_tracker(self.root, write_files=False)
        self.assertFalse((self.root / "outputs" / "policy" / "decision_outcome_summary.json").exists())


# ---------------------------------------------------------------------------
# TestGuiDataLayer
# ---------------------------------------------------------------------------

class TestGuiDataLayer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_summary(self, payload: dict) -> None:
        path = self.root / "outputs" / "policy" / "decision_outcome_summary.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_load_returns_unavailable_when_missing(self):
        from gui_operator_data import load_decision_outcome_summary
        result = load_decision_outcome_summary(self.root)
        self.assertFalse(result["available"])

    def test_load_returns_available_when_file_exists(self):
        from gui_operator_data import load_decision_outcome_summary
        self._write_summary({"total_decisions": 5, "resolved": 3, "hit_rate": 0.67})
        result = load_decision_outcome_summary(self.root)
        self.assertTrue(result["available"])
        self.assertEqual(5, result["total_decisions"])

    def test_load_malformed_json_returns_unavailable(self):
        from gui_operator_data import load_decision_outcome_summary
        path = self.root / "outputs" / "policy" / "decision_outcome_summary.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{bad", encoding="utf-8")
        result = load_decision_outcome_summary(self.root)
        self.assertFalse(result["available"])

    def test_load_in_bundle(self):
        from gui_operator_data import load_operator_dashboard_data
        self._write_summary({"total_decisions": 2, "resolved": 1, "hit_rate": 1.0})
        bundle = load_operator_dashboard_data(self.root)
        self.assertIn("decision_outcome_summary", bundle)
        self.assertTrue(bundle["decision_outcome_summary"]["available"])

    def test_missing_file_does_not_raise(self):
        from gui_operator_data import load_decision_outcome_summary
        try:
            load_decision_outcome_summary(self.root)
        except Exception as exc:
            self.fail(f"load_decision_outcome_summary raised: {exc}")

    def test_empty_fields_present_when_file_missing(self):
        from gui_operator_data import load_decision_outcome_summary
        result = load_decision_outcome_summary(self.root)
        for key in ("total_decisions", "resolved", "by_decision", "last_10_resolved"):
            self.assertIn(key, result)


if __name__ == "__main__":
    unittest.main()
