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


def test_apply_overlay_to_config_object(tmp_path, monkeypatch):
    import portfolio_automation.holdings_resolver as hrmod
    from utils import load_config
    monkeypatch.setattr(hrmod, "resolve_holdings", lambda root, now=None: {
        "holdings_source": "broker", "confidence_modifier": 1.0, "cash": 150.6,
        "holdings": [{"symbol": "NASA", "quantity": 15}, {"symbol": "QQQ", "quantity": 6}]})
    cfg = load_config("/opt/stockbot/config.json")
    cfg2 = hrmod.apply_broker_overlay_to_config(cfg, str(tmp_path))
    by = {h.symbol: h for h in cfg2.holdings}
    assert by["NASA"].shares == 15
    assert by["QQQ"].target_weight == 0.35   # config metadata preserved


def test_apply_overlay_writes_source_artifact(tmp_path, monkeypatch):
    import json as _json
    import portfolio_automation.holdings_resolver as hrmod
    from utils import load_config
    monkeypatch.setattr(hrmod, "resolve_holdings", lambda root, now=None: {
        "holdings_source": "broker", "confidence_modifier": 1.0, "cash": 150.6,
        "holdings": [{"symbol": "QQQ", "quantity": 6}]})
    cfg = load_config("/opt/stockbot/config.json")
    hrmod.apply_broker_overlay_to_config(cfg, str(tmp_path))
    p = tmp_path / "outputs" / "latest" / "decision_holdings_source.json"
    assert p.exists() and _json.loads(p.read_text())["holdings_source"] == "broker"


def test_apply_overlay_config_fallback_returns_unchanged(tmp_path, monkeypatch):
    import portfolio_automation.holdings_resolver as hrmod
    from utils import load_config
    monkeypatch.setattr(hrmod, "resolve_holdings", lambda root, now=None: {"holdings_source": "config", "confidence_modifier": 0.8})
    cfg = load_config("/opt/stockbot/config.json")
    before = [(h.symbol, h.shares) for h in cfg.holdings]
    cfg2 = hrmod.apply_broker_overlay_to_config(cfg, str(tmp_path))
    assert [(h.symbol, h.shares) for h in cfg2.holdings] == before


def test_apply_overlay_records_config_source_on_fallback(tmp_path, monkeypatch):
    # Telemetry must reflect THIS run: a config-fallback (stale broker) run must
    # write holdings_source="config", not leave a stale "broker" value from a
    # prior successful overlay. This is what the daily check's
    # decision_on_config_while_broker_ok signal reads.
    import json as _json
    import portfolio_automation.holdings_resolver as hrmod
    from utils import load_config
    monkeypatch.setattr(hrmod, "resolve_holdings", lambda root, now=None: {
        "holdings_source": "config", "confidence_modifier": 0.8,
        "reason": "broker_data_stale"})
    cfg = load_config("/opt/stockbot/config.json")
    hrmod.apply_broker_overlay_to_config(cfg, str(tmp_path))
    p = tmp_path / "outputs" / "latest" / "decision_holdings_source.json"
    assert p.exists()
    payload = _json.loads(p.read_text())
    assert payload["holdings_source"] == "config"
    assert payload["reason"] == "broker_data_stale"
