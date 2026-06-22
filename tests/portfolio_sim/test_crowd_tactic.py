"""Tests for the crowd-signal tactic: sleeve, overlay, proxy mapper, sentiment tilt."""
from __future__ import annotations

from portfolio_automation.portfolio_sim.crowd_tactic import (
    SENTIMENT_MAX_TILT,
    CrowdTactic,
    apply_sentiment_tilt,
    build_crowd_sleeve,
    proxy_pseudo_state,
)

CORE = {"QQQ": 0.6, "GLD": 0.4}


def _state(ticker, st, prio=1.0):
    return {"ticker": ticker, "crowd_state": st, "crowd_research_priority_score": prio}


def test_sleeve_caps_respected():
    states = [_state("NVDA", "emerging_dd", 5), _state("AMD", "crowd_validation", 3),
              _state("PLTR", "contrarian_neglect", 2)]
    w, flags, sleeve = build_crowd_sleeve(CORE, states, sleeve_total=0.15, per_idea=0.05)
    assert abs(sum(w.values()) - 1.0) < 1e-9
    sleeve_sum = sum(w[t] for t in sleeve)
    assert sleeve_sum <= 0.15 + 1e-9
    for t in sleeve:
        assert w[t] <= 0.05 + 1e-9


def test_priority_weighting_orders_sleeve():
    states = [_state("HI", "emerging_dd", 10), _state("LO", "emerging_dd", 1)]
    w, _, sleeve = build_crowd_sleeve(CORE, states, priority_weighted=True)
    assert w["HI"] >= w["LO"]


def test_caution_excluded_and_core_trimmed():
    # QQQ (a core holding) is in caution → ×0.8 trim + flag; GME caution never enters sleeve
    states = [_state("QQQ", "hype_acceleration"), _state("GME", "reflexive_squeeze_risk"),
              _state("NVDA", "emerging_dd", 4)]
    w, flags, sleeve = build_crowd_sleeve(CORE, states)
    assert "QQQ" in flags
    assert "GME" not in sleeve and "GME" not in w
    assert "NVDA" in sleeve
    # QQQ trimmed relative to GLD vs the untrimmed 0.6/0.4 split
    assert w["QQQ"] / (w["QQQ"] + w["GLD"]) < 0.6


def test_no_useful_states_is_core_only():
    w, flags, sleeve = build_crowd_sleeve(CORE, [])
    assert sleeve == []
    assert abs(sum(w.values()) - 1.0) < 1e-9


def test_proxy_pseudo_state_mapping():
    assert proxy_pseudo_state(volume_z=4.0, momentum=0.10) == "hype_acceleration"
    assert proxy_pseudo_state(volume_z=2.0, momentum=-0.05) == "crowd_exhaustion"
    assert proxy_pseudo_state(volume_z=1.2, momentum=0.03) == "emerging_dd"
    assert proxy_pseudo_state(volume_z=0.2, momentum=0.0) == "dormant_noise"


# ── apply_sentiment_tilt unit tests ────────────────────────────────────────────

def test_apply_sentiment_tilt_basic():
    w = {"QQQ": 0.6, "GLD": 0.4}
    adj = {"QQQ": {"adjustment": 0.02}}
    result = apply_sentiment_tilt(w, adj)
    assert abs(sum(result.values()) - 1.0) < 1e-9
    # QQQ got a positive tilt → higher relative weight
    assert result["QQQ"] > w["QQQ"]


def test_apply_sentiment_tilt_renormalizes_to_one():
    w = {"A": 0.5, "B": 0.3, "C": 0.2}
    adj = {"A": {"adjustment": 0.04}, "B": {"adjustment": -0.03}}
    result = apply_sentiment_tilt(w, adj)
    assert abs(sum(result.values()) - 1.0) < 1e-9


def test_apply_sentiment_tilt_clamped_to_max():
    w = {"X": 0.5, "Y": 0.5}
    # Adjustment of 0.20 exceeds the per-ticker cap; should be clamped to SENTIMENT_MAX_TILT
    adj = {"X": {"adjustment": 0.20}}
    result = apply_sentiment_tilt(w, adj, max_per_ticker=SENTIMENT_MAX_TILT)
    unclamped = apply_sentiment_tilt({"X": 0.5, "Y": 0.5}, {"X": {"adjustment": SENTIMENT_MAX_TILT}})
    assert abs(result["X"] - unclamped["X"]) < 1e-9


def test_apply_sentiment_tilt_no_shorting():
    w = {"A": 0.02, "B": 0.98}
    # Large negative tilt on A — should floor at 0, not go negative
    adj = {"A": {"adjustment": -0.10}}
    result = apply_sentiment_tilt(w, adj)
    assert result.get("A", 0.0) >= 0.0
    assert abs(sum(result.values()) - 1.0) < 1e-9


def test_apply_sentiment_tilt_empty_adjustments_is_identity():
    w = {"QQQ": 0.6, "GLD": 0.4}
    assert apply_sentiment_tilt(w, {}) == w


def test_apply_sentiment_tilt_zero_adjustment_unchanged():
    w = {"QQQ": 0.6, "GLD": 0.4}
    adj = {"QQQ": {"adjustment": 0.0}}
    result = apply_sentiment_tilt(w, adj)
    assert abs(result["QQQ"] - w["QQQ"]) < 1e-9


def test_apply_sentiment_tilt_new_ticker_added():
    # Sentiment data for a ticker not in current weights → gets positive weight allocation
    w = {"QQQ": 0.6, "GLD": 0.4}
    adj = {"NVDA": {"adjustment": 0.03}}
    result = apply_sentiment_tilt(w, adj)
    assert "NVDA" in result
    assert result["NVDA"] > 0.0
    assert abs(sum(result.values()) - 1.0) < 1e-9


def test_apply_sentiment_tilt_negative_tilt_on_absent_ticker_ignored():
    # Negative adjustment for a ticker not in weights → 0.0 weight, floored; should not crash
    w = {"QQQ": 0.6, "GLD": 0.4}
    adj = {"AMZN": {"adjustment": -0.04}}  # not in w → 0 + (-0.04) = clamped to 0
    result = apply_sentiment_tilt(w, adj)
    assert result.get("AMZN", 0.0) >= 0.0
    assert abs(sum(result.values()) - 1.0) < 1e-9


# ── CrowdTactic with_sentiment extension tests ─────────────────────────────────

def test_crowd_tactic_default_tactic_id():
    tac = CrowdTactic(CORE, mode="proxy")
    assert tac.tactic_id == "crowd_signal_tactic"
    assert not tac.with_sentiment


def test_crowd_tactic_sentiment_tactic_id():
    tac = CrowdTactic(CORE, mode="proxy", with_sentiment=True,
                      sentiment_adjustments={"QQQ": {"adjustment": 0.02}})
    assert tac.tactic_id == "crowd_signal_tactic_sentiment"
    assert tac.with_sentiment


def test_crowd_tactic_custom_tactic_id():
    tac = CrowdTactic(CORE, mode="proxy", tactic_id="crowd_signal_only",
                      name="Crowd Signal (proxy, no sentiment)")
    assert tac.tactic_id == "crowd_signal_only"
    assert tac.name == "Crowd Signal (proxy, no sentiment)"


def test_crowd_tactic_with_sentiment_applies_tilt():
    # No price panel available in proxy mode → no crowd states → sleeve is empty
    # So without sentiment: weights == CORE
    tac_no_sent = CrowdTactic(CORE, mode="proxy")
    w_no_sent = tac_no_sent.target_weights_asof("2026-01-01", ctx=None)

    # With sentiment: tilt QQQ up slightly
    adj = {"QQQ": {"adjustment": 0.03}}
    tac_sent = CrowdTactic(CORE, mode="proxy", with_sentiment=True, sentiment_adjustments=adj)
    w_sent = tac_sent.target_weights_asof("2026-01-01", ctx=None)

    assert abs(sum(w_sent.values()) - 1.0) < 1e-9
    # With a positive tilt on QQQ, its weight should increase vs no-sentiment
    assert w_sent["QQQ"] > w_no_sent["QQQ"]


def test_crowd_tactic_sentiment_off_no_tilt():
    # with_sentiment=False → tilt never applied even if adjustments provided
    adj = {"QQQ": {"adjustment": 0.04}}
    tac = CrowdTactic(CORE, mode="proxy", with_sentiment=False, sentiment_adjustments=adj)
    w = tac.target_weights_asof("2026-01-01", ctx=None)
    # Without panel, crowd states are empty → same as CORE renormalized
    core_sum = sum(CORE.values())
    assert abs(w["QQQ"] - CORE["QQQ"] / core_sum) < 1e-9


def test_crowd_tactic_sentiment_metadata_flag():
    tac = CrowdTactic(CORE, mode="proxy", with_sentiment=True,
                      sentiment_adjustments={"GLD": {"adjustment": 0.01}})
    assert tac.metadata["with_sentiment"] is True
