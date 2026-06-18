"""Tests for GET /dashboard/operator route (Task 4)."""
from fastapi.testclient import TestClient
from gui_v2.app import app

client = TestClient(app)


def test_operator_page_renders():
    r = client.get("/dashboard/operator")
    assert r.status_code == 200
    assert "Operator" in r.text
    assert "ready" in r.text.lower()  # readiness section rendered
