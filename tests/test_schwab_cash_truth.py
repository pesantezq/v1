"""Tests for the Schwab-cash-truth wiring:
  - resolve_decision_cash (broker wins + reconcile ledger; stale falls back)
  - risk_delta_advisor + correlation_risk_advisor sourcing Schwab holdings.
All read-only; no trades, no broker writes, no production-state mutation.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from portfolio_automation.holdings_resolver import resolve_decision_cash


# ---------------------------------------------------------------------------
# resolve_decision_cash — broker wins + reconcile ledger policy
# ---------------------------------------------------------------------------

class TestResolveDecisionCash:
    def test_broker_fresh_wins_and_reconciles(self):
        cash, entry = resolve_decision_cash(
            ledger_balance=464.16, broker_fresh=True, broker_cash=3150.60, config_cash=150.6)
        assert cash == pytest.approx(3150.60, abs=0.01)
        assert entry == ("broker_reconcile", pytest.approx(2686.44, abs=0.01),
                         "reconcile ledger to fresh Schwab balance")

    def test_broker_stale_falls_back_to_ledger(self):
        cash, entry = resolve_decision_cash(
            ledger_balance=464.16, broker_fresh=False, broker_cash=None, config_cash=150.6)
        assert cash == pytest.approx(464.16, abs=0.01)
        assert entry is None  # ledger untouched when broker unavailable

    def test_empty_ledger_seeds_from_broker(self):
        cash, entry = resolve_decision_cash(
            ledger_balance=None, broker_fresh=True, broker_cash=3150.60, config_cash=150.6)
        assert cash == pytest.approx(3150.60, abs=0.01)
        assert entry[0] == "seed" and "broker" in entry[2]

    def test_empty_ledger_seeds_from_config_when_no_broker(self):
        cash, entry = resolve_decision_cash(
            ledger_balance=None, broker_fresh=False, broker_cash=None, config_cash=150.6)
        assert cash == pytest.approx(150.6, abs=0.01)
        assert entry[0] == "seed" and "config" in entry[2]

    def test_no_reconcile_when_already_matches(self):
        cash, entry = resolve_decision_cash(
            ledger_balance=3150.60, broker_fresh=True, broker_cash=3150.60, config_cash=150.6)
        assert cash == pytest.approx(3150.60, abs=0.01)
        assert entry is None  # within 1 cent -> no churn entry


# ---------------------------------------------------------------------------
# Advisor broker-sourcing — risk_delta + correlation
# ---------------------------------------------------------------------------

def _fresh_ts() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


def _stale_ts() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()


def _setup_root(tmp_path, *, broker_ts: str | None):
    """config (broker_aware on) + decision_plan + optional Schwab snapshot."""
    (tmp_path / "outputs" / "latest").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.json").write_text(json.dumps({
        "portfolio": {
            "holdings": [
                {"symbol": "QQQ", "shares": 6, "target_weight": 0.35, "leverage_factor": 1},
                {"symbol": "QLD", "shares": 8, "target_weight": 0.05, "is_leveraged": True, "leverage_factor": 2},
                {"symbol": "GLD", "shares": 4, "target_weight": 0.20, "leverage_factor": 1},
            ],
            "cash_available": 150.6, "target_cash_weight": 0.05,
            "broker_aware": {"enabled": True},
        },
        "growth_mode": {"concentration_cap": 0.6, "leverage_cap": 0.25},
    }))
    (tmp_path / "outputs" / "latest" / "decision_plan.json").write_text(json.dumps({
        "portfolio_context": {"total_portfolio_value": 10544.53, "cash": 3150.6},
    }))
    if broker_ts:
        L = tmp_path / "outputs" / "latest"
        L.joinpath("schwab_positions.json").write_text(json.dumps({"positions": [
            {"symbol": "QQQ", "quantity": 6, "market_value": 6200.0, "average_cost": 900.0},
            {"symbol": "QLD", "quantity": 8, "market_value": 1100.0, "average_cost": 120.0},
            {"symbol": "GLD", "quantity": 4, "market_value": 93.93, "average_cost": 20.0},
        ]}))
        L.joinpath("schwab_portfolio_snapshot.json").write_text(json.dumps({
            "snapshot_timestamp": broker_ts, "totals": {"market_value": 10544.53, "cash": 3150.6},
        }))


class TestRiskDeltaBrokerSourcing:
    def test_uses_broker_when_fresh(self, tmp_path):
        from portfolio_automation.risk_delta_advisor import _load_holdings
        _setup_root(tmp_path, broker_ts=_fresh_ts())
        holdings, pv, source = _load_holdings(tmp_path)
        assert source == "broker"
        assert pv == pytest.approx(10544.53, abs=0.01)  # from decision_plan ctx
        # leverage metadata preserved from config via overlay
        qld = next(h for h in holdings if h.get("symbol") == "QLD")
        assert qld.get("is_leveraged") is True

    def test_falls_back_to_config_when_stale(self, tmp_path):
        from portfolio_automation.risk_delta_advisor import _load_holdings
        _setup_root(tmp_path, broker_ts=_stale_ts())
        _, _, source = _load_holdings(tmp_path)
        assert source == "config"

    def test_falls_back_to_config_when_absent(self, tmp_path):
        from portfolio_automation.risk_delta_advisor import _load_holdings
        _setup_root(tmp_path, broker_ts=None)
        _, _, source = _load_holdings(tmp_path)
        assert source == "config"


class TestCorrelationBrokerSourcing:
    def test_uses_broker_market_value_weights(self, tmp_path):
        from portfolio_automation.correlation_risk_advisor import _holdings_with_weights
        _setup_root(tmp_path, broker_ts=_fresh_ts())
        weights, source = _holdings_with_weights(tmp_path)
        assert source == "broker"
        # QQQ market value 6200 of 7393.93 total -> dominant weight (~0.84), NOT config 0.35
        assert weights["QQQ"] > 0.7
        assert abs(sum(weights.values()) - 1.0) < 1e-6

    def test_falls_back_to_config_target_weight(self, tmp_path):
        from portfolio_automation.correlation_risk_advisor import _holdings_with_weights
        _setup_root(tmp_path, broker_ts=None)
        weights, source = _holdings_with_weights(tmp_path)
        assert source == "config"
        # normalized config target weights (QQQ 0.35 of 0.60 active) -> ~0.583
        assert weights["QQQ"] == pytest.approx(0.35 / 0.60, abs=0.01)
