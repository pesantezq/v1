"""
Direction-aware outcome resolution for the POC backtest  (additive | advisory-only | observe-only)

Pattern-Improvement Loop — Step 1b. The base FMPBacktester counts any positive
forward return as a "win" — wrong for bearish signals (e.g. STRONG_MOVE_DOWN,
where a *down* move is the correct call). This module resolves each signal's
intended direction and labels win/loss relative to that direction.

Observe-only: pure functions plus an additive harness breakdown. No protected
scoring/decision/allocation logic is touched, and the existing long-only metrics
are left exactly as the base backtester produced them.
"""

from __future__ import annotations

from typing import Any

_DOWN_TAGS = ("DOWN", "BEAR", "SHORT")
_UP_TAGS = ("UP", "BULL", "LONG")


def signal_direction(signal: dict) -> str:
    """Return 'up' | 'down' | 'neutral' for a signal.

    Precedence: an explicit ``direction`` field wins; otherwise the direction is
    inferred from the representative ``pattern`` and then the ``patterns`` list
    (e.g. STRONG_MOVE_DOWN -> 'down'). Non-directional or missing patterns default
    to 'up' (legacy long-only behavior).
    """
    explicit = str(signal.get("direction") or "").strip().lower()
    if explicit in {"up", "down", "neutral"}:
        return explicit

    labels = [str(signal.get("pattern") or "")]
    extra = signal.get("patterns")
    if isinstance(extra, (list, tuple)):
        labels.extend(str(x) for x in extra)
    blob = " ".join(labels).upper()

    if any(tag in blob for tag in _DOWN_TAGS):
        return "down"
    if any(tag in blob for tag in _UP_TAGS):
        return "up"
    return "up"  # legacy long-only default


def directional_outcome(forward_return_pct: float | None, direction: str) -> str:
    """Return 'win' | 'loss' | 'unknown' for a forward return given the signal's
    direction. 'unknown' when the return is missing; a 'down' thesis wins on a
    negative move; 'up'/'neutral'/unknown directions use long-only semantics
    (positive move wins)."""
    if forward_return_pct is None:
        return "unknown"
    dir_norm = str(direction or "").strip().lower()
    if dir_norm == "down":
        return "win" if forward_return_pct < 0 else "loss"
    # 'up', 'neutral', '' and anything unrecognized → long-only
    return "win" if forward_return_pct > 0 else "loss"


def directional_breakdown(results: list[dict], signals: list[dict], forward_days: int) -> dict[str, Any]:
    """Re-label each evaluated result by its signal's intended direction and
    summarize. Returns an aggregate directional hit rate plus a per-direction
    sub-breakdown. Additive: the input ``results`` are not mutated.

    Results are matched to signals by (ticker, date) — the same key convention the
    harness already uses for its per-pattern breakdown.
    """
    dir_by_key = {
        (str(s.get("ticker", "")).upper(), str(s.get("scan_time", ""))[:10]): signal_direction(s)
        for s in signals
    }
    ret_key = f"return_{forward_days}d"
    by_dir: dict[str, list[str]] = {}
    total_wins = 0
    total_eval = 0
    for r in results:
        ret = r.get(ret_key)
        if ret is None:
            continue
        key = (str(r.get("ticker", "")).upper(), str(r.get("signal_date", ""))[:10])
        direction = dir_by_key.get(key, "up")
        outcome = directional_outcome(ret, direction)
        if outcome == "unknown":
            continue
        by_dir.setdefault(direction, []).append(outcome)
        total_eval += 1
        if outcome == "win":
            total_wins += 1

    rows = []
    for direction, outcomes in sorted(by_dir.items()):
        wins = sum(1 for o in outcomes if o == "win")
        rows.append({
            "direction": direction,
            "count": len(outcomes),
            "hit_rate": round(wins / len(outcomes) * 100.0, 2) if outcomes else 0.0,
        })

    return {
        "evaluated": total_eval,
        "hit_rate": round(total_wins / total_eval * 100.0, 2) if total_eval else 0.0,
        "by_direction": rows,
    }
