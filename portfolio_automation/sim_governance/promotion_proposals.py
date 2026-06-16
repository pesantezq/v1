"""
Pending production-proposal generation (spec §5).

When (and only when) the AI/product review marks a candidate
``ready_for_production_review``, a *pending* production proposal is created. The
proposal carries everything a human needs to approve: the concrete production
change, evidence/review/simulation refs, a risk summary, and a rollback plan.

Every generated proposal defaults to ``approval_status: pending`` and therefore
has NO effect on production until a human approves it.

Writes:
  * outputs/promotion_review/pending_proposals.json   (full current set)
  * outputs/promotion_review/proposals_log.jsonl       (append-only history)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from portfolio_automation.data_governance import (
    OutputNamespace,
    ensure_output_dir,
    get_output_path,
    safe_write_json,
)
from portfolio_automation.sim_governance import schemas as S

logger = logging.getLogger("stockbot.sim_governance.promotion_proposals")

_PENDING_FILE = "pending_proposals.json"
_LOG_FILE = "proposals_log.jsonl"


def _rollback_plan_for(proposal_type: str, symbol: str | None) -> str:
    sym = symbol or "the affected item"
    if proposal_type in (S.PROPOSAL_WATCHLIST_ADD, S.PROPOSAL_DISCOVERY_PROMOTION):
        return (f"Remove {sym} from the approved-watchlist overlay artifact and re-run "
                "the watchlist loader; production reverts to the baseline watchlist.")
    if proposal_type == S.PROPOSAL_WATCHLIST_REMOVE:
        return f"Restore {sym} to the approved-watchlist overlay; baseline membership returns."
    if proposal_type in (S.PROPOSAL_WATCHLIST_RANK, S.PROPOSAL_WATCHLIST_TAG,
                         S.PROPOSAL_FLOCK_WATCHLIST_LOGIC):
        return f"Delete the {sym} overlay entry; ranking/tags revert to baseline."
    if proposal_type in (S.PROPOSAL_FLOCK_CONTEXT_DISPLAY, S.PROPOSAL_FLOCK_ADVISORY_CONTEXT,
                         S.PROPOSAL_FLOCK_RISK_OVERLAY):
        return (f"Delete the {sym} flock-context/risk overlay entry and re-run the advisory "
                "loader; the displayed flock context reverts to absent (decision_engine "
                "untouched; display-only).")
    if proposal_type == S.PROPOSAL_FLOCK_SCORING_ADJUSTMENT:
        return (f"Remove the {sym} flock scoring-adjustment overlay; simulation confidence "
                "reverts to the unadjusted baseline. Production scoring is unaffected unless "
                "this overlay is separately enabled.")
    # advisory / crowd context
    return (f"Delete the {sym} advisory overlay entry and re-run the advisory loader; "
            "the decision plan reverts to baseline inputs (decision_engine untouched).")


def generate_proposals(
    candidates_by_id: dict[str, dict],
    review_result: dict,
    now: str,
    *,
    base_dir: str,
    write_files: bool = True,
) -> dict:
    """Create pending proposals for every candidate the review marked READY.

    Args:
        candidates_by_id: {candidate_id: candidate_dict} from the bundle/lane.
        review_result: the dict from daily_ai_review.run_daily_ai_review.
        now: ISO timestamp (caller-supplied).
    """
    verdicts = review_result.get("verdicts", []) or []
    proposals: list[S.PromotionProposal] = []

    for v in verdicts:
        if v.get("decision") != S.DECISION_READY:
            continue
        cid = v.get("candidate_id")
        cand = candidates_by_id.get(cid)
        if not cand:
            continue
        ptype = cand.get("proposal_type")
        if not S.is_valid_proposal_type(ptype):
            logger.warning("promotion_proposals: skipping unknown proposal_type %r", ptype)
            continue
        symbol = cand.get("symbol")
        pid = S.make_proposal_id(cid, now)
        proposals.append(S.PromotionProposal(
            proposal_id=pid,
            candidate_id=cid,
            proposal_type=ptype,
            workflow=S.workflow_for_proposal_type(ptype),
            proposed_production_change=dict(cand.get("proposed_production_change", {})),
            evidence_refs=list(cand.get("source_evidence", [])),
            ai_review_refs=["outputs/promotion_review/daily_ai_review_result.json"],
            simulation_result_refs=[
                "outputs/simulation/daily_simulation_bundle.json",
                "outputs/sandbox/sim_governance/simulation_candidates.json",
            ],
            risk_summary=(f"risk_impact={cand.get('risk_impact')}, "
                          f"confidence={cand.get('confidence')}, "
                          f"data_quality={cand.get('data_quality')}, "
                          f"evidence_strength={v.get('evidence_strength')}"),
            rollback_plan=_rollback_plan_for(ptype, symbol),
            approval_status=S.APPROVAL_PENDING,
            approved_by=None,
            approved_at=None,
            approval_notes=None,
            created_at=now,
        ))

    payload = {
        "generated_at": now,
        "schema": "pending_proposals.v1",
        "pending_count": len(proposals),
        "note": ("Every proposal defaults to approval_status=pending and has NO effect "
                 "on production until a human approves it. AI cannot self-approve."),
        "proposals": [p.to_dict() for p in proposals],
    }

    if write_files:
        try:
            safe_write_json(OutputNamespace.PROMOTION_REVIEW, _PENDING_FILE, payload, base_dir=base_dir)
            ensure_output_dir(OutputNamespace.PROMOTION_REVIEW, _LOG_FILE, base_dir=base_dir)
            log_path = get_output_path(OutputNamespace.PROMOTION_REVIEW, _LOG_FILE, base_dir=base_dir)
            with Path(log_path).open("a", encoding="utf-8") as fh:
                for p in proposals:
                    fh.write(json.dumps(p.to_dict(), default=str) + "\n")
        except Exception as exc:
            logger.warning("promotion_proposals: write failed: %s", exc)
            payload["write_error"] = str(exc)

    return payload


def load_pending_proposals(base_dir: str) -> list[dict]:
    """Load the current pending-proposal set (best-effort)."""
    path = get_output_path(OutputNamespace.PROMOTION_REVIEW, _PENDING_FILE, base_dir=base_dir)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data.get("proposals", []) or []
    except Exception:
        return []
