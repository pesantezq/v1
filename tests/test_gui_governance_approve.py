import importlib

import pytest


@pytest.fixture
def appmod(monkeypatch):
    monkeypatch.setenv("GUI_V2_AUTH_USER", "op")
    monkeypatch.setenv("GUI_V2_AUTH_PASS", "pw")
    monkeypatch.setenv("GUI_V2_OPERATOR_EDIT", "1")
    import gui_v2.app as app
    importlib.reload(app)
    return app


def test_apply_per_item_approve(appmod, monkeypatch):
    calls = []
    monkeypatch.setattr(appmod, "_promotion_approvals_record",
                        lambda **kw: calls.append(kw) or {"ok": True, "reason": "ok"})
    monkeypatch.setattr(appmod, "_pending_ids", lambda base_dir: {"p1", "p2"})
    monkeypatch.setattr(appmod, "_decided_ids", lambda base_dir: set())
    res = appmod._apply_approval_action(
        action="approve", proposal_id="p1", excluded_ids=set(),
        actor="op", now="n", base_dir="b")
    assert res["applied"] == ["p1"]
    assert calls[0]["decision"] == "approve"
    assert calls[0]["approver"] == "op"


def test_apply_bulk_approve_with_exclusion(appmod, monkeypatch):
    calls = []
    monkeypatch.setattr(appmod, "_promotion_approvals_record",
                        lambda **kw: calls.append(kw["proposal_id"]) or {"ok": True, "reason": "ok"})
    monkeypatch.setattr(appmod, "_pending_ids", lambda base_dir: {"p1", "p2", "p3"})
    monkeypatch.setattr(appmod, "_decided_ids", lambda base_dir: set())
    res = appmod._apply_approval_action(
        action="approve_all", proposal_id=None, excluded_ids={"p2"},
        actor="op", now="n", base_dir="b")
    assert set(res["applied"]) == {"p1", "p3"}
    assert "p2" not in calls


def test_apply_skips_already_decided(appmod, monkeypatch):
    monkeypatch.setattr(appmod, "_promotion_approvals_record",
                        lambda **kw: {"ok": True, "reason": "ok"})
    monkeypatch.setattr(appmod, "_pending_ids", lambda base_dir: {"p1"})
    monkeypatch.setattr(appmod, "_decided_ids", lambda base_dir: {"p1"})
    res = appmod._apply_approval_action(
        action="approve", proposal_id="p1", excluded_ids=set(),
        actor="op", now="n", base_dir="b")
    assert res["applied"] == []
    assert res["skipped"] == ["p1"]


def test_governance_page_renders_packet_panel(appmod, monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    # Point the reader at a seeded packet.
    import gui_v2.data.dash_approval_packet as dap
    monkeypatch.setattr(dap, "load_packet_context", lambda outputs_dir: {
        "available": True, "tier_sim": [], "counts": {"tier_production_pending": 1},
        "tier_production": [{"proposal_id": "p1", "workflow": "watchlist",
                             "symbol": "CVX", "status": "pending human review"}],
        "approval_page_url": "/dashboard/governance"})
    client = TestClient(appmod.app)
    r = client.get("/dashboard/governance", auth=("op", "pw"))
    assert r.status_code == 200
    assert "Approval Packet" in r.text
    assert "/dashboard/governance/approve" in r.text
    assert "p1" in r.text


# ---------------------------------------------------------------------------
# Route-level gating spine for POST /dashboard/governance/approve.
#
# These exercise page_governance_approve itself (not just the
# _apply_approval_action helper) so the operator-edit gate, the same-origin
# CSRF guard, and actor-provenance (auth user, never a form-supplied
# ``approver``) are proven at the route boundary, per the code review gap.
# ---------------------------------------------------------------------------


def _patch_no_disk_side_effects(appmod, monkeypatch, tmp_path):
    """Redirect REPO_ROOT so the route's audit_log.record_event writes land
    under tmp_path instead of the real repo tree."""
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)


def test_route_approve_blocked_when_operator_edit_disabled(appmod, monkeypatch, tmp_path):
    from starlette.testclient import TestClient

    # Auth stays configured (set by the appmod fixture); only the edit flag
    # is withdrawn, mirroring an authenticated read-only viewer.
    monkeypatch.delenv("GUI_V2_OPERATOR_EDIT", raising=False)
    _patch_no_disk_side_effects(appmod, monkeypatch, tmp_path)

    calls = []
    monkeypatch.setattr(appmod, "_promotion_approvals_record",
                        lambda **kw: calls.append(kw) or {"ok": True, "reason": "ok"})

    client = TestClient(appmod.app)
    r = client.post(
        "/dashboard/governance/approve",
        data={"action": "approve", "proposal_id": "p1"},
        auth=("op", "pw"),
        headers={"origin": "http://testserver",
                 "referer": "http://testserver/dashboard/governance"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "disabled" in r.headers["location"].lower()
    assert calls == []


def test_route_approve_rejected_when_cross_origin(appmod, monkeypatch, tmp_path):
    from starlette.testclient import TestClient

    # appmod fixture already sets GUI_V2_OPERATOR_EDIT=1.
    _patch_no_disk_side_effects(appmod, monkeypatch, tmp_path)

    calls = []
    monkeypatch.setattr(appmod, "_promotion_approvals_record",
                        lambda **kw: calls.append(kw) or {"ok": True, "reason": "ok"})
    monkeypatch.setattr(appmod, "_pending_ids", lambda base_dir: {"p1"})
    monkeypatch.setattr(appmod, "_decided_ids", lambda base_dir: set())

    client = TestClient(appmod.app)
    r = client.post(
        "/dashboard/governance/approve",
        data={"action": "approve", "proposal_id": "p1"},
        auth=("op", "pw"),
        headers={"origin": "http://evil.example"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "cross-origin" in r.headers["location"].lower()
    assert calls == []


def test_route_approve_same_origin_records_with_session_actor(appmod, monkeypatch, tmp_path):
    from starlette.testclient import TestClient

    # appmod fixture already sets GUI_V2_OPERATOR_EDIT=1.
    _patch_no_disk_side_effects(appmod, monkeypatch, tmp_path)

    calls = []
    monkeypatch.setattr(appmod, "_promotion_approvals_record",
                        lambda **kw: calls.append(kw) or {"ok": True, "reason": "ok"})
    monkeypatch.setattr(appmod, "_pending_ids", lambda base_dir: {"p1"})
    monkeypatch.setattr(appmod, "_decided_ids", lambda base_dir: set())

    client = TestClient(appmod.app)
    r = client.post(
        "/dashboard/governance/approve",
        # A form-supplied ``approver`` must never override the session actor.
        data={"action": "approve", "proposal_id": "p1", "approver": "attacker"},
        auth=("op", "pw"),
        headers={"origin": "http://testserver",
                 "referer": "http://testserver/dashboard/governance"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert len(calls) == 1
    assert calls[0]["proposal_id"] == "p1"
    assert calls[0]["decision"] == "approve"
    assert calls[0]["approver"] == "op"
