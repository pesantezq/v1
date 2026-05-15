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


def test_severity_badge_classes_via_jinja_filter():
    from gui_v2.app import _severity_classes
    assert "emerald" in _severity_classes("OK")
    assert "sky"     in _severity_classes("INFO")
    assert "amber"   in _severity_classes("WARN")
    assert "rose"    in _severity_classes("FAIL")
    # Unknown severity falls back to neutral zinc tones
    assert "zinc"    in _severity_classes("UNKNOWN")


def test_nav_contains_overall_severity_dot(client):
    body = client.get("/").text
    assert 'role="status"' in body
    assert ("emerald" in body or "sky" in body or "amber" in body or "rose" in body)


def test_health_renders_all_sections(client):
    body = client.get("/health").text
    assert "Pipeline" in body or "status" in body.lower()
    assert "Artifact shape" in body or "smoke" in body.lower()
    assert "Environment" in body or "env" in body.lower()
    assert "registry" in body.lower()


def test_today_renders_decisions_and_memo(tmp_path, monkeypatch):
    import json
    from gui_v2 import app as appmod
    fake = tmp_path
    (fake / "main.py").write_text("# marker\n", encoding="utf-8")
    (fake / "outputs" / "latest").mkdir(parents=True)
    (fake / "outputs" / "latest" / "decision_plan.json").write_text(json.dumps({
        "generated_at": "x", "total_decisions": 1, "observe_only": True,
        "decisions": [{"symbol": "ZZZX", "decision": "BUY",
                       "priority": 1.0, "urgency": "high",
                       "source": "test", "reason": "test reason"}],
    }), encoding="utf-8")
    (fake / "outputs" / "latest" / "daily_memo.md").write_text(
        "# Memo Heading\n\nbody text", encoding="utf-8",
    )
    monkeypatch.setattr(appmod, "REPO_ROOT", fake)

    from fastapi.testclient import TestClient
    client = TestClient(appmod.app)
    body = client.get("/").text
    assert "ZZZX" in body
    assert "test reason" in body
    assert "Memo Heading" in body
    # HTMX auto-refresh marker
    assert "hx-trigger" in body or "hx-get" in body
