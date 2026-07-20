"""Phase 16 tests — health assessor, semantic-liveness, orchestrator inert paths.

Health: RED only on contract breaches (feeds_decision_engine=true, look-ahead
quarter-end, options-directional, production mutation, namespace breach); AMBER
for everything else (registry invalid, UA missing while live, high unresolved,
fresh-but-empty). Orchestrator: inert/disabled/insufficient honest artifacts,
never raises, invariants intact.
"""

from __future__ import annotations

from portfolio_automation.institutional_intelligence import health as h
from portfolio_automation.institutional_intelligence.context_loader import (
    run_institutional_intelligence,
)

_GOOD_STATUS = {"overall_status": "ok", "symbols_covered": 3, "feeds_decision_engine": False}
_GOOD_INTEL = {"feeds_decision_engine": False,
               "records": [{"consensus_state": "moderate_accumulation"}]}


def _assess(**over):
    base = dict(config={"enabled": True, "live_sec_ingestion_enabled": False},
                registry_ok=True, registry_error=None, status_artifact=_GOOD_STATUS,
                intelligence_artifact=_GOOD_INTEL, sec_user_agent_present=True,
                wrote_outside_namespace=False)
    base.update(over)
    return h.assess_institutional_health(**base)


def test_health_green_baseline():
    assert _assess()["overall_status"] == h.STATUS_GREEN


def test_red_feeds_decision_engine_true():
    r = _assess(intelligence_artifact={"feeds_decision_engine": True, "records": []})
    assert r["overall_status"] == h.STATUS_RED
    assert "feeds_decision_engine_true" in r["red_flags"]


def test_red_lookahead_quarter_end():
    r = _assess(status_artifact={**_GOOD_STATUS, "used_quarter_end_as_availability": True})
    assert r["overall_status"] == h.STATUS_RED
    assert "look_ahead_quarter_end_as_availability" in r["red_flags"]


def test_red_options_directional():
    r = _assess(intelligence_artifact={**_GOOD_INTEL, "options_treated_as_directional": True})
    assert r["overall_status"] == h.STATUS_RED


def test_red_production_mutation():
    r = _assess(status_artifact={**_GOOD_STATUS, "production_mutation": True})
    assert r["overall_status"] == h.STATUS_RED


def test_red_namespace_breach():
    assert _assess(wrote_outside_namespace=True)["overall_status"] == h.STATUS_RED


def test_amber_registry_invalid():
    r = _assess(registry_ok=False, registry_error="duplicate CIK")
    assert r["overall_status"] == h.STATUS_AMBER
    assert any("manager_registry_invalid" in f for f in r["amber_flags"])


def test_amber_ua_missing_while_live():
    r = _assess(config={"enabled": True, "live_sec_ingestion_enabled": True},
                sec_user_agent_present=False)
    assert r["overall_status"] == h.STATUS_AMBER
    assert "sec_user_agent_missing_while_live" in r["amber_flags"]


def test_amber_high_unresolved_rate():
    intel = {"feeds_decision_engine": False,
             "records": [{"consensus_state": "insufficient_data"}] * 4
                        + [{"consensus_state": "moderate_accumulation"}]}
    r = _assess(intelligence_artifact=intel)
    assert "high_unresolved_identity_rate" in r["amber_flags"]


def test_amber_fresh_but_empty():
    r = _assess(status_artifact={"overall_status": "ok", "symbols_covered": 0,
                                 "feeds_decision_engine": False},
                intelligence_artifact={"feeds_decision_engine": False, "records": []})
    assert "consensus_fresh_but_empty" in r["amber_flags"]


# --- semantic liveness ---------------------------------------------------

def test_detect_constant_consensus():
    assert h.detect_constant_consensus(["moderate_accumulation"] * 40) is True
    assert h.detect_constant_consensus(["a", "b"] * 20) is False
    assert h.detect_constant_consensus(["a"] * 5) is False        # below min sample


def test_detect_effective_managers_always_zero():
    assert h.detect_effective_managers_always_zero([0.0] * 40) is True
    assert h.detect_effective_managers_always_zero([0.0, 2.0] * 20) is False


def test_detect_all_same_state():
    assert h.detect_all_same_state(["x"] * 30) is True
    assert h.detect_all_same_state(["x", "y", "z"] * 10) is False


# --- orchestrator inert paths -------------------------------------------

def test_loader_disabled_honest(tmp_path):
    r = run_institutional_intelligence(str(tmp_path), write=False, config={"enabled": False})
    assert r["status"] == "disabled"
    assert r["artifact"]["feeds_decision_engine"] is False
    assert r["artifact"]["observe_only"] is True


def test_loader_enabled_no_managers(tmp_path):
    r = run_institutional_intelligence(
        str(tmp_path), write=False,
        config={"enabled": True,
                "manager_registry_path": "config/institutional_managers.yaml"})
    assert r["status"] == "insufficient_data"       # all seed managers disabled


def test_loader_never_raises_on_bad_registry(tmp_path):
    r = run_institutional_intelligence(
        str(tmp_path), write=False,
        config={"enabled": True, "manager_registry_path": str(tmp_path / "nope.yaml")})
    # Missing registry -> failed status, but no exception propagated.
    assert r["status"] in ("failed", "insufficient_data")
    assert r["artifact"]["feeds_decision_engine"] is False
