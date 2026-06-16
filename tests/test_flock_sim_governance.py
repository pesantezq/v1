"""Flock Intelligence — sim-governance integration, AI review, production gating."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from portfolio_automation.sim_governance import (
    ai_review_packet as PKT,
    daily_ai_review as REV,
    daily_simulation_bundle as BUN,
    production_application as APP,
    promotion_approvals as PA,
    promotion_proposals as PP,
    schemas as S,
    simulation_lane as LANE,
)
from portfolio_automation.sim_governance.simulation_lane import experiment_flock_intelligence

NOW = "2026-06-16T00:00:00+00:00"


@pytest.fixture
def base_dir(tmp_path: Path) -> str:
    return str(tmp_path / "outputs")


def _flock_baseline() -> dict:
    """Baseline carrying injected flock simulation context (as the producer writes)."""
    return {
        "watchlist": ["NVDA"],            # NVDA already on WL; ZZZ is a forming add
        "advisory": [{"symbol": "NVDA", "decision": "HOLD"}],
        "crowd": {}, "discovery_candidates": [], "watchlist_ranked": [],
        "flock": {
            "report": {"data_quality_status": "ok"},
            "watchlist_candidates": {"candidates": [
                {"ticker": "ZZZ", "group": "AI", "action": "add",
                 "tags": ["flock_forming", "rotation_candidate"], "flock_state": "flock_forming",
                 "flock_score": 0.5, "confidence": 0.6, "sim_rank_delta": 0,
                 "rationale": "Emerging flock"},
                {"ticker": "NVDA", "group": "AI", "action": "tag",
                 "tags": ["flock_confirmed"], "flock_state": "flock_confirmed",
                 "flock_score": 0.8, "confidence": 0.85, "sim_rank_delta": 0,
                 "rationale": "Confirmed flock"},
            ]},
            "advisory_context": {"by_symbol": {
                "NVDA": {"flock_state": "flock_confirmed", "group": "AI", "flock_score": 0.8,
                         "dispersion_score": 0.2, "confidence": 0.85,
                         "label": "AI: flock confirmed", "meaning": "Broad structure supports monitoring."},
            }},
        },
    }


def _run_flock_lane(base_dir: str):
    return LANE.run_simulation_lane(".", NOW, baseline=_flock_baseline(),
                                    experiments=[experiment_flock_intelligence], base_dir=base_dir)


# ---------------------------------------------------------------------------
# Active simulation behavior
# ---------------------------------------------------------------------------

def test_experiment_emits_flock_candidates():
    cands = experiment_flock_intelligence(_flock_baseline())
    ptypes = {c.proposal_type for c in cands}
    assert S.PROPOSAL_FLOCK_WATCHLIST_LOGIC in ptypes
    assert S.PROPOSAL_FLOCK_ADVISORY_CONTEXT in ptypes
    # advisory pick is confirmed -> a scoring-adjustment candidate too
    assert S.PROPOSAL_FLOCK_SCORING_ADJUSTMENT in ptypes
    assert all(S.is_valid_proposal_type(c.proposal_type) for c in cands)


def test_flock_changes_simulated_watchlist_and_advisory(base_dir):
    res = _run_flock_lane(base_dir)
    assert "ZZZ" in res["simulated_watchlist"]                 # forming add changed membership
    nvda = next(p for p in res["simulated_advisory"] if p["symbol"] == "NVDA")
    assert nvda.get("flock_context") == "AI: flock confirmed"  # advisory context changed
    assert nvda.get("flock_scoring_hint") == "boost"


def test_flock_lane_writes_only_to_sandbox_namespace(base_dir):
    _run_flock_lane(base_dir)
    sandbox = Path(base_dir) / "sandbox" / "sim_governance"
    assert (sandbox / "simulation_candidates.json").exists()
    # The lane itself never writes to production/latest.
    assert not (Path(base_dir) / "latest" / "approved_advisory_proposals.json").exists()


# ---------------------------------------------------------------------------
# Daily consolidated AI review (one call, $0.50 cap)
# ---------------------------------------------------------------------------

def _packet(base_dir: str):
    res = _run_flock_lane(base_dir)
    bundle = BUN.build_daily_simulation_bundle(res, NOW, base_dir=base_dir)
    return res, PKT.build_review_packet(bundle, NOW)


def test_flock_candidates_appear_in_consolidated_packet(base_dir):
    _, packet = _packet(base_dir)
    lines = packet.get("advisory_candidates", []) + packet.get("watchlist_candidates", [])
    ptypes = {c.get("proposal_type") for c in lines}
    assert ptypes & {S.PROPOSAL_FLOCK_WATCHLIST_LOGIC, S.PROPOSAL_FLOCK_ADVISORY_CONTEXT}


def test_single_ai_call_with_flock_included(base_dir):
    _, packet = _packet(base_dir)
    calls = {"n": 0}

    def counting(pk):
        calls["n"] += 1
        return REV.heuristic_reviewer(pk)

    first = REV.run_daily_ai_review(packet, NOW, base_dir=base_dir,
                                    daily_cost_cap_usd=0.50, reviewer=counting)
    second = REV.run_daily_ai_review(packet, NOW, base_dir=base_dir,
                                     daily_cost_cap_usd=0.50, reviewer=counting)
    assert first["status"] == "reviewed"
    assert second["status"] == "already_reviewed_today"
    assert calls["n"] == 1  # flock rides the ONE consolidated review — no extra call


def test_cost_cap_enforced_defers_review(base_dir):
    _, packet = _packet(base_dir)
    res = REV.run_daily_ai_review(packet, NOW, base_dir=base_dir, daily_cost_cap_usd=0.0)
    assert res["status"] == "deferred"
    assert (Path(base_dir) / "promotion_review" / "daily_ai_review_deferred.json").exists()


# ---------------------------------------------------------------------------
# Promotion workflow + production protection
# ---------------------------------------------------------------------------

def _ready_flock_review(base_dir: str):
    res, packet = _packet(base_dir)

    def ready(pk):
        out = []
        for c in (pk.get("advisory_candidates", []) + pk.get("watchlist_candidates", [])):
            if str(c.get("proposal_type", "")).startswith("flock_"):
                out.append(S.ReviewVerdict(c["candidate_id"], c["workflow"],
                                           S.DECISION_READY, reason="ready").to_dict())
        return out

    review = REV.run_daily_ai_review(packet, NOW, base_dir=base_dir,
                                     daily_cost_cap_usd=0.50, reviewer=ready)
    cbi = {c["candidate_id"]: c for c in res["candidates"]}
    return cbi, review


def test_ready_flock_creates_pending_proposal_only(base_dir):
    cbi, review = _ready_flock_review(base_dir)
    pend = PP.generate_proposals(cbi, review, NOW, base_dir=base_dir)
    assert pend["pending_count"] >= 1
    assert all(p["approval_status"] == S.APPROVAL_PENDING for p in pend["proposals"])
    assert any(str(p["proposal_type"]).startswith("flock_") for p in pend["proposals"])
    assert all(p["rollback_plan"] for p in pend["proposals"])


def test_production_ignores_pending_flock_proposals(base_dir):
    cbi, review = _ready_flock_review(base_dir)
    PP.generate_proposals(cbi, review, NOW, base_dir=base_dir)
    # No human approval -> production applies nothing (pending + raw sim ignored).
    state = APP.apply_approved_proposals(NOW, base_dir=base_dir)
    assert state["applied_count"] == 0
    assert state["ignored_count"] >= 1


def test_production_applies_only_approved_flock_proposal(base_dir):
    cbi, review = _ready_flock_review(base_dir)
    pend = PP.generate_proposals(cbi, review, NOW, base_dir=base_dir)
    pid = pend["proposals"][0]["proposal_id"]
    assert PA.record_approval(pid, "approve", "operator: Enrique", NOW, base_dir=base_dir)["ok"]
    state = APP.apply_approved_proposals(NOW, base_dir=base_dir)
    assert state["applied_count"] == 1


def test_ai_cannot_self_approve_flock_proposal(base_dir):
    cbi, review = _ready_flock_review(base_dir)
    pend = PP.generate_proposals(cbi, review, NOW, base_dir=base_dir)
    pid = pend["proposals"][0]["proposal_id"]
    for ai_name in ("ai", "ai_review", "gpt-4o-mini", "openai", "system"):
        assert not PA.record_approval(pid, "approve", ai_name, NOW, base_dir=base_dir)["ok"]
    assert PA.approved_proposal_ids(base_dir) == set()
    assert APP.apply_approved_proposals(NOW, base_dir=base_dir)["applied_count"] == 0
