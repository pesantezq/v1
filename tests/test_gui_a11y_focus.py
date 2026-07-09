"""GUI Phase 1 — Item 5: keyboard-focus visibility.

The interactive macros (page_header's Refresh button, the evidence disclosure
<summary>) had hover states but no keyboard focus indicator, so a keyboard-only
operator could not see where focus was. These tests pin a focus-visible ring on
each interactive macro.
"""

from __future__ import annotations


def _module():
    from gui_v2.app import templates

    return templates.env.get_template("components/_ui.html").module


def test_page_header_refresh_button_has_focus_visible_ring():
    html = _module().page_header("Portfolio", "sub", "/dashboard/portfolio", "content")
    assert "focus-visible:ring" in html
    assert "focus-visible:outline-none" in html


def test_evidence_summary_has_focus_visible_ring():
    html = _module().evidence(["decision_plan.json", "portfolio_snapshot.json"])
    assert "focus-visible:ring" in html
