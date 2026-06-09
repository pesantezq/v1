"""Phase 10 — broker-aware holdings resolver (side-panel only, no writes/trades)."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import portfolio_automation.holdings_resolver as hr


def _now():
    return datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


def _cfg(tmp_path, enabled, holdings=None, cash=500.0):
    (tmp_path / "outputs" / "latest").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.json").write_text(json.dumps({"portfolio": {
        "holdings": holdings if holdings is not None else [
            {"symbol": "QQQ", "shares": 10, "target_weight": 0.5},
            {"symbol": "QLD", "shares": 2, "is_leveraged": True}],
        "cash_available": cash, "broker_aware": {"enabled": enabled}}}))


def _broker(tmp_path, ts):
    L = tmp_path / "outputs" / "latest"
    L.joinpath("schwab_positions.json").write_text(json.dumps({"positions": [
        {"symbol": "QQQ", "quantity": 10, "market_value": 4000.0},
        {"symbol": "QLD", "quantity": 2, "market_value": 1000.0}]}))
    L.joinpath("schwab_portfolio_snapshot.json").write_text(json.dumps({
        "snapshot_timestamp": ts, "totals": {"market_value": 5000.0, "cash": 500.0}}))


def test_flag_off_uses_config(tmp_path):
    _cfg(tmp_path, enabled=False)
    _broker(tmp_path, _now().isoformat())
    res = hr.resolve_holdings(tmp_path, now=_now())
    assert res["holdings_source"] == "config"
    assert res["reason"] == "broker_aware_disabled"


def test_fresh_broker_preferred_when_enabled(tmp_path):
    _cfg(tmp_path, enabled=True)
    _broker(tmp_path, _now().isoformat())
    res = hr.resolve_holdings(tmp_path, now=_now())
    assert res["holdings_source"] == "broker"
    assert res["confidence_modifier"] == 1.0


def test_stale_broker_falls_back_with_lower_confidence(tmp_path):
    _cfg(tmp_path, enabled=True)
    _broker(tmp_path, (_now() - timedelta(days=3)).isoformat())
    res = hr.resolve_holdings(tmp_path, now=_now())
    assert res["holdings_source"] == "config"
    assert res["confidence_modifier"] < 1.0
    assert res["reason"] == "broker_data_stale"


def test_missing_broker_falls_back(tmp_path):
    _cfg(tmp_path, enabled=True)
    res = hr.resolve_holdings(tmp_path, now=_now())
    assert res["holdings_source"] == "config"
    assert res["confidence_modifier"] < 1.0


def test_side_panel_metrics_from_broker(tmp_path):
    _cfg(tmp_path, enabled=True)
    _broker(tmp_path, _now().isoformat())
    hr.write_broker_aware_portfolio(tmp_path, _now())
    p = json.loads((tmp_path / "outputs" / "portfolio" / "broker_aware_portfolio.json").read_text())
    assert p["holdings_source"] == "broker"
    assert p["feeds_decision_plan"] is False  # side-panel only (§23.10)
    assert p["observe_only"] is True
    # QQQ 4000 + QLD 1000 + cash 500 = 5500 → QQQ ~0.727
    assert abs(p["allocation"]["QQQ"] - round(4000 / 5500, 4)) < 1e-3
    assert p["leverage"]["leveraged_exposure"] > 0  # QLD is leveraged
    assert p["degraded_mode"] is False


def test_side_panel_degrades_on_config_only(tmp_path):
    _cfg(tmp_path, enabled=True)  # enabled but no broker data
    hr.write_broker_aware_portfolio(tmp_path, _now())
    p = json.loads((tmp_path / "outputs" / "portfolio" / "broker_aware_portfolio.json").read_text())
    assert p["holdings_source"] == "config"
    assert p["degraded_mode"] is True
    assert p["concentration"]["available"] is False


def test_never_writes_decision_plan(tmp_path):
    _cfg(tmp_path, enabled=True)
    _broker(tmp_path, _now().isoformat())
    hr.write_broker_aware_portfolio(tmp_path, _now())
    assert not (tmp_path / "outputs" / "latest" / "decision_plan.json").exists()
