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


def test_root_renders_base_html():
    from gui_v2.app import app
    client = TestClient(app)
    response = client.get("/")
    body = response.text
    # Tailwind is self-hosted (compiled app.css), not the Play CDN — the CDN was
    # dropped in 963b95cb. Mirrors test_tailwind_is_self_hosted_not_cdn.
    assert "cdn.tailwindcss.com" not in body
    assert "/static/app.css" in body
    # HTMX is vendored locally, not from unpkg/CDN — dropped in f5484fc0.
    assert "htmx.org" not in body
    # Dark theme baseline
    assert "bg-zinc-950" in body
    # App name in title
    assert "StockBot" in body
