"""Route smoke tests using FastAPI TestClient.

Repointed in Task 1 (feat/gui-persona-cockpit):
  - / now redirects to /dashboard/today (302)
  - /portfolio, /research, /health, /operations, /risk-impact now redirect to
    /dashboard/* persona routes (302)
  - Persona nav replaced old nav links (Today/Portfolio/Quant/System/Memo)
  - HTMX select IDs updated for the new routes
  - Old content tests repointed to the redirect assertion + new dashboard path
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from gui_v2.app import app
    return TestClient(app)


@pytest.mark.parametrize("path,expected_persona_heading", [
    ("/dashboard/today", "Today"),
])
def test_new_persona_route_returns_200_and_heading(client, path: str, expected_persona_heading: str):
    """New canonical persona routes render 200 with the expected heading."""
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert expected_persona_heading in response.text


@pytest.mark.parametrize("path,expected_redirect_target", [
    ("/", "/dashboard/today"),
    ("/portfolio", "/dashboard/portfolio"),
    ("/research", "/dashboard/quant"),
    ("/health", "/dashboard/system"),
    ("/operations", "/dashboard/system"),
    ("/risk-impact", "/dashboard/portfolio"),
])
def test_old_route_redirects_to_persona_route(path: str, expected_redirect_target: str):
    """Old routes must redirect (302) to the canonical persona routes — not 404."""
    from gui_v2.app import app
    c = TestClient(app, follow_redirects=False)
    response = c.get(path)
    assert response.status_code == 302, (
        f"{path}: expected 302 redirect, got {response.status_code}"
    )
    assert response.headers["location"] == expected_redirect_target, (
        f"{path}: expected redirect to {expected_redirect_target!r}, "
        f"got {response.headers.get('location')!r}"
    )


def test_persona_nav_links_present_on_dashboard_today(client):
    """Persona top-nav links (Today/Portfolio/Quant/System/Memo) appear on dashboard."""
    body = client.get("/dashboard/today").text
    for nav in ("Today", "Portfolio", "Quant", "System", "Memo"):
        assert nav in body, f"persona nav link {nav!r} missing on /dashboard/today"


def test_htmx_select_present_on_dashboard_today(client):
    """dashboard/today must include hx-select to extract just its content container."""
    body = client.get("/dashboard/today").text
    assert 'hx-select="#dashboard-today-content"' in body, (
        "/dashboard/today: missing hx-select for #dashboard-today-content; "
        "HTMX will inject the full HTML document into the page instead "
        "of just the content fragment."
    )
    assert "?fragment=1" not in body, (
        "/dashboard/today: stale ?fragment=1 querystring present on an hx-get URL"
    )


def test_theme_toggle_present_and_dark_default(client):
    """Light/dark toggle must be present in the nav and the page must default
    to dark mode (the <html> tag itself has no data-theme attribute on
    server render — JS adds it only when the operator picked light).
    The CSS override block defines html[data-theme="light"] rules so the
    toggle works on the client."""
    body = client.get("/dashboard/today").text
    # Toggle button + handler script present
    assert 'id="theme-toggle"' in body
    assert "stockbot-theme" in body
    # The <html> tag itself does NOT carry a data-theme attribute server-side
    # (CSS rule selectors that contain the string are fine — we only care
    # about the actual element's attributes).
    import re
    html_tag = re.search(r"<html\b[^>]*>", body)
    assert html_tag is not None
    assert "data-theme" not in html_tag.group(0), (
        "<html> tag should not have data-theme set server-side; "
        "client JS sets it only when operator picked light."
    )
    # Light-mode CSS override block is shipped so the toggle works
    assert "html[data-theme=\"light\"]" in body


def test_severity_badge_classes_via_jinja_filter():
    from gui_v2.app import _severity_classes
    assert "emerald" in _severity_classes("OK")
    assert "sky"     in _severity_classes("INFO")
    assert "amber"   in _severity_classes("WARN")
    assert "rose"    in _severity_classes("FAIL")
    # Unknown severity falls back to neutral zinc tones
    assert "zinc"    in _severity_classes("UNKNOWN")


def test_nav_contains_overall_severity_dot(client):
    body = client.get("/dashboard/today").text
    assert 'role="status"' in body
    assert ("emerald" in body or "sky" in body or "amber" in body or "rose" in body)


def test_research_route_redirects_to_quant(monkeypatch):
    """Old /research now redirects to /dashboard/quant.
    Previously it rendered the Automatic Promotion Review directly;
    that surface will move to /dashboard/quant in Task 3.
    This test preserves redirect coverage."""
    from gui_v2 import app as appmod
    from fastapi.testclient import TestClient
    c = TestClient(appmod.app, follow_redirects=False)
    r = c.get("/research")
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard/quant"
    # The advisory footer appears on the redirect destination (follow redirects)
    c2 = TestClient(appmod.app)
    # /dashboard/quant isn't built in Task 1 — we verify the redirect chain only
    # (Task 3 will add content assertions for /dashboard/quant)


def test_health_route_redirects_to_system(client):
    """Old /health now redirects to /dashboard/system.
    Previously it rendered all health sections directly;
    that surface will move to /dashboard/system in Task 4.
    This test preserves redirect coverage."""
    from gui_v2.app import app
    c = TestClient(app, follow_redirects=False)
    r = c.get("/health")
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard/system"


def test_today_renders_observe_only_banner_and_cards(tmp_path, monkeypatch):
    """Repointed from old test_today_renders_decisions_and_memo.
    / now redirects to /dashboard/today which renders the new persona cockpit.
    Verifies the observe-only banner and cards are present."""
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
    monkeypatch.setattr(appmod, "REPO_ROOT", fake)

    from fastapi.testclient import TestClient
    client = TestClient(appmod.app)
    # / redirects → /dashboard/today (follow_redirects=True by default)
    body = client.get("/").text
    assert "Observe-only" in body
    assert "Decision core" in body
    # HTMX auto-refresh marker
    assert "hx-trigger" in body or "hx-get" in body


def test_today_renders_decision_center_sections(tmp_path, monkeypatch):
    """Repointed: / now redirects to /dashboard/today (persona cockpit shell).
    The old 'Full decision queue' / 'AI validation' / 'Decision performance'
    sections were on the old today.html — they will be surfaced again under
    /dashboard/portfolio in Task 2. Here we verify / redirects and the new
    dashboard renders the persona card layer."""
    import json
    from gui_v2 import app as appmod
    fake = tmp_path
    (fake / "main.py").write_text("# marker\n", encoding="utf-8")
    (fake / "outputs" / "latest").mkdir(parents=True)
    (fake / "outputs" / "policy").mkdir(parents=True)

    (fake / "outputs" / "latest" / "decision_plan.json").write_text(json.dumps({
        "generated_at": "x", "total_decisions": 7, "observe_only": True,
        "decisions": [
            {"symbol": f"S{i}", "decision": "BUY", "priority": float(i),
             "urgency": "high", "source": "test",
             "reason": f"reason {i}",
             "recommended_amount": 100.0 * (i + 1)}
            for i in range(7)
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(appmod, "REPO_ROOT", fake)

    from fastapi.testclient import TestClient
    c = TestClient(appmod.app)
    # / follows redirect to /dashboard/today
    body = c.get("/").text
    # New today persona renders observe-only banner + decision core card
    assert "Observe-only" in body
    assert "Decision core" in body
    assert "System health" in body

    # Direct redirect test (no follow)
    c_no_follow = TestClient(appmod.app, follow_redirects=False)
    r = c_no_follow.get("/")
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard/today"
