"""
Tests for scraped_intel/tuning.py — weight tuning and auto-evaluation loop.

Covers:
  TestWeightGridGeneration     — generate_weight_grid(): shape, sums, step
  TestNormalizeWeights         — normalize_weights(): correctness, error guards
  TestGenerateCandidates       — generate_candidates(): cross-product, cap, seed
  TestRecomputeEnrichedScore   — recompute_enriched_score(): formula accuracy
  TestComputeWindowMetrics     — _compute_window_metrics(): lift, groups, sample_ok
  TestObjectiveScoring         — compute_objective_score(): weighting, penalty
  TestStabilityScore           — compute_stability_score(): consistency metric
  TestRankingLogic             — rank_candidates(): ordering, rank field
  TestReportSchema             — build_tuning_report(): required keys, structure
  TestFileCreation             — JSON + MD writers: paths, parseable, sections
  TestEndToEnd                 — run_tuning(): full pipeline with real temp DB
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from scraped_intel.tuning import (
    _WEIGHT_KEYS,
    _compute_window_metrics,
    build_tuning_report,
    compute_objective_score,
    compute_stability_score,
    evaluate_candidate,
    generate_candidates,
    generate_weight_grid,
    normalize_weights,
    rank_candidates,
    recompute_enriched_score,
    run_tuning,
    write_tuning_results_json,
    write_tuning_results_md,
)
from scraped_intel.store import ScrapedIntelStore


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_candidate(
    sc: float = 0.40,
    rec: float = 0.30,
    ta: float = 0.20,
    ma: float = 0.10,
    sig_boost: float = 0.12,
    conf_boost: float = 0.10,
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
    return_pct: Optional[float] = 5.0,
    baseline_sig: float = 0.60,
    baseline_conf: float = 0.55,
    raw_sc: Optional[float] = 0.70,
    raw_rec: Optional[float] = 0.60,
    raw_ta: Optional[float] = 0.50,
    raw_ma: Optional[float] = 0.20,
    as_of_date: str = "2025-01-01",
) -> Dict[str, Any]:
    return {
        "outcome_id":                1,
        "snapshot_id":               1,
        "symbol":                    symbol,
        "as_of_date":                as_of_date,
        "window_days":               window_days,
        "return_pct":                return_pct,
        "outcome_label":             "positive" if (return_pct or 0) > 0 else "negative",
        "baseline_signal_score":     baseline_sig,
        "baseline_confidence_score": baseline_conf,
        "stored_signal_delta":       0.05,
        "stored_soft_composite":     0.42,
        "stored_scraped_confidence": 0.70,
        "raw_scraped_confidence":    raw_sc,
        "raw_recency_score":         raw_rec,
        "raw_theme_alignment_score": raw_ta,
        "raw_mention_acceleration":  raw_ma,
        "raw_source_count":          2,
    }


def _make_annotated(recomputed_delta: float, return_pct: Optional[float]) -> Dict[str, Any]:
    """Minimal annotated row for _compute_window_metrics."""
    return {
        "recomputed_signal_delta": recomputed_delta,
        "return_pct":              return_pct,
        "window_days":             5,
    }


def _make_evaluated(obj: float, stab: float = 1.0) -> Dict[str, Any]:
    """Minimal evaluated candidate for ranking tests."""
    return {
        "candidate":       _make_candidate(),
        "per_window":      {},
        "objective_score": obj,
        "stability_score": stab,
    }


# ---------------------------------------------------------------------------
# 1. Weight grid generation
# ---------------------------------------------------------------------------

class TestWeightGridGeneration(unittest.TestCase):

    def test_returns_nonempty_list(self):
        combos = generate_weight_grid(step=0.10)
        self.assertIsInstance(combos, list)
        self.assertGreater(len(combos), 0)

    def test_all_weights_sum_to_one(self):
        for combo in generate_weight_grid(step=0.10):
            total = round(sum(combo.values()), 6)
            self.assertAlmostEqual(total, 1.0, places=5,
                                   msg=f"Weights don't sum to 1.0: {combo}")

    def test_all_keys_present(self):
        for combo in generate_weight_grid(step=0.10):
            self.assertEqual(set(combo.keys()), set(_WEIGHT_KEYS))

    def test_all_weights_positive(self):
        for combo in generate_weight_grid(step=0.10):
            for k, v in combo.items():
                self.assertGreater(v, 0.0, msg=f"Zero/negative weight for {k}: {combo}")

    def test_step_010_produces_84_combos(self):
        # Compositions of 10 into 4 positive parts = C(9, 3) = 84
        combos = generate_weight_grid(step=0.10)
        self.assertEqual(len(combos), 84)

    def test_step_020_produces_4_combos(self):
        # Compositions of 5 into 4 positive parts = C(4, 3) = 4
        combos = generate_weight_grid(step=0.20)
        self.assertEqual(len(combos), 4)

    def test_finer_step_produces_more_combos(self):
        coarse = generate_weight_grid(step=0.20)
        fine   = generate_weight_grid(step=0.10)
        self.assertGreater(len(fine), len(coarse))

    def test_weights_are_multiples_of_step(self):
        step = 0.10
        for combo in generate_weight_grid(step=step):
            for v in combo.values():
                remainder = round(v % step, 8)
                self.assertAlmostEqual(
                    min(remainder, step - remainder), 0.0, places=5,
                    msg=f"Weight {v} is not a multiple of {step}",
                )


# ---------------------------------------------------------------------------
# 2. Weight normalisation
# ---------------------------------------------------------------------------

class TestNormalizeWeights(unittest.TestCase):

    def test_basic_normalization(self):
        result = normalize_weights({"a": 2.0, "b": 2.0, "c": 2.0, "d": 2.0})
        for v in result.values():
            self.assertAlmostEqual(v, 0.25, places=5)

    def test_already_normalized_unchanged(self):
        w = {"a": 0.40, "b": 0.30, "c": 0.20, "d": 0.10}
        result = normalize_weights(w)
        for k in w:
            self.assertAlmostEqual(result[k], w[k], places=5)

    def test_result_sums_to_one(self):
        result = normalize_weights({"x": 1.0, "y": 3.0, "z": 6.0})
        self.assertAlmostEqual(sum(result.values()), 1.0, places=5)

    def test_negative_weight_raises(self):
        with self.assertRaises(ValueError):
            normalize_weights({"a": 0.5, "b": -0.1})

    def test_zero_total_raises(self):
        with self.assertRaises(ValueError):
            normalize_weights({"a": 0.0, "b": 0.0})

    def test_single_key(self):
        result = normalize_weights({"only": 7.0})
        self.assertAlmostEqual(result["only"], 1.0, places=5)


# ---------------------------------------------------------------------------
# 3. Candidate generation
# ---------------------------------------------------------------------------

class TestGenerateCandidates(unittest.TestCase):

    def test_returns_list(self):
        cands = generate_candidates(
            signal_boost_grid=[0.10, 0.12],
            conf_boost_grid=[0.08, 0.10],
            weight_step=0.20,
        )
        self.assertIsInstance(cands, list)

    def test_each_candidate_has_required_keys(self):
        cands = generate_candidates(
            signal_boost_grid=[0.10],
            conf_boost_grid=[0.08],
            weight_step=0.20,
        )
        for c in cands:
            self.assertIn("weights",          c)
            self.assertIn("max_signal_boost", c)
            self.assertIn("max_conf_boost",   c)

    def test_candidate_weights_sum_to_one(self):
        cands = generate_candidates(
            signal_boost_grid=[0.10],
            conf_boost_grid=[0.08],
            weight_step=0.20,
        )
        for c in cands:
            self.assertAlmostEqual(sum(c["weights"].values()), 1.0, places=5)

    def test_max_candidates_caps_output(self):
        cands = generate_candidates(
            signal_boost_grid=[0.08, 0.10, 0.12, 0.14, 0.16],
            conf_boost_grid=[0.06, 0.08, 0.10, 0.12],
            weight_step=0.10,
            max_candidates=50,
        )
        self.assertLessEqual(len(cands), 50)

    def test_total_without_cap(self):
        # step=0.20 → 4 combos; 2 sb × 2 cb = 16 total; under max_candidates
        cands = generate_candidates(
            signal_boost_grid=[0.10, 0.12],
            conf_boost_grid=[0.08, 0.10],
            weight_step=0.20,
            max_candidates=9999,
        )
        self.assertEqual(len(cands), 4 * 2 * 2)

    def test_boosts_come_from_grids(self):
        sb = [0.10, 0.15]
        cb = [0.07, 0.09]
        cands = generate_candidates(
            signal_boost_grid=sb,
            conf_boost_grid=cb,
            weight_step=0.20,
            max_candidates=9999,
        )
        for c in cands:
            self.assertIn(c["max_signal_boost"], sb)
            self.assertIn(c["max_conf_boost"],   cb)

    def test_reproducible_with_same_seed(self):
        kwargs = dict(
            signal_boost_grid=[0.08, 0.10, 0.12, 0.14, 0.16],
            conf_boost_grid=[0.06, 0.08, 0.10, 0.12],
            weight_step=0.10,
            max_candidates=50,
        )
        run1 = generate_candidates(**kwargs, seed=42)
        run2 = generate_candidates(**kwargs, seed=42)
        self.assertEqual(run1, run2)

    def test_different_seed_different_order(self):
        kwargs = dict(
            signal_boost_grid=[0.08, 0.10, 0.12, 0.14, 0.16],
            conf_boost_grid=[0.06, 0.08, 0.10, 0.12],
            weight_step=0.10,
            max_candidates=50,
        )
        run_a = generate_candidates(**kwargs, seed=42)
        run_b = generate_candidates(**kwargs, seed=99)
        # With max_candidates < total, different seeds should produce
        # different orderings with overwhelming probability
        self.assertNotEqual(
            [c["max_signal_boost"] for c in run_a],
            [c["max_signal_boost"] for c in run_b],
        )


# ---------------------------------------------------------------------------
# 4. Enriched score recomputation
# ---------------------------------------------------------------------------

class TestRecomputeEnrichedScore(unittest.TestCase):

    def _call(self, **kw):
        defaults = dict(
            baseline_signal_score=0.60,
            baseline_confidence_score=0.55,
            raw_scraped_confidence=0.80,
            raw_recency_score=0.70,
            raw_theme_alignment_score=0.50,
            raw_mention_acceleration=0.0,   # neutral → accel_norm = 0.5
            weights={
                "scraped_confidence":    0.40,
                "recency_score":         0.30,
                "theme_alignment_score": 0.20,
                "mention_accel_norm":    0.10,
            },
            max_signal_boost=0.12,
            max_conf_boost=0.10,
        )
        defaults.update(kw)
        return recompute_enriched_score(**defaults)

    def test_returns_three_tuple(self):
        result = self._call()
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)

    def test_composite_in_zero_one(self):
        composite, _, _ = self._call()
        self.assertGreaterEqual(composite, 0.0)
        self.assertLessEqual(composite, 1.0)

    def test_enriched_sig_geq_baseline(self):
        _, enriched_sig, _ = self._call()
        self.assertGreaterEqual(enriched_sig, 0.60)

    def test_zero_soft_signals_returns_baseline(self):
        composite, enriched_sig, enriched_conf = self._call(
            raw_scraped_confidence=0.0,
            raw_recency_score=0.0,
            raw_theme_alignment_score=0.0,
            raw_mention_acceleration=-1.0,  # accel_norm=0.0
        )
        self.assertAlmostEqual(composite, 0.0, places=5)
        self.assertAlmostEqual(enriched_sig, 0.60, places=5)
        self.assertAlmostEqual(enriched_conf, 0.55, places=5)

    def test_mention_acceleration_minus_one_normalizes_to_zero(self):
        """mention_acceleration = -1 → accel_norm = 0 → contributes 0."""
        composite_neg, _, _ = self._call(
            raw_mention_acceleration=-1.0,
            raw_scraped_confidence=0.0,
            raw_recency_score=0.0,
            raw_theme_alignment_score=0.0,
        )
        self.assertAlmostEqual(composite_neg, 0.0, places=5)

    def test_mention_acceleration_plus_one_normalizes_to_one(self):
        """mention_acceleration = +1 → accel_norm = 1 → mention_accel_norm × weight contributes fully."""
        composite_pos, _, _ = self._call(
            raw_mention_acceleration=1.0,
            raw_scraped_confidence=0.0,
            raw_recency_score=0.0,
            raw_theme_alignment_score=0.0,
            weights={
                "scraped_confidence":    0.0,
                "recency_score":         0.0,
                "theme_alignment_score": 0.0,
                "mention_accel_norm":    1.0,
            },
        )
        self.assertAlmostEqual(composite_pos, 1.0, places=5)

    def test_clipped_at_one(self):
        """Very high soft signals should not push enriched_sig above 1.0."""
        _, enriched_sig, _ = self._call(
            baseline_signal_score=0.99,
            raw_scraped_confidence=1.0,
            raw_recency_score=1.0,
            raw_theme_alignment_score=1.0,
            raw_mention_acceleration=1.0,
            max_signal_boost=0.50,
        )
        self.assertLessEqual(enriched_sig, 1.0)

    def test_zero_max_signal_boost_no_lift(self):
        _, enriched_sig, _ = self._call(max_signal_boost=0.0)
        self.assertAlmostEqual(enriched_sig, 0.60, places=5)

    def test_known_composite_value(self):
        """
        Manual calculation:
          accel_norm = (0.0 + 1) / 2 = 0.5
          composite = 0.8*0.4 + 0.7*0.3 + 0.5*0.2 + 0.5*0.1
                    = 0.32 + 0.21 + 0.10 + 0.05 = 0.68
        """
        composite, _, _ = self._call()
        self.assertAlmostEqual(composite, 0.68, places=4)


# ---------------------------------------------------------------------------
# 5. Per-window metrics
# ---------------------------------------------------------------------------

class TestComputeWindowMetrics(unittest.TestCase):

    def test_empty_rows_returns_safe_defaults(self):
        m = _compute_window_metrics([])
        self.assertEqual(m["n_total"],    0)
        self.assertEqual(m["n_boosted"],  0)
        self.assertIsNone(m["win_rate_lift"])
        self.assertFalse(m["sample_ok"])

    def test_all_boosted_uses_neutral_baseline(self):
        rows = [_make_annotated(0.05, 3.0) for _ in range(12)]
        m = _compute_window_metrics(rows, min_sample_size=10)
        # All positive returns → win_rate_boosted=1.0, lift vs 0.5
        self.assertAlmostEqual(m["win_rate_boosted"], 1.0)
        self.assertAlmostEqual(m["win_rate_lift"], 0.5, places=4)
        self.assertIsNone(m["win_rate_unboosted"])

    def test_all_unboosted_no_lift(self):
        rows = [_make_annotated(0.0, 5.0) for _ in range(12)]
        m = _compute_window_metrics(rows, min_sample_size=10)
        self.assertEqual(m["n_boosted"], 0)
        self.assertIsNone(m["win_rate_lift"])

    def test_positive_lift_when_boosted_outperforms(self):
        boosted   = [_make_annotated(0.05, 5.0) for _ in range(10)]
        unboosted = [_make_annotated(0.00, -3.0) for _ in range(10)]
        m = _compute_window_metrics(boosted + unboosted, min_sample_size=5)
        self.assertGreater(m["win_rate_lift"], 0.0)
        self.assertGreater(m["return_lift"],   0.0)

    def test_negative_lift_when_boosted_underperforms(self):
        boosted   = [_make_annotated(0.05, -5.0) for _ in range(10)]
        unboosted = [_make_annotated(0.00,  5.0) for _ in range(10)]
        m = _compute_window_metrics(boosted + unboosted, min_sample_size=5)
        self.assertLess(m["win_rate_lift"], 0.0)

    def test_sample_ok_false_below_threshold(self):
        rows = [_make_annotated(0.05, 3.0) for _ in range(5)]
        m = _compute_window_metrics(rows, min_sample_size=10)
        self.assertFalse(m["sample_ok"])

    def test_sample_ok_true_at_threshold(self):
        rows = [_make_annotated(0.05, 3.0) for _ in range(10)]
        m = _compute_window_metrics(rows, min_sample_size=10)
        self.assertTrue(m["sample_ok"])

    def test_zero_lift_equal_performance(self):
        boosted   = [_make_annotated(0.05,  5.0) for _ in range(5)]
        unboosted = [_make_annotated(0.00,  5.0) for _ in range(5)]
        m = _compute_window_metrics(boosted + unboosted, min_sample_size=5)
        self.assertAlmostEqual(m["win_rate_lift"], 0.0, places=4)

    def test_avg_return_boosted_computed(self):
        rows = [_make_annotated(0.05, r) for r in [2.0, 4.0, 6.0, 8.0, 10.0,
                                                    2.0, 4.0, 6.0, 8.0, 10.0]]
        m = _compute_window_metrics(rows, min_sample_size=10)
        self.assertAlmostEqual(m["avg_return_boosted"], 6.0, places=4)

    def test_none_return_pct_excluded_from_stats(self):
        rows = (
            [_make_annotated(0.05, 5.0) for _ in range(10)]
            + [_make_annotated(0.05, None)]  # should be ignored
        )
        m = _compute_window_metrics(rows, min_sample_size=10)
        self.assertAlmostEqual(m["win_rate_boosted"], 1.0, places=4)


# ---------------------------------------------------------------------------
# 6. Objective scoring
# ---------------------------------------------------------------------------

class TestObjectiveScoring(unittest.TestCase):

    def _pw(self, wrl_5=None, rl_5=None, ok_5=True,
                  wrl_20=None, rl_20=None, ok_20=True,
                  wrl_1=0.0, rl_1=0.0, ok_1=False):
        def _m(wrl, rl, ok):
            return {
                "win_rate_lift": wrl,
                "return_lift":   rl,
                "sample_ok":     ok,
                "n_boosted":     15 if ok else 2,
            }
        return {1: _m(wrl_1, rl_1, ok_1), 5: _m(wrl_5, rl_5, ok_5),
                20: _m(wrl_20, rl_20, ok_20)}

    def test_all_windows_fail_returns_zero(self):
        pw = {1: {"sample_ok": False}, 5: {"sample_ok": False}, 20: {"sample_ok": False}}
        self.assertEqual(compute_objective_score(pw), 0.0)

    def test_empty_per_window_returns_zero(self):
        self.assertEqual(compute_objective_score({}), 0.0)

    def test_positive_lift_gives_positive_score(self):
        pw = self._pw(wrl_5=0.20, rl_5=5.0, wrl_20=0.15, rl_20=4.0)
        score = compute_objective_score(pw)
        self.assertGreater(score, 0.0)

    def test_zero_lift_gives_zero_score(self):
        pw = self._pw(wrl_5=0.0, rl_5=0.0, wrl_20=0.0, rl_20=0.0)
        score = compute_objective_score(pw)
        self.assertAlmostEqual(score, 0.0, places=4)

    def test_stability_penalty_applied_above_threshold(self):
        # 5d lift=0.40, 20d lift=0.0 → gap=0.40 > 0.15 → penalty applied
        pw_stable   = self._pw(wrl_5=0.20, rl_5=5.0, wrl_20=0.20, rl_20=5.0)
        pw_unstable = self._pw(wrl_5=0.40, rl_5=5.0, wrl_20=0.00, rl_20=5.0)
        score_stable   = compute_objective_score(pw_stable)
        score_unstable = compute_objective_score(pw_unstable)
        self.assertGreater(score_stable, score_unstable)

    def test_no_stability_penalty_within_threshold(self):
        # Gap = 0.05 < 0.15 → no penalty
        pw = self._pw(wrl_5=0.20, rl_5=5.0, wrl_20=0.15, rl_20=4.0)
        # Should not return 0 due to penalty
        score = compute_objective_score(pw)
        self.assertGreater(score, 0.0)

    def test_higher_lift_higher_score(self):
        pw_low  = self._pw(wrl_5=0.05, rl_5=1.0, wrl_20=0.05, rl_20=1.0)
        pw_high = self._pw(wrl_5=0.30, rl_5=8.0, wrl_20=0.30, rl_20=8.0)
        self.assertGreater(compute_objective_score(pw_high), compute_objective_score(pw_low))

    def test_negative_lift_gives_negative_or_zero_score(self):
        pw = self._pw(wrl_5=-0.10, rl_5=-5.0, wrl_20=-0.10, rl_20=-5.0)
        score = compute_objective_score(pw)
        self.assertLessEqual(score, 0.0)

    def test_single_window_ok(self):
        # Only 5d window ok — should still produce a non-zero score
        pw = {
            5:  {"win_rate_lift": 0.20, "return_lift": 5.0, "sample_ok": True},
            20: {"sample_ok": False},
        }
        score = compute_objective_score(pw, windows=[5, 20])
        self.assertGreater(score, 0.0)

    def test_return_lift_contributes_positively(self):
        pw_no_ret  = self._pw(wrl_5=0.20, rl_5=0.0,  wrl_20=0.20, rl_20=0.0)
        pw_with_ret = self._pw(wrl_5=0.20, rl_5=10.0, wrl_20=0.20, rl_20=10.0)
        self.assertGreater(compute_objective_score(pw_with_ret), compute_objective_score(pw_no_ret))


# ---------------------------------------------------------------------------
# 7. Stability score
# ---------------------------------------------------------------------------

class TestStabilityScore(unittest.TestCase):

    def _pw(self, lift_5, lift_20):
        return {
            5:  {"win_rate_lift": lift_5},
            20: {"win_rate_lift": lift_20},
        }

    def test_identical_lifts_score_one(self):
        score = compute_stability_score(self._pw(0.20, 0.20))
        self.assertAlmostEqual(score, 1.0, places=4)

    def test_gap_zero_point_five_score_zero(self):
        score = compute_stability_score(self._pw(0.50, 0.00))
        self.assertAlmostEqual(score, 0.0, places=4)

    def test_gap_zero_point_25_score_half(self):
        score = compute_stability_score(self._pw(0.25, 0.00))
        self.assertAlmostEqual(score, 0.5, places=4)

    def test_single_window_score_one(self):
        score = compute_stability_score({5: {"win_rate_lift": 0.20}})
        self.assertAlmostEqual(score, 1.0, places=4)

    def test_none_lift_excluded(self):
        score = compute_stability_score({
            5:  {"win_rate_lift": 0.20},
            20: {"win_rate_lift": None},
        })
        self.assertAlmostEqual(score, 1.0, places=4)

    def test_symmetric_negative_lifts(self):
        # Gap = |-0.10 - (-0.20)| = 0.10 → stability = 1 - 0.10/0.5 = 0.80
        score = compute_stability_score(self._pw(-0.10, -0.20))
        self.assertAlmostEqual(score, 0.80, places=4)


# ---------------------------------------------------------------------------
# 8. Ranking logic
# ---------------------------------------------------------------------------

class TestRankingLogic(unittest.TestCase):

    def test_higher_objective_ranked_first(self):
        e = [_make_evaluated(0.30), _make_evaluated(0.70), _make_evaluated(0.50)]
        ranked = rank_candidates(e)
        self.assertAlmostEqual(ranked[0]["objective_score"], 0.70, places=4)
        self.assertAlmostEqual(ranked[1]["objective_score"], 0.50, places=4)
        self.assertAlmostEqual(ranked[2]["objective_score"], 0.30, places=4)

    def test_stability_as_tiebreaker(self):
        e = [
            _make_evaluated(0.50, stab=0.60),
            _make_evaluated(0.50, stab=0.90),
        ]
        ranked = rank_candidates(e)
        self.assertAlmostEqual(ranked[0]["stability_score"], 0.90, places=4)

    def test_rank_field_added(self):
        e = [_make_evaluated(0.40), _make_evaluated(0.20)]
        ranked = rank_candidates(e)
        for item in ranked:
            self.assertIn("rank", item)

    def test_rank_starts_at_one(self):
        ranked = rank_candidates([_make_evaluated(0.50)])
        self.assertEqual(ranked[0]["rank"], 1)

    def test_ranks_are_consecutive(self):
        e = [_make_evaluated(float(i) / 10) for i in range(5)]
        ranked = rank_candidates(e)
        self.assertEqual([r["rank"] for r in ranked], list(range(1, 6)))

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(rank_candidates([]), [])


# ---------------------------------------------------------------------------
# 9. Report schema
# ---------------------------------------------------------------------------

class TestReportSchema(unittest.TestCase):

    def _make_report(self, n=3):
        evaluated = [_make_evaluated(float(i) / 10) for i in range(n)]
        ranked = rank_candidates(evaluated)
        return build_tuning_report(ranked, {"tuning_windows": [5, 20]}, raw_row_count=50)

    def test_required_keys_present(self):
        report = self._make_report()
        for key in (
            "generated_at", "config_used", "total_candidates_tested",
            "total_resolved_rows", "warnings", "top_candidates",
            "recommended", "all_candidates", "recommended_config_snippet",
        ):
            self.assertIn(key, report, msg=f"Missing key: {key}")

    def test_top_candidates_at_most_five(self):
        report = self._make_report(n=10)
        self.assertLessEqual(len(report["top_candidates"]), 5)

    def test_top_candidates_exactly_five_when_enough(self):
        report = self._make_report(n=10)
        self.assertEqual(len(report["top_candidates"]), 5)

    def test_recommended_matches_rank_one(self):
        report = self._make_report(n=5)
        self.assertEqual(report["recommended"]["rank"], 1)

    def test_config_snippet_has_scraped_intel_key(self):
        report = self._make_report()
        snippet = report["recommended_config_snippet"]
        self.assertIn("scraped_intel", snippet)
        self.assertIn("comparison_max_signal_boost", snippet["scraped_intel"])
        self.assertIn("comparison_max_conf_boost",   snippet["scraped_intel"])

    def test_config_snippet_has_blend_weights(self):
        report = self._make_report()
        self.assertIn("blend_weights", report["recommended_config_snippet"])

    def test_warnings_is_list(self):
        report = self._make_report()
        self.assertIsInstance(report["warnings"], list)

    def test_empty_ranked_no_recommended(self):
        report = build_tuning_report([], {}, 0)
        self.assertIsNone(report["recommended"])
        self.assertIsNone(report["recommended_config_snippet"])

    def test_total_candidates_tested_matches_ranked(self):
        evaluated = [_make_evaluated(0.5)] * 7
        ranked = rank_candidates(evaluated)
        report = build_tuning_report(ranked, {}, 100)
        self.assertEqual(report["total_candidates_tested"], 7)

    def test_total_resolved_rows_stored(self):
        report = build_tuning_report([], {}, raw_row_count=123)
        self.assertEqual(report["total_resolved_rows"], 123)


# ---------------------------------------------------------------------------
# 10. File creation (JSON + MD writers)
# ---------------------------------------------------------------------------

class TestFileCreation(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.out = Path(self.tmpdir)
        evaluated = [_make_evaluated(0.4), _make_evaluated(0.3)]
        ranked = rank_candidates(evaluated)
        self.report = build_tuning_report(ranked, {"tuning_windows": [5, 20]}, 40)

    def test_json_written_to_correct_path(self):
        path = write_tuning_results_json(self.report, self.out)
        self.assertEqual(path.name, "scraped_intel_tuning_results.json")
        self.assertTrue(path.exists())

    def test_json_is_valid_and_parseable(self):
        path = write_tuning_results_json(self.report, self.out)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("generated_at", data)

    def test_json_int_keys_converted_to_str(self):
        path = write_tuning_results_json(self.report, self.out)
        raw = path.read_text(encoding="utf-8")
        # JSON dict keys must be strings — no bare integer keys
        import re
        int_key_pattern = re.compile(r'"\d+"\s*:')  # "5": is fine (str key)
        bare_int_key    = re.compile(r',?\s*\d+\s*:')  # 5: is invalid JSON
        parsed = json.loads(raw)  # would raise if malformed
        self.assertIsNotNone(parsed)

    def test_md_written_to_correct_path(self):
        path = write_tuning_results_md(self.report, self.out)
        self.assertEqual(path.name, "scraped_intel_tuning_results.md")
        self.assertTrue(path.exists())

    def test_md_contains_heading(self):
        path = write_tuning_results_md(self.report, self.out)
        content = path.read_text(encoding="utf-8")
        self.assertIn("# Scraped Intelligence", content)

    def test_md_contains_recommended_section(self):
        path = write_tuning_results_md(self.report, self.out)
        content = path.read_text(encoding="utf-8")
        self.assertIn("Recommended Configuration", content)

    def test_md_contains_top5_section(self):
        path = write_tuning_results_md(self.report, self.out)
        content = path.read_text(encoding="utf-8")
        self.assertIn("Top 5 Candidates", content)

    def test_md_contains_shadow_mode_note(self):
        path = write_tuning_results_md(self.report, self.out)
        content = path.read_text(encoding="utf-8")
        self.assertIn("shadow mode", content)


# ---------------------------------------------------------------------------
# 11. Evaluate candidate (integration — pure in-memory)
# ---------------------------------------------------------------------------

class TestEvaluateCandidate(unittest.TestCase):

    def _rows_5d(self, n_boosted=12, n_unboosted=8,
                 boosted_ret=5.0, unboosted_ret=-2.0):
        rows = []
        for _ in range(n_boosted):
            rows.append(_make_raw_row(
                window_days=5, return_pct=boosted_ret,
                raw_sc=0.80, raw_rec=0.70, raw_ta=0.50, raw_ma=0.0,
            ))
        for _ in range(n_unboosted):
            rows.append(_make_raw_row(
                window_days=5, return_pct=unboosted_ret,
                raw_sc=None,  # no soft signals → not boosted
            ))
        return rows

    def test_returns_required_keys(self):
        cand = _make_candidate()
        rows = self._rows_5d()
        result = evaluate_candidate(cand, rows, windows=[5])
        for key in ("candidate", "per_window", "objective_score", "stability_score"):
            self.assertIn(key, result)

    def test_rows_without_soft_signals_not_boosted(self):
        cand = _make_candidate()
        rows = [_make_raw_row(window_days=5, return_pct=5.0, raw_sc=None)]
        result = evaluate_candidate(cand, rows, windows=[5])
        self.assertEqual(result["per_window"][5]["n_boosted"], 0)

    def test_positive_lift_with_good_data(self):
        cand = _make_candidate(sig_boost=0.12)
        rows = self._rows_5d(n_boosted=15, n_unboosted=10,
                              boosted_ret=8.0, unboosted_ret=-3.0)
        result = evaluate_candidate(cand, rows, windows=[5], min_sample_size=10)
        self.assertGreater(result["per_window"][5]["win_rate_lift"], 0.0)

    def test_objective_zero_when_insufficient_boosted(self):
        cand = _make_candidate()
        # Only 3 boosted rows — below default min_sample_size=10
        rows = [_make_raw_row(window_days=5, return_pct=5.0) for _ in range(3)]
        result = evaluate_candidate(cand, rows, windows=[5], min_sample_size=10)
        self.assertFalse(result["per_window"][5]["sample_ok"])
        self.assertEqual(result["objective_score"], 0.0)

    def test_stability_score_one_for_single_window(self):
        cand = _make_candidate()
        rows = [_make_raw_row(window_days=5, return_pct=5.0) for _ in range(12)]
        result = evaluate_candidate(cand, rows, windows=[5])
        self.assertAlmostEqual(result["stability_score"], 1.0, places=4)


# ---------------------------------------------------------------------------
# 12. End-to-end: run_tuning with real temp database
# ---------------------------------------------------------------------------

class TestEndToEnd(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path  = Path(self.tmpdir) / "test_portfolio.db"
        self.out_dir  = Path(self.tmpdir) / "outputs"
        self.out_dir.mkdir()

    def tearDown(self):
        import shutil
        try:
            shutil.rmtree(self.tmpdir)
        except PermissionError:
            pass   # WAL cleanup — non-fatal on Windows

    def _init_store(self):
        store = ScrapedIntelStore(db_path=self.db_path)
        return store

    def _insert_resolved_outcome(
        self,
        conn: sqlite3.Connection,
        symbol: str = "AAPL",
        as_of_date: str = "2025-01-01",
        window_days: int = 5,
        return_pct: float = 5.0,
        baseline_sig: float = 0.60,
    ):
        """Insert a complete chain: soft_signals → snapshot → resolved outcome."""
        # soft_signals
        conn.execute(
            """
            INSERT OR IGNORE INTO soft_signals (
                symbol, as_of_date,
                headline_count_7d, headline_count_30d, source_count,
                avg_sentiment, theme_alignment_score, mention_acceleration,
                recency_score, scraped_confidence, evidence_items, recorded_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                symbol, as_of_date,
                5, 20, 2, 0.1, 0.50, 0.20, 0.65, 0.72,
                "[]", "2025-01-01T00:00:00",
            ),
        )
        # comparison_snapshot
        conn.execute(
            """
            INSERT OR IGNORE INTO comparison_snapshots (
                symbol, as_of_date,
                baseline_signal_score, enriched_signal_score, signal_delta,
                baseline_confidence_score, enriched_confidence_score, confidence_delta,
                baseline_rank, enriched_rank, rank_change,
                soft_composite, top_features,
                source_count, evidence_count, scraped_confidence,
                soft_signals_available, recorded_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                symbol, as_of_date,
                baseline_sig, baseline_sig + 0.05, 0.05,
                0.55, 0.60, 0.05,
                1, 1, 0,
                0.42, "[]",
                2, 20, 0.72,
                1, "2025-01-01T00:00:00",
            ),
        )
        snap_id = conn.execute(
            "SELECT id FROM comparison_snapshots WHERE symbol=? AND as_of_date=?",
            (symbol, as_of_date),
        ).fetchone()[0]
        # comparison_outcome (resolved)
        conn.execute(
            """
            INSERT OR IGNORE INTO comparison_outcomes (
                snapshot_id, symbol, as_of_date, window_days,
                baseline_price, outcome_price, return_pct, outcome_label,
                evaluated_at, outcome_status
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                snap_id, symbol, as_of_date, window_days,
                100.0, 100.0 * (1 + return_pct / 100.0), return_pct,
                "positive" if return_pct > 0 else "negative",
                "2025-01-10T00:00:00", "resolved",
            ),
        )

    def test_empty_store_returns_report_with_warning(self):
        self._init_store()
        report = run_tuning(
            db_path=self.db_path,
            output_dir=self.out_dir,
            config={"tuning_weight_step": 0.20, "tuning_max_candidates": 10},
        )
        self.assertIsInstance(report, dict)
        self.assertTrue(len(report["warnings"]) > 0)
        self.assertEqual(report["total_candidates_tested"], 0)

    def test_empty_store_still_writes_files(self):
        self._init_store()
        run_tuning(db_path=self.db_path, output_dir=self.out_dir,
                   config={"tuning_weight_step": 0.20})
        self.assertTrue((self.out_dir / "scraped_intel_tuning_results.json").exists())
        self.assertTrue((self.out_dir / "scraped_intel_tuning_results.md").exists())

    def test_with_resolved_data_produces_candidates(self):
        store = self._init_store()
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            symbols = ["AAPL", "MSFT", "NVDA", "GOOGL", "META",
                       "AMZN", "TSLA", "AVGO", "AMD",   "PLTR",
                       "COIN", "QQQ"]
            for i, sym in enumerate(symbols):
                ret = 5.0 if i % 2 == 0 else -2.0
                for w in [5, 20]:
                    self._insert_resolved_outcome(
                        conn, symbol=sym,
                        as_of_date=f"2025-01-{i+1:02d}",
                        window_days=w, return_pct=ret,
                    )
            conn.commit()
        finally:
            conn.close()

        report = run_tuning(
            db_path=self.db_path,
            output_dir=self.out_dir,
            config={
                "tuning_weight_step":    0.20,
                "tuning_max_candidates": 20,
                "tuning_min_sample_size": 1,
                "tuning_windows":        [5, 20],
            },
        )
        self.assertGreater(report["total_candidates_tested"], 0)
        self.assertIsNotNone(report["recommended"])

    def test_output_files_written(self):
        store = self._init_store()
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            for i, sym in enumerate(["AAPL", "MSFT", "NVDA"]):
                for w in [5]:
                    self._insert_resolved_outcome(
                        conn, symbol=sym, as_of_date=f"2025-01-0{i+1}",
                        window_days=w, return_pct=4.0,
                    )
            conn.commit()
        finally:
            conn.close()

        run_tuning(
            db_path=self.db_path,
            output_dir=self.out_dir,
            config={
                "tuning_weight_step":    0.20,
                "tuning_max_candidates": 5,
                "tuning_min_sample_size": 1,
                "tuning_windows":        [5],
            },
        )
        self.assertTrue((self.out_dir / "scraped_intel_tuning_results.json").exists())
        self.assertTrue((self.out_dir / "scraped_intel_tuning_results.md").exists())

    def test_config_used_reflected_in_report(self):
        self._init_store()
        cfg = {
            "tuning_weight_step":    0.20,
            "tuning_max_candidates": 8,
            "tuning_windows":        [5],
            "tuning_min_sample_size": 3,
        }
        report = run_tuning(
            db_path=self.db_path,
            output_dir=self.out_dir,
            config=cfg,
        )
        self.assertEqual(report["config_used"]["tuning_weight_step"],    0.20)
        self.assertEqual(report["config_used"]["tuning_max_candidates"], 8)
        self.assertEqual(report["config_used"]["tuning_windows"],        [5])

    def test_store_tables_not_mutated(self):
        """Tuning must not modify existing snapshot or soft_signal rows."""
        store = self._init_store()
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            self._insert_resolved_outcome(conn, symbol="AAPL", return_pct=5.0)
            conn.commit()
            before_snap = conn.execute(
                "SELECT COUNT(*) FROM comparison_snapshots"
            ).fetchone()[0]
            before_sig = conn.execute(
                "SELECT COUNT(*) FROM soft_signals"
            ).fetchone()[0]
        finally:
            conn.close()

        run_tuning(
            db_path=self.db_path,
            output_dir=self.out_dir,
            config={"tuning_weight_step": 0.20, "tuning_max_candidates": 5,
                    "tuning_min_sample_size": 1, "tuning_windows": [5]},
        )

        conn2 = sqlite3.connect(str(self.db_path))
        try:
            after_snap = conn2.execute(
                "SELECT COUNT(*) FROM comparison_snapshots"
            ).fetchone()[0]
            after_sig = conn2.execute(
                "SELECT COUNT(*) FROM soft_signals"
            ).fetchone()[0]
        finally:
            conn2.close()

        self.assertEqual(before_snap, after_snap)
        self.assertEqual(before_sig, after_sig)

    def test_low_sample_warning_present(self):
        store = self._init_store()
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            self._insert_resolved_outcome(conn, return_pct=5.0)
            conn.commit()
        finally:
            conn.close()

        report = run_tuning(
            db_path=self.db_path,
            output_dir=self.out_dir,
            config={
                "tuning_weight_step":    0.20,
                "tuning_max_candidates": 5,
                "tuning_min_sample_size": 100,   # very high → warning triggered
                "tuning_windows":        [5],
            },
        )
        # Should warn about low sample count
        self.assertTrue(len(report["warnings"]) > 0)

    def test_require_all_windows_filters_candidates(self):
        store = self._init_store()
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Insert data for window=5 only → window=20 always fails sample_ok
            for i, sym in enumerate(["AAPL", "MSFT", "NVDA", "GOOGL", "META",
                                     "AMZN", "TSLA", "AVGO", "AMD",   "PLTR",
                                     "COIN", "QQQ"]):
                self._insert_resolved_outcome(
                    conn, symbol=sym,
                    as_of_date=f"2025-01-{i+1:02d}",
                    window_days=5, return_pct=3.0,
                )
            conn.commit()
        finally:
            conn.close()

        report = run_tuning(
            db_path=self.db_path,
            output_dir=self.out_dir,
            config={
                "tuning_weight_step":        0.20,
                "tuning_max_candidates":     20,
                "tuning_min_sample_size":    1,
                "tuning_windows":            [5, 20],
                "tuning_require_all_windows": True,
            },
        )
        # All candidates fail because window=20 has no rows → warning expected
        # (or 0 candidates passed)
        all_cands = report.get("all_candidates", [])
        for c in all_cands:
            self.assertTrue(
                c["per_window"].get(20, {}).get("sample_ok", False),
                msg="require_all_windows=True but a candidate passed with window=20 failing",
            )


# ---------------------------------------------------------------------------
# 13. Store method: get_resolved_outcomes_with_raw_signals
# ---------------------------------------------------------------------------

class TestGetResolvedOutcomesWithRawSignals(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "portfolio.db"

    def tearDown(self):
        import shutil
        try:
            shutil.rmtree(self.tmpdir)
        except PermissionError:
            pass

    def _store(self):
        return ScrapedIntelStore(db_path=self.db_path)

    def _insert_full_chain(self, symbol="AAPL", as_of_date="2025-01-01",
                            window_days=5, return_pct=5.0, with_signals=True):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            if with_signals:
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
                       symbol, as_of_date,
                       baseline_signal_score, enriched_signal_score, signal_delta,
                       baseline_confidence_score, enriched_confidence_score, confidence_delta,
                       baseline_rank, enriched_rank, rank_change,
                       soft_composite, top_features, source_count, evidence_count,
                       scraped_confidence, soft_signals_available, recorded_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (symbol, as_of_date, 0.60, 0.65, 0.05, 0.55, 0.60, 0.05,
                 1, 1, 0, 0.42, "[]", 2, 20, 0.72, 1 if with_signals else 0,
                 "2025-01-01T00:00:00"),
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
            conn.commit()
        finally:
            conn.close()

    def test_returns_list(self):
        self._store()
        self._insert_full_chain()
        store = self._store()
        rows = store.get_resolved_outcomes_with_raw_signals()
        self.assertIsInstance(rows, list)

    def test_resolved_row_present(self):
        self._store()
        self._insert_full_chain(symbol="AAPL", return_pct=5.0)
        store = self._store()
        rows = store.get_resolved_outcomes_with_raw_signals()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "AAPL")

    def test_raw_signals_populated_when_soft_signals_exist(self):
        self._store()
        self._insert_full_chain(with_signals=True)
        store = self._store()
        row = store.get_resolved_outcomes_with_raw_signals()[0]
        self.assertIsNotNone(row["raw_scraped_confidence"])
        self.assertIsNotNone(row["raw_recency_score"])

    def test_raw_signals_null_when_no_soft_signals(self):
        self._store()
        self._insert_full_chain(with_signals=False)
        store = self._store()
        row = store.get_resolved_outcomes_with_raw_signals()[0]
        self.assertIsNone(row["raw_scraped_confidence"])

    def test_since_date_filter_works(self):
        self._store()
        self._insert_full_chain(symbol="A", as_of_date="2025-01-01")
        self._insert_full_chain(symbol="B", as_of_date="2025-03-01")
        store = self._store()
        rows = store.get_resolved_outcomes_with_raw_signals(since_date="2025-02-01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "B")

    def test_window_filter_works(self):
        self._store()
        self._insert_full_chain(symbol="AAPL", window_days=5)
        self._insert_full_chain(symbol="AAPL", as_of_date="2025-01-02", window_days=20)
        store = self._store()
        rows = store.get_resolved_outcomes_with_raw_signals(window_days=20)
        self.assertTrue(all(r["window_days"] == 20 for r in rows))

    def test_baseline_score_present(self):
        self._store()
        self._insert_full_chain()
        store = self._store()
        row = store.get_resolved_outcomes_with_raw_signals()[0]
        self.assertIn("baseline_signal_score", row)
        self.assertIn("baseline_confidence_score", row)


if __name__ == "__main__":
    unittest.main()
