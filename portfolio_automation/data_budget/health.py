from __future__ import annotations
from typing import Any, Optional


def data_budget_health(budget_status: Optional[dict[str, Any]]) -> dict[str, str]:
    """Pure GREEN/AMBER classifier for the data-budget layer (never RED — observe-only)."""
    if not budget_status:
        return {"status": "green", "reason": "data_budget_status absent (inert)"}
    pct = budget_status.get("monthly_bandwidth_pct")
    if budget_status.get("discovery_skipped_due_to_budget") or \
       budget_status.get("backtest_skipped_due_to_budget"):
        return {"status": "amber", "reason": "discovery/backtest skipped due to FMP budget"}
    if (pct is not None and pct >= 0.8) or \
       budget_status.get("overall_status") in ("near_cap", "constrained"):
        return {"status": "amber", "reason": f"monthly bandwidth at {pct} of cap"}
    return {"status": "green", "reason": "within budget"}
