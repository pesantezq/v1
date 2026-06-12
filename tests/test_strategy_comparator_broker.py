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


def test_cash_drag_is_dollar_fraction_not_weight_sum(tmp_path):
    _setup(tmp_path)
    from pathlib import Path as _P
    from datetime import datetime, timezone
    ctx = sc._build_context(_P(tmp_path), datetime.now(timezone.utc))
    # broker: cash 50, AAA mv 1500 -> total 1550 -> cash_drag ~0.032 (NOT ~1.0)
    assert ctx["cash_drag"] is not None and ctx["cash_drag"] < 0.1


def test_positions_none_when_not_broker(tmp_path):
    import json
    from pathlib import Path as _P
    from datetime import datetime, timezone
    # broker_aware OFF -> resolver returns config -> tax positions must be None (honest degrade)
    L = tmp_path / "outputs" / "latest"; L.mkdir(parents=True)
    (tmp_path / "config.json").write_text(json.dumps({"portfolio": {"holdings": [], "cash_available": 0.0}}))
    (L / "schwab_positions.json").write_text(json.dumps({"positions": [
        {"symbol": "AAA", "quantity": 10, "market_value": 1500.0, "average_cost": 100.0}]}))
    ctx = sc._build_context(_P(tmp_path), datetime.now(timezone.utc))
    assert ctx["holdings_source"] == "config"
    assert ctx["positions"] is None and ctx["has_tax_lot_data"] is False
