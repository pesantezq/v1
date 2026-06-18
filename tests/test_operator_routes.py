"""Tests for GET /dashboard/operator route (Task 4) and POST cancel (Task 5)."""
import json
import os
from pathlib import Path

import gui_v2.app as appmod
from fastapi.testclient import TestClient
from gui_v2.app import app

client = TestClient(app)


def test_operator_page_renders():
    r = client.get("/dashboard/operator")
    assert r.status_code == 200
    assert "Operator" in r.text
    assert "ready" in r.text.lower()  # readiness section rendered


# ---------------------------------------------------------------------------
# Task 5: POST /dashboard/operator/cancel — cancel route tests
# ---------------------------------------------------------------------------


def _seed(root, wid, status):
    rec = {
        "work_order_id": wid,
        "status": status,
        "created_at": "2026-06-18T00:00:00+00:00",
        "status_history": [{"status": status, "at": "2026-06-18T00:00:00+00:00"}],
    }
    d = Path(root) / "outputs" / "operator_control"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "work_orders.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def test_cancel_blocked_without_edit_flag(monkeypatch):
    monkeypatch.setattr(appmod, "_operator_edit_enabled", lambda: False)
    r = client.post(
        "/dashboard/operator/cancel",
        data={"work_order_id": "wo_x", "reason": "test"},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code in (303, 403)
    if r.status_code == 303:
        assert "level=error" in r.headers["location"]


def test_cancel_rejects_cross_origin(monkeypatch):
    monkeypatch.setattr(appmod, "_operator_edit_enabled", lambda: True)
    r = client.post(
        "/dashboard/operator/cancel",
        data={"work_order_id": "wo_x", "reason": "t"},
        headers={"origin": "http://evil.example"},
        follow_redirects=False,
    )
    assert r.status_code in (303, 403)
    if r.status_code == 303:
        assert "level=error" in r.headers["location"]


def test_cancel_requires_reason(monkeypatch):
    monkeypatch.setattr(appmod, "_operator_edit_enabled", lambda: True)
    r = client.post(
        "/dashboard/operator/cancel",
        data={"work_order_id": "wo_x", "reason": "  "},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code in (303, 422)


def test_cancel_legal_transition(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_operator_edit_enabled", lambda: True)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "alice")
    _seed(tmp_path, "wo_legal", "queued")
    r = client.post(
        "/dashboard/operator/cancel",
        data={"work_order_id": "wo_legal", "reason": "stale"},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    from operator_control.work_orders import get_work_order

    cur = get_work_order(tmp_path, "wo_legal")
    assert cur["status"] == "cancelled"
    # actor came from auth, not the form
    assert cur["status_history"][-1]["actor"] == "alice"


def test_cancel_idempotent_when_already_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_operator_edit_enabled", lambda: True)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "alice")
    _seed(tmp_path, "wo_done", "cancelled")
    r = client.post(
        "/dashboard/operator/cancel",
        data={"work_order_id": "wo_done", "reason": "again"},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "level=error" not in r.headers["location"]  # treated as info/success no-op


def test_cancel_unknown_id_audits_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_operator_edit_enabled", lambda: True)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "alice")
    r = client.post(
        "/dashboard/operator/cancel",
        data={"work_order_id": "wo_nope", "reason": "x"},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "level=error" in r.headers["location"]


def test_cancel_edit_disabled_audits_rejection(tmp_path, monkeypatch):
    """Verify that edit-disabled branch emits an audit event."""
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_operator_edit_enabled", lambda: False)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "bob")
    r = client.post(
        "/dashboard/operator/cancel",
        data={"work_order_id": "wo_test", "reason": "cancel_reason"},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    # Should reject and return error redirect
    assert r.status_code == 303
    assert "level=error" in r.headers["location"]
    # Verify audit event was recorded
    audit_log_path = Path(tmp_path) / "outputs" / "operator_control" / "audit_log.jsonl"
    assert audit_log_path.exists()
    with open(audit_log_path) as f:
        events = [json.loads(line) for line in f if line.strip()]
    cancel_rejected = [e for e in events if e.get("event_type") == "work_order_cancel_rejected"]
    assert len(cancel_rejected) == 1
    evt = cancel_rejected[0]
    assert evt["actor"] == "bob"
    assert evt["work_order_id"] == "wo_test"
    assert evt["details"]["why"] == "edit_disabled"
    assert evt["details"]["actor_source"] == "dashboard_auth"


def test_cancel_empty_reason_audits_rejection(tmp_path, monkeypatch):
    """Verify that empty-reason branch emits an audit event."""
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_operator_edit_enabled", lambda: True)
    monkeypatch.setattr(appmod, "_require_auth", lambda *a, **k: "charlie")
    r = client.post(
        "/dashboard/operator/cancel",
        data={"work_order_id": "wo_empty", "reason": "   "},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    # Should reject and return error redirect
    assert r.status_code == 303
    assert "level=error" in r.headers["location"]
    # Verify audit event was recorded
    audit_log_path = Path(tmp_path) / "outputs" / "operator_control" / "audit_log.jsonl"
    assert audit_log_path.exists()
    with open(audit_log_path) as f:
        events = [json.loads(line) for line in f if line.strip()]
    cancel_rejected = [e for e in events if e.get("event_type") == "work_order_cancel_rejected"]
    assert len(cancel_rejected) == 1
    evt = cancel_rejected[0]
    assert evt["actor"] == "charlie"
    assert evt["work_order_id"] == "wo_empty"
    assert evt["details"]["why"] == "empty_reason"
    assert evt["details"]["actor_source"] == "dashboard_auth"


# ---------------------------------------------------------------------------
# Task 6: GET /dashboard/operator/quarantine/{work_order_id}/diff
# ---------------------------------------------------------------------------


def test_quarantine_diff_unknown_404(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    r = client.get("/dashboard/operator/quarantine/wo_missing/diff")
    assert r.status_code == 404


def test_quarantine_diff_malicious_id_404(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    r = client.get("/dashboard/operator/quarantine/..%2f..%2fetc%2fpasswd/diff")
    assert r.status_code in (404, 422)
