"""Regression: the memo's Top Decisions section must use the SAME deterministic
tie-break as memo_coherence.apply_tie_break so the two operator-facing artifacts
never name different top-5 sets when priorities are tied.

Root cause (memo-reviewer 2026-07-19): daily_memo._top_decision_rows sorted by
`priority` alone with a stable sort, so within an N-way priority tie it preserved
the incoming summary order, which diverged from decision_plan/memo_coherence's
canonical (priority desc -> entry_move_pct desc -> confidence desc -> symbol asc)
ordering. Real-world symptom: memo showed PANW in the top-5 while
decision_plan/coherence showed CSCO, from an 18-way priority=0.55 plateau.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.daily_memo import _top_decision_rows
from portfolio_automation.memo_coherence import apply_tie_break


def _tied_summary() -> dict:
    """Six BUY rows all at the same priority (0.55) but different entry momentum,
    deliberately supplied in a NON-canonical order (not entry_move_pct desc)."""
    decisions = [
        {"symbol": "PANW", "decision": "BUY", "priority": 0.55, "entry_move_pct": 1.32, "confidence": 0.8},
        {"symbol": "FANG", "decision": "BUY", "priority": 0.55, "entry_move_pct": 2.85, "confidence": 0.8},
        {"symbol": "XBI", "decision": "BUY", "priority": 0.55, "entry_move_pct": 1.49, "confidence": 0.8},
        {"symbol": "LCID", "decision": "BUY", "priority": 0.55, "entry_move_pct": 13.93, "confidence": 0.8},
        {"symbol": "CSCO", "decision": "BUY", "priority": 0.55, "entry_move_pct": 2.08, "confidence": 0.8},
        {"symbol": "NET", "decision": "BUY", "priority": 0.55, "entry_move_pct": 1.89, "confidence": 0.8},
    ]
    return {"_decision_plan": {"decisions": decisions}}


def test_top_decision_rows_use_canonical_tie_break():
    summary = _tied_summary()
    top = _top_decision_rows(summary, limit=5)
    got = [r["symbol"] for r in top]
    # canonical: priority desc -> entry_move_pct desc -> confidence desc -> symbol asc
    assert got == ["LCID", "FANG", "CSCO", "NET", "XBI"], got


def test_memo_and_coherence_agree_on_top5():
    """The memo top-5 must equal the coherence-ranked top-5 for the same rows,
    regardless of the order the rows were supplied in."""
    decisions = _tied_summary()["_decision_plan"]["decisions"]
    coherence_top5 = [a["symbol"] for a in apply_tie_break(list(decisions))[:5]]
    memo_top5 = [r["symbol"] for r in _top_decision_rows({"_decision_plan": {"decisions": decisions}}, limit=5)]
    assert memo_top5 == coherence_top5, (memo_top5, coherence_top5)
