"""
Pending promotion-backlog review (daily-tool-analysis 6n consumer).

Read-only join of the two existing sim-governance artifacts:

  * outputs/promotion_review/pending_proposals.json    (the pending queue)
  * outputs/promotion_review/daily_ai_review_result.json (per-candidate verdicts)

For each pending proposal it resolves the AI verdict (by ``candidate_id``),
classifies readiness (``ready`` / ``hold`` / ``reject`` / ``unknown``), parses
the risk summary, computes how long the proposal has been pending, and derives a
recommended *human* action. When a proposal is testing-ready it is routed to
human approval — surfaced as ``AWAITING_HUMAN_APPROVAL`` with a hand-off pointer
to the existing ``promotion_approvals.record_approval`` / GUI ``/governance``
path.

Hard invariants (mirrors CLAUDE.md two-lane + human-gate contract):
  * This module NEVER writes a file — it is a pure read/derive helper.
  * This module NEVER approves. ``ai_can_approve_production`` is surfaced
    verbatim (always False); the hand-off only points at the human path.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from portfolio_automation.data_governance import OutputNamespace, get_output_path
from portfolio_automation.sim_governance import schemas as S

logger = logging.getLogger("stockbot.sim_governance.backlog_review")

_PENDING_FILE = "pending_proposals.json"
_REVIEW_FILE = "daily_ai_review_result.json"

# readiness -> recommended human action
_RECOMMENDATION = {
    "ready": "AWAITING_HUMAN_APPROVAL",
    "hold": "HOLD",
    "reject": "DROP_CANDIDATE",
    "unknown": "SURFACE_FOR_REVIEW",
}


def _load(base_dir: str, filename: str) -> dict | None:
    try:
        # base_dir is the repo root; sim-gov artifacts live under <root>/outputs/.
        path = get_output_path(
            OutputNamespace.PROMOTION_REVIEW, filename, base_dir=str(Path(base_dir) / "outputs")
        )
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    except Exception as exc:  # never raise into the daily check
        logger.warning("backlog_review: failed to load %s: %s", filename, exc)
        return None


def _parse_risk_summary(text: str | None) -> dict:
    """Parse ``"risk_impact=medium, confidence=0.9, data_quality=ok, ..."``."""
    out: dict[str, str] = {}
    if not isinstance(text, str):
        return out
    for part in text.split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


def _age_days(created_at: str | None, now: datetime) -> float | None:
    if not isinstance(created_at, str):
        return None
    try:
        ts = datetime.fromisoformat(created_at)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return round((now - ts).total_seconds() / 86400.0, 2)


def _classify(candidate_id: str, verdict: dict | None, ready_ids: set[str]) -> str:
    if verdict is not None:
        decision = verdict.get("decision")
        if decision == S.DECISION_READY:
            return "ready"
        if decision == S.DECISION_CONTINUE_TESTING:
            return "hold"
        if decision == S.DECISION_REJECT:
            return "reject"
    if candidate_id in ready_ids:
        return "ready"
    return "unknown"


def _approval_hint(proposal_id: str) -> str:
    return (
        f"Approve via GUI /governance or "
        f"promotion_approvals.record_approval('{proposal_id}', 'approve', <human_approver>, ...). "
        "AI cannot approve (ai_can_approve_production=false)."
    )


def review_pending_backlog(base_dir: str = ".", now: datetime | None = None) -> dict:
    """Join pending proposals with AI verdicts and classify each for a human.

    Returns a degraded dict ``{"available": False, "reason": ...}`` (still
    carrying the safety invariants) when the pending-proposals artifact is
    missing. Never raises; never writes.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    pending = _load(base_dir, _PENDING_FILE)
    if pending is None:
        return {
            "available": False,
            "reason": "pending_proposals.json missing or unreadable",
            "observe_only": True,
            "human_gated": True,
            "ai_can_approve_production": False,
        }

    review = _load(base_dir, _REVIEW_FILE) or {}
    verdict_by_cand = {
        v.get("candidate_id"): v
        for v in (review.get("verdicts") or [])
        if isinstance(v, dict) and v.get("candidate_id")
    }
    ready_ids = set(review.get("ready_candidate_ids") or [])
    ai_can_approve = bool(review.get("ai_can_approve_production", False))

    items: list[dict] = []
    counts = {"ready": 0, "hold": 0, "reject": 0, "unknown": 0}
    for prop in pending.get("proposals") or []:
        if not isinstance(prop, dict):
            continue
        cand = prop.get("candidate_id")
        verdict = verdict_by_cand.get(cand)
        readiness = _classify(cand, verdict, ready_ids)
        counts[readiness] += 1
        risk = _parse_risk_summary(prop.get("risk_summary"))
        change = prop.get("proposed_production_change") or {}
        age = _age_days(prop.get("created_at"), now)
        pid = prop.get("proposal_id", "?")
        rollback_readiness = (
            (verdict or {}).get("rollback_readiness")
            or ("documented" if prop.get("rollback_plan") else "unknown")
        )
        items.append({
            "proposal_id": pid,
            "candidate_id": cand,
            "proposal_type": prop.get("proposal_type"),
            "workflow": prop.get("workflow"),
            "symbol": change.get("symbol"),
            "risk_level": (verdict or {}).get("risk_level") or risk.get("risk_impact"),
            "evidence_strength": (verdict or {}).get("evidence_strength") or risk.get("evidence_strength"),
            "data_quality": risk.get("data_quality"),
            "confidence": risk.get("confidence"),
            "rollback_readiness": rollback_readiness,
            "ai_decision": (verdict or {}).get("decision"),
            "ai_reason": (verdict or {}).get("reason"),
            "readiness": readiness,
            "age_days": age,
            "recommendation": _RECOMMENDATION[readiness],
            "approval_hint": _approval_hint(pid) if readiness == "ready" else None,
        })

    ready_ages = [it["age_days"] for it in items if it["readiness"] == "ready" and it["age_days"] is not None]
    return {
        "available": True,
        "generated_at": pending.get("generated_at"),
        "pending_count": pending.get("pending_count", len(items)),
        "ready_count": counts["ready"],
        "hold_count": counts["hold"],
        "reject_count": counts["reject"],
        "unknown_count": counts["unknown"],
        "oldest_ready_age_days": max(ready_ages) if ready_ages else None,
        "ai_can_approve_production": ai_can_approve,
        "observe_only": True,
        "human_gated": True,
        "items": items,
    }
