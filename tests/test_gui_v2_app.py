"""Smoke tests for the gui_v2 FastAPI app shell."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_app_importable():
    from gui_v2.app import app
    assert app is not None


def test_app_title():
    from gui_v2.app import app
    assert "StockBot" in app.title


def test_root_route_returns_200():
    from gui_v2.app import app
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
