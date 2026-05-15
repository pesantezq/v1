"""Route smoke tests using FastAPI TestClient."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from gui_v2.app import app
    return TestClient(app)


@pytest.mark.parametrize("path,expected_heading", [
    ("/", "Today"),
    ("/portfolio", "Portfolio"),
    ("/research", "Research"),
    ("/health", "Health"),
    ("/operations", "Operations"),
])
def test_route_returns_200_and_heading(client, path: str, expected_heading: str):
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert expected_heading in response.text


def test_nav_links_present_on_every_page(client):
    for path in ("/", "/portfolio", "/research", "/health", "/operations"):
        body = client.get(path).text
        for nav in ("Today", "Portfolio", "Research", "Health", "Operations"):
            assert f">{nav}<" in body, f"nav link {nav!r} missing on {path}"
