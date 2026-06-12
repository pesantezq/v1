"""Tests for the social-signal backtest: sample-size gating, benchmark stats."""
from __future__ import annotations

from portfolio_automation.social_intelligence.social_signal_backtest import (
    SignalObservation,
    build_social_signal_backtest,
    evaluate_state,
)


def _obs(state, r5d, ticker="X"):
    return SignalObservation(
        ticker=ticker, crowd_state=state, signal_date="2026-01-01",
        returns={"5D": {"vs_spy": r5d - 0.1, "vs_qqq": r5d - 0.2, "vs_sector": r5d}},
        raw_returns={"1D": r5d / 5, "5D": r5d, "20D": r5d * 2, "60D": r5d * 3},
        max_drawdown=-abs(r5d) / 2, volatility=1.0,
    )


def test_insufficient_sample_labeled():
    block = evaluate_state([_obs("emerging_dd", 1.0)], min_sample=20)
    assert block["confidence_bucket"] == "insufficient_data"
    assert block["reliable"] is False
    assert block["impact"] == "research_priority_only"


def test_sufficient_sample_is_reliable():
    obs = [_obs("emerging_dd", 1.0) for _ in range(25)]
    block = evaluate_state(obs, min_sample=20)
    assert block["reliable"] is True
    assert block["confidence_bucket"] in ("low", "medium", "high")


def test_hit_rate_and_false_positive_rate():
    obs = [_obs("emerging_dd", 2.0) for _ in range(15)] + \
          [_obs("emerging_dd", -1.0) for _ in range(5)]
    block = evaluate_state(obs, min_sample=10)
    h5 = block["by_horizon"]["5D"]
    assert abs(h5["hit_rate"] - 0.75) < 1e-6
    assert abs(block["false_positive_rate"] - 0.25) < 1e-6


def test_build_payload_groups_by_state_and_flags_maturity():
    obs = (
        [_obs("emerging_dd", 1.0) for _ in range(25)]
        + [_obs("hype_acceleration", -2.0) for _ in range(3)]
    )
    payload = build_social_signal_backtest(
        obs, run_id="rid", run_mode="backtest", min_sample=20,
    )
    assert payload["observe_only"] is True
    assert payload["total_observations"] == 28
    assert "emerging_dd" in payload["records"]
    assert payload["records"]["emerging_dd"]["reliable"] is True
    assert payload["records"]["hype_acceleration"]["reliable"] is False
    assert "emerging_dd" in payload["states_matured"]
    assert payload["data_quality_status"] == "ok"


def test_empty_history_insufficient():
    payload = build_social_signal_backtest([], run_id="r", run_mode="backtest")
    assert payload["data_quality_status"] == "insufficient_data"
    assert payload["total_observations"] == 0
