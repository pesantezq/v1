from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_budget.usage_ledger import UsageLedger
from portfolio_automation.data_budget.cache import cache_stats
from portfolio_automation.data_governance import OutputNamespace, safe_write_text

_OBSERVE_ONLY = True


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_status(*, ledger: UsageLedger, cache_dir: Path, portfolio_symbols: list[str],
                 month: str, monthly_bandwidth_gb: float,
                 run_modes: dict[str, Any]) -> tuple[dict, dict, dict]:
    monthly_bytes = ledger.monthly_bytes(month=month)
    cap_bytes = int(float(monthly_bandwidth_gb) * 1024**3)
    hit_rate = ledger.cache_hit_rate(month=month)
    # Budget-only skips (run_budget / bandwidth_guard) — transient rate_limited
    # skips are excluded by skipped_count's default so they don't flip these
    # "..._due_to_budget" flags (which gate the AMBER health signal).
    discovery_skipped = ledger.skipped_count(month=month, run_mode="discovery") > 0
    replay_skipped = ledger.skipped_count(month=month, run_mode="historical_replay") > 0

    usage = {
        "generated_at": _ts(), "observe_only": _OBSERVE_ONLY, "source": "fmp_usage_status",
        "month": month,
        "calls_by_run_mode": ledger.calls_by_run_mode(month=month),
        "calls_by_endpoint": ledger.calls_by_endpoint(month=month),
    }
    cstats = cache_stats(cache_dir,
                         fresh_keys=[f"quote_short_{s.upper()}" for s in portfolio_symbols],
                         ttl_seconds=3600)
    cache = {
        "generated_at": _ts(), "observe_only": _OBSERVE_ONLY, "source": "fmp_cache_status",
        "cache_hit_rate": hit_rate,
        "file_count": cstats["file_count"], "total_size_bytes": cstats["total_size_bytes"],
        "portfolio_fresh": cstats["fresh"],
    }
    pct = round(monthly_bytes / cap_bytes, 4) if cap_bytes else None
    overall = "ok"
    if pct is not None and pct >= 1.0:
        overall = "constrained"
    elif pct is not None and pct >= 0.8:
        overall = "near_cap"
    budget = {
        "generated_at": _ts(), "observe_only": _OBSERVE_ONLY, "source": "data_budget_status",
        "overall_status": overall,
        "monthly_bandwidth_bytes": monthly_bytes,
        "monthly_bandwidth_gb_cap": monthly_bandwidth_gb,
        "monthly_bandwidth_pct": pct,
        "discovery_skipped_due_to_budget": discovery_skipped,
        "backtest_skipped_due_to_budget": replay_skipped,
        "enabled": True,
        "run_mode_budgets": run_modes,
    }
    return usage, cache, budget


def write_status_artifacts(*, ledger: UsageLedger, cache_dir: Path,
                           portfolio_symbols: list[str], month: str,
                           monthly_bandwidth_gb: float, run_modes: dict[str, Any],
                           base_dir: Path | str = "outputs") -> None:
    usage, cache, budget = build_status(
        ledger=ledger, cache_dir=cache_dir, portfolio_symbols=portfolio_symbols,
        month=month, monthly_bandwidth_gb=monthly_bandwidth_gb, run_modes=run_modes)
    for name, payload in (("fmp_usage_status.json", usage),
                          ("fmp_cache_status.json", cache),
                          ("data_budget_status.json", budget)):
        # OutputNamespace.LATEST governance (CLAUDE.md: all writes go through it).
        safe_write_text(OutputNamespace.LATEST, name,
                        json.dumps(payload, indent=2), base_dir=base_dir)
