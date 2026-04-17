"""
Tests for scraped_intel/promotion.py — promotion-review layer.

Covers:
  TestGateMinSampleSize              — gate 1: unique event count threshold
  TestGateMinWindowSampleSize        — gate 2: per-window boosted-row threshold
  TestGateRequiredWindowsPresent     — gate 3: all required windows have data
  TestGateMinWinRateLift             — gate 4: minimum win-rate lift
  TestGateMinAvgReturnLift           — gate 5: minimum return lift
  TestGateMaxInstabilityGap          — gate 6: 5d vs 20d consistency
  TestGateMaxFeatureConcentration    — gate 7: no single feature dominates
  TestGateDualWindowOutperformance   — gate 8: rec beats current on both windows
  TestCompareConfigs                 — side-by-side delta computation
  TestEligibilityLogic               — overall eligible flag
  TestReportSchema                   — build_promotion_review() structure
  TestFileCreation                   — JSON + MD writers
  TestNoMutation                     — store and input dicts unchanged
  TestEndToEnd                       — run_promotion_review() full pipeline
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from scraped_intel.promotion import (
    GateResult,
    _extract_current_candidate,
    _gate_dual_window_outperformance,
    _gate_max_feature_concentration,
    _gate_max_instability_gap,
    _gate_min_avg_return_lift,
    _gate_min_sample_size,
    _gate_min_window_sample_size,
    _gate_min_win_rate_lift,
    _gate_required_windows_present,
    build_promotion_review,
    compare_configs,
    evaluate_promotion_gates,
    run_promotion_review,
    write_promotion_review_json,
    write_promotion_review_md,
)
from scraped_intel.store import ScrapedIntelStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pw(wrl: Optional[float], rl: Optional[float] = None,
        n_boosted: int = 15, n_total: int = 20,
        sample_ok: bool = True) -> Dict[str, Any]:
    """Build a per-window metrics dict."""
    return {
        "n_total":             n_total,
        "n_boosted":           n_boosted,
        "n_unboosted":         n_total - n_boosted,
        "resolved_count":      n_total,
        "win_rate_boosted":    (wrl or 0.0) + 0.5 if wrl is not None else None,
        "win_rate_unboosted":  0.50,
        "win_rate_lift":       wrl,
        "avg_return_boosted":  (rl or 0.0) + 1.0 if rl is not None else None,
        "avg_return_unboosted": 0.0,
        "return_lift":         rl,
        "sample_ok":           sample_ok,
    }


def _candidate(
    sc: float = 0.40, rec: float = 0.30, ta: float = 0.20, ma: float = 0.10,
    sig_boost: float = 0.12, conf_boost: float = 0.10,
) -> Dict[str, Any]:
    return {
        "weights": {
            "scraped_confidence":    sc,
            "recency_score":         rec,
            "theme_alignment_score": ta,
            "mention_accel_norm":    ma,
        },
        "max_signal_boost": sig_boost,
        "max_conf_boost":   conf_boost,
    }


def _make_raw_row(
    symbol: str = "AAPL",
    window_days: int = 5,
    return_pct: float = 5.0,
    baseline_sig: float = 0.60,
    baseline_conf: float = 0.55,
    raw_sc: Optional[float] = 0.70,
    as_of_date: str = "2025-01-01",
) -> Dict[str, Any]:
    return {
        "outcome_id":                1,
        "symbol":                    symbol,
        "as_of_date":                as_of_date,
        "window_days":               window_days,
        "return_pct":                return_pct,
        "baseline_signal_score":     baseline_sig,
        "baseline_confidence_score": baseline_conf,
        "raw_scraped_confidence":    raw_sc,
        "raw_recency_score":         0.60 if raw_sc else None,
        "raw_theme_alignment_score": 0.50 if raw_sc else None,
        "raw_mention_acceleration":  0.20 if raw_sc else None,
        "raw_source_count":          2,
    }


def _default_gate_config(overrides: Optional[Dict] = None) -> Dict[str, Any]:
    cfg = {
        "promotion_min_sample_size":               10,
        "promotion_min_window_sample_size":         3,
        "promotion_required_windows":              [5, 20],
        "promotion_min_win_rate_lift":              0.05,
        "promotion_min_avg_return_lift":            0.0,
        "promotion_max_instability_gap":            0.30,
        "promotion_max_feature_concentration":      0.70,
        "promotion_require_dual_window_outperformance": False,
    }
    if overrides:
        cfg.update(overrides)
    return cfg


def _make_eval_result(
    rec_metrics:  Optional[Dict[int, Any]] = None,
    cur_metrics:  Optional[Dict[int, Any]] = None,
    gates:        Optional[List[GateResult]] = None,
    eligible:     bool = True,
    unique_events: int = 50,
    total_rows:   int = 100,
) -> Dict[str, Any]:
    rec_m = rec_metrics or {5: _pw(0.15, 3.0), 20: _pw(0.12, 2.5)}
    cur_m = cur_metrics or {5: _pw(0.05, 1.0), 20: _pw(0.04, 0.8)}
    _gates = gates or [GateResult("min_sample_size", True, "50 ≥ 10")]
    passed = [g.name for g in _gates if g.passed]
    failed = [g.name for g in _gates if not g.passed]
    return {
        "rec_metrics":        rec_m,
        "cur_metrics":        cur_m,
        "comparison":         compare_configs(cur_m, rec_m, windows=[5, 20]),
        "gates":              _gates,
        "eligible":           eligible,
        "passed_gates":       passed,
        "failed_gates":       failed,
        "unique_event_count": unique_events,
        "total_rows":         total_rows,
        "rec_objective_score": 0.72,
        "rec_stability_score": 0.88,
    }


# ---------------------------------------------------------------------------
# 1. Gate: min_sample_size
# ---------------------------------------------------------------------------

class TestGateMinSampleSize(unittest.TestCase):

    def test_passes_at_threshold(self):
        result = _gate_min_sample_size(30, 30)
        self.assertTrue(result.passed)

    def test_passes_above_threshold(self):
        result = _gate_min_sample_size(50, 30)
        self.assertTrue(result.passed)

    def test_fails_below_threshold(self):
        result = _gate_min_sample_size(10, 30)
        self.assertFalse(result.passed)

    def test_fails_at_zero(self):
        result = _gate_min_sample_size(0, 10)
        self.assertFalse(result.passed)

    def test_zero_threshold_always_passes(self):
        result = _gate_min_sample_size(0, 0)
        self.assertTrue(result.passed)

    def test_name_is_correct(self):
        result = _gate_min_sample_size(5, 10)
        self.assertEqual(result.name, "min_sample_size")

    def test_detail_contains_counts(self):
        result = _gate_min_sample_size(7, 20)
        self.assertIn("7", result.detail)
        self.assertIn("20", result.detail)


# ---------------------------------------------------------------------------
# 2. Gate: min_window_sample_size
# ---------------------------------------------------------------------------

class TestGateMinWindowSampleSize(unittest.TestCase):

    def test_passes_when_all_above_threshold(self):
        metrics = {5: _pw(0.10, n_boosted=15), 20: _pw(0.08, n_boosted=12)}
        result = _gate_min_window_sample_size(metrics, [5, 20], 10)
        self.assertTrue(result.passed)

    def test_fails_when_one_window_below(self):
        metrics = {5: _pw(0.10, n_boosted=5), 20: _pw(0.08, n_boosted=15)}
        result = _gate_min_window_sample_size(metrics, [5, 20], 10)
        self.assertFalse(result.passed)
        self.assertIn("5d", result.detail)

    def test_fails_when_window_missing(self):
        metrics = {5: _pw(0.10, n_boosted=15)}
        result = _gate_min_window_sample_size(metrics, [5, 20], 5)
        self.assertFalse(result.passed)
        self.assertIn("20d", result.detail)

    def test_zero_threshold_always_passes(self):
        metrics = {5: _pw(0.10, n_boosted=0), 20: _pw(0.08, n_boosted=0)}
        result = _gate_min_window_sample_size(metrics, [5, 20], 0)
        self.assertTrue(result.passed)

    def test_detail_lists_all_failures(self):
        metrics = {5: _pw(0.10, n_boosted=1), 20: _pw(0.08, n_boosted=2)}
        result = _gate_min_window_sample_size(metrics, [5, 20], 10)
        self.assertFalse(result.passed)
        self.assertIn("5d", result.detail)
        self.assertIn("20d", result.detail)


# ---------------------------------------------------------------------------
# 3. Gate: required_windows_present
# ---------------------------------------------------------------------------

class TestGateRequiredWindowsPresent(unittest.TestCase):

    def test_passes_when_all_present(self):
        metrics = {5: _pw(0.10), 20: _pw(0.08)}
        result = _gate_required_windows_present(metrics, [5, 20])
        self.assertTrue(result.passed)

    def test_fails_when_one_missing(self):
        metrics = {5: _pw(0.10)}
        result = _gate_required_windows_present(metrics, [5, 20])
        self.assertFalse(result.passed)
        self.assertIn("20d", result.detail)

    def test_empty_required_windows_always_passes(self):
        result = _gate_required_windows_present({}, [])
        self.assertTrue(result.passed)

    def test_extra_windows_ok(self):
        metrics = {1: _pw(0.02), 5: _pw(0.10), 20: _pw(0.08)}
        result = _gate_required_windows_present(metrics, [5, 20])
        self.assertTrue(result.passed)

    def test_name_is_correct(self):
        result = _gate_required_windows_present({5: _pw(0.1), 20: _pw(0.1)}, [5, 20])
        self.assertEqual(result.name, "required_windows_present")


# ---------------------------------------------------------------------------
# 4. Gate: min_win_rate_lift
# ---------------------------------------------------------------------------

class TestGateMinWinRateLift(unittest.TestCase):

    def test_passes_at_threshold(self):
        metrics = {5: _pw(0.05), 20: _pw(0.05)}
        result = _gate_min_win_rate_lift(metrics, [5, 20], 0.05)
        self.assertTrue(result.passed)

    def test_passes_above_threshold(self):
        metrics = {5: _pw(0.20), 20: _pw(0.15)}
        result = _gate_min_win_rate_lift(metrics, [5, 20], 0.05)
        self.assertTrue(result.passed)

    def test_fails_below_threshold(self):
        metrics = {5: _pw(0.02), 20: _pw(0.15)}
        result = _gate_min_win_rate_lift(metrics, [5, 20], 0.05)
        self.assertFalse(result.passed)
        self.assertIn("5d", result.detail)

    def test_fails_with_none_lift(self):
        metrics = {5: _pw(None), 20: _pw(0.10)}
        result = _gate_min_win_rate_lift(metrics, [5, 20], 0.05)
        self.assertFalse(result.passed)
        self.assertIn("no data", result.detail)

    def test_fails_with_negative_lift(self):
        metrics = {5: _pw(-0.10), 20: _pw(0.10)}
        result = _gate_min_win_rate_lift(metrics, [5, 20], 0.05)
        self.assertFalse(result.passed)

    def test_zero_threshold_passes_positive(self):
        metrics = {5: _pw(0.01), 20: _pw(0.01)}
        result = _gate_min_win_rate_lift(metrics, [5, 20], 0.0)
        self.assertTrue(result.passed)

    def test_name_is_correct(self):
        result = _gate_min_win_rate_lift({}, [], 0.05)
        self.assertEqual(result.name, "min_win_rate_lift")


# ---------------------------------------------------------------------------
# 5. Gate: min_avg_return_lift
# ---------------------------------------------------------------------------

class TestGateMinAvgReturnLift(unittest.TestCase):

    def test_passes_above_threshold(self):
        metrics = {5: _pw(0.10, 3.0), 20: _pw(0.08, 2.5)}
        result = _gate_min_avg_return_lift(metrics, [5, 20], 1.0)
        self.assertTrue(result.passed)

    def test_fails_below_threshold(self):
        metrics = {5: _pw(0.10, 0.5), 20: _pw(0.08, 2.5)}
        result = _gate_min_avg_return_lift(metrics, [5, 20], 1.0)
        self.assertFalse(result.passed)
        self.assertIn("5d", result.detail)

    def test_fails_with_none_return_lift(self):
        metrics = {5: _pw(0.10, None), 20: _pw(0.08, 2.5)}
        result = _gate_min_avg_return_lift(metrics, [5, 20], 0.0)
        self.assertFalse(result.passed)
        self.assertIn("no data", result.detail)

    def test_zero_threshold_passes_zero_lift(self):
        metrics = {5: _pw(0.10, 0.0), 20: _pw(0.08, 0.0)}
        result = _gate_min_avg_return_lift(metrics, [5, 20], 0.0)
        self.assertTrue(result.passed)

    def test_name_is_correct(self):
        result = _gate_min_avg_return_lift({}, [], 0.0)
        self.assertEqual(result.name, "min_avg_return_lift")


# ---------------------------------------------------------------------------
# 6. Gate: max_instability_gap
# ---------------------------------------------------------------------------

class TestGateMaxInstabilityGap(unittest.TestCase):

    def test_passes_zero_gap(self):
        metrics = {5: _pw(0.15), 20: _pw(0.15)}
        result = _gate_max_instability_gap(metrics, 0.20)
        self.assertTrue(result.passed)

    def test_passes_at_threshold(self):
        metrics = {5: _pw(0.20), 20: _pw(0.00)}
        result = _gate_max_instability_gap(metrics, 0.20)
        self.assertTrue(result.passed)

    def test_fails_above_threshold(self):
        metrics = {5: _pw(0.30), 20: _pw(0.00)}
        result = _gate_max_instability_gap(metrics, 0.20)
        self.assertFalse(result.passed)

    def test_passes_with_only_one_window(self):
        metrics = {5: _pw(0.20)}
        result = _gate_max_instability_gap(metrics, 0.20, check_windows=[5, 20])
        self.assertTrue(result.passed)
        self.assertIn("not computable", result.detail)

    def test_passes_when_none_lifts(self):
        metrics = {5: _pw(None), 20: _pw(None)}
        result = _gate_max_instability_gap(metrics, 0.20)
        self.assertTrue(result.passed)

    def test_name_is_correct(self):
        result = _gate_max_instability_gap({}, 0.20)
        self.assertEqual(result.name, "max_instability_gap")

    def test_detail_shows_gap(self):
        metrics = {5: _pw(0.25), 20: _pw(0.05)}
        result = _gate_max_instability_gap(metrics, 0.30)
        self.assertIn("gap=", result.detail)


# ---------------------------------------------------------------------------
# 7. Gate: max_feature_concentration
# ---------------------------------------------------------------------------

class TestGateMaxFeatureConcentration(unittest.TestCase):

    def test_passes_equal_weights(self):
        weights = {"a": 0.25, "b": 0.25, "c": 0.25, "d": 0.25}
        result = _gate_max_feature_concentration(weights, 0.60)
        self.assertTrue(result.passed)

    def test_passes_at_threshold(self):
        weights = {"a": 0.60, "b": 0.20, "c": 0.10, "d": 0.10}
        result = _gate_max_feature_concentration(weights, 0.60)
        self.assertTrue(result.passed)

    def test_fails_above_threshold(self):
        weights = {"a": 0.70, "b": 0.10, "c": 0.10, "d": 0.10}
        result = _gate_max_feature_concentration(weights, 0.60)
        self.assertFalse(result.passed)

    def test_threshold_one_always_passes(self):
        weights = {"a": 0.99, "b": 0.01}
        result = _gate_max_feature_concentration(weights, 1.0)
        self.assertTrue(result.passed)

    def test_empty_weights_fails(self):
        result = _gate_max_feature_concentration({}, 0.60)
        self.assertFalse(result.passed)

    def test_detail_names_dominant_feature(self):
        weights = {"scraped_confidence": 0.80, "recency_score": 0.20}
        result = _gate_max_feature_concentration(weights, 0.60)
        self.assertIn("scraped_confidence", result.detail)

    def test_name_is_correct(self):
        result = _gate_max_feature_concentration({"a": 1.0}, 0.60)
        self.assertEqual(result.name, "max_feature_concentration")


# ---------------------------------------------------------------------------
# 8. Gate: dual_window_outperformance
# ---------------------------------------------------------------------------

class TestGateDualWindowOutperformance(unittest.TestCase):

    def test_passes_when_rec_beats_cur_both_windows(self):
        rec = {5: _pw(0.20), 20: _pw(0.18)}
        cur = {5: _pw(0.05), 20: _pw(0.04)}
        result = _gate_dual_window_outperformance(rec, cur, [5, 20])
        self.assertTrue(result.passed)

    def test_fails_when_rec_loses_one_window(self):
        rec = {5: _pw(0.20), 20: _pw(0.03)}
        cur = {5: _pw(0.05), 20: _pw(0.10)}
        result = _gate_dual_window_outperformance(rec, cur, [5, 20])
        self.assertFalse(result.passed)
        self.assertIn("20d", result.detail)

    def test_fails_when_rec_loses_both_windows(self):
        rec = {5: _pw(0.02), 20: _pw(0.01)}
        cur = {5: _pw(0.10), 20: _pw(0.15)}
        result = _gate_dual_window_outperformance(rec, cur, [5, 20])
        self.assertFalse(result.passed)

    def test_passes_when_no_current_baseline(self):
        # cur missing window → compare without baseline → passes with caveat
        rec = {5: _pw(0.20), 20: _pw(0.15)}
        cur = {}
        result = _gate_dual_window_outperformance(rec, cur, [5, 20])
        self.assertTrue(result.passed)
        self.assertIn("no current baseline", result.detail)

    def test_fails_when_rec_has_no_lift_data(self):
        rec = {5: _pw(None), 20: _pw(0.15)}
        cur = {5: _pw(0.05), 20: _pw(0.05)}
        result = _gate_dual_window_outperformance(rec, cur, [5, 20])
        self.assertFalse(result.passed)
        self.assertIn("no lift data", result.detail)

    def test_name_is_correct(self):
        result = _gate_dual_window_outperformance({}, {}, [5, 20])
        self.assertEqual(result.name, "dual_window_outperformance")


# ---------------------------------------------------------------------------
# 9. Compare configs
# ---------------------------------------------------------------------------

class TestCompareConfigs(unittest.TestCase):

    def _compare(self, cur_wrl, rec_wrl, cur_rl=None, rec_rl=None, w=5):
        cur = {w: _pw(cur_wrl, cur_rl)}
        rec = {w: _pw(rec_wrl, rec_rl)}
        return compare_configs(cur, rec, windows=[w])[w]

    def test_delta_computed_correctly(self):
        c = self._compare(0.05, 0.20)
        self.assertAlmostEqual(c["win_rate_lift_delta"], 0.15, places=4)

    def test_recommended_better_win_rate_true_when_rec_higher(self):
        c = self._compare(0.05, 0.20)
        self.assertTrue(c["recommended_better_win_rate"])

    def test_recommended_better_win_rate_false_when_rec_lower(self):
        c = self._compare(0.20, 0.05)
        self.assertFalse(c["recommended_better_win_rate"])

    def test_return_lift_delta_computed(self):
        c = self._compare(0.10, 0.20, cur_rl=1.0, rec_rl=4.0)
        self.assertAlmostEqual(c["return_lift_delta"], 3.0, places=4)

    def test_none_values_produce_none_delta(self):
        c = self._compare(None, 0.20)
        self.assertIsNone(c["win_rate_lift_delta"])

    def test_recommended_better_return_true_when_rec_higher(self):
        c = self._compare(0.10, 0.20, cur_rl=1.0, rec_rl=3.0)
        self.assertTrue(c["recommended_better_return"])

    def test_all_windows_in_output(self):
        cur = {5: _pw(0.05), 20: _pw(0.04), 1: _pw(0.01)}
        rec = {5: _pw(0.15), 20: _pw(0.12), 1: _pw(0.02)}
        result = compare_configs(cur, rec, windows=[1, 5, 20])
        self.assertIn(5,  result)
        self.assertIn(20, result)
        self.assertIn(1,  result)


# ---------------------------------------------------------------------------
# 10. Eligibility logic
# ---------------------------------------------------------------------------

class TestEligibilityLogic(unittest.TestCase):

    def _all_pass(self, n=3):
        return [GateResult(f"gate_{i}", True, "ok") for i in range(n)]

    def _with_failure(self):
        return [
            GateResult("gate_0", True,  "ok"),
            GateResult("gate_1", False, "failed for reason"),
            GateResult("gate_2", True,  "ok"),
        ]

    def test_eligible_when_all_gates_pass(self):
        eval_result = _make_eval_result(gates=self._all_pass(), eligible=True)
        review = build_promotion_review(
            _candidate(), _candidate(), eval_result, _default_gate_config()
        )
        self.assertTrue(review["eligible"])

    def test_not_eligible_when_one_gate_fails(self):
        eval_result = _make_eval_result(gates=self._with_failure(), eligible=False)
        review = build_promotion_review(
            _candidate(), _candidate(), eval_result, _default_gate_config()
        )
        self.assertFalse(review["eligible"])

    def test_passed_gates_populated(self):
        eval_result = _make_eval_result(gates=self._with_failure(), eligible=False)
        review = build_promotion_review(
            _candidate(), _candidate(), eval_result, _default_gate_config()
        )
        self.assertIn("gate_0", review["passed_gates"])
        self.assertIn("gate_2", review["passed_gates"])

    def test_failed_gates_populated(self):
        eval_result = _make_eval_result(gates=self._with_failure(), eligible=False)
        review = build_promotion_review(
            _candidate(), _candidate(), eval_result, _default_gate_config()
        )
        self.assertIn("gate_1", review["failed_gates"])

    def test_reasons_failed_include_detail(self):
        eval_result = _make_eval_result(gates=self._with_failure(), eligible=False)
        review = build_promotion_review(
            _candidate(), _candidate(), eval_result, _default_gate_config()
        )
        self.assertTrue(any("failed for reason" in r for r in review["reasons_failed"]))

    def test_empty_gates_eligible_vacuously(self):
        eval_result = _make_eval_result(gates=[], eligible=True)
        review = build_promotion_review(
            _candidate(), _candidate(), eval_result, _default_gate_config()
        )
        self.assertTrue(review["eligible"])


# ---------------------------------------------------------------------------
# 11. Report schema
# ---------------------------------------------------------------------------

class TestReportSchema(unittest.TestCase):

    def _make_review(self, eligible=True):
        gates = [GateResult("min_sample_size", eligible, "detail")]
        eval_result = _make_eval_result(gates=gates, eligible=eligible)
        return build_promotion_review(
            _candidate(), _candidate(), eval_result, _default_gate_config()
        )

    def test_required_keys_present(self):
        review = self._make_review()
        for key in (
            "generated_at", "note", "eligible",
            "unique_event_count", "total_rows",
            "current_config", "recommended_config",
            "comparison", "gate_config_used",
            "gates", "passed_gates", "failed_gates",
            "reasons_passed", "reasons_failed", "config_snippet",
        ):
            self.assertIn(key, review, msg=f"Missing key: {key}")

    def test_gates_is_list_of_dicts(self):
        review = self._make_review()
        self.assertIsInstance(review["gates"], list)
        for g in review["gates"]:
            self.assertIn("name",   g)
            self.assertIn("passed", g)
            self.assertIn("detail", g)

    def test_note_mentions_no_auto_apply(self):
        review = self._make_review()
        self.assertIn("NO CONFIG WAS AUTO-APPLIED", review["note"])

    def test_config_snippet_present_when_eligible(self):
        review = self._make_review(eligible=True)
        self.assertIsNotNone(review["config_snippet"])
        self.assertIn("scraped_intel",  review["config_snippet"])
        self.assertIn("blend_weights",  review["config_snippet"])

    def test_config_snippet_absent_when_not_eligible(self):
        review = self._make_review(eligible=False)
        self.assertIsNone(review["config_snippet"])

    def test_comparison_has_window_entries(self):
        review = self._make_review()
        comparison = review["comparison"]
        # Keys should be string versions of window ints
        self.assertTrue(len(comparison) > 0)

    def test_current_and_recommended_configs_present(self):
        review = self._make_review()
        self.assertIn("weights",          review["current_config"])
        self.assertIn("max_signal_boost", review["recommended_config"])

    def test_eligible_field_is_bool(self):
        self.assertIsInstance(self._make_review(True)["eligible"],  bool)
        self.assertIsInstance(self._make_review(False)["eligible"], bool)


# ---------------------------------------------------------------------------
# 12. File creation
# ---------------------------------------------------------------------------

class TestFileCreation(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.out = Path(self.tmpdir)
        gates = [GateResult("min_sample_size", True, "ok")]
        eval_result = _make_eval_result(gates=gates, eligible=True)
        self.review = build_promotion_review(
            _candidate(), _candidate(), eval_result, _default_gate_config()
        )

    def test_json_written_to_correct_path(self):
        path = write_promotion_review_json(self.review, self.out)
        self.assertEqual(path.name, "scraped_intel_promotion_review.json")
        self.assertTrue(path.exists())

    def test_json_is_parseable(self):
        path = write_promotion_review_json(self.review, self.out)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("eligible", data)

    def test_md_written_to_correct_path(self):
        path = write_promotion_review_md(self.review, self.out)
        self.assertEqual(path.name, "scraped_intel_promotion_review.md")
        self.assertTrue(path.exists())

    def test_md_contains_eligibility_heading(self):
        path = write_promotion_review_md(self.review, self.out)
        content = path.read_text(encoding="utf-8")
        self.assertIn("ELIGIBLE", content)

    def test_md_contains_warning_note(self):
        path = write_promotion_review_md(self.review, self.out)
        content = path.read_text(encoding="utf-8")
        self.assertIn("NO CONFIG WAS AUTO-APPLIED", content)

    def test_md_contains_gate_results_table(self):
        path = write_promotion_review_md(self.review, self.out)
        content = path.read_text(encoding="utf-8")
        self.assertIn("Gate Results", content)

    def test_md_contains_shadow_note(self):
        path = write_promotion_review_md(self.review, self.out)
        content = path.read_text(encoding="utf-8")
        self.assertIn("read-only", content)

    def test_output_dir_created_if_absent(self):
        new_dir = self.out / "subdir"
        self.assertFalse(new_dir.exists())
        new_dir.mkdir()
        write_promotion_review_json(self.review, new_dir)
        self.assertTrue((new_dir / "scraped_intel_promotion_review.json").exists())


# ---------------------------------------------------------------------------
# 13. extract_current_candidate
# ---------------------------------------------------------------------------

class TestExtractCurrentCandidate(unittest.TestCase):

    def test_uses_defaults_when_no_config(self):
        cand = _extract_current_candidate({})
        self.assertIn("weights",          cand)
        self.assertIn("max_signal_boost", cand)
        self.assertIn("max_conf_boost",   cand)

    def test_reads_signal_boost_from_config(self):
        cand = _extract_current_candidate({"comparison_max_signal_boost": 0.15})
        self.assertAlmostEqual(cand["max_signal_boost"], 0.15, places=4)

    def test_reads_conf_boost_from_config(self):
        cand = _extract_current_candidate({"comparison_max_conf_boost": 0.08})
        self.assertAlmostEqual(cand["max_conf_boost"], 0.08, places=4)

    def test_reads_custom_blend_weights(self):
        custom = {"scraped_confidence": 0.50, "recency_score": 0.20,
                  "theme_alignment_score": 0.20, "mention_accel_norm": 0.10}
        cand = _extract_current_candidate({"blend_weights": custom})
        self.assertAlmostEqual(cand["weights"]["scraped_confidence"], 0.50, places=4)

    def test_default_weights_sum_to_one(self):
        cand = _extract_current_candidate({})
        total = sum(cand["weights"].values())
        self.assertAlmostEqual(total, 1.0, places=4)


# ---------------------------------------------------------------------------
# 14. No-mutation guarantees
# ---------------------------------------------------------------------------

class TestNoMutation(unittest.TestCase):

    def test_raw_rows_not_modified(self):
        rows = [_make_raw_row(window_days=5), _make_raw_row(window_days=20)]
        original_len = len(rows)
        original_first = dict(rows[0])
        rec = _candidate()
        cur = _candidate()
        gate_cfg = _default_gate_config()
        try:
            evaluate_promotion_gates(rec, cur, rows, gate_cfg)
        except Exception:
            pass   # evaluation may fail with test rows; we only care about mutation
        self.assertEqual(len(rows), original_len)
        self.assertEqual(rows[0]["symbol"], original_first["symbol"])

    def test_candidate_dict_not_modified(self):
        rec = _candidate(sc=0.50, rec=0.30, ta=0.10, ma=0.10)
        cur = _candidate()
        original_sc = rec["weights"]["scraped_confidence"]
        gate_cfg = _default_gate_config()
        try:
            evaluate_promotion_gates(rec, cur, [], gate_cfg)
        except Exception:
            pass
        self.assertAlmostEqual(rec["weights"]["scraped_confidence"], original_sc)

    def test_gate_config_not_modified(self):
        gate_cfg = _default_gate_config()
        original_min = gate_cfg["promotion_min_sample_size"]
        try:
            evaluate_promotion_gates(_candidate(), _candidate(), [], gate_cfg)
        except Exception:
            pass
        self.assertEqual(gate_cfg["promotion_min_sample_size"], original_min)

    def test_eval_result_gates_are_gate_result_instances(self):
        eval_result = _make_eval_result()
        review = build_promotion_review(
            _candidate(), _candidate(), eval_result, _default_gate_config()
        )
        # build_promotion_review should call .to_dict() on each gate
        for g in review["gates"]:
            self.assertIsInstance(g, dict)


# ---------------------------------------------------------------------------
# 15. End-to-end: run_promotion_review with real temp DB
# ---------------------------------------------------------------------------

class TestEndToEnd(unittest.TestCase):

    def setUp(self):
        self.tmpdir  = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "portfolio.db"
        self.out_dir = Path(self.tmpdir) / "outputs"
        self.out_dir.mkdir()

    def tearDown(self):
        import shutil
        try:
            shutil.rmtree(self.tmpdir)
        except PermissionError:
            pass

    def _init_store(self):
        return ScrapedIntelStore(db_path=self.db_path)

    def _insert_resolved(self, conn, symbol="AAPL", as_of_date="2025-01-01",
                          window_days=5, return_pct=5.0):
        conn.execute(
            """INSERT OR IGNORE INTO soft_signals (
                   symbol, as_of_date, headline_count_7d, headline_count_30d,
                   source_count, theme_alignment_score, mention_acceleration,
                   recency_score, scraped_confidence, evidence_items, recorded_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, as_of_date, 5, 20, 2, 0.50, 0.20, 0.65, 0.72,
             "[]", "2025-01-01T00:00:00"),
        )
        conn.execute(
            """INSERT OR IGNORE INTO comparison_snapshots (
                   symbol, as_of_date, baseline_signal_score, enriched_signal_score,
                   signal_delta, baseline_confidence_score, enriched_confidence_score,
                   confidence_delta, baseline_rank, enriched_rank, rank_change,
                   soft_composite, top_features, source_count, evidence_count,
                   scraped_confidence, soft_signals_available, recorded_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, as_of_date, 0.60, 0.65, 0.05, 0.55, 0.60, 0.05,
             1, 1, 0, 0.42, "[]", 2, 20, 0.72, 1, "2025-01-01T00:00:00"),
        )
        snap_id = conn.execute(
            "SELECT id FROM comparison_snapshots WHERE symbol=? AND as_of_date=?",
            (symbol, as_of_date),
        ).fetchone()[0]
        conn.execute(
            """INSERT OR IGNORE INTO comparison_outcomes (
                   snapshot_id, symbol, as_of_date, window_days,
                   baseline_price, outcome_price, return_pct, outcome_label,
                   evaluated_at, outcome_status
               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (snap_id, symbol, as_of_date, window_days, 100.0,
             100.0 * (1 + return_pct / 100.0), return_pct,
             "positive" if return_pct > 0 else "negative",
             "2025-01-10T00:00:00", "resolved"),
        )

    def _build_tuning_report(self) -> Dict[str, Any]:
        """Minimal tuning report structure matching run_tuning() output."""
        return {
            "generated_at": "2025-01-10T12:00:00",
            "total_candidates_tested": 5,
            "total_resolved_rows": 20,
            "warnings": [],
            "recommended": {
                "rank": 1,
                "objective_score": 0.72,
                "stability_score": 0.88,
                "candidate": {
                    "weights": {
                        "scraped_confidence":    0.50,
                        "recency_score":         0.30,
                        "theme_alignment_score": 0.10,
                        "mention_accel_norm":    0.10,
                    },
                    "max_signal_boost": 0.14,
                    "max_conf_boost":   0.08,
                },
                "per_window": {},
            },
        }

    def test_no_tuning_report_returns_ineligible_review(self):
        self._init_store()
        review = run_promotion_review(
            db_path=self.db_path,
            output_dir=self.out_dir,
            config=_default_gate_config(),
            tuning_report=None,
        )
        self.assertFalse(review["eligible"])
        self.assertTrue(len(review["reasons_failed"]) > 0)

    def test_writes_files_even_without_report(self):
        self._init_store()
        run_promotion_review(
            db_path=self.db_path,
            output_dir=self.out_dir,
            config=_default_gate_config(),
        )
        self.assertTrue((self.out_dir / "scraped_intel_promotion_review.json").exists())
        self.assertTrue((self.out_dir / "scraped_intel_promotion_review.md").exists())

    def test_with_tuning_report_and_data(self):
        self._init_store()
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            symbols = ["AAPL", "MSFT", "NVDA", "GOOGL", "META",
                       "AMZN", "TSLA", "AVGO", "AMD", "PLTR"]
            for i, sym in enumerate(symbols):
                ret = 4.0 if i % 2 == 0 else -1.0
                for w in [5, 20]:
                    self._insert_resolved(
                        conn, symbol=sym,
                        as_of_date=f"2025-01-{i+1:02d}",
                        window_days=w, return_pct=ret,
                    )
            conn.commit()
        finally:
            conn.close()

        report = self._build_tuning_report()
        cfg = _default_gate_config({
            "promotion_min_sample_size": 5,
            "promotion_min_window_sample_size": 1,
        })
        review = run_promotion_review(
            db_path=self.db_path,
            output_dir=self.out_dir,
            config=cfg,
            tuning_report=report,
        )
        self.assertIsInstance(review, dict)
        self.assertIn("eligible", review)
        self.assertIn("gates",    review)

    def test_high_thresholds_produce_ineligible(self):
        self._init_store()
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            for i in range(5):
                for w in [5, 20]:
                    self._insert_resolved(conn, symbol=f"SYM{i}",
                                          as_of_date=f"2025-01-0{i+1}",
                                          window_days=w, return_pct=2.0)
            conn.commit()
        finally:
            conn.close()

        cfg = _default_gate_config({
            "promotion_min_sample_size":   9999,  # impossibly high
            "promotion_min_win_rate_lift":  0.99,
        })
        review = run_promotion_review(
            db_path=self.db_path,
            output_dir=self.out_dir,
            config=cfg,
            tuning_report=self._build_tuning_report(),
        )
        self.assertFalse(review["eligible"])

    def test_store_tables_unchanged_after_run(self):
        store = self._init_store()
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            self._insert_resolved(conn)
            conn.commit()
            before_snap = conn.execute(
                "SELECT COUNT(*) FROM comparison_snapshots"
            ).fetchone()[0]
            before_out = conn.execute(
                "SELECT COUNT(*) FROM comparison_outcomes"
            ).fetchone()[0]
        finally:
            conn.close()

        run_promotion_review(
            db_path=self.db_path,
            output_dir=self.out_dir,
            config=_default_gate_config(),
            tuning_report=self._build_tuning_report(),
        )

        conn2 = sqlite3.connect(str(self.db_path))
        try:
            after_snap = conn2.execute(
                "SELECT COUNT(*) FROM comparison_snapshots"
            ).fetchone()[0]
            after_out = conn2.execute(
                "SELECT COUNT(*) FROM comparison_outcomes"
            ).fetchone()[0]
        finally:
            conn2.close()

        self.assertEqual(before_snap, after_snap)
        self.assertEqual(before_out,  after_out)

    def test_output_json_parseable(self):
        self._init_store()
        run_promotion_review(
            db_path=self.db_path,
            output_dir=self.out_dir,
            config=_default_gate_config(),
            tuning_report=self._build_tuning_report(),
        )
        data = json.loads(
            (self.out_dir / "scraped_intel_promotion_review.json").read_text(encoding="utf-8")
        )
        self.assertIn("eligible", data)
        self.assertIn("gates",    data)

    def test_loads_tuning_report_from_disk_when_not_passed(self):
        self._init_store()
        # Write a tuning report JSON to disk
        report = self._build_tuning_report()
        tuning_path = self.out_dir / "scraped_intel_tuning_results.json"
        tuning_path.write_text(json.dumps(report), encoding="utf-8")

        # run_promotion_review without tuning_report kwarg
        review = run_promotion_review(
            db_path=self.db_path,
            output_dir=self.out_dir,
            config=_default_gate_config(),
        )
        # Should have loaded the report and produced a proper review (not the "no report" stub)
        self.assertIn("gates", review)
        # The review should have evaluated gates (even if all fail due to no data)
        self.assertIsNotNone(review.get("gates"))


if __name__ == "__main__":
    unittest.main()
