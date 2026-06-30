"""Phase 6 — semantic-liveness / degeneracy detectors (observe-only).

Generic, reusable detectors that catch pipelines which are technically green but
semantically broken (one-value collapse, excessive defaults, zero variance,
class disappearance) — WITH min-sample + variation guards so legitimately
single-state windows do not false-positive.

TDD: written before portfolio_automation/semantic_liveness.py existed.
"""
from __future__ import annotations

import portfolio_automation.semantic_liveness as sl


# ---------------------------------------------------------------------------
# Single-value collapse
# ---------------------------------------------------------------------------


def test_single_value_collapse_detected_on_varied_window():
    f = sl.detect_single_value_collapse(["neutral"] * 40, probe="regime_label", min_sample=30)
    assert f is not None and f["kind"] == "single_value_collapse"
    assert f["observed_distinct"] == 1 and f["n_samples"] == 40


def test_small_window_does_not_false_positive():
    # below min_sample: not enough evidence to call it degenerate
    assert sl.detect_single_value_collapse(["neutral"] * 5, probe="regime_label", min_sample=30) is None


def test_healthy_varied_window_is_clean():
    assert sl.detect_single_value_collapse(["bull", "bear", "neutral"] * 20, probe="x", min_sample=30) is None


def test_documented_single_state_exception_suppresses():
    # an explicitly-allowed single value (legitimately calm regime) is not a defect
    f = sl.detect_single_value_collapse(["neutral"] * 40, probe="regime_label",
                                        min_sample=30, allowed_single_values={"neutral"})
    assert f is None


# ---------------------------------------------------------------------------
# Excessive default
# ---------------------------------------------------------------------------


def test_excessive_default_detected():
    vals = [0.55] * 19 + [0.7]  # 95% defaulted
    f = sl.detect_excessive_default(vals, default=0.55, probe="priority", min_sample=20, max_default_frac=0.9)
    assert f is not None and f["kind"] == "excessive_default"
    assert f["default_frac"] >= 0.9


def test_default_below_threshold_is_clean():
    vals = [0.55] * 5 + [0.7] * 15
    assert sl.detect_excessive_default(vals, default=0.55, probe="priority", min_sample=20, max_default_frac=0.9) is None


# ---------------------------------------------------------------------------
# Zero variance / class disappearance
# ---------------------------------------------------------------------------


def test_zero_variance_detected():
    f = sl.detect_zero_variance([0.8] * 40, probe="confidence", min_sample=30)
    assert f is not None and f["kind"] == "zero_variance"


def test_nonzero_variance_clean():
    assert sl.detect_zero_variance([0.1, 0.5, 0.9] * 15, probe="confidence", min_sample=30) is None


def test_class_disappearance_detected():
    f = sl.detect_class_disappearance(current=["bull", "neutral"],
                                      expected=["bull", "bear", "neutral"], probe="regime")
    assert f is not None and f["kind"] == "class_disappearance"
    assert "bear" in f["missing"]


def test_no_class_disappearance_when_all_present():
    assert sl.detect_class_disappearance(current=["bull", "bear", "neutral"],
                                         expected=["bull", "bear"], probe="regime") is None


# ---------------------------------------------------------------------------
# Runner: surfaces findings + stays observe-only (cannot mutate decisions)
# ---------------------------------------------------------------------------


def test_run_semantic_liveness_is_observe_only_and_degrades(tmp_path):
    res = sl.run_semantic_liveness(tmp_path, now="2026-06-30T09:00:00+00:00")
    assert res["observe_only"] is True
    assert "findings" in res and isinstance(res["findings"], list)
    assert res["overall_status"] in ("green", "amber")  # never red (meta-monitor)
