"""Phase 18 tests — live-SEC-activation readiness assessment (read-only)."""

from __future__ import annotations

from portfolio_automation.institutional_intelligence import health as h

_READY_CFG = {"sec_requests_per_second": 5, "feeds_decision_engine": False,
              "production_gated": True}


def test_ready_when_all_preconditions_met():
    r = h.assess_activation_readiness(
        config=_READY_CFG, user_agent_present=True,
        enabled_verified_manager_count=2, kill_switch_available=True)
    assert r["ready"] is True and r["blocking"] == []
    assert all(r["checks"].values())


def test_not_ready_missing_user_agent():
    r = h.assess_activation_readiness(
        config=_READY_CFG, user_agent_present=False,
        enabled_verified_manager_count=2)
    assert r["ready"] is False
    assert "user_agent_configured" in r["blocking"]


def test_not_ready_no_verified_manager():
    r = h.assess_activation_readiness(
        config=_READY_CFG, user_agent_present=True,
        enabled_verified_manager_count=0)
    assert r["ready"] is False
    assert "at_least_one_verified_enabled_manager" in r["blocking"]


def test_not_ready_rate_limit_too_high():
    r = h.assess_activation_readiness(
        config={**_READY_CFG, "sec_requests_per_second": 50},
        user_agent_present=True, enabled_verified_manager_count=1)
    assert r["ready"] is False
    assert "rate_limit_conservative" in r["blocking"]


def test_not_ready_if_feeds_decision_engine_true():
    r = h.assess_activation_readiness(
        config={**_READY_CFG, "feeds_decision_engine": True},
        user_agent_present=True, enabled_verified_manager_count=1)
    assert r["ready"] is False
    assert "feeds_decision_engine_false" in r["blocking"]


def test_shipped_config_is_not_ready_by_default():
    # The shipped config has no UA and no verified managers -> NOT ready
    # (proves the subsystem cannot silently self-activate).
    import json
    from pathlib import Path
    cfg = json.loads(Path("config/base.json").read_text())["institutional_intelligence"]
    r = h.assess_activation_readiness(
        config=cfg, user_agent_present=False, enabled_verified_manager_count=0)
    assert r["ready"] is False
