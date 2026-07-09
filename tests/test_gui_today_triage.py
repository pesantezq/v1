"""GUI Phase 3 — surface decision_triage on the Today cockpit (counts only).

decision_triage.json buckets all decisions into critical_action / action_candidate
/ monitor / ignore_for_now with a ranked top_actions list. The Today cockpit gets
a verb-free "Decision triage" card (bucket counts + summary line) so the operator
sees the decision workload at a glance. Action VERBS (SCALE/BUY) stay on the
Portfolio advisory decision queue per the observe-only contract — they are NOT
rendered here.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path


def _today(payload: dict | None) -> dict:
    from gui_v2.data.dash_today import collect_today_view

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        latest = root / "outputs" / "latest"
        latest.mkdir(parents=True)
        if payload is not None:
            (latest / "decision_triage.json").write_text(json.dumps(payload), encoding="utf-8")
        return collect_today_view(root)


def _triage_card(view: dict):
    return next((c for c in view["cards"] if c["title"] == "Decision triage"), None)


def test_triage_card_present_with_counts():
    v = _today({
        "available": True,
        "generated_at": "2026-07-09T09:03:00",
        "total_decisions": 45,
        "bucket_counts": {"critical_action": 0, "action_candidate": 1,
                          "monitor": 30, "ignore_for_now": 14},
        "summary_line": "45 decisions triaged. 0 critical, 1 action candidate(s).",
        "top_actions": [{"symbol": "VFH", "decision": "SCALE"}],
    })
    c = _triage_card(v)
    assert c is not None
    assert "45" in (c["label"] + c["summary"])
    assert "1 action candidate" in c["summary"]
    # verb-free: no trade verb leaks onto the Today cockpit
    assert "SCALE" not in (c["summary"] + c["label"])
    # action_candidate present but no critical → warning
    assert c["status"] == "warning"


def test_triage_card_red_when_critical():
    v = _today({
        "available": True, "total_decisions": 10,
        "bucket_counts": {"critical_action": 2, "action_candidate": 0,
                          "monitor": 5, "ignore_for_now": 3},
        "summary_line": "10 decisions triaged. 2 critical.",
    })
    assert _triage_card(v)["status"] == "red"


def test_triage_card_ok_when_all_monitor_or_ignore():
    v = _today({
        "available": True, "total_decisions": 8,
        "bucket_counts": {"critical_action": 0, "action_candidate": 0,
                          "monitor": 5, "ignore_for_now": 3},
        "summary_line": "8 decisions triaged. 0 critical.",
    })
    assert _triage_card(v)["status"] == "ok"


def test_no_triage_card_when_artifact_absent():
    v = _today(None)
    assert _triage_card(v) is None
