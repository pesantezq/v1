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
- responsive tables: the memo permits only responsive/contained tables — every
  rendered <table> must live inside an ancestor whose class contains
  `overflow-x-auto` (uncontained wide tables are rejected). Sections stay stacked.
- responsive header: memo header carries the mobile stacked / sm-horizontal classes
- operator tools live inside a collapsed <details> disclosure hidden from print
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


# ---------------------------------------------------------------------------
# Responsive-table contract (replaces the stale absolute no-<table> assertion).
#
# A <table> is acceptable iff it is scroll-contained: some ANCESTOR element
# carries a class containing `overflow-x-auto` (the ui.responsive_table() macro).
# Only an *uncontained* wide table fails. Uses a stdlib HTMLParser — no new
# dependency (per the task: do not add BeautifulSoup).
# ---------------------------------------------------------------------------

from html.parser import HTMLParser as _HTMLParser  # noqa: E402  (stdlib, grouped w/ helper)

# HTML void elements never have children, so they must not be pushed onto the
# ancestor stack (there is no matching end tag to pop them).
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})


class _ResponsiveTableParser(_HTMLParser):
    """Count <table>s and how many lack an `overflow-x-auto` ancestor."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[tuple[str, bool]] = []  # (tag, ancestor_has_overflow)
        self.tables_total = 0
        self.tables_uncontained = 0

    @staticmethod
    def _has_overflow(attrs) -> bool:
        cls = dict(attrs).get("class") or ""
        return "overflow-x-auto" in cls

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables_total += 1
            if not any(flag for _, flag in self._stack):
                self.tables_uncontained += 1
        if tag not in _VOID_TAGS:
            # propagate "an ancestor has overflow" down the open stack
            ancestor_overflow = self._has_overflow(attrs) or any(
                flag for _, flag in self._stack
            )
            self._stack.append((tag, ancestor_overflow))

    def handle_startendtag(self, tag, attrs):
        # self-closing (e.g. SVG <path/>) — never pushed.
        if tag == "table":
            self.tables_total += 1
            if not any(flag for _, flag in self._stack):
                self.tables_uncontained += 1

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break


def _scan_tables(html: str) -> _ResponsiveTableParser:
    p = _ResponsiveTableParser()
    p.feed(html)
    return p


def test_responsive_table_helper_passes_contained_table():
    """A <table> inside an overflow-x-auto ancestor is accepted."""
    html = '<div class="rounded-xl overflow-x-auto"><table><tr><td>x</td></tr></table></div>'
    p = _scan_tables(html)
    assert p.tables_total == 1
    assert p.tables_uncontained == 0


def test_responsive_table_helper_flags_uncontained_table():
    """A <table> with no responsive ancestor is flagged by the helper."""
    html = '<div class="p-4"><table><tr><td>x</td></tr></table></div>'
    p = _scan_tables(html)
    assert p.tables_total == 1
    assert p.tables_uncontained == 1


def test_memo_route_tables_are_responsive():
    """Every <table> on the memo page must be inside an overflow-x-auto ancestor.

    Tables are allowed (the operator work-order queue is a legitimate responsive
    table); only uncontained wide tables are rejected.
    """
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/memo")
    assert r.status_code == 200
    p = _scan_tables(r.text)
    assert p.tables_uncontained == 0, (
        f"{p.tables_uncontained} of {p.tables_total} table(s) on /dashboard/memo "
        "are not inside an overflow-x-auto container"
    )


def test_memo_route_header_is_responsive():
    """The memo header stacks on phones and goes horizontal from the sm breakpoint."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/memo")
    assert r.status_code == 200
    text = r.text
    assert "flex-col" in text and "sm:flex-row" in text, (
        "memo header missing responsive stacked/horizontal classes"
    )
    # action buttons wrap and are full-width on narrow screens
    assert "flex-wrap" in text
    assert "flex-1 sm:flex-none" in text


def test_memo_route_operator_tools_are_disclosed_and_print_hidden():
    """Operator tools live inside a <details> disclosure hidden when printing."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/memo")
    assert r.status_code == 200
    text = r.text
    # the disclosure exists, is labeled, and the whole area is print-hidden
    assert "Operator tools" in text
    assert re.search(r"<details[^>]*print:hidden", text), (
        "operator-tools <details> should carry print:hidden"
    )
    # copy/print/refresh toolbar preserved
    assert "Copy link" in text and "Print" in text and "Refresh" in text


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


# ---------------------------------------------------------------------------
# M2: inline-markdown conversion tests
# ---------------------------------------------------------------------------


def test_render_inline_md_bold_and_code():
    """_render_inline_md converts **x** to <strong>x</strong> and `x` to <code>x</code>."""
    from gui_v2.data.dash_memo import _render_inline_md

    result = _render_inline_md("**BUY** `CSX`")
    assert "<strong>BUY</strong>" in result, f"Expected <strong>BUY</strong> in: {result!r}"
    assert "<code>CSX</code>" in result, f"Expected <code>CSX</code> in: {result!r}"
    assert "**" not in result


def test_render_inline_md_xss_protection():
    """_render_inline_md HTML-escapes raw < > & characters — no XSS."""
    from gui_v2.data.dash_memo import _render_inline_md

    result = _render_inline_md("<script>alert('xss')</script>")
    assert "<script>" not in result, "XSS: raw <script> survived HTML escaping"
    assert "&lt;script&gt;" in result


def test_memo_route_renders_strong_and_code_for_bold_text(tmp_path, monkeypatch):
    """Rendered memo page shows <strong> and <code> for **bold** and `code` in memo lines."""
    import json as _json
    from gui_v2 import app as app_module
    from fastapi.testclient import TestClient as _TestClient

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    (latest / "daily_memo.md").write_text(
        "# Daily Investment Memo — 2026-06-08\n\n"
        "## Today's Verdict\n"
        "- **BUY** `CSX` on momentum signal\n"
    )

    original_root = app_module.REPO_ROOT
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    try:
        client = _TestClient(app_module.app)
        r = client.get("/dashboard/memo")
        assert r.status_code == 200
        html = r.text
        assert "<strong>BUY</strong>" in html, (
            f"Expected <strong>BUY</strong> in rendered memo; literal ** present: {'**' in html}"
        )
        assert "<code>CSX</code>" in html, "Expected <code>CSX</code> in rendered memo"
        assert "**BUY**" not in html
    finally:
        monkeypatch.setattr(app_module, "REPO_ROOT", original_root)


# ---------------------------------------------------------------------------
# Regression: memo section headers -> GUI section contract
#
# Commit a5387a27 replaced the memo's "Top Decisions" / "Capital Actions"
# sections with the "Today's Capital Plan" block, but _HEADER_MAP was not
# updated. Every capital-plan header mapped to None, so ~21 lines of the
# operator's actual capital actions, deferrals and bottom line were silently
# dropped from the mobile memo view (the section still rendered, filled only
# with "Top Movers" price lines, so the loss was invisible).
#
# That first fix (8686898d) was itself incomplete: it only mapped the three
# headers visible in the memo sample it was written against. "Funded Market
# Opportunities" and "Sell and Funding Dependencies" sit inside conditional
# blocks in capital_plan_view.render_capital_plan_md and were still silently
# dropped whenever those blocks fired.
#
# The root cause both times was the same: a HAND-MAINTAINED literal list of
# headers "someone observed once" cannot catch a header the author didn't
# happen to see. So instead of hand-listing capital_plan_view.py's headers,
# `_capital_plan_view_headers()` below statically extracts every literal
# string passed to `h(...)` in render_capital_plan_md via AST — including
# ones gated behind an `if` that a single fixture might never execute. A
# future header added to that function is picked up automatically; it cannot
# silently orphan itself the way both prior bugs did.
# ---------------------------------------------------------------------------


def _capital_plan_view_headers() -> list[str]:
    """Statically extract every header `capital_plan_view.render_capital_plan_md`
    can emit — the exhaustive set of `h("...")` call-site literals, including
    ones inside conditional branches (e.g. "Funded Market Opportunities" only
    renders when a funded action has entry-setup data available).

    This is source-derived, not a hand-maintained list: a header added to a
    new (or existing) conditional branch is picked up the moment this test
    module runs, without anyone needing to remember to update a literal.
    """
    import ast
    import inspect

    from portfolio_automation import capital_plan_view

    source = inspect.getsource(capital_plan_view.render_capital_plan_md)
    tree = ast.parse(source)
    headers: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "h"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            headers.append(node.args[0].value)
    return headers


# The capital-plan headers that belong in "Portfolio Decisions" — i.e. every
# header the producer emits except "Bottom Line" (which is the closing
# verdict and belongs with Top Insight; see test_bottom_line_maps_to_top_insight).
SHIPPED_CAPITAL_PLAN_HEADERS = [
    h for h in _capital_plan_view_headers() if h != "Bottom Line"
]


def test_capital_plan_view_headers_nonempty():
    """Sanity check that the AST extraction actually found the producer's
    h(...) call sites — an empty result would make every test below vacuous."""
    assert len(_capital_plan_view_headers()) >= 6, (
        "static extraction found too few headers in capital_plan_view.py — "
        "the AST walk may no longer match its render_capital_plan_md shape"
    )


@pytest.mark.parametrize("header", SHIPPED_CAPITAL_PLAN_HEADERS)
def test_capital_plan_headers_map_to_portfolio_decisions(header):
    """Every shipped capital-plan header (source-derived) must reach the
    Portfolio Decisions section — including headers inside conditional
    blocks, which is exactly what the previous, hand-maintained list missed."""
    from gui_v2.data.dash_memo import _map_header

    assert _map_header(header) == "Portfolio Decisions", (
        f"memo header {header!r} maps to {_map_header(header)!r}; "
        "capital-plan content would be dropped from the mobile memo view"
    )


def test_bottom_line_maps_to_top_insight():
    """`Bottom Line` is the closing verdict, so it joins Top Insight."""
    from gui_v2.data.dash_memo import _map_header

    assert _map_header("Bottom Line") == "Top Insight"


def test_operator_appendix_maps_to_data_quality():
    """`Operator / System Appendix` is system context, not an action."""
    from gui_v2.data.dash_memo import _map_header

    assert _map_header("Operator / System Appendix") == "Data Quality"


def test_no_shipped_memo_header_is_orphaned():
    """Every ## header the memo emits must map to some GUI section.

    This is the guard that would have caught a5387a27 (and its incomplete
    follow-up fix 8686898d): an unmapped header is silently skipped by
    _parse_memo, so a rename — or a new conditional header — loses content
    with no error.

    The capital_plan_view.py portion of this list is source-derived (see
    `_capital_plan_view_headers`), not hand-maintained, so it cannot repeat
    the exact failure mode that caused both prior bugs. The remaining
    headers below come from other memo-producing modules that do not yet
    have an equivalent static-extraction helper; they are out of scope for
    this regression (capital_plan_view.py was the implicated producer).
    """
    from gui_v2.data.dash_memo import _map_header

    shipped_headers = _capital_plan_view_headers() + [
        "Today's Verdict",
        "Top Insight",
        "Risk Focus",
        "What Changed",
        "Operator / System Appendix",
        "Portfolio Pulse",
        "Risk Delta",
        "Advisor Stack",
        "Watch list — pattern-confirmed candidates (advisory)",
        "Portfolio Growth",
        "Top Movers",
        "Decision Hit Rate — Predicted vs Actual",
        "What To Watch — Sandbox Only",
        "Crowd Radar — Sandbox Research",
        "System / Data Health",
        "Discovery Research — Sandbox Only",
    ]
    orphaned = [h for h in shipped_headers if _map_header(h) is None]
    assert orphaned == [], (
        f"memo headers with no GUI section: {orphaned} — their content is "
        "silently dropped from /dashboard/memo"
    )


def test_orphan_guard_detects_a_removed_mapping():
    """Meta-test: prove the orphan guard is not a tautology.

    Simulates the exact a5387a27 regression (capital-plan mappings dropped
    from _HEADER_MAP) and asserts the source-derived header list catches it.
    Without this, a future edit could make `_capital_plan_view_headers()`
    return `[]` (e.g. a refactor that renames `h(...)` to something else)
    and every test above would pass vacuously while the real bug recurred.
    """
    from gui_v2.data import dash_memo

    headers = _capital_plan_view_headers()
    assert headers, "extraction must find headers for this test to be meaningful"

    original_map = dash_memo._HEADER_MAP
    try:
        dash_memo._HEADER_MAP = [
            (fragment, section) for fragment, section in original_map
            if section != "Portfolio Decisions"
        ]
        orphaned = [h for h in headers if dash_memo._map_header(h) is None]
        assert orphaned, (
            "expected removing the Portfolio Decisions mappings to orphan "
            "the capital-plan headers; the guard would not have caught the "
            "real regression"
        )
    finally:
        dash_memo._HEADER_MAP = original_map


def test_capital_plan_content_reaches_portfolio_decisions(tmp_path):
    """End-to-end: capital-plan body lines land in Portfolio Decisions."""
    from gui_v2.data.dash_memo import _parse_memo

    memo = (
        "# Daily Investment Memo — 2026-07-28\n\n"
        "## Today's Capital Plan\n"
        "- Deployable now: $1,602.13\n"
        "## What To Do Today\n"
        "- Fund NVDA starter position\n"
        "## Bottom Line\n"
        "- Hold steady; nothing funded today.\n"
    )
    sections = _parse_memo(memo)

    decisions = "\n".join(sections.get("Portfolio Decisions") or [])
    assert "Deployable now: $1,602.13" in decisions
    assert "Fund NVDA starter position" in decisions

    insight = "\n".join(sections.get("Top Insight") or [])
    assert "Hold steady; nothing funded today." in insight
