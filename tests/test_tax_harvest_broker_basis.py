import json
from datetime import datetime, timezone
from pathlib import Path
from portfolio_automation import tax_harvest_advisor as tha


def _setup(tmp_path):
    L = tmp_path / "outputs" / "latest"; L.mkdir(parents=True)
    (tmp_path / "config.json").write_text(json.dumps(
        {"portfolio": {"is_taxable_account": True, "broker_aware": {"enabled": True},
                       "holdings": [], "cash_available": 0.0}}))
    (L / "schwab_positions.json").write_text(json.dumps({"positions": [
        {"symbol": "BBB", "quantity": 5, "average_cost": 200.0, "market_value": 800.0}]}))
    (L / "schwab_portfolio_snapshot.json").write_text(json.dumps(
        {"snapshot_timestamp": datetime.now(timezone.utc).isoformat(), "totals": {"cash": 0.0}}))


def test_broker_basis_harvest(tmp_path):
    _setup(tmp_path)
    plan = tha.run_tax_harvest_advisor(tmp_path, base_dir=tmp_path / "outputs")
    assert plan["basis_source"] == "broker"
    assert plan["harvestable_count"] == 1
    row = next(r for r in plan["positions"] if r["symbol"] == "BBB")
    assert row["harvest_recommended"] is True and row["loss_dollars"] == 200.0


def test_config_basis_when_broker_off(tmp_path):
    L = tmp_path / "outputs" / "latest"; L.mkdir(parents=True)
    (tmp_path / "config.json").write_text(json.dumps(
        {"portfolio": {"is_taxable_account": True, "holdings": [
            {"symbol": "CCC", "shares": 2, "cost_basis": 100.0}]}}))
    plan = tha.run_tax_harvest_advisor(tmp_path, base_dir=tmp_path / "outputs",
                                       price_overrides={"CCC": 40.0})
    assert plan["basis_source"] == "config"
