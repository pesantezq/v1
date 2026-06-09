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
