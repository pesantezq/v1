"""
Evening governance digest builder + email sender.

Builder is pure; the sender's SMTP transport is injected so no real email is sent.
"""
from __future__ import annotations

import pytest

from portfolio_automation.sim_governance import auto_approval as AA
from portfolio_automation.sim_governance import governance_digest as GD

NOW = "2026-07-14T22:00:00Z"


def _applied_event(symbol="NVDA", eid="evt_a", applied_at="2026-07-14T12:00:00Z"):
    return {"kind": AA.EVENT_APPLIED, "event_id": eid, "idempotency_key": "idk_" + symbol,
            "candidate_type": "watchlist", "target_id": symbol, "symbol": symbol,
            "confidence": 0.92, "gpt_reasoning": "clean evidence",
            "gate_trace": [{"gate_name": "min_confidence", "passed": True}],
            "application_timestamp": applied_at, "ts": applied_at}


# --------------------------------------------------------------------------
# Builder
# --------------------------------------------------------------------------

def test_digest_lists_auto_applied_items_with_sim_wording():
    events = [_applied_event()]
    summary = {"active_items": [{"idempotency_key": "idk_NVDA", "event_id": "evt_a",
                                 "target_id": "NVDA", "applied_at": "2026-07-14T12:00:00Z"}],
               "circuit_breaker": {"engaged": False, "reason": None}}
    d = GD.build_governance_digest(summary=summary, events=events, now=NOW)
    assert d["json"]["auto_applied"]
    assert "Auto-applied in simulation" in d["html"]
    # Must never be bare "approved" without the simulation qualifier.
    assert "· veto available" in d["html"]


def test_digest_uses_event_id_links_not_symbol():
    events = [_applied_event(eid="evt_specific")]
    summary = {"active_items": [], "circuit_breaker": {"engaged": False}}
    d = GD.build_governance_digest(summary=summary, events=events, now=NOW,
                                   gui_base_url="https://dash.example.com")
    assert "evt_specific" in d["html"]
    assert d["json"]["auto_applied"][0]["veto_link"].endswith("evt_specific")


def test_empty_digest_renders_cleanly():
    d = GD.build_governance_digest(summary={"active_items": [], "circuit_breaker": {"engaged": False}},
                                   events=[], now=NOW)
    assert d["json"]["auto_applied"] == []
    assert "No auto-approval activity" in d["html"]


def test_digest_renders_rollback_conflict_and_authority_rejection():
    events = [
        {"kind": AA.EVENT_ROLLBACK_CONFLICT, "event_id": "evt_c", "target_id": "AMD", "ts": NOW},
        {"kind": AA.EVENT_DETERMINISTIC_REJECT, "event_id": "evt_r", "target_id": "TSLA",
         "reason": "authority_gate_failed", "ts": NOW},
    ]
    d = GD.build_governance_digest(summary={"active_items": [], "circuit_breaker": {"engaged": False}},
                                   events=events, now=NOW)
    assert d["json"]["rollback_conflicts"]
    assert d["json"]["authority_rejections"]
    assert "rollback conflict" in d["html"].lower()


def test_digest_reports_circuit_breaker_state():
    d = GD.build_governance_digest(
        summary={"active_items": [], "circuit_breaker": {"engaged": True, "reason": "rollback_failed"}},
        events=[], now=NOW)
    assert d["json"]["circuit_breaker"]["engaged"] is True
    assert "rollback_failed" in d["html"]


# --------------------------------------------------------------------------
# Email sender — gated, degrades safely, never leaks credentials
# --------------------------------------------------------------------------

def _digest():
    return {"json": {"auto_applied": []}, "html": "<p>digest</p>",
            "text": "digest", "subject_date": "2026-07-14"}


def test_email_disabled_skips_cleanly(tmp_path):
    calls = []
    out = GD.send_governance_digest(_digest(), now=NOW, base_dir=str(tmp_path),
                                    env={}, transport=lambda c, m: calls.append(1))
    assert out["status"] == "skipped" and out["reason"] == "disabled"
    assert calls == []


def test_email_enabled_but_missing_creds_surfaces_failure(tmp_path):
    out = GD.send_governance_digest(
        _digest(), now=NOW, base_dir=str(tmp_path),
        env={"GOVERNANCE_DIGEST_ENABLED": "1"},  # no SMTP_SERVER/EMAIL_* -> no creds
        transport=lambda c, m: None)
    assert out["status"] == "delivery_failed"
    assert out["health"] == "AMBER"


def test_email_send_success(tmp_path):
    calls = []
    env = {"GOVERNANCE_DIGEST_ENABLED": "1", "SMTP_SERVER": "smtp.example.com",
           "EMAIL_USER": "bot@example.com", "EMAIL_PASS": "secret-pw",
           "EMAIL_TO": "ops@example.com"}
    out = GD.send_governance_digest(_digest(), now=NOW, base_dir=str(tmp_path), env=env,
                                    transport=lambda c, m: calls.append((c, m)))
    assert out["status"] == "sent"
    assert len(calls) == 1


def test_email_send_failure_is_amber_and_sanitized(tmp_path):
    env = {"GOVERNANCE_DIGEST_ENABLED": "1", "SMTP_SERVER": "smtp.example.com",
           "EMAIL_USER": "bot@example.com", "EMAIL_PASS": "secret-pw",
           "EMAIL_TO": "ops@example.com"}

    def boom(c, m):
        raise RuntimeError("auth failed for password secret-pw")

    out = GD.send_governance_digest(_digest(), now=NOW, base_dir=str(tmp_path), env=env,
                                    transport=boom)
    assert out["status"] == "delivery_failed"
    assert out["health"] == "AMBER"
    assert "secret-pw" not in (out.get("error") or "")


def test_local_time_send_gate_dst_safe():
    # 18:00 America/New_York is due; another hour is not. Uses real tz (DST-aware).
    assert GD.should_send_now("2026-07-14T22:00:00Z", send_hour_local=18,
                              timezone="America/New_York") is True   # EDT: UTC-4 -> 18:00 local
    assert GD.should_send_now("2026-07-14T20:00:00Z", send_hour_local=18,
                              timezone="America/New_York") is False  # 16:00 local
