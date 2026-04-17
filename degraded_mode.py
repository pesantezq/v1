from __future__ import annotations

from pathlib import Path
from typing import Any


DEFAULT_STALE_DAYS = 7
MIN_TRUSTED_DATASET_SIZE = 5


def infer_degraded_reason(
    *,
    fmp_attempted: bool = False,
    fmp_succeeded: bool = False,
    fmp_error: str | None = None,
    fallback_used: bool = False,
    watchlist_source: str = "none",
    scan_status: str | None = None,
) -> str | None:
    err = (fmp_error or "").lower()
    if "circuit breaker" in err:
        return "circuit_breaker"
    if "http 403" in err:
        return "fmp_403"
    if "http 401" in err:
        return "fmp_401"
    if "http 429" in err:
        return "fmp_429"
    if "budget" in err:
        return "budget_exhausted"
    if "http 5" in err:
        return "fmp_5xx"
    if scan_status == "cache_only":
        return "cache_only"
    if scan_status == "degraded":
        return "partial_cache"
    if fallback_used or (watchlist_source or "").startswith("fallback"):
        return "fallback_watchlist"
    if fmp_attempted and not fmp_succeeded:
        return "fmp_failed"
    return None


def infer_data_sources_used(
    *,
    fmp_succeeded: bool = False,
    fallback_used: bool = False,
    watchlist_source: str = "none",
    scan_status: str | None = None,
    extra_sources: list[str] | None = None,
) -> list[str]:
    sources: list[str] = []
    if fmp_succeeded:
        sources.append("fmp")
    if fallback_used or (watchlist_source or "").startswith("fallback"):
        sources.append("fallback")
    if scan_status in {"cache_only", "degraded"}:
        sources.append("cache")
    for src in extra_sources or []:
        if src and src not in sources:
            sources.append(src)
    if not sources:
        sources.append("live")
    return sources


def infer_data_mode(data_sources_used: list[str]) -> str:
    normalized = [s for s in data_sources_used if s]
    if not normalized or normalized == ["live"] or normalized == ["fmp"] or normalized == ["alphavantage"]:
        return "live"
    if all(src in {"fallback", "cache"} for src in normalized):
        return "fallback"
    if "fmp" in normalized or "alphavantage" in normalized or "live" in normalized:
        return "mixed"
    return "fallback"


def infer_confidence_penalty(
    *,
    degraded_mode: bool,
    degraded_reason: str | None,
    stale_cache_days: int | None = None,
) -> float:
    if not degraded_mode:
        return 0.0
    if degraded_reason == "circuit_breaker":
        penalty = 0.30
    elif degraded_reason in {"fmp_401", "fmp_403", "fmp_429", "fmp_5xx"}:
        penalty = 0.25
    elif degraded_reason in {"cache_only", "budget_exhausted"}:
        penalty = 0.20
    else:
        penalty = 0.15
    if stale_cache_days is not None and stale_cache_days > DEFAULT_STALE_DAYS:
        penalty = max(penalty, 0.30)
    return round(min(penalty, 0.5), 3)


def compute_fallback_depth(data_sources_used: list[str]) -> int:
    return len([src for src in data_sources_used if src not in {"live", "fmp", "alphavantage", "rss", "sp500_cache"}])


def build_data_health_context(
    *,
    fmp_attempted: bool = False,
    fmp_succeeded: bool = False,
    fmp_error: str | None = None,
    fallback_used: bool = False,
    watchlist_source: str = "none",
    scan_status: str | None = None,
    data_latency_ms: int | None = None,
    stale_cache_days: int | None = None,
    extra_sources: list[str] | None = None,
) -> dict[str, Any]:
    degraded_reason = infer_degraded_reason(
        fmp_attempted=fmp_attempted,
        fmp_succeeded=fmp_succeeded,
        fmp_error=fmp_error,
        fallback_used=fallback_used,
        watchlist_source=watchlist_source,
        scan_status=scan_status,
    )
    data_sources_used = infer_data_sources_used(
        fmp_succeeded=fmp_succeeded,
        fallback_used=fallback_used,
        watchlist_source=watchlist_source,
        scan_status=scan_status,
        extra_sources=extra_sources,
    )
    degraded_mode = bool(degraded_reason)
    data_mode = infer_data_mode(data_sources_used)
    return {
        "degraded_mode": degraded_mode,
        "degraded_reason": degraded_reason,
        "data_sources_used": data_sources_used,
        "data_mode": data_mode,
        "data_fallback_triggered": fallback_used or watchlist_source.startswith("fallback"),
        "llm_fallback_triggered": False,
        "data_latency_ms": data_latency_ms,
        "fallback_depth": compute_fallback_depth(data_sources_used),
        "degraded_confidence_penalty": infer_confidence_penalty(
            degraded_mode=degraded_mode,
            degraded_reason=degraded_reason,
            stale_cache_days=stale_cache_days,
        ),
        "stale_cache_days": stale_cache_days,
    }


def stale_cache_days_for_path(path: str | Path | None) -> int | None:
    if not path:
        return None
    try:
        p = Path(path)
        if not p.exists():
            return None
        delta = __import__("datetime").datetime.now() - __import__("datetime").datetime.fromtimestamp(p.stat().st_mtime)
        return delta.days
    except Exception:
        return None


def summarize_data_health(ctx: dict[str, Any]) -> str:
    return (
        f"degraded mode: {'yes' if ctx.get('degraded_mode') else 'no'}, "
        f"data={ctx.get('data_mode', 'live')}, "
        f"reason={ctx.get('degraded_reason') or 'none'}, "
        f"fallback_depth={ctx.get('fallback_depth', 0)}, "
        f"latency={ctx.get('data_latency_ms', '(n/a)')}ms"
    )
