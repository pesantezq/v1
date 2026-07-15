"""Governance — simulation/production two-lane visibility view (spec §10).

Observe-only dashboard reader. Surfaces the daily simulation-governance lane:
simulation-lane status, AI-review status + $0.50/day budget, candidate review
counts, and the pending/approved/rejected/deferred promotion-proposal queue —
clearly separating the ACTIVE simulation lane from the human-gated production
lane. Tolerant of absent artifacts (renders a neutral "not yet produced" state).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gui_v2.data.shared import _read_json, card

# Lifecycle labels the operator sees (spec §10).
LABEL_SIM_ACTIVE = "Simulation Active"
LABEL_PENDING = "Production Pending Approval"
LABEL_APPROVED = "Approved for Production"
LABEL_APPLIED = "Applied to Production"


def collect_governance_view(root: Path) -> dict[str, Any]:
    root = Path(root)
    review_dir = root / "outputs" / "promotion_review"
    appr_dir = root / "outputs" / "promotion_approvals"
    sim_dir = root / "outputs" / "simulation"

    policy_dir = root / "outputs" / "policy"

    status = _read_json(review_dir / "daily_governance_status.json") or {}
    bundle = _read_json(sim_dir / "daily_simulation_bundle.json") or {}
    review = _read_json(review_dir / "daily_ai_review_result.json") or {}
    deferred = _read_json(review_dir / "daily_ai_review_deferred.json") or {}
    pending = _read_json(review_dir / "pending_proposals.json") or {}
    approvals = _read_json(appr_dir / "approved_proposals.json") or {}
    app_state = _read_json(appr_dir / "production_application_state.json") or {}

    # ── AI review budget summary ────────────────────────────────────────────
    cap = float((review.get("daily_cost_cap_usd")
                 or deferred.get("daily_cost_cap_usd") or 0.50))
    spent = float(review.get("actual_cost_usd") or 0.0)
    review_status = review.get("status") or deferred.get("status") or "not_run"
    is_deferred = bool(deferred) and deferred.get("review_date") == review.get("review_date") \
        and review_status != "reviewed"
    remaining = round(max(0.0, cap - spent), 6)

    # ── proposal queue counts ───────────────────────────────────────────────
    proposals = pending.get("proposals", []) or []
    approval_recs = approvals.get("approvals", []) or []
    approved_ids = {r.get("proposal_id") for r in approval_recs if r.get("decision") == "approve"}
    rejected_ids = {r.get("proposal_id") for r in approval_recs if r.get("decision") == "reject"}
    applied_count = int(app_state.get("applied_count", 0) or 0)

    pending_only = [p for p in proposals
                    if p.get("proposal_id") not in approved_ids
                    and p.get("proposal_id") not in rejected_ids]

    # ── lane status ─────────────────────────────────────────────────────────
    sim_active = bool(status.get("simulation_lane_active", True))
    prod_overlay = status.get("production_overlay_live", {}) or {}
    prod_live = bool(prod_overlay.get("watchlist") or prod_overlay.get("advisory"))

    cards: list[dict] = []
    cards.append(card(
        "Simulation lane",
        status="ok" if sim_active else "unknown",
        label=LABEL_SIM_ACTIVE if sim_active else "Disabled",
        summary=f"{bundle.get('candidate_count', 0)} candidate(s) this run; "
                f"experiments may change simulation outputs.",
        source_artifacts=["outputs/simulation/daily_simulation_bundle.json"],
        updated_at=bundle.get("generated_at"),
    ))
    cards.append(card(
        "Production lane",
        status="info" if not prod_live else "ok",
        label="Live overlays ON" if prod_live else "Human-gated (overlays OFF)",
        summary=f"{applied_count} approved proposal(s) applied; "
                "production changes only after human approval.",
        source_artifacts=["outputs/promotion_approvals/production_application_state.json"],
        updated_at=app_state.get("generated_at"),
    ))
    cards.append(card(
        "AI/product review",
        status="ok" if review_status == "reviewed" else ("warning" if is_deferred else "unknown"),
        label=("deferred (> cap)" if is_deferred else review_status),
        summary=f"${spent:.4f} spent of ${cap:.2f}/day cap · ${remaining:.4f} remaining · "
                f"adv {review.get('advisory_candidates_reviewed', 0)} + "
                f"wl {review.get('watchlist_candidates_reviewed', 0)} reviewed together.",
        source_artifacts=["outputs/promotion_review/daily_ai_review_result.json"],
        updated_at=review.get("generated_at"),
    ))
    cards.append(card(
        "Promotion queue",
        status="warning" if pending_only else "ok",
        label=f"{len(pending_only)} pending · {len(approved_ids)} approved · {len(rejected_ids)} rejected",
        summary="Pending proposals await human approval before any production effect.",
        source_artifacts=["outputs/promotion_review/pending_proposals.json"],
        updated_at=pending.get("generated_at"),
    ))

    # ── Auto-approval (SIMULATION) — bounded GPT channel, human-vetoable ──────
    aa_summary = _read_json(policy_dir / "auto_approval_audit.json") or {}
    aa_active = aa_summary.get("active_items", []) or []
    aa_cb = aa_summary.get("circuit_breaker", {}) or {}
    auto_applied_items: list[dict] = []
    try:
        from portfolio_automation.sim_governance import auto_approval as _aa
        aa_events = _aa.load_events(base_dir=str(root / "outputs"))
        applied_by_id = {e.get("event_id"): e for e in aa_events
                         if e.get("kind") == _aa.EVENT_APPLIED}
        for it in aa_active:
            ev = applied_by_id.get(it.get("event_id"), {})
            auto_applied_items.append({
                **it,
                "gpt_reasoning": ev.get("gpt_reasoning"),
                "confidence": ev.get("confidence", it.get("confidence")),
                "gate_summary": [g.get("gate_name") for g in (ev.get("gate_trace") or [])
                                 if isinstance(g, dict) and g.get("passed")],
                "target_lane": "simulation",
                "feeds_decision_engine": False,
                "status_label": "Auto-applied in simulation · veto available",
            })
    except Exception:
        auto_applied_items = []

    if aa_summary or auto_applied_items:
        cb_on = bool(aa_cb.get("engaged"))
        cards.append(card(
            "Auto-approval (simulation)",
            status="warning" if (auto_applied_items or cb_on) else "ok",
            label=(f"circuit breaker: {aa_cb.get('reason')}" if cb_on
                   else f"{len(auto_applied_items)} awaiting veto"),
            summary="Bounded GPT auto-approvals in the SIMULATION lane only — never "
                    "production, never the decision engine. Vetoable per event.",
            source_artifacts=["outputs/policy/auto_approval_audit.json",
                              "outputs/policy/auto_approval_events.jsonl"],
            updated_at=aa_summary.get("generated_at"),
        ))

    return {
        "persona": "governance",
        "observe_only": False,   # this lane is gated, not observe-only
        "cards": cards,
        # lane status
        "simulation_lane_active": sim_active,
        "production_overlay_live": prod_overlay,
        "last_simulation_run": bundle.get("generated_at") or status.get("generated_at"),
        # AI review / budget
        "ai_review_status": review_status,
        "ai_review_deferred": is_deferred,
        "ai_cost_today_usd": spent,
        "ai_daily_cap_usd": cap,
        "ai_budget_remaining_usd": remaining,
        "ai_review_method": review.get("review_method"),
        "advisory_candidates_reviewed": review.get("advisory_candidates_reviewed", 0),
        "watchlist_candidates_reviewed": review.get("watchlist_candidates_reviewed", 0),
        # proposal queues
        "pending_proposals": pending_only,
        "approved_proposal_ids": sorted(approved_ids),
        "rejected_proposal_ids": sorted(rejected_ids),
        "applied_count": applied_count,
        "approval_records": approval_recs,
        # auto-approval (simulation) channel
        "auto_applied_items": auto_applied_items,
        "auto_approval_circuit_breaker": aa_cb,
        "labels": {
            "sim_active": LABEL_SIM_ACTIVE,
            "pending": LABEL_PENDING,
            "approved": LABEL_APPROVED,
            "applied": LABEL_APPLIED,
        },
        "has_data": bool(status or bundle or review),
    }
