"""
GUI veto surface: POST /dashboard/governance/veto (strict operator pattern) + the
veto_from_gui helper. REPO_ROOT points at tmp so nothing touches the real tree.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import gui_v2.app as appmod
from portfolio_automation.sim_governance import auto_approval as AA
from portfolio_automation.sim_governance import schemas as S

client = TestClient(appmod.app)
NOW = "2026-07-14T12:00:00Z"
APPROVE = '{"decision":"approve","within_bounds":true,"reason":"clean"}'


def _seed_applied(root):
    """Produce a genuine applied event + a sim watchlist row under *root*."""
    sim_cfg = {"auto_approval": {"enabled": True, "watchlist_enabled": True,
                                 "strategy_enabled": False, "min_confidence": 0.85,
                                 "watchlist_daily_cap": 2, "strategy_daily_cap": 0,
                                 "max_active_awaiting_veto": 5,
                                 "sim_watchlist_db_path": "data/sim_governance_watchlist.db",
                                 "sim_max_symbols": 5}}
    review = {"generated_at": NOW, "verdicts": [
        {"candidate_id": "c1", "decision": S.DECISION_READY, "workflow": S.WORKFLOW_WATCHLIST}]}
    cbi = {"c1": {"candidate_id": "c1", "symbol": "NVDA", "confidence": 0.9,
                  "proposal_type": S.PROPOSAL_WATCHLIST_ADD}}
    AA.run_stage(root=str(root), now=NOW, sim_gov_config=sim_cfg, review_result=review,
                 candidates_by_id=cbi, base_dir=str(root / "outputs"), env={},
                 approver=lambda p: APPROVE)
    ev = [e for e in AA.load_events(base_dir=str(root / "outputs"))
          if e["kind"] == AA.EVENT_APPLIED][0]
    return ev["event_id"]


def test_veto_disabled_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "pesantez")
    monkeypatch.delenv("GUI_V2_OPERATOR_EDIT", raising=False)
    eid = _seed_applied(tmp_path)
    r = client.post("/dashboard/governance/veto", data={"event_id": eid},
                    headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303
    # No rollback happened — symbol still active in the sim DB.
    from watchlist_scanner.extended_watchlist import ExtendedWatchlist
    wl = ExtendedWatchlist(db_path=str(tmp_path / "data" / "sim_governance_watchlist.db"))
    assert wl.get_symbol("NVDA")["is_active"] == 1


def test_veto_cross_origin_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "pesantez")
    monkeypatch.setenv("GUI_V2_AUTH_USER", "u")
    monkeypatch.setenv("GUI_V2_AUTH_PASS", "p")
    monkeypatch.setenv("GUI_V2_OPERATOR_EDIT", "1")
    eid = _seed_applied(tmp_path)
    r = client.post("/dashboard/governance/veto", data={"event_id": eid},
                    headers={"origin": "http://evil.example.com"}, follow_redirects=False)
    assert r.status_code == 303
    from watchlist_scanner.extended_watchlist import ExtendedWatchlist
    wl = ExtendedWatchlist(db_path=str(tmp_path / "data" / "sim_governance_watchlist.db"))
    assert wl.get_symbol("NVDA")["is_active"] == 1  # unchanged


def test_veto_success_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "pesantez")
    monkeypatch.setenv("GUI_V2_AUTH_USER", "u")
    monkeypatch.setenv("GUI_V2_AUTH_PASS", "p")
    monkeypatch.setenv("GUI_V2_OPERATOR_EDIT", "1")
    eid = _seed_applied(tmp_path)
    r = client.post("/dashboard/governance/veto",
                    data={"event_id": eid, "reason": "not convinced"},
                    headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303
    from watchlist_scanner.extended_watchlist import ExtendedWatchlist
    wl = ExtendedWatchlist(db_path=str(tmp_path / "data" / "sim_governance_watchlist.db"))
    assert wl.get_symbol("NVDA") is None  # rolled back
    kinds = [e["kind"] for e in AA.load_events(base_dir=str(tmp_path / "outputs"))]
    assert AA.EVENT_HUMAN_VETO in kinds and AA.EVENT_ROLLBACK in kinds


def test_veto_missing_event_id_is_bad_request(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "pesantez")
    monkeypatch.setenv("GUI_V2_AUTH_USER", "u")
    monkeypatch.setenv("GUI_V2_AUTH_PASS", "p")
    monkeypatch.setenv("GUI_V2_OPERATOR_EDIT", "1")
    r = client.post("/dashboard/governance/veto", data={},
                    headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 400


def test_veto_from_gui_helper_rolls_back(tmp_path):
    eid = _seed_applied(tmp_path)
    out = AA.veto_from_gui(str(tmp_path), eid, operator="pesantez",
                           reason="manual", now=NOW)
    assert out["status"] == "rolled_back"
