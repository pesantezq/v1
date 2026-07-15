"""The governance GUI view surfaces auto-applied simulation items + veto affordance."""
from __future__ import annotations

import json

from gui_v2.data.dash_governance import collect_governance_view
from portfolio_automation.sim_governance import auto_approval as AA


def _seed(root):
    pol = root / "outputs" / "policy"
    pol.mkdir(parents=True, exist_ok=True)
    AA.append_event({"kind": AA.EVENT_APPLIED, "event_id": "evt_1", "idempotency_key": "idk_1",
                     "candidate_type": "watchlist", "target_id": "NVDA", "symbol": "NVDA",
                     "confidence": 0.92, "gpt_reasoning": "clean evidence",
                     "gate_trace": [{"gate_name": "min_confidence", "passed": True}],
                     "application_timestamp": "2026-07-14T12:00:00Z"},
                    base_dir=str(root / "outputs"))
    summary = AA.build_summary(base_dir=str(root / "outputs"), now="2026-07-14T13:00:00Z")
    (pol / "auto_approval_audit.json").write_text(json.dumps(summary), encoding="utf-8")


def test_view_surfaces_auto_applied_items(tmp_path):
    _seed(tmp_path)
    view = collect_governance_view(tmp_path)
    items = view["auto_applied_items"]
    assert len(items) == 1
    it = items[0]
    assert it["target_id"] == "NVDA"
    assert it["gpt_reasoning"] == "clean evidence"
    assert it["feeds_decision_engine"] is False
    assert "veto available" in it["status_label"]
    # A governance card is present for the auto-approval channel.
    assert any("Auto-approval" in c.get("title", "") for c in view["cards"])


def test_view_no_auto_approval_activity(tmp_path):
    view = collect_governance_view(tmp_path)
    assert view["auto_applied_items"] == []
