"""GUI Phase 3 — surface memo_coherence structured data on the Memo tab.

memo_coherence.json is the structured backbone behind the daily memo (funding
math, reconciliation, investor summary, coherence verdict) and was consumed by
nothing in the GUI. Phase 3 surfaces it as a panel above the memo prose so the
operator gets the structured summary + a coherence-status verdict, not just the
rendered markdown.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

_MC = {
    "generated_at": "2026-07-09T09:03:00",
    "observe_only": True,
    "coherence_status": "ok",
    "funding": {
        "available": True, "status": "ok",
        "portfolio_value": 10451.09, "available_cash": 3151.09,
        "cash_reserve_amount": 522.55,
    },
    "reconciliation": {
        "status": "ok", "issue_count": 6,
        "unresolved_issues": [],
    },
    "investor_summary": {
        "posture_paragraph": "Steady and mostly hold. Lead theme is AI Infrastructure.",
        "main_opportunity": "NVDA",
        "main_risk": "Portfolio is highly correlated.",
        "what_changed": ["Top theme changed: Defense -> AI Infrastructure"],
    },
}


def _memo_view(with_mc: bool, with_memo: bool = True) -> dict:
    from gui_v2.data.dash_memo import collect_memo_view

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        latest = root / "outputs" / "latest"
        latest.mkdir(parents=True)
        if with_memo:
            (latest / "daily_memo.md").write_text("# Daily Investment Memo — 2026-07-09\n\nbody\n", encoding="utf-8")
        if with_mc:
            (latest / "memo_coherence.json").write_text(json.dumps(_MC), encoding="utf-8")
        return collect_memo_view(root)


def test_coherence_view_shaped():
    v = _memo_view(with_mc=True)
    coh = v.get("coherence")
    assert coh is not None
    assert coh["status"] == "ok"
    assert coh["main_opportunity"] == "NVDA"
    assert "AI Infrastructure" in coh["posture"]
    assert coh["portfolio_value"] == 10451.09
    assert coh["available_cash"] == 3151.09
    assert coh["unresolved_issue_count"] == 0
    assert coh["what_changed"] == ["Top theme changed: Defense -> AI Infrastructure"]


def test_coherence_absent_when_artifact_missing():
    v = _memo_view(with_mc=False)
    assert v.get("coherence") is None


def test_coherence_present_even_when_memo_absent():
    # memo_coherence can exist independently; still surface it.
    v = _memo_view(with_mc=True, with_memo=False)
    assert v.get("coherence") is not None
    assert v["empty"] is True


def test_coherence_panel_renders():
    from gui_v2.app import templates

    ctx = {
        "persona": "memo", "observe_only": True, "empty": False,
        "empty_message": "", "memo_date": "2026-07-09", "sections": [],
        "source_artifacts": ["daily_memo.md"],
        "coherence": {
            "status": "ok", "posture": "Steady and mostly hold.",
            "main_opportunity": "NVDA", "main_risk": "Highly correlated.",
            "what_changed": ["Top theme changed"],
            "portfolio_value": 10451.09, "available_cash": 3151.09,
            "cash_reserve_amount": 522.55, "unresolved_issue_count": 0,
            "issue_count": 6, "generated_at": "2026-07-09T09:03:00",
        },
    }
    html = templates.env.get_template("dashboard/memo.html").render(**ctx)
    assert "Memo Coherence" in html
    assert "NVDA" in html
    assert "$10,451" in html          # portfolio value formatted
    assert "Steady and mostly hold" in html
