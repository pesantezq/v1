from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from portfolio_automation.decision_performance_attribution import (
    MIN_RESOLVED_ROWS,
    _enrich_triage,
    _group_stats,
    _best_worst,
    _breakdown,
    build_attribution,
    render_attribution_md,
    run_performance_attribution,
    _load_triage_bucket_map,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OUTCOMES_REL = "outputs/policy/decision_outcomes.jsonl"
_TRIAGE_REL = "outputs/latest/decision_triage.json"
_ATTRIBUTION_JSON_REL = "outputs/policy/decision_performance_attribution.json"


def _row(
    symbol: str = "QLD",
    decision: str = "SELL",
    source: str = "structural",
    validation_status: str = "caution",
    resolved: bool = True,
    direction_correct: bool | None = True,
    return_pct: float | None = -0.05,
    date: str = "2026-04-20",
) -> dict:
    return {
        "symbol": symbol,
        "decision": decision,
        "source": source,
        "validation_status": validation_status,
        "resolved": resolved,
        "direction_correct": direction_correct,
        "return_pct": return_pct,
        "date": date,
        "priority": 0.9,
        "confidence": 0.8,
    }


def _make_resolved_rows(n: int, *, correct_rate: float = 0.6) -> list[dict]:
    rows = []
    for i in range(n):
        correct = i < int(n * correct_rate)
        rows.append(_row(
            symbol=f"SYM{i}",
            direction_correct=correct,
            return_pct=-0.03 if correct else 0.05,
        ))
    return rows


def _write_jsonl(root: Path, rows: list[dict]) -> None:
    path = root / _OUTCOMES_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )


def _write_triage(root: Path, buckets: dict) -> None:
    path = root / _TRIAGE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"buckets": buckets, "generated_at": "2026-04-29T08:00:00"}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# _group_stats
# ---------------------------------------------------------------------------

class TestGroupStats(unittest.TestCase):
    def test_empty_returns_none_rates(self):
        s = _group_stats([])
        self.assertEqual(0, s["total"])
        self.assertEqual(0, s["resolved"])
        self.assertIsNone(s["hit_rate"])
        self.assertIsNone(s["avg_return"])

    def test_unresolved_rows_not_counted(self):
        rows = [_row(resolved=False)]
        s = _group_stats(rows)
        self.assertEqual(1, s["total"])
        self.assertEqual(0, s["resolved"])
        self.assertIsNone(s["hit_rate"])

    def test_hit_rate_computed_correctly(self):
        rows = [
            _row(direction_correct=True, return_pct=-0.05),
            _row(direction_correct=True, return_pct=-0.03),
            _row(direction_correct=False, return_pct=0.04),
        ]
        s = _group_stats(rows)
        self.assertAlmostEqual(2 / 3, s["hit_rate"], places=5)

    def test_avg_return_computed_correctly(self):
        rows = [
            _row(return_pct=-0.10),
            _row(return_pct=0.04),
        ]
        s = _group_stats(rows)
        self.assertAlmostEqual(-0.03, s["avg_return"], places=5)

    def test_neutral_direction_excluded_from_hit_rate(self):
        rows = [
            _row(direction_correct=None, return_pct=0.01),  # HOLD — neutral
            _row(direction_correct=True, return_pct=-0.02),
        ]
        s = _group_stats(rows)
        self.assertEqual(2, s["resolved"])
        self.assertAlmostEqual(1.0, s["hit_rate"], places=5)  # only 1 judgeable, 1 correct


# ---------------------------------------------------------------------------
# _best_worst
# ---------------------------------------------------------------------------

class TestBestWorst(unittest.TestCase):
    def test_empty_returns_none_none(self):
        best, worst = _best_worst([])
        self.assertIsNone(best)
        self.assertIsNone(worst)

    def test_single_row_is_both_best_and_worst(self):
        rows = [_row(symbol="QLD", return_pct=-0.05)]
        best, worst = _best_worst(rows)
        self.assertEqual("QLD", best["symbol"])
        self.assertEqual("QLD", worst["symbol"])

    def test_best_has_highest_return(self):
        rows = [
            _row(symbol="A", return_pct=0.10),
            _row(symbol="B", return_pct=-0.20),
            _row(symbol="C", return_pct=0.05),
        ]
        best, worst = _best_worst(rows)
        self.assertEqual("A", best["symbol"])
        self.assertEqual("B", worst["symbol"])

    def test_row_without_return_excluded(self):
        rows = [
            _row(symbol="A", return_pct=None),
            _row(symbol="B", return_pct=0.03),
        ]
        best, worst = _best_worst(rows)
        self.assertEqual("B", best["symbol"])
        self.assertEqual("B", worst["symbol"])

    def test_summary_fields_present(self):
        rows = [_row(symbol="X", decision="SELL", return_pct=-0.07)]
        best, _ = _best_worst(rows)
        for key in ("symbol", "decision", "date", "return_pct", "direction_correct"):
            self.assertIn(key, best)


# ---------------------------------------------------------------------------
# _breakdown
# ---------------------------------------------------------------------------

class TestBreakdown(unittest.TestCase):
    def test_canonical_keys_always_present(self):
        rows = [_row(decision="BUY")]
        result = _breakdown(rows, "decision", ("BUY", "SELL", "SCALE"))
        self.assertIn("BUY", result)
        self.assertIn("SELL", result)
        self.assertIn("SCALE", result)

    def test_nonzero_group_populated(self):
        rows = [_row(decision="BUY"), _row(decision="BUY")]
        result = _breakdown(rows, "decision", ("BUY", "SELL"))
        self.assertEqual(2, result["BUY"]["total"])
        self.assertEqual(0, result["SELL"]["total"])

    def test_unknown_key_created_for_missing_field(self):
        rows = [{"resolved": True, "direction_correct": True, "return_pct": 0.01}]
        result = _breakdown(rows, "decision", ())
        self.assertIn("unknown", result)


# ---------------------------------------------------------------------------
# _enrich_triage
# ---------------------------------------------------------------------------

class TestEnrichTriage(unittest.TestCase):
    def test_known_symbol_gets_bucket(self):
        bucket_map = {("QLD", "SELL"): "critical_action"}
        rows = [_row(symbol="QLD", decision="SELL")]
        enriched = _enrich_triage(rows, bucket_map)
        self.assertEqual("critical_action", enriched[0]["triage_bucket"])

    def test_unknown_symbol_gets_unknown(self):
        rows = [_row(symbol="ZZZ", decision="BUY")]
        enriched = _enrich_triage(rows, {})
        self.assertEqual("unknown", enriched[0]["triage_bucket"])

    def test_original_row_not_mutated(self):
        original = _row(symbol="QLD", decision="SELL")
        bucket_map = {("QLD", "SELL"): "critical_action"}
        _enrich_triage([original], bucket_map)
        self.assertNotIn("triage_bucket", original)

    def test_case_insensitive_lookup(self):
        bucket_map = {("QLD", "SELL"): "monitor"}
        rows = [_row(symbol="qld", decision="sell")]
        enriched = _enrich_triage(rows, bucket_map)
        self.assertEqual("monitor", enriched[0]["triage_bucket"])


# ---------------------------------------------------------------------------
# _load_triage_bucket_map
# ---------------------------------------------------------------------------

class TestLoadTriageBucketMap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_returns_empty(self):
        result = _load_triage_bucket_map(self.root / "nonexistent.json")
        self.assertEqual({}, result)

    def test_malformed_json_returns_empty(self):
        p = self.root / "triage.json"
        p.write_text("{bad", encoding="utf-8")
        self.assertEqual({}, _load_triage_bucket_map(p))

    def test_buckets_parsed_correctly(self):
        _write_triage(self.root, {
            "critical_action": [{"symbol": "QLD", "decision": "SELL"}],
            "monitor": [{"symbol": "QQQ", "decision": "WAIT"}],
        })
        m = _load_triage_bucket_map(self.root / _TRIAGE_REL)
        self.assertEqual("critical_action", m[("QLD", "SELL")])
        self.assertEqual("monitor", m[("QQQ", "WAIT")])


# ---------------------------------------------------------------------------
# build_attribution (full payload)
# ---------------------------------------------------------------------------

class TestBuildAttribution(unittest.TestCase):
    def _make_mixed(self) -> list[dict]:
        return [
            _row("A", "SELL", "structural", "caution", True, True, -0.08),
            _row("B", "BUY", "market", "aligned", True, True, 0.06),
            _row("C", "WAIT", "portfolio", "neutral", True, False, 0.02),
            _row("D", "SCALE", "structural", "aligned", True, True, -0.01),
        ]

    def test_available_true(self):
        rows = self._make_mixed()
        payload = build_attribution(rows, [r for r in rows if r["resolved"]])
        self.assertTrue(payload["available"])
        self.assertFalse(payload["insufficient_data"])

    def test_overall_total_matches(self):
        rows = self._make_mixed()
        payload = build_attribution(rows, [r for r in rows if r["resolved"]])
        self.assertEqual(4, payload["total_decisions"])

    def test_by_decision_keys_present(self):
        rows = self._make_mixed()
        payload = build_attribution(rows, [r for r in rows if r["resolved"]])
        by_dec = payload["by_decision"]
        self.assertIn("SELL", by_dec)
        self.assertIn("BUY", by_dec)

    def test_by_strategy_uses_source_field(self):
        rows = self._make_mixed()
        payload = build_attribution(rows, [r for r in rows if r["resolved"]])
        self.assertEqual(2, payload["by_strategy"]["structural"]["total"])
        self.assertEqual(1, payload["by_strategy"]["market"]["total"])

    def test_by_validation_status_populated(self):
        rows = self._make_mixed()
        payload = build_attribution(rows, [r for r in rows if r["resolved"]])
        self.assertIn("caution", payload["by_validation_status"])
        self.assertIn("aligned", payload["by_validation_status"])

    def test_best_worst_present(self):
        rows = self._make_mixed()
        payload = build_attribution(rows, [r for r in rows if r["resolved"]])
        self.assertIsNotNone(payload["best_decision"])
        self.assertIsNotNone(payload["worst_decision"])

    def test_best_has_highest_return(self):
        rows = self._make_mixed()
        payload = build_attribution(rows, [r for r in rows if r["resolved"]])
        self.assertEqual(0.06, payload["best_decision"]["return_pct"])

    def test_worst_has_lowest_return(self):
        rows = self._make_mixed()
        payload = build_attribution(rows, [r for r in rows if r["resolved"]])
        self.assertEqual(-0.08, payload["worst_decision"]["return_pct"])

    def test_observe_only_always_true(self):
        rows = self._make_mixed()
        payload = build_attribution(rows, [r for r in rows if r["resolved"]])
        self.assertTrue(payload["observe_only"])


# ---------------------------------------------------------------------------
# run_performance_attribution — sparse / empty / enough data
# ---------------------------------------------------------------------------

class TestRunPerformanceAttribution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # empty history
    # ------------------------------------------------------------------

    def test_empty_history_produces_unavailable(self):
        _write_jsonl(self.root, [])
        payload, _ = run_performance_attribution(self.root, write_files=False)
        self.assertFalse(payload["available"])
        self.assertTrue(payload["insufficient_data"])

    def test_empty_history_does_not_raise(self):
        try:
            run_performance_attribution(self.root, write_files=False)
        except Exception as exc:
            self.fail(f"Raised unexpectedly: {exc}")

    def test_missing_jsonl_produces_unavailable(self):
        payload, _ = run_performance_attribution(self.root, write_files=False)
        self.assertFalse(payload["available"])

    # ------------------------------------------------------------------
    # sparse history (resolved < MIN_RESOLVED_ROWS)
    # ------------------------------------------------------------------

    def test_sparse_history_unavailable(self):
        rows = _make_resolved_rows(5)
        _write_jsonl(self.root, rows)
        payload, _ = run_performance_attribution(self.root, write_files=False, min_resolved=20)
        self.assertFalse(payload["available"])
        self.assertTrue(payload["insufficient_data"])
        self.assertEqual(5, payload["resolved_decisions"])

    def test_sparse_summary_line_mentions_count(self):
        rows = _make_resolved_rows(3)
        _write_jsonl(self.root, rows)
        payload, _ = run_performance_attribution(self.root, write_files=False, min_resolved=20)
        self.assertIn("3", payload["summary_line"])

    def test_sparse_still_writes_artifacts(self):
        rows = _make_resolved_rows(5)
        _write_jsonl(self.root, rows)
        run_performance_attribution(self.root, write_files=True, min_resolved=20)
        json_path = self.root / _ATTRIBUTION_JSON_REL
        self.assertTrue(json_path.exists())
        artifact = json.loads(json_path.read_text())
        self.assertFalse(artifact["available"])

    # ------------------------------------------------------------------
    # enough history
    # ------------------------------------------------------------------

    def test_enough_history_available_true(self):
        rows = _make_resolved_rows(MIN_RESOLVED_ROWS)
        _write_jsonl(self.root, rows)
        payload, _ = run_performance_attribution(self.root, write_files=False)
        self.assertTrue(payload["available"])
        self.assertFalse(payload["insufficient_data"])

    def test_enough_history_writes_json_and_md(self):
        rows = _make_resolved_rows(MIN_RESOLVED_ROWS)
        _write_jsonl(self.root, rows)
        run_performance_attribution(self.root, write_files=True)
        self.assertTrue((self.root / _ATTRIBUTION_JSON_REL).exists())
        md_path = self.root / "outputs" / "policy" / "decision_performance_attribution.md"
        self.assertTrue(md_path.exists())

    def test_enough_history_hit_rate_computed(self):
        rows = _make_resolved_rows(MIN_RESOLVED_ROWS, correct_rate=0.7)
        _write_jsonl(self.root, rows)
        payload, _ = run_performance_attribution(self.root, write_files=False)
        self.assertIsNotNone(payload["hit_rate"])
        self.assertAlmostEqual(0.7, payload["hit_rate"], places=1)

    # ------------------------------------------------------------------
    # breakdown math
    # ------------------------------------------------------------------

    def test_breakdown_by_decision_sums_to_total(self):
        rows = _make_resolved_rows(MIN_RESOLVED_ROWS)
        _write_jsonl(self.root, rows)
        payload, _ = run_performance_attribution(self.root, write_files=False)
        by_dec = payload["by_decision"]
        total_from_groups = sum(v["total"] for v in by_dec.values())
        self.assertEqual(payload["total_decisions"], total_from_groups)

    def test_breakdown_by_strategy_sums_to_total(self):
        rows = _make_resolved_rows(MIN_RESOLVED_ROWS)
        _write_jsonl(self.root, rows)
        payload, _ = run_performance_attribution(self.root, write_files=False)
        by_strat = payload["by_strategy"]
        total_from_groups = sum(v["total"] for v in by_strat.values())
        self.assertEqual(payload["total_decisions"], total_from_groups)

    def test_breakdown_by_validation_status_sums_to_total(self):
        rows = _make_resolved_rows(MIN_RESOLVED_ROWS)
        _write_jsonl(self.root, rows)
        payload, _ = run_performance_attribution(self.root, write_files=False)
        by_val = payload["by_validation_status"]
        total_from_groups = sum(v["total"] for v in by_val.values())
        self.assertEqual(payload["total_decisions"], total_from_groups)

    # ------------------------------------------------------------------
    # best / worst sorting
    # ------------------------------------------------------------------

    def test_best_decision_is_highest_return(self):
        base = _make_resolved_rows(MIN_RESOLVED_ROWS)
        base.append(_row("STAR", "BUY", return_pct=0.99))
        _write_jsonl(self.root, base)
        payload, _ = run_performance_attribution(self.root, write_files=False)
        self.assertEqual("STAR", payload["best_decision"]["symbol"])

    def test_worst_decision_is_lowest_return(self):
        base = _make_resolved_rows(MIN_RESOLVED_ROWS)
        base.append(_row("DOG", "SELL", return_pct=-0.99))
        _write_jsonl(self.root, base)
        payload, _ = run_performance_attribution(self.root, write_files=False)
        self.assertEqual("DOG", payload["worst_decision"]["symbol"])

    # ------------------------------------------------------------------
    # missing optional fields (triage absent)
    # ------------------------------------------------------------------

    def test_missing_triage_file_does_not_fail(self):
        rows = _make_resolved_rows(MIN_RESOLVED_ROWS)
        _write_jsonl(self.root, rows)
        try:
            payload, _ = run_performance_attribution(self.root, write_files=False)
        except Exception as exc:
            self.fail(f"Raised unexpectedly: {exc}")
        self.assertTrue(payload["available"])

    def test_triage_enrichment_applies_when_file_present(self):
        rows = _make_resolved_rows(MIN_RESOLVED_ROWS)
        rows[0]["symbol"] = "QLD"
        rows[0]["decision"] = "SELL"
        _write_jsonl(self.root, rows)
        _write_triage(self.root, {
            "critical_action": [{"symbol": "QLD", "decision": "SELL"}],
        })
        payload, _ = run_performance_attribution(self.root, write_files=False)
        by_triage = payload["by_triage_bucket"]
        self.assertGreater(by_triage.get("critical_action", {}).get("total", 0), 0)

    # ------------------------------------------------------------------
    # non-fatal pipeline behavior
    # ------------------------------------------------------------------

    def test_write_files_false_produces_no_files(self):
        rows = _make_resolved_rows(MIN_RESOLVED_ROWS)
        _write_jsonl(self.root, rows)
        run_performance_attribution(self.root, write_files=False)
        self.assertFalse((self.root / _ATTRIBUTION_JSON_REL).exists())

    def test_returns_tuple_always(self):
        result = run_performance_attribution(self.root, write_files=False)
        self.assertIsInstance(result, tuple)
        self.assertEqual(2, len(result))
        payload, md = result
        self.assertIsInstance(payload, dict)
        self.assertIsInstance(md, str)


# ---------------------------------------------------------------------------
# render_attribution_md
# ---------------------------------------------------------------------------

class TestRenderAttributionMd(unittest.TestCase):
    def test_insufficient_data_banner_present(self):
        payload = {
            "available": False,
            "insufficient_data": True,
            "total_decisions": 3,
            "resolved_decisions": 3,
            "min_required": 20,
            "hit_rate": None,
            "avg_return": None,
            "generated_at": "2026-04-29T00:00:00",
        }
        md = render_attribution_md(payload)
        self.assertIn("Insufficient data", md)
        self.assertIn("3", md)

    def test_full_render_includes_breakdown_titles(self):
        payload = {
            "available": True,
            "insufficient_data": False,
            "total_decisions": 20,
            "resolved_decisions": 20,
            "hit_rate": 0.65,
            "avg_return": 0.02,
            "generated_at": "2026-04-29T00:00:00",
            "by_decision": {"SELL": {"total": 10, "resolved": 10, "hit_rate": 0.6, "avg_return": -0.03}},
            "by_strategy": {"structural": {"total": 10, "resolved": 10, "hit_rate": 0.6, "avg_return": -0.03}},
            "by_validation_status": {"caution": {"total": 10, "resolved": 10, "hit_rate": 0.6, "avg_return": -0.03}},
            "by_triage_bucket": {"critical_action": {"total": 5, "resolved": 5, "hit_rate": 0.8, "avg_return": -0.05}},
            "best_decision": None,
            "worst_decision": None,
        }
        md = render_attribution_md(payload)
        self.assertIn("By Decision Type", md)
        self.assertIn("By Strategy", md)
        self.assertIn("By Validation Status", md)
        self.assertIn("By Triage Bucket", md)

    def test_observe_only_footer_present(self):
        payload = {
            "available": False,
            "insufficient_data": True,
            "total_decisions": 0,
            "resolved_decisions": 0,
            "hit_rate": None,
            "avg_return": None,
            "generated_at": "2026-04-29T00:00:00",
            "min_required": 20,
        }
        md = render_attribution_md(payload)
        self.assertIn("Observe-only", md)


# ---------------------------------------------------------------------------
# gui_operator_data loader
# ---------------------------------------------------------------------------

class TestLoadDecisionPerformanceAttribution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_attribution(self, payload: dict) -> None:
        path = self.root / _ATTRIBUTION_JSON_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_missing_file_returns_unavailable(self):
        from gui_operator_data import load_decision_performance_attribution
        result = load_decision_performance_attribution(self.root)
        self.assertFalse(result["available"])
        self.assertIn("No performance attribution", result["summary_line"])

    def test_malformed_json_returns_unavailable(self):
        from gui_operator_data import load_decision_performance_attribution
        path = self.root / _ATTRIBUTION_JSON_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{bad json", encoding="utf-8")
        result = load_decision_performance_attribution(self.root)
        self.assertFalse(result["available"])

    def test_valid_payload_passed_through(self):
        from gui_operator_data import load_decision_performance_attribution
        self._write_attribution({"available": True, "resolved_decisions": 25, "hit_rate": 0.64})
        result = load_decision_performance_attribution(self.root)
        self.assertTrue(result["available"])
        self.assertEqual(25, result["resolved_decisions"])

    def test_in_bundle(self):
        from gui_operator_data import load_operator_dashboard_data
        self._write_attribution({"available": True, "resolved_decisions": 25})
        bundle = load_operator_dashboard_data(self.root)
        self.assertIn("decision_performance_attribution", bundle)


if __name__ == "__main__":
    unittest.main()
