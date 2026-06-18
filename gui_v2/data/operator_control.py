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

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from operator_control.probe_registry import probes_for_view
from operator_control.skill_registry import skill_for_probe_action
from operator_control import work_orders as wo
from operator_control.work_orders import list_work_orders
from portfolio_automation.operator_worker_readiness import operator_worker_readiness
from gui_v2.data.operator_quarantine import quarantine_inventory
from gui_v2.data.shared import card

STALE_HOURS = 24
CANCELLABLE = frozenset({"queued", "awaiting_approval", "approved"})
_OPEN = frozenset({"queued", "awaiting_approval", "claimed", "running", "approved"})

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
                "label": "Repair" if mode == "safe_repair" else _ACTION_LABELS.get(mode, mode),
                "mode": mode,
                "skill_id": skill.skill_id,
                "skill_name": skill.name,
                "approval_required": approval,
                # safe_repair actions DISPATCH an autonomous worker (the click is
                # the approval); diagnose/propose_fix only create a queued order.
                "dispatch": mode == "safe_repair",
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

    ctx: dict[str, Any] = {
        "operator_probes": _operator_probes(view),
        "operator_work_orders": view_orders,
        "operator_summary": _summarize(all_orders),
        "operator_proposal_only": view in _PROPOSAL_ONLY_VIEWS,
        "operator_post_url": "/dashboard/operator/create",
        "operator_dispatch_url": "/dashboard/operator/dispatch",
        "operator_observe_only": True,
    }
    # The Phase 2 worker-runner status is a System-tab (developer) concern only.
    if view == "system":
        ctx["operator_runner"] = worker_runner_status(root)
    return ctx


def worker_runner_status(root: Path | str) -> dict[str, Any]:
    """Read-only Phase 2 worker-runner summary card for the System tab.

    Best-effort: any failure yields a neutral card so the System tab never
    breaks if the runner module is unavailable.
    """
    try:
        from operator_control import worker_runner
        st = worker_runner.status(root)
    except Exception:
        st = {"by_status": {}, "worktrees": [], "autonomous_enabled": False}

    by = st.get("by_status", {})
    completed = by.get("completed", 0)
    failed = by.get("failed", 0)
    running = by.get("running", 0)
    claimed = by.get("claimed", 0)
    worktrees = [w for w in st.get("worktrees", []) if ".worktrees" in str(w)]

    if failed:
        status = "red"
    elif running or claimed:
        status = "warning"
    else:
        status = "ok"
    label = "autonomous ON" if st.get("autonomous_enabled") else "scaffold-only"
    cost = st.get("operational_cost_usd_total", 0.0)
    runs = st.get("operational_runs", 0)

    return card(
        "Worker Runner",
        status=status,
        label=label,
        summary=(
            f"{completed} completed; {failed} failed; {running} running; "
            f"{claimed} claimed; {len(worktrees)} worktrees · "
            f"operational cost ${cost} over {runs} run(s) "
            f"(separate from FMP/AI budget)"
        ),
        source_artifacts=[
            "outputs/operator_control/work_orders.jsonl",
            "outputs/operator_control/worker_cost_log.jsonl",
        ],
    )


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


def _age_hours(created_at: str) -> float | None:
    try:
        dt = datetime.fromisoformat(created_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - dt).total_seconds() / 3600, 1)
    except (TypeError, ValueError):
        return None


def operator_worker_view(root) -> dict[str, Any]:
    """Compose the /dashboard/operator view-model (observe-only, live)."""
    readiness = operator_worker_readiness(root)
    try:
        raw = list_work_orders(root)
    except Exception:
        raw = []
    orders = []
    counts = {k: 0 for k in ("open", "awaiting_approval", "failed", "quarantined",
                              "cancelled", "completed", "stale")}
    for o in raw:
        status = o.get("status")
        age = _age_hours(o.get("created_at"))
        stale = bool(status in _OPEN and age is not None and age > STALE_HOURS)
        orders.append({
            "work_order_id": o.get("work_order_id"), "status": status,
            "created_at": o.get("created_at"), "age_hours": age,
            "probe_id": o.get("probe_id"), "skill_id": o.get("skill_id"),
            "cancellable": status in CANCELLABLE, "stale": stale,
        })
        if status in _OPEN:
            counts["open"] += 1
        if status in counts:
            counts[status] += 1
        if stale:
            counts["stale"] += 1
    try:
        quarantine = quarantine_inventory(root)
    except Exception:
        quarantine = []
    counts["quarantined"] = len(quarantine)
    return {"readiness": readiness, "cost": readiness.get("cost", {}),
            "orders": orders, "counts": counts, "quarantine": quarantine,
            "degraded": bool(readiness.get("error"))}


__all__ = [
    "operator_control_context",
    "today_operator_summary",
    "worker_runner_status",
    "operator_worker_view",
]
