"""Characterization test: strategy comparator uses broker context when broker holdings present."""
import json
from datetime import datetime, timezone
from pathlib import Path
from portfolio_automation.strategy import strategy_comparator as sc


def _setup(tmp_path):
    L = tmp_path / "outputs" / "latest"
    L.mkdir(parents=True)
    (tmp_path / "config.json").write_text(json.dumps(
        {"portfolio": {"broker_aware": {"enabled": True}, "holdings": [], "cash_available": 50.0}}))
    (L / "schwab_positions.json").write_text(json.dumps({"positions": [
        {"symbol": "AAA", "quantity": 10, "market_value": 1500.0, "average_cost": 100.0}]}))
    (L / "schwab_portfolio_snapshot.json").write_text(json.dumps(
        {"snapshot_timestamp": datetime.now(timezone.utc).isoformat(), "totals": {"cash": 50.0}}))


def test_comparison_uses_broker_context(tmp_path):
    _setup(tmp_path)
    now = datetime.now(timezone.utc)
    out = sc.build_comparison(root=Path(tmp_path), now=now)
    assert out.get("context_source") == "broker"
