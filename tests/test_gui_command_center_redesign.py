"""Command-center redesign (feat/gui-command-center-redesign).

Covers the NEW presentation surface added by the redesign while the data
loaders and their contracts are unchanged:

  * `human_time` Jinja filter (timestamp humanization, raw kept in tooltip)
  * per-tab empty-state rendering (no artifacts → 200, no crash)
  * active-tab nav state (aria-current)
  * grouped advisory decision queue (BUY/SELL/... groups inside the queue only)
  * compact all-clear failure-queue state
  * memo report toolbar (copy link / print / refresh) with no new JS deps
  * a global "no execution controls" guardrail across every rendered page

These complement the milestone tests in test_gui_dashboard_*.py — they assert
the redesign's added affordances, not the loader behavior.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

DASH_ROUTES = [
    "/dashboard/today",
    "/dashboard/portfolio",
    "/dashboard/quant",
    "/dashboard/system",
    "/dashboard/memo",
]

# Execution-oriented language that must never appear anywhere in the UI.
_FORBIDDEN = (
    "execute trade",
    "buy now",
    "sell now",
    "place order",
    "auto-trade",
    "auto trade",
    "auto-approve",
    "submit order",
    "place trade",
)


@pytest.fixture(scope="module")
def client() -> TestClient:
    from gui_v2.app import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# human_time filter
# ---------------------------------------------------------------------------


def test_human_time_datetime():
    from gui_v2.app import _human_time

    assert _human_time("2026-06-08T09:00:00") == "Jun 08, 09:00"
    assert _human_time("2026-06-08T09:00:00Z") == "Jun 08, 09:00"


def test_human_time_date_only():
    from gui_v2.app import _human_time

    assert _human_time("2026-06-08") == "Jun 08, 2026"


def test_human_time_empty_and_garbage_are_safe():
    from gui_v2.app import _human_time

    assert _human_time(None) == ""
    assert _human_time("") == ""
    # Unparseable input is returned unchanged — never hidden from the operator.
    assert _human_time("sometime yesterday") == "sometime yesterday"


def test_human_time_registered_as_filter():
    from gui_v2.app import templates

    assert "human_time" in templates.env.filters


# ---------------------------------------------------------------------------
# Empty-state rendering — every tab survives missing artifacts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", DASH_ROUTES)
def test_every_tab_renders_200_with_no_artifacts(route, tmp_path, monkeypatch):
    from gui_v2 import app as app_module

    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    c = TestClient(app_module.app)
    r = c.get(route)
    assert r.status_code == 200, f"{route} did not render 200 with empty artifacts"
    # Global safety banner survives the empty state (accurate two-lane wording:
    # no brokerage execution + human-gated production — no longer "observe-only").
    assert "No brokerage trade execution" in r.text


def test_today_empty_state_message(tmp_path, monkeypatch):
    from gui_v2 import app as app_module

    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    c = TestClient(app_module.app)
    # Today always emits its 3 cards even when artifacts are absent, so the
    # hero strip renders; assert it does not crash and shows the strip.
    r = c.get("/dashboard/today")
    assert r.status_code == 200
    assert "Today status strip" in r.text


# ---------------------------------------------------------------------------
# Active-tab nav state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", DASH_ROUTES)
def test_active_nav_marked(client, route):
    body = client.get(route).text
    assert 'aria-current="page"' in body, f"{route}: no active nav marker"


# ---------------------------------------------------------------------------
# Grouped advisory decision queue
# ---------------------------------------------------------------------------


def test_decision_queue_groups_by_action(tmp_path, monkeypatch):
    """A plan with BUY + SELL renders both action groups, and the action verbs
    appear only inside the advisory decision queue section."""
    from gui_v2 import app as app_module

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    (tmp_path / "outputs" / "portfolio").mkdir(parents=True)
    (latest / "decision_plan.json").write_text(json.dumps({
        "generated_at": "2026-06-08T09:00:00",
        "observe_only": True,
        "total_decisions": 2,
        "decisions": [
            {"symbol": "AAPL", "decision": "BUY", "priority": 0.9,
             "urgency": "high", "reason": "momentum", "confidence": 0.8,
             "source": "decision_plan"},
            {"symbol": "TSLA", "decision": "SELL", "priority": 0.7,
             "urgency": "medium", "reason": "overextended", "confidence": 0.6,
             "source": "decision_plan"},
        ],
    }))
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    c = TestClient(app_module.app)
    r = c.get("/dashboard/portfolio")
    assert r.status_code == 200
    html = r.text

    assert "AAPL" in html and "TSLA" in html
    assert 'aria-label="Advisory decision queue"' in html

    # Action verbs must live ONLY inside the advisory decision queue section.
    sec = re.search(
        r'<section[^>]*aria-label="Advisory decision queue"[^>]*>(.*?)</section>',
        html, re.DOTALL | re.IGNORECASE,
    )
    assert sec, "advisory decision queue section missing"
    inside = sec.group(1)
    assert ">BUY<" in inside and ">SELL<" in inside
    outside = html[: sec.start()] + html[sec.end():]
    # No inline-flex badge outside the queue may carry a directional verb.
    for badge in re.findall(r'class="inline-flex[^"]*"[^>]*>([^<]{1,40})<', outside):
        assert not re.search(r"\b(buy|sell|scale)\b", badge, re.IGNORECASE), badge


# ---------------------------------------------------------------------------
# Compact all-clear failure-queue state
# ---------------------------------------------------------------------------


def test_failure_queue_all_clear_when_healthy(tmp_path, monkeypatch):
    from gui_v2 import app as app_module

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    (latest / "daily_run_status.json").write_text(json.dumps({
        "generated_at": "2026-06-08T09:00:00",
        "overall_status": "ok",
        "observe_only": True,
        "stage_summary": {"ok": 12, "failed": 0, "warn": 0},
        "content_liveness": {},
    }))
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    c = TestClient(app_module.app)
    r = c.get("/dashboard/system")
    assert r.status_code == 200
    assert "No failed or warned stages" in r.text


# ---------------------------------------------------------------------------
# Memo report toolbar
# ---------------------------------------------------------------------------


def test_memo_toolbar_actions_present(client):
    body = client.get("/dashboard/memo").text
    assert "Copy link" in body
    assert "window.print()" in body  # print/export with no new dependency
    assert "Refresh" in body


def test_memo_blockquote_strips_marker_keeps_bold(tmp_path, monkeypatch):
    """Regression: blockquote verdict lines must not render the 'gt;' artifact
    (caused by slicing an html-escaped '&gt;' by one char) and must preserve
    inline bold/code formatting."""
    from gui_v2 import app as app_module

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    (latest / "daily_memo.md").write_text(
        "# Daily Investment Memo — 2026-06-08\n\n"
        "## Today's Verdict\n\n"
        "> **Cautious** — portfolio near a cap.\n"
    )
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    c = TestClient(app_module.app)
    r = c.get("/dashboard/memo")
    assert r.status_code == 200
    html = r.text
    bq = re.search(r"<blockquote[^>]*>(.*?)</blockquote>", html, re.DOTALL)
    assert bq, "verdict blockquote missing"
    content = bq.group(1).strip()
    assert not content.startswith("gt;"), f"mangled blockquote marker: {content!r}"
    assert "<strong>Cautious</strong>" in content, "inline bold lost in blockquote"


def test_memo_adds_no_script_dependencies(client):
    """The redesign must not pull in a heavy frontend framework. The memo page
    should ship no <script src=...> tags of its own (htmx/tailwind load from
    base.html; inline onclick handlers are allowed)."""
    body = client.get("/dashboard/memo").text
    # base.html ships exactly the htmx + tailwind CDN tags; the memo content
    # block must not add more script-src tags.
    content = body.split("dashboard-memo-content", 1)[-1]
    assert "<script src" not in content


# ---------------------------------------------------------------------------
# Global "no execution controls" guardrail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", DASH_ROUTES)
def test_no_execution_controls_anywhere(client, route):
    body = client.get(route).text.lower()
    offenders = [w for w in _FORBIDDEN if w in body]
    assert offenders == [], f"{route}: execution-oriented language found: {offenders}"


@pytest.mark.parametrize("route", DASH_ROUTES)
def test_no_trade_post_forms(client, route):
    """No tab may contain a form that POSTs to a broker/trade/order endpoint."""
    body = client.get(route).text
    for m in re.findall(r'<form[^>]*action="([^"]*)"', body, re.IGNORECASE):
        assert not re.search(r"(buy|sell|order|trade|execute)", m, re.IGNORECASE), (
            f"{route}: form posts to execution-like endpoint {m!r}"
        )


# ---------------------------------------------------------------------------
# Shared macro library behavior
# ---------------------------------------------------------------------------


def test_status_badge_macro_humanizes_label(client):
    """The status_card badge runs labels through status_label (no raw enums)."""
    # The system page exercises status_card heavily; a healthy run with
    # ok_with_warnings must not leak the raw snake_case token.
    body = client.get("/dashboard/system").text
    assert "ok_with_warnings" not in body


def test_evidence_disclosure_present(client):
    """Source artifacts are disclosed via the low-noise 'Evidence ·' affordance
    rather than the old repeated 'Sources (n)' clutter."""
    body = client.get("/dashboard/system").text
    assert "Evidence ·" in body
    assert "Sources (" not in body  # old noisy affordance removed
