"""
Auto-approval simulation-watchlist apply + event-aware compare-and-swap rollback.

Uses a real sqlite ExtendedWatchlist against a temp DB (never the production DB).
"""
from __future__ import annotations

import pytest

from portfolio_automation.sim_governance import auto_approval as AA
from portfolio_automation.sim_governance import schemas as S
from watchlist_scanner.extended_watchlist import ExtendedWatchlist


@pytest.fixture
def wl(tmp_path):
    return ExtendedWatchlist(db_path=str(tmp_path / "sim_watchlist.db"), max_symbols=5)


def _cand(symbol="NVDA", **over):
    base = {
        "candidate_id": "cand_1", "candidate_type": "watchlist",
        "workflow": S.WORKFLOW_WATCHLIST, "proposal_type": S.PROPOSAL_WATCHLIST_ADD,
        "symbol": symbol, "confidence": 0.92,
        "target_lane": "simulation", "production_mutation": False,
        "feeds_decision_engine": False, "is_human_approved": False,
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# ExtendedWatchlist simulation primitives
# --------------------------------------------------------------------------

def test_promote_auto_approved_inserts_active(wl):
    res = wl.promote_auto_approved("NVDA", confidence=0.92)
    assert res["status"] == "promoted"
    row = wl.get_symbol("NVDA")
    assert row is not None and row["is_active"] == 1
    assert row["theme_name"] == "auto_approval_sim"


def test_promote_auto_approved_skips_when_already_active(wl):
    wl.promote_auto_approved("NVDA")
    res = wl.promote_auto_approved("NVDA")
    assert res["status"] == "skipped"


def test_get_symbol_none_when_absent(wl):
    assert wl.get_symbol("ZZZZ") is None


def test_restore_state_none_deletes_row(wl):
    wl.promote_auto_approved("NVDA")
    wl.restore_state("NVDA", None)
    assert wl.get_symbol("NVDA") is None


def test_restore_state_reinstates_inactive_row(wl):
    wl.promote_auto_approved("NVDA")
    before = wl.get_symbol("NVDA")
    before["is_active"] = 0
    before["drop_reason"] = "prior"
    wl.restore_state("NVDA", before)
    row = wl.get_symbol("NVDA")
    assert row["is_active"] == 0 and row["drop_reason"] == "prior"


def test_demote_vetoed_deactivates_with_reason(wl):
    wl.promote_auto_approved("NVDA")
    wl.demote_vetoed("NVDA")
    row = wl.get_symbol("NVDA")
    assert row["is_active"] == 0 and row["drop_reason"] == "vetoed"


# --------------------------------------------------------------------------
# auto_approval apply
# --------------------------------------------------------------------------

def test_apply_watchlist_candidate_captures_before_after(wl):
    res = AA.apply_watchlist_candidate(_cand("NVDA"), wl, now="2026-07-14T00:00:00Z")
    assert res["status"] == "applied"
    assert res["before_state"] is None
    assert res["after_state"]["is_active"] == 1


def test_apply_then_rollback_removes_newly_created_symbol(wl):
    res = AA.apply_watchlist_candidate(_cand("NVDA"), wl, now="2026-07-14T00:00:00Z")
    event = {"target_id": "NVDA", "symbol": "NVDA",
             "before_state": res["before_state"], "after_state": res["after_state"]}
    rb = AA.rollback_watchlist_event(event, wl)
    assert rb["status"] == "rolled_back"
    assert wl.get_symbol("NVDA") is None  # created fresh -> removed on rollback


def test_rollback_restores_previously_inactive_symbol(wl):
    # Seed a previously-inactive row, then auto-apply reactivates it.
    wl.promote_auto_approved("NVDA")
    wl.demote_vetoed("NVDA")            # now inactive, drop_reason='vetoed'
    inactive_before = wl.get_symbol("NVDA")
    assert inactive_before["is_active"] == 0
    res = AA.apply_watchlist_candidate(_cand("NVDA"), wl, now="2026-07-14T00:00:00Z")
    assert res["after_state"]["is_active"] == 1
    event = {"target_id": "NVDA", "symbol": "NVDA",
             "before_state": res["before_state"], "after_state": res["after_state"]}
    rb = AA.rollback_watchlist_event(event, wl)
    assert rb["status"] == "rolled_back"
    row = wl.get_symbol("NVDA")
    assert row["is_active"] == 0  # restored to the inactive prior state


def test_rollback_conflict_when_state_changed_since_apply(wl):
    res = AA.apply_watchlist_candidate(_cand("NVDA"), wl, now="2026-07-14T00:00:00Z")
    # A human (or another run) changes the symbol AFTER the auto-apply.
    wl.demote_vetoed("NVDA")
    event = {"target_id": "NVDA", "symbol": "NVDA",
             "before_state": res["before_state"], "after_state": res["after_state"]}
    rb = AA.rollback_watchlist_event(event, wl)
    assert rb["status"] == "rollback_conflict"
    # Current state preserved (still inactive from the human change) — NOT overwritten.
    assert wl.get_symbol("NVDA")["is_active"] == 0
    assert rb.get("conflicting_fields")
