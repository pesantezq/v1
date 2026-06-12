import json
from datetime import datetime, timezone
from pathlib import Path
from portfolio_automation import holdings_resolver as hr


def _setup(tmp_path, positions):
    L = tmp_path / "outputs" / "latest"; L.mkdir(parents=True)
    (tmp_path / "config.json").write_text(json.dumps(
        {"portfolio": {"broker_aware": {"enabled": True}, "holdings": [], "cash_available": 100.0}}))
    (L / "schwab_positions.json").write_text(json.dumps({"positions": positions}))
    (L / "schwab_portfolio_snapshot.json").write_text(json.dumps(
        {"snapshot_timestamp": datetime.now(timezone.utc).isoformat(), "totals": {"cash": 100.0}}))


def test_broker_holdings_carry_cost_basis(tmp_path):
    _setup(tmp_path, [{"symbol": "AAA", "quantity": 10, "market_value": 1500.0, "average_cost": 100.0}])
    res = hr.resolve_holdings(tmp_path, now=datetime.now(timezone.utc))
    assert res["holdings_source"] == "broker"
    h = res["holdings"][0]
    assert h["average_cost"] == 100.0
    assert h["cost_basis"] == 1000.0
    assert h["market_value"] == 1500.0


def test_broker_holdings_cost_basis_none_when_avg_missing(tmp_path):
    _setup(tmp_path, [{"symbol": "BBB", "quantity": 5, "market_value": 250.0}])
    res = hr.resolve_holdings(tmp_path, now=datetime.now(timezone.utc))
    h = res["holdings"][0]
    assert h["average_cost"] is None and h["cost_basis"] is None
