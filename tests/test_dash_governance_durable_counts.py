"""Operator-facing truth for durable production ops (Fix 7, 2026-07-28).

`applied_count` counts only the LAST RUN's approvals. Durable membership ops stay
live across runs without being re-applied, so the Governance page's hero stat read
"0 applied to production" while ops were in force. The view must surface the
durable count as well, and must surface a fail-closed rebuild refusal.
"""
from __future__ import annotations

import json

from gui_v2.data.dash_governance import collect_governance_view
from portfolio_automation.sim_governance import promotion_proposals as PP
from portfolio_automation.sim_governance import schemas as S


def _seed_state(root, state: dict) -> None:
    d = root / "outputs" / "promotion_approvals"
    d.mkdir(parents=True, exist_ok=True)
    (d / "production_application_state.json").write_text(json.dumps(state),
                                                         encoding="utf-8")


def test_quiet_day_still_reports_the_durable_live_count(tmp_path):
    _seed_state(tmp_path, {
        "generated_at": "2026-07-28T13:00:00+00:00",
        "applied_count": 0, "applied_today_count": 0,
        "watchlist_applied": 3, "watchlist_applied_today": 0,
        "watchlist_carried_forward": 3, "durably_live_count": 3,
    })
    view = collect_governance_view(tmp_path)

    assert view["applied_count"] == 0
    assert view["applied_today_count"] == 0
    assert view["durably_live_count"] == 3, "3 ops are live; the page must say so"
    prod = [c for c in view["cards"] if c["title"] == "Production lane"][0]
    assert "3 durable op(s) live" in prod["summary"]
    assert "0 newly applied this run" in prod["summary"]


def test_legacy_state_without_the_new_fields_falls_back(tmp_path):
    """Backward compatible: a state file from before this fix still renders."""
    _seed_state(tmp_path, {"generated_at": "2026-07-01T00:00:00+00:00",
                           "applied_count": 2, "watchlist_applied": 2})
    view = collect_governance_view(tmp_path)
    assert view["applied_today_count"] == 2
    assert view["durably_live_count"] == 2
    assert view["overlay_rebuild_skipped"] is False


def test_fail_closed_rebuild_refusal_is_surfaced(tmp_path):
    _seed_state(tmp_path, {
        "generated_at": "2026-07-28T13:00:00+00:00",
        "overlay_rebuild_skipped": True,
        "approvals_log_unreadable": "unparseable_json: boom",
        "applied_count": 0, "applied_today_count": 0,
        "watchlist_applied": 1, "durably_live_count": 1,
    })
    view = collect_governance_view(tmp_path)

    assert view["overlay_rebuild_skipped"] is True
    assert view["approvals_log_unreadable"] == "unparseable_json: boom"
    prod = [c for c in view["cards"] if c["title"] == "Production lane"][0]
    assert prod["status"] == "warning"
    assert "REFUSED" in prod["label"]
    assert "unparseable_json: boom" in prod["summary"]


# ---------------------------------------------------------------------------
# The per-op rollback instruction shipped to the operator must be executable.
# ---------------------------------------------------------------------------

def test_durable_rollback_plans_point_at_revoke_application():
    for ptype in (S.PROPOSAL_WATCHLIST_ADD, S.PROPOSAL_WATCHLIST_REMOVE,
                  S.PROPOSAL_WATCHLIST_RANK, S.PROPOSAL_WATCHLIST_TAG,
                  S.PROPOSAL_DISCOVERY_PROMOTION):
        plan = PP._rollback_plan_for(ptype, "XOM")
        assert "revoke_application" in plan, ptype
        assert "XOM" in plan and "{sym}" not in plan, ptype


def test_non_durable_watchlist_plan_still_describes_refresh():
    plan = PP._rollback_plan_for(S.PROPOSAL_FLOCK_WATCHLIST_LOGIC, "GOOGL")
    assert "NOT durable" in plan
    assert "revoke_application" not in plan
