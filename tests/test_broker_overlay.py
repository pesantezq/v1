import json
from datetime import datetime, timezone
from pathlib import Path
from portfolio_automation import holdings_resolver as hr


def _setup(tmp, positions, enabled=True):
    L = tmp / "outputs" / "latest"; L.mkdir(parents=True)
    (tmp / "config.json").write_text(json.dumps({"portfolio": {"broker_aware": {"enabled": enabled}}}))
    (L / "schwab_positions.json").write_text(json.dumps({"positions": positions}))
    (L / "schwab_portfolio_snapshot.json").write_text(json.dumps(
        {"snapshot_timestamp": datetime.now(timezone.utc).isoformat(), "totals": {"cash": 150.6}}))


_BLOCK = {"holdings": [
    {"symbol": "QQQ", "shares": 6, "target_weight": 0.35, "asset_class": "us_equity",
     "is_leveraged": False, "leverage_factor": 1},
    {"symbol": "NASA", "shares": 14, "target_weight": 0.10, "asset_class": "us_equity",
     "is_leveraged": False, "leverage_factor": 1},
    {"symbol": "VFH", "shares": 0, "target_weight": 0.15, "asset_class": "us_equity_sector",
     "is_leveraged": False, "leverage_factor": 1},
], "cash_available": 464.16, "target_cash_weight": 0.05}


def test_overlay_broker_preferred_preserves_metadata(tmp_path):
    _setup(tmp_path, [{"symbol": "QQQ", "quantity": 6, "market_value": 4200.0, "average_cost": 700.0},
                      {"symbol": "NASA", "quantity": 15, "market_value": 300.0, "average_cost": 20.0}])
    out = hr.broker_overlaid_portfolio(_BLOCK, tmp_path)
    assert out["holdings_source"] == "broker"
    by = {h["symbol"]: h for h in out["holdings"]}
    assert by["NASA"]["shares"] == 15
    assert by["NASA"]["target_weight"] == 0.10
    assert "VFH" in by and by["VFH"]["shares"] == 0
    assert out["cash_available"] == 150.6


def test_overlay_adds_broker_only_symbol_with_defaults(tmp_path):
    _setup(tmp_path, [{"symbol": "QQQ", "quantity": 6, "market_value": 4200.0, "average_cost": 700.0},
                      {"symbol": "CHAT", "quantity": 4, "market_value": 100.0, "average_cost": 24.0}])
    out = hr.broker_overlaid_portfolio(_BLOCK, tmp_path)
    by = {h["symbol"]: h for h in out["holdings"]}
    assert by["CHAT"]["shares"] == 4 and by["CHAT"]["target_weight"] == 0.0
    assert by["CHAT"]["asset_class"] == "us_equity"


def test_overlay_config_fallback_when_disabled(tmp_path):
    _setup(tmp_path, [{"symbol": "QQQ", "quantity": 6}], enabled=False)
    out = hr.broker_overlaid_portfolio(_BLOCK, tmp_path)
    assert out["holdings_source"] == "config"
    assert out["holdings"] == _BLOCK["holdings"] and out["cash_available"] == 464.16


def test_overlay_config_fallback_when_stale(tmp_path):
    L = tmp_path / "outputs" / "latest"; L.mkdir(parents=True)
    (tmp_path / "config.json").write_text(json.dumps({"portfolio": {"broker_aware": {"enabled": True}}}))
    (L / "schwab_positions.json").write_text(json.dumps({"positions": [{"symbol": "QQQ", "quantity": 6}]}))
    (L / "schwab_portfolio_snapshot.json").write_text(json.dumps(
        {"snapshot_timestamp": "2020-01-01T00:00:00+00:00", "totals": {"cash": 0}}))
    out = hr.broker_overlaid_portfolio(_BLOCK, tmp_path)
    assert out["holdings_source"] == "config"


def test_overlay_never_raises_on_garbage(tmp_path):
    out = hr.broker_overlaid_portfolio({"holdings": "bad"}, tmp_path)
    assert out["holdings_source"] in ("config", "broker")


def test_main_module_imports_overlay():
    src = Path("watchlist_scanner/__main__.py").read_text(encoding="utf-8")
    assert "broker_overlaid_portfolio" in src
