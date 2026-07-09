"""GUI backlog — per-decision triage badges on Portfolio decision cards.

decision_triage.json's buckets list each decision with its triage_bucket +
severity. Annotate each Portfolio decision (by symbol) with a triage badge so the
operator sees which bucket a specific pick fell into, not just the header totals.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path


def _view(dp: dict, triage: dict | None) -> dict:
    from gui_v2.data.dash_portfolio import collect_portfolio_view

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        latest = root / "outputs" / "latest"
        latest.mkdir(parents=True)
        (latest / "decision_plan.json").write_text(json.dumps(dp), encoding="utf-8")
        if triage is not None:
            (latest / "decision_triage.json").write_text(json.dumps(triage), encoding="utf-8")
        return collect_portfolio_view(root)


_DP = {"observe_only": True, "decisions": [
    {"symbol": "VFH", "decision": "SCALE", "reason": "rebalance"},
    {"symbol": "FANG", "decision": "BUY", "reason": "momentum"},
]}
_TRIAGE = {
    "available": True, "total_decisions": 2,
    "bucket_counts": {"critical_action": 0, "action_candidate": 1, "monitor": 1, "ignore_for_now": 0},
    "buckets": {
        "critical_action": [],
        "action_candidate": [{"symbol": "VFH", "decision": "SCALE", "triage_bucket": "action_candidate", "severity": "medium", "reason": "eligible action candidate"}],
        "monitor": [{"symbol": "FANG", "decision": "BUY", "triage_bucket": "monitor", "severity": "low", "reason": "watch, no action"}],
        "ignore_for_now": [],
    },
}


def _decision(view, ticker):
    return next(d for d in view["decisions"] if d["ticker"] == ticker)


def test_decisions_annotated_with_triage_bucket():
    v = _view(_DP, _TRIAGE)
    vfh = _decision(v, "VFH")
    assert vfh["triage_bucket"] == "action_candidate"
    assert vfh["triage_label"] == "Action candidate"
    assert vfh["triage_sev"] == "yellow"          # action_candidate → yellow
    fang = _decision(v, "FANG")
    assert fang["triage_label"] == "Monitor"
    assert fang["triage_sev"] == "blue"           # monitor → blue


def test_no_triage_fields_when_artifact_absent():
    v = _view(_DP, None)
    vfh = _decision(v, "VFH")
    assert "triage_label" not in vfh or vfh.get("triage_label") is None


def test_decision_card_renders_triage_badge():
    from gui_v2.app import templates

    d = {"ticker": "VFH", "action": "SCALE", "rationale": "rebalance",
         "conviction": "medium", "confidence": 55, "priority": 0.55,
         "triage_bucket": "action_candidate", "triage_label": "Action candidate",
         "triage_sev": "yellow"}
    html = templates.env.get_template("components/decision_card.html").render(d=d)
    assert "Action candidate" in html
