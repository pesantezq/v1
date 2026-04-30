"""
Outcome resolver for historical replay.

For each decision row, looks up forward prices in the historical price data
and resolves outcomes at the longest available window (7d → 3d → 1d).
Rows with insufficient forward data remain unresolved.

All data is already known at resolve time (offline replay), so this step
runs synchronously without any network calls.
"""
from __future__ import annotations

import bisect
import logging
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger("stockbot.portfolio_automation.historical_replay.outcome_resolver")

WAIT_CORRECT_THRESHOLD = 0.03
DEFAULT_WINDOW_DAYS = (1, 3, 7)

_WANT_UP = frozenset({"BUY", "SCALE"})
_WANT_DOWN = frozenset({"SELL", "AVOID"})


def _build_price_map(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Build {date_str: close} from normalized oldest-first price rows."""
    return {r["date"]: float(r["close"]) for r in rows if r.get("date") and r.get("close")}


def _is_direction_correct(
    decision: str,
    return_pct: float,
    wait_threshold: float = WAIT_CORRECT_THRESHOLD,
) -> bool | None:
    dec = decision.upper()
    if dec in _WANT_UP:
        return return_pct > 0
    if dec in _WANT_DOWN:
        return return_pct < 0
    if dec == "WAIT":
        return abs(return_pct) < wait_threshold
    return None  # HOLD and unknown are neutral


def _find_price_at_offset(
    price_map: dict[str, float],
    sorted_dates: list[str],
    decision_date: str,
    offset_days: int,
) -> tuple[str | None, float | None]:
    """
    Find the first trading date at or after (decision_date + offset_days).

    Uses binary search on the sorted date list for O(log n) lookup.
    Returns (date_str, price) or (None, None) when data is unavailable.
    """
    try:
        target = (date.fromisoformat(decision_date) + timedelta(days=offset_days)).isoformat()
    except ValueError:
        return None, None

    idx = bisect.bisect_left(sorted_dates, target)
    if idx >= len(sorted_dates):
        return None, None

    candidate = sorted_dates[idx]
    price = price_map.get(candidate)
    return (candidate, price) if price is not None else (None, None)


def resolve_outcomes(
    decision_rows: list[dict[str, Any]],
    price_data: dict[str, list[dict[str, Any]]],
    *,
    window_days: tuple[int, ...] = DEFAULT_WINDOW_DAYS,
    wait_threshold: float = WAIT_CORRECT_THRESHOLD,
) -> list[dict[str, Any]]:
    """
    Resolve outcomes for all decision rows using historical price data.

    Attempts windows from longest to shortest; settles on the first one
    that has a forward price available.  Rows with no usable forward
    price remain unresolved (resolved=False).

    Returns a new list — input rows are not mutated.
    """
    # Pre-build per-symbol price maps and sorted date lists
    price_maps: dict[str, dict[str, float]] = {}
    sorted_date_lists: dict[str, list[str]] = {}
    for sym, rows in price_data.items():
        pm = _build_price_map(rows)
        price_maps[sym] = pm
        sorted_date_lists[sym] = sorted(pm.keys())

    resolved_count = 0
    updated: list[dict[str, Any]] = []

    for row in decision_rows:
        row = dict(row)  # shallow copy to avoid mutating caller's data
        sym = str(row.get("symbol") or "").upper()
        pm = price_maps.get(sym)
        dl = sorted_date_lists.get(sym)

        if not pm or not dl:
            updated.append(row)
            continue

        decision_date = str(row.get("date") or "")
        entry_price_raw = row.get("price_at_decision")
        try:
            entry_price = float(entry_price_raw) if entry_price_raw is not None else None
        except (TypeError, ValueError):
            entry_price = None

        if not decision_date or not entry_price or entry_price <= 0:
            updated.append(row)
            continue

        # Try windows longest → shortest
        for w in sorted(window_days, reverse=True):
            exit_date, exit_price = _find_price_at_offset(pm, dl, decision_date, w)
            if exit_date is None or exit_price is None:
                continue

            return_pct = (exit_price - entry_price) / entry_price
            direction_correct = _is_direction_correct(
                str(row.get("decision") or ""), return_pct, wait_threshold
            )
            try:
                days_elapsed = (
                    date.fromisoformat(exit_date) - date.fromisoformat(decision_date)
                ).days
            except ValueError:
                days_elapsed = w

            row.update({
                "resolved": True,
                "resolved_at": exit_date,
                "days_elapsed": days_elapsed,
                "price_at_resolution": round(exit_price, 4),
                "return_pct": round(return_pct, 6),
                "direction_correct": direction_correct,
                "window_days": w,
                "outcome_price": round(exit_price, 4),
            })
            resolved_count += 1
            break

        updated.append(row)

    logger.info(
        "outcome_resolver: resolved %d / %d rows",
        resolved_count, len(decision_rows),
    )
    return updated
