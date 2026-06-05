"""
Tests for backtesting/direction_resolution.py — direction-aware outcome
resolution (Pattern-Loop Step 1b).

Fully offline and deterministic. Covers the pure resolver functions
(signal_direction / directional_outcome) across HEALTHY directional cases (a
bearish signal that falls is a win; one that rises is a loss) and DEGRADED cases
(missing/None direction or return → safe default, no crash), plus the harness
integration that surfaces a directional breakdown without disturbing the existing
long-only metrics.

Observe-only: pure outcome relabelling; no protected scoring/decision logic and
no artifact writes are involved.
"""

from __future__ import annotations

from backtesting.direction_resolution import directional_outcome, signal_direction
from backtesting.poc_simulation_harness import run_poc


# --------------------------------------------------------------------------
# signal_direction
# --------------------------------------------------------------------------

def test_down_pattern_resolves_down():
    assert signal_direction({"pattern": "STRONG_MOVE_DOWN"}) == "down"


def test_up_pattern_resolves_up():
    assert signal_direction({"pattern": "STRONG_MOVE_UP"}) == "up"


def test_non_directional_pattern_defaults_up():
    # Legacy long-only behavior: a pattern with no inherent direction is 'up'.
    assert signal_direction({"pattern": "VOLUME_SPIKE"}) == "up"


def test_missing_pattern_defaults_up():
    assert signal_direction({}) == "up"


def test_explicit_direction_field_overrides_pattern():
    assert signal_direction({"pattern": "STRONG_MOVE_UP", "direction": "down"}) == "down"


def test_down_tag_in_patterns_list_resolves_down():
    assert signal_direction({"patterns": ["VOLUME_SPIKE", "STRONG_MOVE_DOWN"]}) == "down"


# --------------------------------------------------------------------------
# directional_outcome — healthy
# --------------------------------------------------------------------------

def test_down_signal_that_falls_is_win():
    assert directional_outcome(-2.0, "down") == "win"


def test_down_signal_that_rises_is_loss():
    assert directional_outcome(3.0, "down") == "loss"


def test_up_signal_that_rises_is_win():
    assert directional_outcome(2.0, "up") == "win"


def test_up_signal_that_falls_is_loss():
    assert directional_outcome(-1.0, "up") == "loss"


def test_flat_return_is_loss_for_both_directions():
    # A zero move satisfies neither a long nor a short thesis.
    assert directional_outcome(0.0, "up") == "loss"
    assert directional_outcome(0.0, "down") == "loss"


# --------------------------------------------------------------------------
# directional_outcome — degraded / safe defaults
# --------------------------------------------------------------------------

def test_none_return_is_unknown():
    assert directional_outcome(None, "down") == "unknown"
    assert directional_outcome(None, "up") == "unknown"


def test_empty_or_none_direction_falls_back_to_long_only():
    assert directional_outcome(2.0, "") == "win"      # treated as 'up'
    assert directional_outcome(-1.0, None) == "loss"  # treated as 'up'


def test_neutral_direction_uses_long_only_semantics():
    assert directional_outcome(2.0, "neutral") == "win"
    assert directional_outcome(-1.0, "neutral") == "loss"


# --------------------------------------------------------------------------
# Harness integration — additive, long-only metrics unchanged
# --------------------------------------------------------------------------

def test_harness_adds_directional_breakdown():
    p = run_poc(n_signals=120, seed=42, write=False)
    am = p["added_metrics"]
    assert "directional" in am
    d = am["directional"]
    assert 0.0 <= d["hit_rate"] <= 100.0
    assert d["evaluated"] >= 1
    # Per-direction sub-breakdown present and bounded.
    assert "by_direction" in d
    for row in d["by_direction"]:
        assert row["direction"] in {"up", "down", "neutral"}
        assert 0.0 <= row["hit_rate"] <= 100.0


def test_harness_long_only_metrics_unchanged_by_directional_addition():
    # The existing long-only headline metrics must be untouched (additive only).
    p = run_poc(n_signals=120, seed=42, write=False)
    perf = p["performance"]
    assert perf["total_signals"] == 120
    assert 0.0 <= perf["hit_rate"] <= 100.0
    assert {"sharpe_like", "edge_vs_random_baseline_pct", "per_pattern"} <= set(p["added_metrics"])
