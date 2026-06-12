"""Tests for the simulable-universe resolver."""
from __future__ import annotations

import json

from portfolio_automation.portfolio_sim.universe import resolve_simulable_universe


def _write_config(root, **extra):
    cfg = {"portfolio": {"holdings": [{"symbol": "QQQ"}, {"symbol": "GLD"}]}}
    cfg.update(extra)
    (root / "config.json").write_text(json.dumps(cfg))


def test_includes_holdings_and_default_proxies(tmp_path):
    _write_config(tmp_path)
    u = resolve_simulable_universe(tmp_path)
    assert u["QQQ"]["source"] == "holding" and u["QQQ"]["in_holdings"] is True
    assert "BND" in u and u["BND"]["source"] == "proxy"
    assert u["BND"]["in_holdings"] is False


def test_custom_proxy_list(tmp_path):
    _write_config(tmp_path, portfolio_sim={"universe": {"proxy_etfs": ["AGG"]}})
    u = resolve_simulable_universe(tmp_path)
    assert "AGG" in u
    assert "BND" not in u  # custom list replaces default


def test_missing_config_is_safe(tmp_path):
    u = resolve_simulable_universe(tmp_path)
    # No config → no holdings, but default proxies still resolve, no crash.
    assert isinstance(u, dict)
    assert all(v["source"] in ("holding", "proxy", "universe_list") for v in u.values())
