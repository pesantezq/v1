import json
from pathlib import Path

from portfolio_automation.sim_governance import approval_packet as ap


def _seed_pending(base_dir, proposals):
    d = Path(base_dir) / "promotion_review"
    d.mkdir(parents=True, exist_ok=True)
    (d / "pending_proposals.json").write_text(
        json.dumps({"schema": "pending_proposals.v1", "proposals": proposals}),
        encoding="utf-8",
    )


def test_build_packet_two_tiers(tmp_path, monkeypatch):
    base = str(tmp_path / "outputs")
    # tier-a: stub the sim summary
    monkeypatch.setattr(
        ap.auto_approval, "build_summary",
        lambda *, base_dir, now: {
            "active_items": [
                {"event_id": "ev1", "candidate_type": "watchlist", "symbol": "XOM",
                 "strategy_id": None, "applied_at": "2026-07-15T00:00:00+00:00",
                 "confidence": 0.9},
            ],
            "active_item_count": 1,
        },
    )
    # tier-b: pending proposals on disk (one pending, one already approved -> excluded)
    _seed_pending(base, [
        {"proposal_id": "p1", "workflow": "watchlist", "proposal_type": "watchlist_add",
         "candidate_id": "c1", "proposed_production_change": {"symbol": "CVX"},
         "risk_summary": "low", "rollback_plan": "remove", "approval_status": "pending",
         "evidence_refs": ["e1"], "created_at": "2026-07-14T00:00:00+00:00"},
        {"proposal_id": "p2", "approval_status": "approved"},
    ])
    packet = ap.build_operator_packet(base, "2026-07-15T12:00:00+00:00",
                                      deep_link_base="https://x", veto_window_hours=48)
    assert packet["schema"] == "operator_approval_packet.v1"
    assert packet["observe_only"] is True
    assert packet["approval_page_url"] == "https://x/dashboard/governance"
    assert packet["counts"] == {"tier_sim_within_veto": 1, "tier_production_pending": 1}
    assert packet["tier_sim"][0]["event_id"] == "ev1"
    assert packet["tier_sim"][0]["veto_deadline"] == "2026-07-17T00:00:00+00:00"
    assert packet["tier_sim"][0]["status"] == "auto-applied in simulation · veto available"
    assert packet["tier_production"][0]["proposal_id"] == "p1"
    assert packet["tier_production"][0]["symbol"] == "CVX"
    assert packet["tier_production"][0]["status"] == "pending human review"


def test_build_packet_degraded_on_failure(tmp_path, monkeypatch):
    base = str(tmp_path / "outputs")

    def _boom(*, base_dir, now):
        raise RuntimeError("ledger corrupt")

    monkeypatch.setattr(ap.auto_approval, "build_summary", _boom)
    packet = ap.build_operator_packet(base, "2026-07-15T12:00:00+00:00")
    assert packet["observe_only"] is True
    assert packet["tier_sim"] == []
    assert packet["tier_production"] == []
    assert "error" in packet


def test_write_packet_creates_artifacts(tmp_path):
    base = str(tmp_path / "outputs")
    packet = {"schema": "operator_approval_packet.v1", "observe_only": True,
              "generated_at": "n", "tier_sim": [], "tier_production": [],
              "counts": {"tier_sim_within_veto": 0, "tier_production_pending": 0}}
    ap.write_operator_packet(packet, base_dir=base)
    assert (Path(base) / "promotion_review" / "operator_approval_packet.json").exists()
    assert (Path(base) / "promotion_review" / "operator_approval_packet.md").exists()


def test_assess_health_green_when_empty(tmp_path, monkeypatch):
    base = str(tmp_path / "outputs")
    monkeypatch.setattr(ap.auto_approval, "build_summary",
                        lambda *, base_dir, now: {"active_items": []})
    ap.write_operator_packet(
        ap.build_operator_packet(base, "2026-07-15T00:00:00+00:00"), base_dir=base)
    h = ap.assess_packet_health(base, "2026-07-15T00:00:00+00:00")
    assert h["status"] == "GREEN"


def test_assess_health_amber_on_stale_pending(tmp_path, monkeypatch):
    base = str(tmp_path / "outputs")
    monkeypatch.setattr(ap.auto_approval, "build_summary",
                        lambda *, base_dir, now: {"active_items": []})
    _seed_pending(base, [
        {"proposal_id": "p1", "approval_status": "pending",
         "created_at": "2026-07-01T00:00:00+00:00",
         "proposed_production_change": {"symbol": "CVX"}, "workflow": "watchlist"},
    ])
    ap.write_operator_packet(
        ap.build_operator_packet(base, "2026-07-15T00:00:00+00:00"), base_dir=base)
    h = ap.assess_packet_health(base, "2026-07-15T00:00:00+00:00", stale_pending_days=3)
    assert h["status"] == "AMBER"
    assert any("stale_pending" in r for r in h["reasons"])
