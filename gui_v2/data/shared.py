"""Shared helpers for the persona dashboard: normalized card shape + json reader."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

_STATUS_TO_SEVERITY = {
    "ok": "green",
    "warning": "yellow",
    "red": "red",
    "info": "blue",
    "unknown": "gray",
}


def _read_json(path: Path) -> Any | None:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def weekly_deployment_view(cash: dict | None) -> dict:
    """Display-ready projection of the cash_deployment_plan monthly envelope +
    weekly pacing (feature 2026-07-07). Observe-only; reads the artifact, never
    recomputes. Null-guards a pre-feature artifact: an envelope emitted before the
    feature landed has no ``glide_slice`` / ``weekly_pacing`` keys, so those degrade
    to None and the weekly line is simply omitted by consumers.

    Returns ``{"available": False, ...}`` when no OK envelope is present.
    """
    if not isinstance(cash, dict):
        return {"available": False, "reason": "cash_deployment_plan_missing"}
    env = cash.get("monthly_capital_envelope") or {}
    if not isinstance(env, dict) or env.get("status") != "ok":
        return {"available": False, "reason": "envelope_unavailable"}
    pacing = env.get("weekly_pacing") or {}
    rows = cash.get("deployment_rows") or []

    def _num(v):
        return v if isinstance(v, (int, float)) else None

    funded = [
        {
            "symbol": r.get("symbol"),
            "amount": _num(r.get("suggested_amount")),
            "pct_of_net_investable": r.get("pct_of_net_investable"),
            "status": r.get("status"),
        }
        for r in rows
        if isinstance(r, dict) and (_num(r.get("suggested_amount")) or 0) > 0
    ]

    def _deferred(status):
        return [
            r.get("symbol") for r in rows
            if isinstance(r, dict) and r.get("status") == status
        ]

    return {
        "available": True,
        "net_investable": _num(env.get("monthly_contribution_net_investable")),
        "contribution_base": _num(env.get("monthly_contribution_net_investable_base")),
        "glide_slice": _num(env.get("glide_slice")),
        "reserve": _num(env.get("cash_reserve_target_amount")),
        "utilization_pct": _num(env.get("monthly_utilization_pct")),
        "history_status": env.get("monthly_history_status"),
        "deploy_cadence": pacing.get("deploy_cadence"),
        "weekly_tranche": _num(pacing.get("weekly_tranche")),
        "weekly_remaining": _num(pacing.get("weekly_remaining")),
        "deployed_this_week": _num(pacing.get("deployed_this_week")),
        "funded": funded,
        "deferred_weekly": _deferred("DEFERRED_BY_WEEKLY_PACING"),
        "deferred_monthly": _deferred("DEFERRED_BY_MONTHLY_BUDGET"),
    }


def card(
    title: str,
    *,
    status: str = "unknown",
    label: str = "",
    summary: str = "",
    source_artifacts: list[str] | None = None,
    updated_at: str | None = None,
) -> dict:
    """Normalized dashboard card. status in ok|warning|red|info|unknown."""
    status = status if status in _STATUS_TO_SEVERITY else "unknown"
    return {
        "title": title,
        "status": status,
        "label": label,
        "summary": summary,
        "source_artifacts": source_artifacts or [],
        "updated_at": updated_at,
        "severity": _STATUS_TO_SEVERITY[status],
    }


# Old-route -> persona-route redirect map (Task 1 wires these in app.py).
REDIRECT_MAP = {
    "/portfolio": "/dashboard/portfolio",
    "/risk-impact": "/dashboard/portfolio",
    "/research": "/dashboard/quant",
    "/health": "/dashboard/system",
    "/operations": "/dashboard/system",
}
