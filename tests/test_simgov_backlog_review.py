"""Tests for the sim-governance pending-backlog review helper.

The helper is read-only: it joins pending promotion proposals with the daily
AI-review verdicts, classifies each proposal's readiness, and computes a
recommended *human* action. It NEVER approves anything (production is
human-gated) and NEVER writes a file. These tests assert the classification,
the degraded-state contract, and the write-nothing safety invariant under both
healthy and degraded fixtures.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from portfolio_automation.sim_governance import backlog_review as BR


def _write(base_dir, rel, payload):
    path = os.path.join(base_dir, "outputs", "promotion_review", rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(payload, fh)


def _proposal(pid, cand, ptype="flock_watchlist_candidate_logic", symbol="XYZ",
              created="2026-07-10T09:00:00+00:00"):
    return {
        "proposal_id": pid,
        "candidate_id": cand,
        "proposal_type": ptype,
        "workflow": "watchlist",
        "proposed_production_change": {"op": "watchlist_add", "symbol": symbol},
        "risk_summary": "risk_impact=medium, confidence=0.9, data_quality=ok, evidence_strength=strong",
        "rollback_plan": "Remove overlay entry and re-run loader.",
        "approval_status": "pending",
        "created_at": created,
    }


def _verdict(cand, decision):
    return {
        "candidate_id": cand,
        "workflow": "watchlist",
        "decision": decision,
        "reason": "test",
        "evidence_strength": "strong",
        "risk_level": "low",
        "required_human_review": True,
        "rollback_readiness": "ready",
    }


NOW = datetime(2026, 7, 13, 9, 0, 0, tzinfo=timezone.utc)


def test_healthy_all_ready(tmp_path):
    base = str(tmp_path)
    cands = [f"cand_{i}" for i in range(6)]
    _write(base, "pending_proposals.json", {
        "generated_at": "2026-07-13T09:12:54+00:00",
        "pending_count": 6,
        "proposals": [_proposal(f"prop_{i}", c) for i, c in enumerate(cands)],
    })
    _write(base, "daily_ai_review_result.json", {
        "status": "reviewed",
        "ready_candidate_ids": cands,
        "ai_can_approve_production": False,
        "counts": {"reject": 0, "continue_testing": 0, "ready_for_production_review": 6},
        "verdicts": [_verdict(c, "ready_for_production_review") for c in cands],
    })
    r = BR.review_pending_backlog(base_dir=base, now=NOW)
    assert r["available"] is True
    assert r["pending_count"] == 6
    assert r["ready_count"] == 6
    assert r["hold_count"] == 0
    assert r["reject_count"] == 0
    assert r["ai_can_approve_production"] is False
    assert r["observe_only"] is True and r["human_gated"] is True
    assert all(it["recommendation"] == "AWAITING_HUMAN_APPROVAL" for it in r["items"])
    # created 2026-07-10 -> ~3 days before NOW
    assert r["oldest_ready_age_days"] is not None and 2.9 <= r["oldest_ready_age_days"] <= 3.1
    # ready items carry a hand-off pointer that names the human-only approval path
    ready = r["items"][0]
    assert "record_approval" in ready["approval_hint"]
    assert "AI cannot approve" in ready["approval_hint"]


def test_mixed_classification(tmp_path):
    base = str(tmp_path)
    _write(base, "pending_proposals.json", {
        "generated_at": "2026-07-13T09:12:54+00:00",
        "pending_count": 3,
        "proposals": [
            _proposal("prop_r", "cand_r"),
            _proposal("prop_h", "cand_h"),
            _proposal("prop_x", "cand_x"),
        ],
    })
    _write(base, "daily_ai_review_result.json", {
        "status": "reviewed",
        "ready_candidate_ids": ["cand_r"],
        "ai_can_approve_production": False,
        "verdicts": [
            _verdict("cand_r", "ready_for_production_review"),
            _verdict("cand_h", "continue_testing"),
            _verdict("cand_x", "reject"),
        ],
    })
    r = BR.review_pending_backlog(base_dir=base, now=NOW)
    assert (r["ready_count"], r["hold_count"], r["reject_count"]) == (1, 1, 1)
    by_id = {it["proposal_id"]: it for it in r["items"]}
    assert by_id["prop_r"]["recommendation"] == "AWAITING_HUMAN_APPROVAL"
    assert by_id["prop_h"]["recommendation"] == "HOLD"
    assert by_id["prop_x"]["recommendation"] == "DROP_CANDIDATE"


def test_missing_ai_review_marks_unknown(tmp_path):
    base = str(tmp_path)
    _write(base, "pending_proposals.json", {
        "pending_count": 1,
        "proposals": [_proposal("prop_u", "cand_u")],
    })
    # no daily_ai_review_result.json
    r = BR.review_pending_backlog(base_dir=base, now=NOW)
    assert r["available"] is True
    assert r["pending_count"] == 1
    assert r["items"][0]["readiness"] == "unknown"
    assert r["items"][0]["recommendation"] == "SURFACE_FOR_REVIEW"


def test_degraded_when_no_proposals_artifact(tmp_path):
    base = str(tmp_path)
    r = BR.review_pending_backlog(base_dir=base, now=NOW)
    assert r["available"] is False
    assert "reason" in r
    # degraded contract still exposes the safety invariants
    assert r.get("observe_only") is True


def test_helper_writes_nothing(tmp_path):
    base = str(tmp_path)
    cands = ["cand_a", "cand_b"]
    _write(base, "pending_proposals.json", {
        "proposals": [_proposal(f"prop_{c}", c) for c in cands],
    })
    _write(base, "daily_ai_review_result.json", {
        "ready_candidate_ids": cands, "ai_can_approve_production": False,
        "verdicts": [_verdict(c, "ready_for_production_review") for c in cands],
    })
    before = sorted(os.listdir(os.path.join(base, "outputs", "promotion_review")))
    BR.review_pending_backlog(base_dir=base, now=NOW)
    after = sorted(os.listdir(os.path.join(base, "outputs", "promotion_review")))
    assert before == after  # pure read: no artifact created or mutated
