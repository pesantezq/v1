"""
Tests for the simulation-governance lane (spec §11).

Covers: active simulation behavior, production protection, the $0.50/day single
AI review, the promotion workflow, and the watchlist/advisory production loaders.
All deterministic — timestamps are injected, never read from the clock.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.sim_governance import (
    ai_review_packet as PKT,
    daily_ai_review as REV,
    daily_simulation_bundle as BUN,
    production_application as APP,
    production_overlays as OV,
    promotion_approvals as PA,
    promotion_proposals as PP,
    schemas as S,
    simulation_lane as LANE,
)

NOW = "2026-06-16T00:00:00+00:00"


@pytest.fixture
def base_dir(tmp_path: Path) -> str:
    return str(tmp_path / "outputs")


def _baseline() -> dict:
    return {
        "watchlist": ["AAPL", "MSFT"],
        "watchlist_ranked": [{"symbol": "AAPL", "rank": 3}],
        "advisory": [{"symbol": "AAPL", "decision": "HOLD"}],
        "crowd": {"AAPL": {"state": "rising", "velocity": 1.6, "confidence": 0.9, "confirmed": True}},
        "discovery_candidates": [
            {"symbol": "NVDA", "score": 0.9, "reason": "AI", "tags": ["ai"],
             "evidence": ["s1"], "risk_impact": "low", "data_quality": "ok"},
        ],
    }


def _run_lane(base_dir: str, baseline: dict | None = None, experiments=None) -> dict:
    return LANE.run_simulation_lane(".", NOW, baseline=baseline or _baseline(),
                                    experiments=experiments, base_dir=base_dir)


# ===========================================================================
# Simulation ACTIVE behavior
# ===========================================================================


def test_simulation_can_change_watchlist_outputs(base_dir):
    res = _run_lane(base_dir)
    assert "NVDA" in res["simulated_watchlist"]
    assert set(res["simulated_watchlist"]) != set(_baseline()["watchlist"])


def test_simulation_can_change_advisory_outputs(base_dir):
    res = _run_lane(base_dir)
    aapl = next(p for p in res["simulated_advisory"] if p["symbol"] == "AAPL")
    # crowd context was attached by the active experiment
    assert aapl.get("crowd_context") == "rising"


def test_simulation_can_apply_crowd_and_discovery_experiments(base_dir):
    res = _run_lane(base_dir)
    ptypes = {c["proposal_type"] for c in res["candidates"]}
    assert S.PROPOSAL_CROWD_CONTEXT in ptypes        # crowd experiment fired
    assert S.PROPOSAL_WATCHLIST_ADD in ptypes        # discovery experiment fired
    assert res["advisory_candidate_count"] >= 1
    assert res["watchlist_candidate_count"] >= 1


def test_simulation_writes_to_sandbox_namespace(base_dir):
    _run_lane(base_dir)
    p = Path(base_dir) / "sandbox" / "sim_governance" / "simulation_candidates.json"
    assert p.exists()
    # bundle lands in the dedicated SIMULATION namespace
    res = _run_lane(base_dir)
    BUN.build_daily_simulation_bundle(res, NOW, base_dir=base_dir)
    assert (Path(base_dir) / "simulation" / "daily_simulation_bundle.json").exists()


def test_simulation_watchlist_add_remove_rank_tag(base_dir):
    """Simulation lane can produce add/remove/rank/tag changes (spec §8)."""
    def custom(_bl):
        return [
            S.SimulationCandidate("c_add", S.WORKFLOW_WATCHLIST, S.PROPOSAL_WATCHLIST_ADD,
                                  "TSLA", "add TSLA", "why", proposed_production_change={"op": "add", "symbol": "TSLA"}),
            S.SimulationCandidate("c_rm", S.WORKFLOW_WATCHLIST, S.PROPOSAL_WATCHLIST_REMOVE,
                                  "MSFT", "remove MSFT", "why", proposed_production_change={"op": "remove", "symbol": "MSFT"}),
            S.SimulationCandidate("c_rank", S.WORKFLOW_WATCHLIST, S.PROPOSAL_WATCHLIST_RANK,
                                  "AAPL", "rank AAPL", "why", proposed_production_change={"op": "rank", "symbol": "AAPL", "rank": 1}),
            S.SimulationCandidate("c_tag", S.WORKFLOW_WATCHLIST, S.PROPOSAL_WATCHLIST_TAG,
                                  "AAPL", "tag AAPL", "why", proposed_production_change={"op": "tag", "symbol": "AAPL", "tags": ["x"]}),
        ]
    res = _run_lane(base_dir, experiments=[custom])
    sim_wl = res["simulated_watchlist"]
    assert "TSLA" in sim_wl and "MSFT" not in sim_wl
    ptypes = {c["proposal_type"] for c in res["candidates"]}
    assert {S.PROPOSAL_WATCHLIST_ADD, S.PROPOSAL_WATCHLIST_REMOVE,
            S.PROPOSAL_WATCHLIST_RANK, S.PROPOSAL_WATCHLIST_TAG} <= ptypes


# ===========================================================================
# Daily AI budget (single call, $0.50 cap, advisory+watchlist together)
# ===========================================================================


def _packet(base_dir: str) -> dict:
    res = _run_lane(base_dir)
    bundle = BUN.build_daily_simulation_bundle(res, NOW, base_dir=base_dir)
    return PKT.write_review_packet(PKT.build_review_packet(bundle, NOW), base_dir=base_dir)


def test_review_covers_advisory_and_watchlist_together(base_dir):
    packet = _packet(base_dir)
    assert packet["covers_workflows"] == [S.WORKFLOW_ADVISORY, S.WORKFLOW_WATCHLIST]
    assert packet["advisory_candidates"] and packet["watchlist_candidates"]
    res = REV.run_daily_ai_review(packet, NOW, base_dir=base_dir, daily_cost_cap_usd=0.50)
    assert res["advisory_candidates_reviewed"] >= 1
    assert res["watchlist_candidates_reviewed"] >= 1
    assert res["covers_workflows"] == [S.WORKFLOW_ADVISORY, S.WORKFLOW_WATCHLIST]


def test_review_runs_when_cost_within_cap(base_dir):
    packet = _packet(base_dir)
    res = REV.run_daily_ai_review(packet, NOW, base_dir=base_dir, daily_cost_cap_usd=0.50)
    assert res["status"] == "reviewed"
    assert res["estimated_cost_usd"] <= 0.50


def test_review_skipped_and_deferred_when_cost_exceeds_cap(base_dir):
    packet = _packet(base_dir)
    res = REV.run_daily_ai_review(packet, NOW, base_dir=base_dir, daily_cost_cap_usd=0.0)
    assert res["status"] == "deferred"
    assert res["reason"] == "estimated_cost_exceeds_daily_cap"
    assert (Path(base_dir) / "promotion_review" / "daily_ai_review_deferred.json").exists()


def test_only_one_ai_review_call_per_day(base_dir):
    packet = _packet(base_dir)
    calls = {"n": 0}

    def counting_reviewer(pk):
        calls["n"] += 1
        return REV.heuristic_reviewer(pk)

    first = REV.run_daily_ai_review(packet, NOW, base_dir=base_dir,
                                    daily_cost_cap_usd=0.50, reviewer=counting_reviewer)
    second = REV.run_daily_ai_review(packet, NOW, base_dir=base_dir,
                                     daily_cost_cap_usd=0.50, reviewer=counting_reviewer)
    assert first["status"] == "reviewed"
    assert second["status"] == "already_reviewed_today"
    assert calls["n"] == 1  # reviewer invoked exactly once


# ===========================================================================
# Promotion workflow
# ===========================================================================


def _ready_review(base_dir: str):
    res = _run_lane(base_dir)
    bundle = BUN.build_daily_simulation_bundle(res, NOW, base_dir=base_dir)
    packet = PKT.build_review_packet(bundle, NOW)

    def ready_reviewer(pk):
        out = []
        for c in (pk.get("advisory_candidates", []) + pk.get("watchlist_candidates", [])):
            out.append(S.ReviewVerdict(c["candidate_id"], c["workflow"], S.DECISION_READY,
                                       reason="ready").to_dict())
        return out

    review = REV.run_daily_ai_review(packet, NOW, base_dir=base_dir,
                                     daily_cost_cap_usd=0.50, reviewer=ready_reviewer)
    cbi = {c["candidate_id"]: c for c in res["candidates"]}
    return cbi, review


def test_ready_creates_pending_proposal(base_dir):
    cbi, review = _ready_review(base_dir)
    pend = PP.generate_proposals(cbi, review, NOW, base_dir=base_dir)
    assert pend["pending_count"] >= 1
    assert all(p["approval_status"] == S.APPROVAL_PENDING for p in pend["proposals"])
    assert all(p["rollback_plan"] for p in pend["proposals"])  # rollback plan required


def test_continue_testing_does_not_create_proposal(base_dir):
    res = _run_lane(base_dir)
    bundle = BUN.build_daily_simulation_bundle(res, NOW, base_dir=base_dir)
    packet = PKT.build_review_packet(bundle, NOW)

    def keep_testing(pk):
        return [S.ReviewVerdict(c["candidate_id"], c["workflow"], S.DECISION_CONTINUE_TESTING).to_dict()
                for c in (pk["advisory_candidates"] + pk["watchlist_candidates"])]

    review = REV.run_daily_ai_review(packet, NOW, base_dir=base_dir, reviewer=keep_testing)
    cbi = {c["candidate_id"]: c for c in res["candidates"]}
    pend = PP.generate_proposals(cbi, review, NOW, base_dir=base_dir)
    assert pend["pending_count"] == 0


def test_pending_proposal_does_not_affect_production(base_dir):
    cbi, review = _ready_review(base_dir)
    PP.generate_proposals(cbi, review, NOW, base_dir=base_dir)
    # No approvals recorded → application applies nothing.
    state = APP.apply_approved_proposals(NOW, base_dir=base_dir)
    assert state["applied_count"] == 0
    assert state["ignored_count"] >= 1


def test_approved_proposal_affects_production_and_audits(base_dir):
    cbi, review = _ready_review(base_dir)
    pend = PP.generate_proposals(cbi, review, NOW, base_dir=base_dir)
    pid = pend["proposals"][0]["proposal_id"]
    ok = PA.record_approval(pid, "approve", "operator: Enrique", NOW, base_dir=base_dir)
    assert ok["ok"]
    state = APP.apply_approved_proposals(NOW, base_dir=base_dir)
    assert state["applied_count"] == 1
    # audit trail exists and references the proposal
    audit = (Path(base_dir) / "promotion_approvals" / "production_application_audit.jsonl")
    assert audit.exists()
    rows = [json.loads(l) for l in audit.read_text().splitlines() if l.strip()]
    assert any(r.get("proposal_id") == pid and r.get("rollback_plan") for r in rows)


def test_invalid_approval_metadata_is_ignored(base_dir):
    cbi, review = _ready_review(base_dir)
    pend = PP.generate_proposals(cbi, review, NOW, base_dir=base_dir)
    pid = pend["proposals"][0]["proposal_id"]
    # missing timestamp / bad decision / non-dict → all rejected
    assert not PA.record_approval(pid, "approve", "operator", "", base_dir=base_dir)["ok"]
    assert not PA.record_approval(pid, "maybe", "operator", NOW, base_dir=base_dir)["ok"]
    assert PA.approved_proposal_ids(base_dir) == set()
    state = APP.apply_approved_proposals(NOW, base_dir=base_dir)
    assert state["applied_count"] == 0


def test_ai_cannot_self_approve_production(base_dir):
    cbi, review = _ready_review(base_dir)
    pend = PP.generate_proposals(cbi, review, NOW, base_dir=base_dir)
    pid = pend["proposals"][0]["proposal_id"]
    for ai_name in ("ai", "ai_review", "gpt", "openai", "claude", "system", "auto"):
        res = PA.record_approval(pid, "approve", ai_name, NOW, base_dir=base_dir)
        assert not res["ok"], f"{ai_name} must not be able to self-approve"
    assert PA.approved_proposal_ids(base_dir) == set()


def test_rejected_proposal_is_not_applied(base_dir):
    cbi, review = _ready_review(base_dir)
    pend = PP.generate_proposals(cbi, review, NOW, base_dir=base_dir)
    pid = pend["proposals"][0]["proposal_id"]
    PA.record_approval(pid, "reject", "operator: Enrique", NOW, base_dir=base_dir)
    state = APP.apply_approved_proposals(NOW, base_dir=base_dir)
    assert pid not in {a["proposal_id"] for a in state["applied"]}


# ===========================================================================
# Production protection — loaders ignore everything except approved overlays
# ===========================================================================


def test_production_ignores_raw_simulation_outputs(base_dir):
    # Run the active lane (writes sandbox candidates) but record NO approvals.
    _run_lane(base_dir)
    APP.apply_approved_proposals(NOW, base_dir=base_dir)  # empty overlays
    out = OV.load_production_watchlist(["AAPL", "MSFT"], base_dir=base_dir, enabled=True)
    # NVDA was a simulation candidate but never approved → not in production.
    assert "NVDA" not in out["watchlist"]
    assert out["watchlist"] == ["AAPL", "MSFT"]


def test_production_watchlist_only_changes_after_approval(base_dir):
    cbi, review = _ready_review(base_dir)
    pend = PP.generate_proposals(cbi, review, NOW, base_dir=base_dir)
    # find the watchlist-add proposal (NVDA)
    add = next(p for p in pend["proposals"] if p["proposal_type"] == S.PROPOSAL_WATCHLIST_ADD)

    # before approval: production unchanged
    APP.apply_approved_proposals(NOW, base_dir=base_dir)
    before = OV.load_production_watchlist(["AAPL"], base_dir=base_dir, enabled=True)
    assert add["proposed_production_change"]["symbol"] not in before["watchlist"]

    # after approval: production reflects the approved add
    PA.record_approval(add["proposal_id"], "approve", "operator: Enrique", NOW, base_dir=base_dir)
    APP.apply_approved_proposals(NOW, base_dir=base_dir)
    after = OV.load_production_watchlist(["AAPL"], base_dir=base_dir, enabled=True)
    assert add["proposed_production_change"]["symbol"] in after["watchlist"]


def test_production_overlay_disabled_is_noop(base_dir):
    cbi, review = _ready_review(base_dir)
    pend = PP.generate_proposals(cbi, review, NOW, base_dir=base_dir)
    add = next(p for p in pend["proposals"] if p["proposal_type"] == S.PROPOSAL_WATCHLIST_ADD)
    PA.record_approval(add["proposal_id"], "approve", "operator: Enrique", NOW, base_dir=base_dir)
    APP.apply_approved_proposals(NOW, base_dir=base_dir)
    # enabled=False → strict no-op even though an approved overlay exists
    out = OV.load_production_watchlist(["AAPL"], base_dir=base_dir, enabled=False)
    assert out["watchlist"] == ["AAPL"]
    assert out["overlay_enabled"] is False


def _advisory_context_ready(base_dir: str):
    """A genuinely human-gated advisory candidate (advisory_context_change) marked
    READY. Used to exercise the approve->overlay path now that crowd_context is an
    observe-only annotation that never enters the promotion queue."""
    cid = "adv-ctx-AAPL"
    cand = {
        "candidate_id": cid,
        "workflow": S.WORKFLOW_ADVISORY,
        "proposal_type": S.PROPOSAL_ADVISORY_CONTEXT,
        "symbol": "AAPL",
        "source_evidence": ["outputs/latest/decision_plan.json"],
        "risk_impact": "low",
        "confidence": 0.7,
        "data_quality": "ok",
        "proposed_production_change": {"op": "context", "symbol": "AAPL", "context": "rising"},
    }
    review = {"verdicts": [
        S.ReviewVerdict(cid, S.WORKFLOW_ADVISORY, S.DECISION_READY, reason="ready").to_dict()
    ]}
    return {cid: cand}, review


def test_production_advisory_only_changes_after_approval(base_dir):
    cbi, review = _advisory_context_ready(base_dir)
    pend = PP.generate_proposals(cbi, review, NOW, base_dir=base_dir)
    ctx = next(p for p in pend["proposals"] if p["proposal_type"] == S.PROPOSAL_ADVISORY_CONTEXT)
    baseline_adv = [{"symbol": "AAPL", "decision": "HOLD"}]

    APP.apply_approved_proposals(NOW, base_dir=base_dir)
    before = OV.load_production_advisory(baseline_adv, base_dir=base_dir, enabled=True)
    assert not before["applied_proposal_ids"]
    assert "overlay_context" not in before["advisory"][0]

    PA.record_approval(ctx["proposal_id"], "approve", "operator: Enrique", NOW, base_dir=base_dir)
    APP.apply_approved_proposals(NOW, base_dir=base_dir)
    after = OV.load_production_advisory(baseline_adv, base_dir=base_dir, enabled=True)
    aapl = next(r for r in after["advisory"] if r["symbol"] == "AAPL")
    assert aapl.get("overlay_context") == "rising"
    assert ctx["proposal_id"] in after["applied_proposal_ids"]


def test_approved_advisory_overlay_never_touches_scoring_fields(base_dir):
    """Advisory overlay only adds annotation fields — no score mutation."""
    overlay = {"feeds_production": True, "ops": [
        {"proposal_id": "p1", "proposal_type": S.PROPOSAL_CROWD_CONTEXT,
         "change": {"op": "context", "symbol": "AAPL", "crowd_context": "rising"}},
    ]}
    base = [{"symbol": "AAPL", "decision": "BUY", "signal_score": 0.7, "confidence_score": 0.6}]
    out = OV.apply_approved_advisory(base, overlay)
    aapl = out["advisory"][0]
    # protected fields are untouched; only annotation added
    assert aapl["signal_score"] == 0.7 and aapl["confidence_score"] == 0.6
    assert aapl["decision"] == "BUY"
    assert aapl["overlay_context"] == "rising"


# ===========================================================================
# Rollback
# ===========================================================================


def test_rollback_restores_previous_overlay(base_dir):
    cbi, review = _ready_review(base_dir)
    pend = PP.generate_proposals(cbi, review, NOW, base_dir=base_dir)
    add = next(p for p in pend["proposals"] if p["proposal_type"] == S.PROPOSAL_WATCHLIST_ADD)
    # first application writes the empty overlay (snapshot baseline)
    APP.apply_approved_proposals(NOW, base_dir=base_dir)
    # approve + apply again (snapshots the empty overlay, writes the populated one)
    PA.record_approval(add["proposal_id"], "approve", "operator: Enrique", NOW, base_dir=base_dir)
    APP.apply_approved_proposals("2026-06-16T01:00:00+00:00", base_dir=base_dir)
    populated = OV.load_production_watchlist(["AAPL"], base_dir=base_dir, enabled=True)
    assert populated["applied_proposal_ids"]
    # roll back to the prior (empty) overlay
    rb = APP.rollback_last(APP.WATCHLIST_OVERLAY, base_dir=base_dir, now="2026-06-16T02:00:00+00:00")
    assert rb["ok"]
    restored = OV.load_production_watchlist(["AAPL"], base_dir=base_dir, enabled=True)
    assert not restored["applied_proposal_ids"]


# ===========================================================================
# Schema validators
# ===========================================================================


def test_human_approver_detection():
    assert S.is_human_approver("operator: Enrique")
    assert S.is_human_approver("Enrique Pesantez")
    assert not S.is_human_approver("ai_review")
    assert not S.is_human_approver("gpt")
    assert not S.is_human_approver("")
    assert not S.is_human_approver(None)


def test_proposal_type_workflow_routing():
    assert S.workflow_for_proposal_type(S.PROPOSAL_WATCHLIST_ADD) == S.WORKFLOW_WATCHLIST
    assert S.workflow_for_proposal_type(S.PROPOSAL_ADVISORY_CONTEXT) == S.WORKFLOW_ADVISORY
    assert S.workflow_for_proposal_type(S.PROPOSAL_CROWD_CONTEXT) == S.WORKFLOW_ADVISORY


# ===========================================================================
# Phase 3 — daily sim bound to the immutable Phase 2 input snapshot
# (every experiment shares ONE frozen input identity; production untouched)
# ===========================================================================

import portfolio_automation.daily_input_snapshot as _DS


def test_lane_binds_to_frozen_input_snapshot(tmp_path, base_dir):
    # Phase 2 snapshot present at base_dir/sandbox/daily_input_snapshot.json
    _DS.write_input_snapshot(tmp_path, {"snapshot_hash": "HASH123", "run_id": "2026-06-16_daily_official", "inputs": []})
    res = LANE.run_simulation_lane(str(tmp_path), NOW, baseline=_baseline(),
                                   experiments=None, base_dir=base_dir, write_files=False)
    assert res["input_snapshot_hash"] == "HASH123"
    assert res["input_snapshot_run_id"] == "2026-06-16_daily_official"


def test_lane_snapshot_binding_absent_is_safe(tmp_path, base_dir):
    res = LANE.run_simulation_lane(str(tmp_path), NOW, baseline=_baseline(),
                                   experiments=[], base_dir=base_dir, write_files=False)
    assert res["input_snapshot_hash"] is None
    assert res["input_snapshot_run_id"] is None


def test_bundle_propagates_snapshot_binding(tmp_path, base_dir):
    _DS.write_input_snapshot(tmp_path, {"snapshot_hash": "HASHXYZ", "run_id": "RID", "inputs": []})
    lane = LANE.run_simulation_lane(str(tmp_path), NOW, baseline=_baseline(),
                                    experiments=None, base_dir=base_dir, write_files=False)
    bundle = BUN.build_daily_simulation_bundle(lane, now=NOW, base_dir=base_dir)
    assert bundle["input_snapshot_hash"] == "HASHXYZ"


def test_lane_stays_sandbox_only_and_production_safe(tmp_path, base_dir):
    # active-but-isolated: lane_active True, but never writes outside SANDBOX
    res = LANE.run_simulation_lane(str(tmp_path), NOW, baseline=_baseline(),
                                   experiments=None, base_dir=base_dir, write_files=True)
    assert res["lane_active"] is True and res["production_safe"] is True
    # no production namespaces were written by the lane
    for ns in ("latest", "policy", "portfolio"):
        d = tmp_path / "outputs" / ns
        assert not (d.exists() and any(d.iterdir())), f"lane wrote into production ns {ns}"


# ===========================================================================
# Crowd-context auto-refresh (observe-only annotation, never human-gated)
# ===========================================================================

_UNIFIED_CROWD_EVIDENCE = "outputs/latest/unified_crowd_intelligence.json"


def test_crowd_context_never_becomes_pending_proposal(base_dir):
    """crowd_context is an observe-only annotation: even when the reviewer marks
    every candidate READY, no crowd_context_change proposal is minted, so the
    advisory backlog cannot accumulate."""
    cbi, review = _ready_review(base_dir)
    # sanity: a crowd_context candidate DID exist and WAS marked ready by the reviewer
    assert any(c["proposal_type"] == S.PROPOSAL_CROWD_CONTEXT for c in cbi.values())
    pend = PP.generate_proposals(cbi, review, NOW, base_dir=base_dir)
    ptypes = {p["proposal_type"] for p in pend["proposals"]}
    assert S.PROPOSAL_CROWD_CONTEXT not in ptypes
    assert pend.get("skipped_observe_only", 0) >= 1


def test_genuinely_gated_advisory_still_promotes(base_dir):
    """No regression to the human gate: a genuinely-gated advisory type still
    becomes a pending proposal when the reviewer marks it READY."""
    cbi, review = _advisory_context_ready(base_dir)
    pend = PP.generate_proposals(cbi, review, NOW, base_dir=base_dir)
    ptypes = {p["proposal_type"] for p in pend["proposals"]}
    assert S.PROPOSAL_ADVISORY_CONTEXT in ptypes
    assert all(p["approval_status"] == S.APPROVAL_PENDING for p in pend["proposals"])


def test_watchlist_promotions_unaffected_by_crowd_skip(base_dir):
    """The crowd-context skip must not suppress watchlist promotions (the gate
    still works for behavior-affecting changes)."""
    cbi, review = _ready_review(base_dir)
    pend = PP.generate_proposals(cbi, review, NOW, base_dir=base_dir)
    ptypes = {p["proposal_type"] for p in pend["proposals"]}
    assert S.PROPOSAL_WATCHLIST_ADD in ptypes


def test_crowd_context_source_evidence_points_at_unified_bus(base_dir):
    """Provenance repair: crowd_context and watchlist_rerank cite the real unified
    crowd artifact, not the absent outputs/sandbox/crowd_radar."""
    res = _run_lane(base_dir)
    for c in res["candidates"]:
        if c["proposal_type"] in (S.PROPOSAL_CROWD_CONTEXT, S.PROPOSAL_WATCHLIST_RANK):
            assert _UNIFIED_CROWD_EVIDENCE in c["source_evidence"]
            assert "outputs/sandbox/crowd_radar" not in c["source_evidence"]


def test_advisory_crowd_context_self_refreshes_from_unified_bus(base_dir):
    """The SANDBOX advisory view annotates crowd_context live from the unified bus
    each run — a changed crowd_state is reflected on the next run with NO approval
    step."""
    bl1 = _baseline()
    bl1["crowd"] = {"AAPL": {"state": "rising", "velocity": 1.6, "confidence": 0.9, "confirmed": True}}
    res1 = _run_lane(base_dir, baseline=bl1)
    aapl1 = next(p for p in res1["simulated_advisory"] if p["symbol"] == "AAPL")
    assert aapl1.get("crowd_context") == "rising"

    bl2 = _baseline()
    bl2["crowd"] = {"AAPL": {"state": "confirmed_attention", "velocity": 1.6,
                             "confidence": 0.95, "confirmed": True}}
    res2 = _run_lane(base_dir, baseline=bl2)
    aapl2 = next(p for p in res2["simulated_advisory"] if p["symbol"] == "AAPL")
    assert aapl2.get("crowd_context") == "confirmed_attention"


def test_crowd_context_never_feeds_decision_plan(base_dir, tmp_path):
    """Observe-only invariant: running the lane writes no production decision_plan."""
    _run_lane(base_dir)
    assert not (tmp_path / "outputs" / "latest" / "decision_plan.json").exists()
