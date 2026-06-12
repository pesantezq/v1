"""Tests for rebalance policies."""
from __future__ import annotations

from portfolio_automation.portfolio_sim.rebalance import (
    BuyAndHold,
    ConfigRules,
    Periodic,
    make_policy,
)


def test_buy_and_hold_only_initial_then_drifts():
    p = BuyAndHold()
    assert p.due("2026-01-01", None) is True
    assert p.due("2026-02-01", "2026-01-01") is False
    # cash routed to target; existing values untouched (drift preserved)
    out = p.apply({"A": 60.0, "B": 40.0}, {"A": 0.5, "B": 0.5}, "2026-02-01", 10.0)
    assert out["A"] == 65.0 and out["B"] == 45.0


def test_periodic_due_monthly_and_resets():
    p = Periodic("monthly")
    assert p.due("2026-02-01", "2026-01-31") is True
    assert p.due("2026-01-15", "2026-01-01") is False
    out = p.apply({"A": 80.0, "B": 20.0}, {"A": 0.5, "B": 0.5}, "2026-02-01", 0.0)
    assert abs(out["A"] - 50.0) < 1e-9 and abs(out["B"] - 50.0) < 1e-9


def test_config_rules_band_and_cash_first():
    p = ConfigRules({"band_threshold": 0.12, "use_cash_before_selling": True})
    # A is overweight (70 vs target 50) but cash-first + incoming cash → no forced sale
    out = p.apply({"A": 70.0, "B": 30.0}, {"A": 0.5, "B": 0.5}, "2026-02-01", 20.0)
    # cash deployed to underweight B first
    assert out["B"] > 30.0
    assert out["A"] >= 70.0  # not sold


def test_config_rules_within_band_no_change():
    p = ConfigRules({"band_threshold": 0.12})
    out = p.apply({"A": 52.0, "B": 48.0}, {"A": 0.5, "B": 0.5}, "2026-02-01", 0.0)
    # drift 0.02 < band → untouched
    assert out["A"] == 52.0 and out["B"] == 48.0


def test_make_policy():
    assert make_policy("buy_and_hold").name == "buy_and_hold"
    assert make_policy("periodic").name == "periodic"
    assert make_policy("config_rules", rebalance_rules={}).name == "config_rules"
