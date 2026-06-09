"""GUI adapter for the operator-control plane.

Maps the probe + skill registries and the (folded) append-only work-order log
into a presentation view-model the persona dashboards can render. This is a thin
*control-plane over existing artifacts* — it does not execute anything and adds
no second source of truth.

SAFETY:
  * Emits only registry-derived action descriptors (probe_id, skill_id, mode).
    There is no field a template could turn into an executable command.
  * Quant actions carry ``proposal_only=True`` so the UI never dresses
    proposal-only evidence as official advice.
  * observe_only=True hardcoded.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from operator_control.probe_registry import probes_for_view
from operator_control.skill_registry import skill_for_probe_action
from operator_control import work_orders as wo

# Human labels for the action buttons (the only place mode → label happens).
_ACTION_LABELS = {
    "diagnose": "Diagnose",
    "propose_fix": "Propose Fix",
    "safe_repair": "Safe Repair",
}

# Views whose operator actions are proposal-only evidence (not official advice).
_PROPOSAL_ONLY_VIEWS = frozenset({"quant"})

# How many recent work orders to surface per view.
_RECENT_LIMIT = 8


def _action_descriptors(probe) -> list[dict[str, Any]]:
    """Build the bounded button descriptors for one probe."""
    actions: list[dict[str, Any]] = []
    for mode in probe.allowed_actions:
        skill = skill_for_probe_action(probe.probe_id, mode)
        if skill is None:
            continue  # no allowlisted skill serves this mode — omit the button
        approval = (
            mode in skill.approval_required_for_modes
            or probe.approval_required
            or mode == "safe_repair"
        )
        actions.append(
            {
                "label": _ACTION_LABELS.get(mode, mode),
                "mode": mode,
                "skill_id": skill.skill_id,
                "skill_name": skill.name,
                "approval_required": approval,
            }
        )
    return actions


def _operator_probes(view: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for probe in probes_for_view(view):
        out.append(
            {
                "probe_id": probe.probe_id,
                "display_name": probe.display_name,
                "description": probe.description,
                "severity": probe.severity,
                "risk_level": probe.risk_level,
                "source_artifact": probe.source_artifact,
                "observe_only_notice": probe.observe_only_notice,
                "actions": _action_descriptors(probe),
            }
        )
    return out


def _summarize(orders: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"total": len(orders)}
    for o in orders:
        st = o.get("status", "unknown")
        summary[st] = summary.get(st, 0) + 1
    return summary


def operator_control_context(root: Path | str, view: str) -> dict[str, Any]:
    """Return the operator-control keys to merge into a persona view context.

    Keys (all additive — never collide with existing loader keys):
      * ``operator_probes``      — probes for this view + their action buttons
      * ``operator_work_orders`` — recent work orders for THIS view (folded)
      * ``operator_summary``     — counts by status across ALL work orders
      * ``operator_proposal_only`` — True for the quant view
      * ``operator_post_url``    — the create endpoint
      * ``operator_observe_only``— hardcoded True
    """
    root = Path(root)
    try:
        all_orders = wo.list_work_orders(root)
    except Exception:
        all_orders = []

    view_orders = [o for o in all_orders if o.get("source_view") == view][:_RECENT_LIMIT]

    return {
        "operator_probes": _operator_probes(view),
        "operator_work_orders": view_orders,
        "operator_summary": _summarize(all_orders),
        "operator_proposal_only": view in _PROPOSAL_ONLY_VIEWS,
        "operator_post_url": "/dashboard/operator/create",
        "operator_observe_only": True,
    }


def today_operator_summary(root: Path | str) -> dict[str, Any]:
    """Compact summary for the Today tab — open work-order count only."""
    root = Path(root)
    try:
        orders = wo.list_work_orders(root)
    except Exception:
        orders = []
    open_statuses = {"queued", "awaiting_approval", "claimed", "running", "approved"}
    open_count = sum(1 for o in orders if o.get("status") in open_statuses)
    return {
        "operator_open_count": open_count,
        "operator_total_count": len(orders),
        "operator_observe_only": True,
    }


__all__ = ["operator_control_context", "today_operator_summary"]
