import json
from pathlib import Path

from gui_v2.data.dash_approval_packet import load_packet_context


def test_load_packet_present(tmp_path):
    d = tmp_path / "promotion_review"
    d.mkdir(parents=True)
    (d / "operator_approval_packet.json").write_text(json.dumps({
        "schema": "operator_approval_packet.v1", "observe_only": True,
        "tier_sim": [{"event_id": "ev1"}],
        "tier_production": [{"proposal_id": "p1"}],
        "counts": {"tier_sim_within_veto": 1, "tier_production_pending": 1},
    }), encoding="utf-8")
    ctx = load_packet_context(str(tmp_path))
    assert ctx["available"] is True
    assert ctx["counts"]["tier_production_pending"] == 1
    assert ctx["tier_production"][0]["proposal_id"] == "p1"


def test_load_packet_absent(tmp_path):
    ctx = load_packet_context(str(tmp_path))
    assert ctx["available"] is False
    assert ctx["tier_sim"] == []
    assert ctx["tier_production"] == []
