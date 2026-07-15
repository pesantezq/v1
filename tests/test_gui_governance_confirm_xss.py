"""
Adversarial render-smoke coverage for the confirm()-text XSS class fixed in
this hardening pass (see tests/test_template_inline_js_safety.py for the
durable class-level guard).

Drives the real pages via starlette's TestClient with the page's context
provider monkeypatched to inject adversarial identifier values (symbol,
proposal_id, event_id, target_id, work_order_id), matching the pattern used
in tests/test_gui_governance_approve.py::test_governance_page_renders_packet_panel
(which monkeypatched gui_v2.data.dash_approval_packet.load_packet_context).

For each affected page we assert:
  1. HTTP 200.
  2. The raw adversarial substring never appears unescaped (it must come back
     HTML-entity-escaped, e.g. `<script>` -> `&lt;script&gt;`).
  3. No on<word>= handler in the response contains the adversarial payload or
     a raw `{{` (i.e. no Jinja leaked into a handler and no payload broke out
     of one).
"""
from __future__ import annotations

import importlib
import re

import pytest
from starlette.testclient import TestClient

ADVERSARIAL_VALUES = [
    "'onerror=alert(1)",
    "');alert(1);//",
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
]

_HANDLER_RE = re.compile(r"""on[a-z]+\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)


def _assert_no_handler_breakout(text: str, payload: str) -> None:
    for match in _HANDLER_RE.finditer(text):
        value = match.group(1) if match.group(1) is not None else match.group(2)
        assert payload not in value, (
            f"adversarial payload leaked into an inline handler: {match.group(0)!r}"
        )
        assert "{{" not in value, (
            f"raw Jinja expression leaked into an inline handler: {match.group(0)!r}"
        )


@pytest.fixture
def appmod(monkeypatch):
    monkeypatch.setenv("GUI_V2_AUTH_USER", "op")
    monkeypatch.setenv("GUI_V2_AUTH_PASS", "pw")
    monkeypatch.setenv("GUI_V2_OPERATOR_EDIT", "1")
    import gui_v2.app as app
    importlib.reload(app)
    return app


def _governance_ctx(adv: str) -> dict:
    """Full collect_governance_view()-shaped context with adv injected into
    every identifier field surfaced near the /decide and /veto confirm()
    handlers (proposal_id/symbol/type for /decide, target_id/event_id for
    /veto)."""
    return {
        "persona": "governance",
        "observe_only": False,
        "cards": [],
        "simulation_lane_active": True,
        "production_overlay_live": {"watchlist": False, "advisory": False},
        "last_simulation_run": None,
        "ai_review_status": "reviewed",
        "ai_review_deferred": False,
        "ai_cost_today_usd": 0.0,
        "ai_daily_cap_usd": 0.5,
        "ai_budget_remaining_usd": 0.5,
        "ai_review_method": "llm",
        "advisory_candidates_reviewed": 0,
        "watchlist_candidates_reviewed": 0,
        "pending_proposals": [{
            "proposal_id": adv,
            "proposal_type": "watchlist_add",
            "proposed_production_change": {"symbol": adv},
            "risk_summary": adv,
            "workflow": adv,
            "created_at": None,
            "rollback_plan": None,
            "evidence_refs": [],
            "simulation_result_refs": [],
        }],
        "approved_proposal_ids": [],
        "rejected_proposal_ids": [],
        "applied_count": 0,
        "approval_records": [],
        "auto_applied_items": [{
            "target_id": adv,
            "event_id": adv,
            "symbol": adv,
            "candidate_type": adv,
            "feeds_decision_engine": False,
            "confidence": None,
            "gpt_reasoning": adv,
            "gate_summary": [],
            "applied_at": None,
            "target_lane": "simulation",
            "status_label": "Auto-applied in simulation · veto available",
        }],
        "auto_approval_circuit_breaker": {},
        "labels": {"sim_active": "Simulation Active", "pending": "Production Pending Approval",
                   "approved": "Approved for Production", "applied": "Applied to Production"},
        "has_data": True,
    }


@pytest.mark.parametrize("adv", ADVERSARIAL_VALUES)
def test_governance_page_survives_adversarial_identifiers(appmod, monkeypatch, adv):
    import gui_v2.data.dash_governance as dg
    monkeypatch.setattr(dg, "collect_governance_view", lambda root: _governance_ctx(adv))

    client = TestClient(appmod.app)
    r = client.get("/dashboard/governance", auth=("op", "pw"))
    assert r.status_code == 200

    # The raw payload must never appear unescaped in the body.
    assert adv not in r.text, f"adversarial payload appeared unescaped: {adv!r}"

    # No inline handler may contain the payload or a raw Jinja expression.
    _assert_no_handler_breakout(r.text, adv)

    # The static confirm() guards must still be present (fix preserves UX).
    assert "confirm('Approve this production candidate?')" in r.text
    assert "confirm('Reject this production candidate?')" in r.text
    assert "confirm('Veto and roll back this simulation item?')" in r.text


def _operator_view(adv: str) -> dict:
    """Full operator_worker_view()-shaped context with adv injected as the
    work_order_id near the /operator cancel confirm() handler."""
    return {
        "readiness": {
            "overall_ready": "3/3",
            "autonomous_enabled": False,
            "gates": {},
        },
        "cost": {"today_usd": 0.0, "cap_configured": False, "cap_usd": 0.0,
                  "cap_pct": None, "lifetime_usd": 0.0},
        "orders": [{
            "work_order_id": adv,
            "status": "queued",
            "created_at": None,
            "age_hours": None,
            "probe_id": adv,
            "skill_id": adv,
            "cancellable": True,
            "stale": False,
        }],
        "counts": {"open": 1, "awaiting_approval": 0, "failed": 0, "quarantined": 0,
                    "cancelled": 0, "completed": 0, "stale": 0},
        "quarantine": [],
        "degraded": False,
    }


@pytest.mark.parametrize("adv", ADVERSARIAL_VALUES)
def test_operator_page_survives_adversarial_work_order_id(appmod, monkeypatch, adv):
    import gui_v2.data.operator_control as oc
    monkeypatch.setattr(oc, "operator_worker_view", lambda root: _operator_view(adv))

    client = TestClient(appmod.app)
    r = client.get("/dashboard/operator", auth=("op", "pw"))
    assert r.status_code == 200

    assert adv not in r.text, f"adversarial payload appeared unescaped: {adv!r}"
    _assert_no_handler_breakout(r.text, adv)

    # The static confirm() guard must still be present (fix preserves UX).
    assert "confirm('Cancel this work order? This is a terminal, audited action.');" in r.text
