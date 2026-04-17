from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from watchlist_scanner.cache_manager import CacheManager
from watchlist_scanner.state import WatchlistStateStore

logger = logging.getLogger("watchlist_scanner.outcomes")


def _load_next_available_close(
    cache: CacheManager,
    symbol: str,
    target_date: date,
    as_of_date: date,
) -> tuple[date, float] | None:
    """
    Return the first cached close on or after target_date and on or before as_of_date.

    This uses cached TIME_SERIES_DAILY payloads only so evaluation stays
    additive and separate from live scanner execution.
    """
    raw = cache.get_stale(f"daily_{symbol}")
    if not raw:
        return None

    ts = raw.get("Time Series (Daily)", {})
    if not ts:
        return None

    candidates: list[tuple[date, float]] = []
    for day_str, payload in ts.items():
        try:
            day = date.fromisoformat(day_str)
            if day < target_date or day > as_of_date:
                continue
            close = float(payload.get("4. close", 0) or 0)
            if close <= 0:
                continue
            candidates.append((day, close))
        except (TypeError, ValueError):
            continue

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    return candidates[0]


def _label_return(return_pct: float) -> str:
    """Simple first-pass outcome label for the first resolved checkpoint."""
    if return_pct >= 1.0:
        return "positive"
    if return_pct <= -1.0:
        return "negative"
    return "flat"


def evaluate_pending_alert_outcomes(
    db_path: str | Path = "data/portfolio.db",
    cache_dir: str | Path = "data/watchlist_cache",
    *,
    as_of: datetime | None = None,
    limit: int = 100,
    window_days: int = 1,
) -> dict[str, Any]:
    """
    Evaluate pending alert lifecycles using one next-available-close checkpoint.

    This first pass is intentionally narrow:
    - pending rows only
    - one evaluation window (`window_days`, default 1)
    - cached daily prices only
    - one resolved label: positive / flat / negative
    """
    now = as_of or datetime.now()
    as_of_date = now.date()
    store = WatchlistStateStore(db_path)
    cache = CacheManager(cache_dir=cache_dir)
    pending_rows = store.list_pending_alert_lifecycles(limit=limit)

    evaluated = 0
    not_due = 0
    missing_price = 0
    invalid_baseline = 0
    skipped = 0

    for row in pending_rows:
        outcome_id = int(row.get("id") or 0)
        ticker = str(row.get("ticker") or "").upper()
        surfaced_at_raw = row.get("surfaced_at")
        baseline_price = row.get("baseline_price")

        if not outcome_id or not ticker or not surfaced_at_raw:
            skipped += 1
            continue

        try:
            surfaced_at = datetime.fromisoformat(str(surfaced_at_raw))
        except (TypeError, ValueError):
            skipped += 1
            continue

        due_date = surfaced_at.date() + timedelta(days=window_days)
        if as_of_date < due_date:
            not_due += 1
            continue

        if baseline_price is None or float(baseline_price) <= 0:
            invalid_baseline += 1
            continue

        next_close = _load_next_available_close(cache, ticker, due_date, as_of_date)
        if next_close is None:
            missing_price += 1
            continue

        eval_date, evaluation_price = next_close
        return_pct = round(((evaluation_price - float(baseline_price)) / float(baseline_price)) * 100.0, 2)
        outcome_label = _label_return(return_pct)
        evaluated_at = datetime.combine(eval_date, datetime.min.time()).isoformat()

        resolved = store.resolve_alert_lifecycle(
            outcome_id,
            evaluation_price=evaluation_price,
            return_pct=return_pct,
            evaluated_at=evaluated_at,
            outcome_label=outcome_label,
            outcome_status=f"resolved_{window_days}d",
        )
        if resolved is not None:
            evaluated += 1
            logger.info(
                "Resolved alert lifecycle id=%s ticker=%s return=%+.2f%% label=%s",
                outcome_id,
                ticker,
                return_pct,
                outcome_label,
            )

    return {
        "evaluated": evaluated,
        "not_due": not_due,
        "missing_price": missing_price,
        "invalid_baseline": invalid_baseline,
        "skipped": skipped,
        "pending_remaining": len(store.list_pending_alert_lifecycles(limit=limit)),
        "window_days": window_days,
        "as_of": now.isoformat(),
    }
