"""
One-shot operator approval packet (design 2026-07-15).

Read-only builder that consolidates BOTH governance tiers into ONE artifact both
the evening email and the GUI approval page read:

  * tier-a: simulation items the GPT auto-approval channel auto-applied and that
    are still awaiting veto (source: auto_approval.build_summary active_items).
  * tier-b: production-promotion candidates still pending human approval
    (source: promotion_proposals.load_pending_proposals, approval_status=pending).

This module NEVER mutates governance state. Production approval happens only via
the existing human-gated promotion_approvals.record_approval, invoked from the GUI.

Writes:
  * outputs/promotion_review/operator_approval_packet.json
  * outputs/promotion_review/operator_approval_packet.md
"""
from __future__ import annotations

import datetime as _dt
import logging

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)
from portfolio_automation.sim_governance import auto_approval, promotion_proposals

logger = logging.getLogger("stockbot.sim_governance.approval_packet")

_PACKET_JSON = "operator_approval_packet.json"
_PACKET_MD = "operator_approval_packet.md"
_SCHEMA = "operator_approval_packet.v1"


def _veto_deadline(applied_at: str, veto_window_hours: int) -> str | None:
    try:
        t = _dt.datetime.fromisoformat(applied_at)
        return (t + _dt.timedelta(hours=veto_window_hours)).isoformat()
    except Exception:
        return None


def _sim_item(item: dict, veto_window_hours: int) -> dict:
    applied_at = item.get("applied_at")
    return {
        "event_id": item.get("event_id"),
        "candidate_type": item.get("candidate_type"),
        "symbol_or_strategy": item.get("symbol") or item.get("strategy_id"),
        "applied_at": applied_at,
        "veto_deadline": _veto_deadline(applied_at, veto_window_hours) if applied_at else None,
        "confidence": item.get("confidence"),
        "target_lane": "simulation",
        "feeds_decision_engine": False,
        "status": "auto-applied in simulation · veto available",
    }


def _prod_item(p: dict) -> dict:
    change = p.get("proposed_production_change") or {}
    return {
        "proposal_id": p.get("proposal_id"),
        "workflow": p.get("workflow"),
        "proposal_type": p.get("proposal_type"),
        "candidate_id": p.get("candidate_id"),
        "symbol": change.get("symbol"),
        "change": change,
        "risk_summary": p.get("risk_summary"),
        "rollback_plan": p.get("rollback_plan"),
        "evidence": p.get("evidence_refs", []),
        "approval_status": p.get("approval_status"),
        "created_at": p.get("created_at"),
        "status": "pending human review",
    }


def build_operator_packet(base_dir: str, now: str, *, deep_link_base: str = "",
                          veto_window_hours: int = 48) -> dict:
    """Assemble the two-tier packet. Read-only; never raises."""
    packet = {
        "schema": _SCHEMA,
        "observe_only": True,
        "generated_at": now,
        "generated_by": "portfolio_automation.sim_governance.approval_packet",
        "approval_page_url": (f"{deep_link_base.rstrip('/')}/dashboard/governance"
                              if deep_link_base else "/dashboard/governance"),
        "tier_sim": [],
        "tier_production": [],
        "counts": {"tier_sim_within_veto": 0, "tier_production_pending": 0},
    }
    try:
        summary = auto_approval.build_summary(base_dir=base_dir, now=now)
        packet["tier_sim"] = [_sim_item(i, veto_window_hours)
                              for i in (summary.get("active_items") or [])]
        pending = promotion_proposals.load_pending_proposals(base_dir)
        packet["tier_production"] = [_prod_item(p) for p in pending
                                     if (p.get("approval_status") == "pending")]
        packet["counts"] = {
            "tier_sim_within_veto": len(packet["tier_sim"]),
            "tier_production_pending": len(packet["tier_production"]),
        }
    except Exception as exc:  # degraded, never raise into the pipeline
        logger.warning("approval_packet: build failed: %s", exc)
        packet["error"] = str(exc)
    return packet


def _render_md(packet: dict) -> str:
    c = packet.get("counts", {})
    lines = [
        "# Operator Approval Packet",
        "",
        f"Generated: {packet.get('generated_at')}",
        f"Review & approve: {packet.get('approval_page_url')}",
        "",
        f"## Simulation items awaiting veto ({c.get('tier_sim_within_veto', 0)})",
    ]
    for i in packet.get("tier_sim", []):
        lines.append(f"- [{i.get('candidate_type')}] {i.get('symbol_or_strategy')} "
                     f"(event {i.get('event_id')}) — {i.get('status')}")
    lines += ["", f"## Production candidates pending approval "
                  f"({c.get('tier_production_pending', 0)})"]
    for p in packet.get("tier_production", []):
        lines.append(f"- [{p.get('workflow')}] {p.get('symbol')} "
                     f"(proposal {p.get('proposal_id')}) — {p.get('status')}")
    return "\n".join(lines) + "\n"


def write_operator_packet(packet: dict, *, base_dir: str) -> dict:
    """Write JSON + MD artifacts. Best-effort; logs on failure."""
    try:
        safe_write_json(OutputNamespace.PROMOTION_REVIEW, _PACKET_JSON, packet,
                        base_dir=base_dir)
        safe_write_text(OutputNamespace.PROMOTION_REVIEW, _PACKET_MD, _render_md(packet),
                        base_dir=base_dir)
    except Exception as exc:
        logger.warning("approval_packet: write failed: %s", exc)
    return packet
