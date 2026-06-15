"""
Tests for the multi-source Crowd Radar activation checklist
(portfolio_automation/social_intelligence/activation_check.py).

Gatekeepers:
  - crowd_radar.enabled=false → disabled, not ready (no crash)
  - enabled + ApeWisdom active → ready_to_collect true, all 16 fields present
  - kill-switch forces disabled
  - run never mutates official outputs
  - artifact carries the no-trade / sandbox invariants and no trade verbs
"""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.social_intelligence.activation_check import (
    build_activation_check,
    render_activation_check_md,
    run_activation_check,
)
from portfolio_automation.social_intelligence.base import FORBIDDEN_TRADE_VERBS

ARTIFACT_REL = "outputs/sandbox/discovery/crowd_radar_activation_check.json"
REQUIRED_FIELDS = (
    "enabled", "cost_policy", "allow_paid_sources", "active_sources", "probe_only_sources",
    "blocked_sources", "credentials_present", "entitlements_confirmed", "api_docs_audited",
    "rate_limit_configured", "raw_text_storage_allowed", "ai_processing_allowed",
    "sandbox_only_assertion", "decision_engine_blocked", "ready_to_collect", "warnings",
)


def _write_config(root: Path, enabled: bool):
    cfg = {"crowd_radar": {
        "enabled": enabled, "cost_policy": "no_extra_cost", "allow_paid_sources": False,
        "source_policy": {
            "apewisdom": {"enabled": True, "max_pages": 1},
            "fmp_social_sentiment": {"enabled": True, "entitlement_probe_only_until_confirmed": True},
            "stocktwits": {"enabled": False, "probe_only": True},
            "finnhub_social": {"enabled": False, "probe_only": True},
            "quiver_wsb": {"enabled": False, "blocked_no_extra_cost": True},
        },
    }}
    (root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def _clear(monkeypatch):
    for k in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT",
              "FMP_API_KEY", "FINNHUB_API_KEY", "QUIVER_API_KEY", "STOCKTWITS_TOKEN",
              "STOCKTWITS_API_KEY", "STOCKBOT_CROWD_RADAR_DISABLED"):
        monkeypatch.delenv(k, raising=False)


def test_disabled_produces_disabled_artifact_no_crash(tmp_path, monkeypatch):
    _write_config(tmp_path, enabled=False)
    _clear(monkeypatch)
    result = run_activation_check(root=tmp_path)
    artifact = tmp_path / ARTIFACT_REL
    assert artifact.exists()
    payload = json.loads(artifact.read_text())
    for f in REQUIRED_FIELDS:
        assert f in payload, f"missing {f}"
    assert payload["enabled"] is False
    assert payload["ready_to_collect"] is False
    assert payload["source_status"] == "disabled"
    assert "crowd_radar.enabled=false" in payload["warnings"]
    assert result["status"] == "disabled"


def test_enabled_with_active_apewisdom_is_ready(tmp_path, monkeypatch):
    _write_config(tmp_path, enabled=True)
    _clear(monkeypatch)
    payload = build_activation_check(tmp_path)
    assert "apewisdom" in payload["active_sources"]
    assert set(payload["probe_only_sources"]) == {"fmp_social_sentiment", "finnhub_social"}
    assert set(payload["blocked_sources"]) == {"stocktwits", "quiver_wsb"}
    assert payload["ready_to_collect"] is True
    assert payload["cost_policy"] == "no_extra_cost"
    assert payload["allow_paid_sources"] is False


def test_kill_switch_env_forces_disabled(tmp_path, monkeypatch):
    _write_config(tmp_path, enabled=True)
    _clear(monkeypatch)
    monkeypatch.setenv("STOCKBOT_CROWD_RADAR_DISABLED", "1")
    payload = build_activation_check(tmp_path)
    assert payload["ready_to_collect"] is False
    assert "kill_switch_active" in payload["warnings"]


def test_no_official_mutation(tmp_path, monkeypatch):
    _write_config(tmp_path, enabled=True)
    _clear(monkeypatch)
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    decision = tmp_path / "outputs" / "latest" / "decision_plan.json"
    decision.write_text('{"x": "UNTOUCHED"}', encoding="utf-8")
    before = decision.read_bytes()
    run_activation_check(root=tmp_path)
    assert decision.read_bytes() == before


def test_invariants_and_no_trade_verbs(tmp_path, monkeypatch):
    _write_config(tmp_path, enabled=True)
    _clear(monkeypatch)
    payload = build_activation_check(tmp_path)
    md = render_activation_check_md(payload)
    assert payload["decision_engine_blocked"] is True
    assert payload["sandbox_only_assertion"] is True
    assert payload["raw_text_storage_allowed"] is False
    blob = (json.dumps(payload) + " " + md).lower()
    for verb in ("buy", "sell", "hold", "rebalance", "promote"):
        assert verb in FORBIDDEN_TRADE_VERBS
        assert f" {verb} " not in f" {blob} "
