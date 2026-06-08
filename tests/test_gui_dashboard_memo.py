"""Task 5 (Milestone 5): /dashboard/memo — phone-readable daily memo view.

Tests
-----
- collect_memo_view: memo present → 6 sections, source_artifacts, persona, observe_only
- collect_memo_view: memo absent → explicit empty state
- collect_memo_view: fixture memo with ## headers → all 6 section titles present
- no raw 16-hex fingerprint hash in view sections when memo contains one
- source_artifacts == ["daily_memo.md"]
- route renders 200 (memo present + absent → empty state page)
- all 6 section headings appear in rendered HTML when memo present
- no raw 16-hex fingerprint hash in rendered mobile memo HTML
- no forbidden action labels in rendered HTML
- no forbidden action labels in template file
- mobile-first layout: no wide table; sections are stacked divs
- empty state: "No memo" message visible when memo absent
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SECTION_TITLES = [
    "Top Insight",
    "Risk Focus",
    "Portfolio Decisions",
    "Data Quality",
    "Quant Notes",
    "Watchlist Notes",
]

_FORBIDDEN_LABELS = (
    "execute trade",
    "buy now",
    "sell now",
    "place order",
    "auto-trade",
    "auto trade",
    "auto-approve",
)

# Regex: 16 consecutive hex chars that are NOT surrounded by word chars
# Used to assert absence of raw fingerprint hashes.
_HEX_HASH_RE = re.compile(r"\b[0-9a-f]{16}\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_latest(tmp_path: Path) -> Path:
    d = tmp_path / "outputs" / "latest"
    d.mkdir(parents=True)
    return d


# Minimal fixture memo covering all 6 section headers
_FIXTURE_MEMO = """\
# Daily Investment Memo — 2026-06-08

**Date:** 2026-06-08
**Generated:** 2026-06-08 09:00:00

## Today's Verdict

> **Cautious** — portfolio near a cap; 21 advisory action(s). Retune NOT validated vs prior gauge f60e0b9d51bec808 (n=176).

## Top Insight

> Defense is the dominant theme with strong persistence.

## Top Decisions
- **BUY** `CSX` | priority `0.550` | source `market` | urgency `medium`
  - momentum: +1.64% today, RS: near 52wk high (-0.5%).

## Capital Actions
- SELL: 0 | SCALE: 3 | BUY: 18
- Total recommended capital: $3,559.61

## Risk Focus
- No structural risk actions lead the current decision set.

## What Changed
- Top theme changed: AI Infrastructure → Defense

## Portfolio Pulse
- Conviction allocation — high 0.0%, normal 6.0%, starter 3.0%

## Risk Delta
- Concentration — top position QQQ at 56.8% (cap 60%, headroom +3.2pp)

## Advisor Stack
- Pattern recognition (ml_advisor): ON — 5367 history records
- Retune impact: NOT validated — current-fp -24.1pp vs prior gauge f60e0b9d (n=176)

## Watch list — pattern-confirmed candidates (advisory)
- `AMD` (Technology) — 1 winning tag(s): Technology
- `NVDA` (Technology) — 1 winning tag(s): Technology

## Portfolio Growth
- **Total value:** $7,452.76  (cash: $464.16)

## Decision Hit Rate — Predicted vs Actual
- **Past 30 days:** 98 of 197 resolved decisions correct (49.8%).

## What To Watch — Sandbox Only
_No sandbox research candidates in MONITOR or NEEDS_REVIEW state._

## System / Data Health
- 2 advisory artifacts not yet populated.

---
_Advisory only — no trades executed._
"""


def _write_memo(latest: Path, content: str = _FIXTURE_MEMO) -> None:
    (latest / "daily_memo.md").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Unit tests: collect_memo_view — absent memo
# ---------------------------------------------------------------------------


def test_memo_view_absent_returns_empty_state(tmp_path):
    """When daily_memo.md is absent, view returns empty=True with message."""
    from gui_v2.data.dash_memo import collect_memo_view

    _make_latest(tmp_path)  # no memo file written
    v = collect_memo_view(tmp_path)
    assert v["empty"] is True
    assert v["sections"] == []
    assert "No memo" in v["empty_message"]


def test_memo_view_absent_source_artifacts(tmp_path):
    """source_artifacts must be ['daily_memo.md'] even when memo absent."""
    from gui_v2.data.dash_memo import collect_memo_view

    _make_latest(tmp_path)
    v = collect_memo_view(tmp_path)
    assert v["source_artifacts"] == ["daily_memo.md"]


def test_memo_view_absent_persona_field(tmp_path):
    from gui_v2.data.dash_memo import collect_memo_view

    _make_latest(tmp_path)
    v = collect_memo_view(tmp_path)
    assert v["persona"] == "memo"


def test_memo_view_absent_observe_only(tmp_path):
    from gui_v2.data.dash_memo import collect_memo_view

    _make_latest(tmp_path)
    v = collect_memo_view(tmp_path)
    assert v.get("observe_only") is True


# ---------------------------------------------------------------------------
# Unit tests: collect_memo_view — memo present
# ---------------------------------------------------------------------------


def test_memo_view_present_not_empty(tmp_path):
    """When memo present, empty=False."""
    from gui_v2.data.dash_memo import collect_memo_view

    latest = _make_latest(tmp_path)
    _write_memo(latest)
    v = collect_memo_view(tmp_path)
    assert v["empty"] is False


def test_memo_view_has_six_sections(tmp_path):
    """All 6 section titles are present in the sections list."""
    from gui_v2.data.dash_memo import collect_memo_view

    latest = _make_latest(tmp_path)
    _write_memo(latest)
    v = collect_memo_view(tmp_path)
    titles = [s["title"] for s in v["sections"]]
    for expected in SECTION_TITLES:
        assert expected in titles, f"Section '{expected}' missing from sections: {titles}"


def test_memo_view_source_artifacts_present(tmp_path):
    """source_artifacts == ['daily_memo.md'] when memo present."""
    from gui_v2.data.dash_memo import collect_memo_view

    latest = _make_latest(tmp_path)
    _write_memo(latest)
    v = collect_memo_view(tmp_path)
    assert v["source_artifacts"] == ["daily_memo.md"]


def test_memo_view_persona_present(tmp_path):
    from gui_v2.data.dash_memo import collect_memo_view

    latest = _make_latest(tmp_path)
    _write_memo(latest)
    v = collect_memo_view(tmp_path)
    assert v["persona"] == "memo"


def test_memo_view_observe_only_present(tmp_path):
    from gui_v2.data.dash_memo import collect_memo_view

    latest = _make_latest(tmp_path)
    _write_memo(latest)
    v = collect_memo_view(tmp_path)
    assert v.get("observe_only") is True


def test_memo_view_memo_date_extracted(tmp_path):
    """memo_date is extracted from the memo header."""
    from gui_v2.data.dash_memo import collect_memo_view

    latest = _make_latest(tmp_path)
    _write_memo(latest)
    v = collect_memo_view(tmp_path)
    assert v["memo_date"] is not None
    assert "2026-06-08" in v["memo_date"]


# ---------------------------------------------------------------------------
# Unit tests: fingerprint hash stripping
# ---------------------------------------------------------------------------


def test_no_raw_hex_hash_in_section_lines(tmp_path):
    """Raw 16-hex fingerprint tokens must not appear in any section lines."""
    from gui_v2.data.dash_memo import collect_memo_view

    latest = _make_latest(tmp_path)
    # The fixture memo contains f60e0b9d51bec808 — it must be stripped
    _write_memo(latest)
    v = collect_memo_view(tmp_path)

    violations: list[str] = []
    for sec in v["sections"]:
        for line in sec["lines"]:
            m = _HEX_HASH_RE.search(line)
            if m:
                violations.append(
                    f"Section '{sec['title']}': raw hash '{m.group()}' found in: {line!r}"
                )
    assert violations == [], "Raw 16-hex fingerprint hashes found:\n" + "\n".join(violations)


def test_strip_fingerprint_preserves_other_content(tmp_path):
    """Hash stripping must not remove non-hash content (e.g. prices, tickers)."""
    from gui_v2.data.dash_memo import collect_memo_view

    latest = _make_latest(tmp_path)
    _write_memo(latest)
    v = collect_memo_view(tmp_path)

    all_text = " ".join(
        line for sec in v["sections"] for line in sec["lines"]
    )
    # Ticker and price must survive
    assert "CSX" in all_text or "NVDA" in all_text or "QQQ" in all_text, (
        "Expected ticker names to survive hash stripping"
    )


# ---------------------------------------------------------------------------
# Route / integration tests
# ---------------------------------------------------------------------------


def test_memo_route_returns_200_memo_present():
    """GET /dashboard/memo returns 200 when daily memo exists."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/memo")
    assert r.status_code == 200


def test_memo_route_returns_200_memo_absent(tmp_path, monkeypatch):
    """GET /dashboard/memo returns 200 and shows empty state when memo absent."""
    from gui_v2 import app as app_module

    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    (tmp_path / "outputs" / "latest").mkdir(parents=True)

    from gui_v2.app import app as fastapi_app
    client = TestClient(fastapi_app)
    r = client.get("/dashboard/memo")
    assert r.status_code == 200
    assert "No memo" in r.text or "daily pipeline" in r.text.lower()


def test_memo_route_all_six_section_headings_present():
    """Rendered /dashboard/memo contains all 6 section headings in the HTML."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/memo")
    assert r.status_code == 200
    text = r.text
    for title in SECTION_TITLES:
        assert title in text, f"Section heading '{title}' not found in rendered HTML"


def test_memo_route_no_raw_hex_hash_in_html():
    """Rendered /dashboard/memo must not contain raw 16-hex fingerprint tokens."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/memo")
    assert r.status_code == 200
    # Find all 16-hex-char tokens in the HTML body
    matches = _HEX_HASH_RE.findall(r.text)
    assert matches == [], (
        f"Raw 16-hex fingerprint hashes found in rendered memo HTML: {matches[:5]}"
    )


def test_memo_route_no_forbidden_labels():
    """Rendered /dashboard/memo must not contain forbidden action labels."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/memo")
    assert r.status_code == 200
    text = r.text.lower()
    offenders = [label for label in _FORBIDDEN_LABELS if label in text]
    assert offenders == [], f"Forbidden labels in /dashboard/memo: {offenders}"


def test_memo_route_no_wide_table():
    """Mobile-first: memo HTML must not contain a <table> element (no wide tables)."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/memo")
    assert r.status_code == 200
    assert "<table" not in r.text.lower(), (
        "Wide <table> element found in memo page — sections should be stacked divs/sections"
    )


def test_memo_route_has_stacked_sections():
    """Rendered memo contains stacked <section> elements (mobile-first layout)."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/memo")
    assert r.status_code == 200
    # Must have multiple section elements — the stacked layout
    assert r.text.count("<section") >= 3, (
        "Expected at least 3 <section> elements for stacked mobile layout"
    )


# ---------------------------------------------------------------------------
# Template file grep: no forbidden labels in template
# ---------------------------------------------------------------------------


def test_no_forbidden_action_labels_in_memo_template():
    """memo.html must not contain forbidden action label strings."""
    template_path = Path("gui_v2/templates/dashboard/memo.html")
    text = template_path.read_text(encoding="utf-8").lower()
    offenders = [label for label in _FORBIDDEN_LABELS if label in text]
    assert offenders == [], f"Forbidden labels in memo.html: {offenders}"
