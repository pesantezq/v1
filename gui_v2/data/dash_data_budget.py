from __future__ import annotations
import json
from pathlib import Path
from typing import Any


def _load(root: Path, name: str) -> dict[str, Any] | None:
    try:
        return json.loads((root / "outputs" / "latest" / name).read_text(encoding="utf-8"))
    except Exception:
        return None


def data_budget_view(root: Path | str = ".") -> dict[str, Any]:
    root = Path(root)
    usage = _load(root, "fmp_usage_status.json")
    cache = _load(root, "fmp_cache_status.json")
    budget = _load(root, "data_budget_status.json")
    if not (usage or cache or budget):
        return {"available": False}
    calls = sum((usage or {}).get("calls_by_run_mode", {}).values()) if usage else 0
    hit = (cache or {}).get("cache_hit_rate")
    pct = (budget or {}).get("monthly_bandwidth_pct")
    return {
        "available": True,
        "observe_only": True,
        "calls_this_run": calls,
        "cache_hit_rate_pct": round(hit * 100, 1) if hit is not None else None,
        "bandwidth_pct": round(pct * 100, 1) if pct is not None else None,
        "overall_status": (budget or {}).get("overall_status", "unknown"),
        "discovery_skipped": bool((budget or {}).get("discovery_skipped_due_to_budget")),
        "backtest_skipped": bool((budget or {}).get("backtest_skipped_due_to_budget")),
        "portfolio_fresh": (cache or {}).get("portfolio_fresh", {}),
    }
