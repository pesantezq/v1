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


@pytest.mark.parametrize("path,target_id", [
    ("/", "today-content"),
    ("/portfolio", "portfolio-content"),
    ("/research", "research-content"),
    ("/health", "health-content"),
    ("/operations", "operations-content"),
])
def test_htmx_select_present_to_prevent_full_doc_nesting(client, path: str, target_id: str):
    """Every HTMX-driven page must include hx-select to extract just its
    content container from the server response. Without this, HTMX dumps
    the entire HTML document (including <!doctype>...<html>) into the
    target div, nesting on each refresh and breaking layout."""
    body = client.get(path).text
    # Must reference its own content container in hx-select
    assert f'hx-select="#{target_id}"' in body, (
        f"{path}: missing hx-select=\"#{target_id}\" on HTMX call sites; "
        "HTMX will inject the full HTML document into the page instead "
        "of just the content fragment."
    )
    # The legacy `?fragment=1` querystring should not appear — it was a
    # leftover from the initial implementation and has no behaviour.
    assert "?fragment=1" not in body, (
        f"{path}: stale ?fragment=1 querystring present on an hx-get URL; "
        "drop it — the server does not handle it and it confuses caches."
    )


def test_theme_toggle_present_and_dark_default(client):
    """Light/dark toggle must be present in the nav and the page must default
    to dark mode (the <html> tag itself has no data-theme attribute on
    server render — JS adds it only when the operator picked light).
    The CSS override block defines html[data-theme="light"] rules so the
    toggle works on the client."""
    body = client.get("/").text
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
    body = client.get("/").text
    assert 'role="status"' in body
    assert ("emerald" in body or "sky" in body or "amber" in body or "rose" in body)


def test_research_renders_promotion_review(tmp_path, monkeypatch):
    """Research page surfaces the migrated Automatic Promotion Review when
    the producer artifact is present."""
    import json
    from gui_v2 import app as appmod
    fake = tmp_path
    (fake / "main.py").write_text("# marker\n", encoding="utf-8")
    (fake / "outputs" / "sandbox" / "discovery").mkdir(parents=True)
    (fake / "outputs" / "sandbox" / "discovery" / "automatic_promotion_candidates.json").write_text(
        json.dumps({
            "available": True,
            "generated_at": "2026-05-15T15:00:00+00:00",
            "run_mode": "discovery",
            "run_id": "x",
            "observe_only": True,
            "no_trade": True,
            "not_recommendation": True,
            "discovery_only": True,
            "sandbox_only": True,
            "no_portfolio_mutation": True,
            "no_watchlist_mutation": True,
            "no_allocation_policy_change": True,
            "no_decision_override": True,
            "decision_count": 1,
            "decisions": [{
                "ticker": "ZZZA",
                "proposed_status": "MONITOR",
                "composite_score": 0.72,
                "news_relevance_score": 0.6,
                "corroboration_score": 0.55,
                "risk_flag_count": 0,
                "risk_flags": [],
                "reason": "Strong corroboration; sandbox watch.",
            }],
        }), encoding="utf-8",
    )
    monkeypatch.setattr(appmod, "REPO_ROOT", fake)

    from fastapi.testclient import TestClient
    client = TestClient(appmod.app)
    body = client.get("/research").text
    assert "Automatic Promotion Review" in body
    assert "Moved to Monitor" in body
    assert "ZZZA" in body
    assert "Safety boundary" in body
    # Disclaimer language
    assert "not buy/sell recommendations" in body
    # The advisory footer
    assert "Advisory only" in body


def test_health_renders_all_sections(client):
    body = client.get("/health").text
    assert "Pipeline" in body or "status" in body.lower()
    assert "Artifact shape" in body or "smoke" in body.lower()
    assert "Environment" in body or "env" in body.lower()
    assert "registry" in body.lower()


def test_today_renders_decision_center_sections(tmp_path, monkeypatch):
    """Migrated Decision Center sections (validation, full queue, performance)
    appear when the underlying artifacts are present."""
    import json
    from gui_v2 import app as appmod
    fake = tmp_path
    (fake / "main.py").write_text("# marker\n", encoding="utf-8")
    (fake / "outputs" / "latest").mkdir(parents=True)
    (fake / "outputs" / "policy").mkdir(parents=True)

    # 7 decisions to trigger the full-queue section (cap is 5 for top)
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
    (fake / "outputs" / "latest" / "ai_decision_validation.json").write_text(json.dumps({
        "generated_at": "x", "observe_only": True, "available": True,
        "total_validated": 1, "aligned_count": 1, "caution_count": 0,
        "contradiction_count": 0, "insufficient_context_count": 0,
        "ai_used": False, "summary_line": "1 aligned",
        "validations": [{"symbol": "S0", "decision": "BUY",
                         "validation_status": "aligned",
                         "plain_english_summary": "ok",
                         "contradictions": [], "watch_next": ["earnings"]}],
    }), encoding="utf-8")
    (fake / "outputs" / "policy" / "decision_outcome_summary.json").write_text(json.dumps({
        "generated_at": "x", "total_decisions": 40, "resolved": 2,
        "unresolved": 38, "hit_rate": 0.0, "avg_return_pct": 0.0088,
        "by_decision": {}, "by_validation_status": {},
        "last_10_resolved": [], "best_decision": None, "worst_decision": None,
    }), encoding="utf-8")
    monkeypatch.setattr(appmod, "REPO_ROOT", fake)

    from fastapi.testclient import TestClient
    client = TestClient(appmod.app)
    body = client.get("/").text
    # Full queue surface
    assert "Full decision queue" in body
    assert "7 decisions total" in body
    # AI validation surface
    assert "AI validation" in body
    assert "Aligned" in body
    # Decision performance surface
    assert "Decision performance" in body
    assert "Hit rate" in body
    assert "Avg return" in body


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
