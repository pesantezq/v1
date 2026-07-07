"""Task 1 (Milestone 1): Shell — safety tests, card shape, base nav/banner/bottom-nav,
redirects, /dashboard/today."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# shared.card tests (Step 1)
# ---------------------------------------------------------------------------

from gui_v2.data import shared


def test_card_shape_and_severity_mapping():
    c = shared.card(
        "X",
        status="warning",
        label="L",
        summary="S",
        source_artifacts=["a.json"],
        updated_at="t",
    )
    assert set(c) == {"title", "status", "label", "summary", "source_artifacts", "updated_at", "severity"}
    assert c["severity"] == "yellow"
    assert shared.card("Y", status="bogus")["status"] == "unknown"
    assert shared.card("Z")["severity"] == "gray"


# ---------------------------------------------------------------------------
# dash_today tests (Step 4)
# ---------------------------------------------------------------------------


def test_today_view_has_health_and_decision_core_cards(tmp_path):
    from gui_v2.data.dash_today import collect_today_view

    (tmp_path / "outputs/latest").mkdir(parents=True)
    (tmp_path / "outputs/latest/decision_plan.json").write_text("{}")
    v = collect_today_view(tmp_path)
    titles = {c["title"] for c in v["cards"]}
    assert {"System health", "Decision core", "Risk"} <= titles
    dc = next(c for c in v["cards"] if c["title"] == "Decision core")
    assert dc["status"] == "ok"  # present


def test_today_decision_core_missing_is_red(tmp_path):
    from gui_v2.data.dash_today import collect_today_view

    (tmp_path / "outputs/latest").mkdir(parents=True)
    v = collect_today_view(tmp_path)
    dc = next(c for c in v["cards"] if c["title"] == "Decision core")
    assert dc["status"] == "red"  # absent -> red


# ---------------------------------------------------------------------------
# Safety tests (Step 7)
# ---------------------------------------------------------------------------

_FORBIDDEN = (
    "execute trade",
    "buy now",
    "sell now",
    "place order",
    "auto-trade",
    "auto trade",
    "auto-approve",
)


def test_no_forbidden_action_labels_in_templates():
    tpl = Path("gui_v2/templates")
    offenders = []
    for f in tpl.rglob("*.html"):
        text = f.read_text(encoding="utf-8").lower()
        for bad in _FORBIDDEN:
            if bad in text:
                offenders.append(f"{f}: {bad}")
    assert offenders == [], f"forbidden action labels: {offenders}"


def test_root_redirects_to_dashboard_today():
    from fastapi.testclient import TestClient
    from gui_v2.app import app

    c = TestClient(app, follow_redirects=False)
    r = c.get("/")
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard/today"


def test_old_routes_redirect_not_404():
    from fastapi.testclient import TestClient
    from gui_v2.app import app

    c = TestClient(app, follow_redirects=False)
    for old in ("/portfolio", "/risk-impact", "/research", "/health", "/operations"):
        resp = c.get(old)
        assert resp.status_code == 302, f"{old} returned {resp.status_code}, expected 302"


def test_dashboard_today_renders_and_has_observe_only_banner():
    from fastapi.testclient import TestClient
    from gui_v2.app import app

    c = TestClient(app)
    r = c.get("/dashboard/today")
    assert r.status_code == 200
    assert "Observe-only" in r.text


def test_htmx_is_vendored_locally_not_from_cdn():
    """htmx must be served from same-origin /static, not the unpkg CDN, so the
    dashboard's auto-refresh does not depend on an external host (2026-07-07)."""
    from fastapi.testclient import TestClient
    from gui_v2.app import app

    c = TestClient(app)
    html = c.get("/dashboard/today").text
    assert "/static/htmx.min.js" in html
    assert "unpkg.com" not in html
    # the static asset itself is served
    r = c.get("/static/htmx.min.js")
    assert r.status_code == 200
    assert b"htmx" in r.content


def test_stale_class_filter_flags_age():
    """The timestamp staleness filter colors old artifacts amber/rose and leaves
    fresh/unknown/unparseable muted (never alarms on missing data)."""
    from datetime import datetime, timezone, timedelta
    from gui_v2.app import _time_stale_class

    now = datetime.now(timezone.utc)
    assert _time_stale_class(now.isoformat()) == "text-zinc-500"          # fresh
    assert _time_stale_class((now - timedelta(hours=30)).isoformat()) == "text-amber-400"
    assert _time_stale_class((now - timedelta(days=3)).isoformat()) == "text-rose-400"
    assert _time_stale_class(None) == "text-zinc-500"                     # unknown
    assert _time_stale_class("not-a-date") == "text-zinc-500"             # unparseable
