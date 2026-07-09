"""GUI Phase 3 — triage context on the Portfolio advisory decision queue.

The Portfolio tab is where decisions are worked. A verb-free triage breakdown
(from decision_triage.json) at the queue header gives the operator the
prioritization lens in place — distinct from the Today cockpit's at-a-glance
card. Action verbs stay on the decision cards themselves.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path


def _view(payload: dict | None) -> dict:
    from gui_v2.data.dash_portfolio import collect_portfolio_view

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        latest = root / "outputs" / "latest"
        latest.mkdir(parents=True)
        if payload is not None:
            (latest / "decision_triage.json").write_text(json.dumps(payload), encoding="utf-8")
        return collect_portfolio_view(root)


def test_triage_summary_shaped():
    v = _view({
        "available": True, "total_decisions": 45,
        "bucket_counts": {"critical_action": 0, "action_candidate": 1,
                          "monitor": 30, "ignore_for_now": 14},
    })
    ts = v.get("triage_summary")
    assert ts is not None
    assert ts["total"] == 45
    assert ts["critical"] == 0
    assert ts["action"] == 1
    assert ts["monitor"] == 30
    assert ts["ignore"] == 14


def test_triage_summary_absent_when_unavailable():
    assert _view(None).get("triage_summary") is None
    assert _view({"available": False}).get("triage_summary") is None
