"""GUI Phase 4 — cross-cutting keyboard-focus visibility.

Phase 1 added focus-visible rings to the two shared _ui macros, but many tabs
have hand-rolled buttons/links/summaries (memo copy/print, portfolio links, etc.)
with no focus indicator. Rather than edit every template, a single global
:focus-visible rule in base.html's <style> gives every interactive element a
visible keyboard-focus ring. This test pins that rule.
"""

from __future__ import annotations


def _render_any_page() -> str:
    from fastapi.testclient import TestClient
    from gui_v2.app import app

    return TestClient(app).get("/dashboard/today").text


def test_global_focus_visible_rule_present():
    html = _render_any_page()
    # The base <style> must define a keyboard-focus outline for interactive elements.
    assert ":focus-visible" in html
    assert "button:focus-visible" in html
    assert "outline" in html


def test_focus_rule_covers_links_and_summary():
    html = _render_any_page()
    for selector in ("a:focus-visible", "summary:focus-visible"):
        assert selector in html, f"missing {selector} in global focus rule"
