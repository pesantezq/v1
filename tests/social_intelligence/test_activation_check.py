"""
Tests for the Crowd Radar activation checklist
(portfolio_automation/social_intelligence/activation_check.py).

Acceptance gatekeepers:
  - missing Reddit creds → a 'not ready' artifact, never a crash
  - crowd_radar.enabled=false → a 'disabled' artifact
  - enabled=true with missing creds → still no official-output mutation
  - the activation artifact can never carry a trade verb / is decision-blocked
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.social_intelligence.activation_check import (
    build_activation_check,
    render_activation_check_md,
    run_activation_check,
)
from portfolio_automation.social_intelligence.base import FORBIDDEN_TRADE_VERBS

ARTIFACT_REL = "outputs/sandbox/discovery/crowd_radar_activation_check.json"
REQUIRED_FIELDS = (
    "enabled", "credentials_present", "source_status", "source_terms_status",
    "rate_limit_configured", "raw_text_storage_allowed", "ai_processing_allowed",
    "sandbox_only_assertion", "decision_engine_blocked", "last_smoke_test_status",
    "ready_to_collect", "warnings",
)


def _write_config(root: Path, enabled: bool):
    cfg = {"crowd_radar": {"enabled": enabled, "sources": ["reddit"]}}
    (root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def _clear_reddit_env(monkeypatch):
    for k in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("STOCKBOT_CROWD_RADAR_DISABLED", raising=False)


def _set_reddit_env(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
    monkeypatch.setenv("REDDIT_USER_AGENT", "stockbot-test/0.1")
    monkeypatch.delenv("STOCKBOT_CROWD_RADAR_DISABLED", raising=False)


# --- Test 1: missing creds → not-ready artifact, no crash -------------------

def test_missing_credentials_produces_artifact_not_crash(tmp_path, monkeypatch):
    _write_config(tmp_path, enabled=True)  # enabled, but no creds
    _clear_reddit_env(monkeypatch)

    result = run_activation_check(root=tmp_path)  # must not raise

    artifact = tmp_path / ARTIFACT_REL
    assert artifact.exists(), "activation artifact must be written even without creds"
    payload = json.loads(artifact.read_text())
    for field in REQUIRED_FIELDS:
        assert field in payload, f"missing required field: {field}"
    assert payload["credentials_present"] is False
    assert payload["ready_to_collect"] is False
    assert payload["source_status"] == "no_credentials"
    assert "REDDIT_* credentials not set" in payload["warnings"]
    assert result["status"] == "no_credentials"


# --- Test 2: enabled=false → disabled artifact ------------------------------

def test_disabled_flag_produces_disabled_artifact(tmp_path, monkeypatch):
    _write_config(tmp_path, enabled=False)
    _set_reddit_env(monkeypatch)  # creds present, but feature off

    payload = build_activation_check(tmp_path)

    assert payload["enabled"] is False
    assert payload["source_status"] == "disabled"
    assert payload["ready_to_collect"] is False
    assert "crowd_radar.enabled=false" in payload["warnings"]


def test_kill_switch_env_forces_disabled(tmp_path, monkeypatch):
    _write_config(tmp_path, enabled=True)
    _set_reddit_env(monkeypatch)
    monkeypatch.setenv("STOCKBOT_CROWD_RADAR_DISABLED", "1")

    payload = build_activation_check(tmp_path)

    assert payload["source_status"] == "disabled"
    assert payload["ready_to_collect"] is False
    assert "kill_switch_active" in payload["warnings"]


# --- Test 3: enabled + missing creds → no official mutation -----------------

def test_enabled_missing_creds_no_official_mutation(tmp_path, monkeypatch):
    _write_config(tmp_path, enabled=True)
    _clear_reddit_env(monkeypatch)

    # Seed the official decision artifacts and snapshot their bytes.
    latest = tmp_path / "outputs" / "latest"
    portfolio = tmp_path / "outputs" / "portfolio"
    latest.mkdir(parents=True)
    portfolio.mkdir(parents=True)
    decision = latest / "decision_plan.json"
    snapshot = portfolio / "portfolio_snapshot.json"
    decision.write_text('{"decisions": ["UNTOUCHED"]}', encoding="utf-8")
    snapshot.write_text('{"holdings": ["UNTOUCHED"]}', encoding="utf-8")
    before = (decision.read_bytes(), snapshot.read_bytes())

    run_activation_check(root=tmp_path)

    after = (decision.read_bytes(), snapshot.read_bytes())
    assert before == after, "activation check must not touch official outputs"
    # Only the sandbox artifact may appear.
    assert (tmp_path / ARTIFACT_REL).exists()


# --- Test 4: artifact can never carry a trade verb / is decision-blocked ----

def test_no_trade_verbs_and_decision_engine_blocked(tmp_path, monkeypatch):
    _write_config(tmp_path, enabled=True)
    _set_reddit_env(monkeypatch)

    payload = build_activation_check(tmp_path)
    md = render_activation_check_md(payload)

    # Invariants are hardcoded-true and cannot be flipped at activation time.
    assert payload["decision_engine_blocked"] is True
    assert payload["no_trade"] is True
    assert payload["sandbox_only_assertion"] is True

    # No forbidden trade verb may appear as a value token in the payload or MD.
    blob = (json.dumps(payload) + " " + md).lower()
    for verb in ("buy", "sell", "hold", "rebalance", "promote"):
        assert verb in FORBIDDEN_TRADE_VERBS  # guard the guard
        # whole-word check (avoid 'household' etc. — none of these substring-collide
        # with our field names, but be explicit)
        assert f' {verb} ' not in f" {blob} ", f"forbidden trade verb leaked: {verb}"
