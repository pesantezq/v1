"""Tests for the portfolio-sim shared envelope."""
from __future__ import annotations

from portfolio_automation.portfolio_sim.sim_base import SimStatus, sim_envelope


def test_envelope_stamps_observe_only_invariants():
    env = sim_envelope(run_id="r1", run_mode="discovery")
    assert env["observe_only"] is True
    assert env["sandbox_only"] is True
    assert env["no_trade"] is True
    assert env["schema_version"] == "1"
    assert env["source"] == "portfolio_sim"
    assert env["run_id"] == "r1"
    assert env["run_mode"] == "discovery"
    assert env["status"] == "ok"
    assert "created_at" in env
    assert env["warnings"] == []


def test_envelope_status_and_warnings():
    env = sim_envelope(run_id="r", run_mode="backtest",
                       status=SimStatus.INSUFFICIENT_DATA.value, warnings=["w1"])
    assert env["status"] == "insufficient_data"
    assert env["warnings"] == ["w1"]


def test_sim_status_vocabulary():
    assert {s.value for s in SimStatus} == {
        "ok", "insufficient_data", "degraded", "error", "disabled"}
