"""
Resolve named backtest windows into (start_date, end_date) ranges.

Supports trailing windows AND intra-year calendar periods so the operator can see
performance "over different periods of the year" (seasonality), not just trailing
multi-year returns. All ranges are clamped to the available price calendar.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Window:
    key: str          # e.g. "trailing_1y", "ytd", "2025-Q3", "2026-03"
    label: str
    start: str        # YYYY-MM-DD (inclusive, snapped to first calendar date ≥ start)
    end: str          # YYYY-MM-DD (inclusive, snapped to last calendar date ≤ end)
    years: float      # span in years (for CAGR)


def _span_years(start: str, end: str) -> float:
    from datetime import date
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    return max((e - s).days / 365.25, 1e-9)


def _clamp(dates: list[str], start: str, end: str) -> tuple[str, str] | None:
    """First date ≥ start and last date ≤ end within the calendar."""
    lo = next((d for d in dates if d >= start), None)
    hi = next((d for d in reversed(dates) if d <= end), None)
    if lo is None or hi is None or lo > hi:
        return None
    return lo, hi


def resolve_windows(names: list[str], dates: list[str]) -> list[Window]:
    """
    Resolve window names against the sorted (oldest-first) calendar *dates*.

    Recognized names: `ytd`, `trailing_1y|3y|5y`, `calendar_quarter` (most recent
    complete-ish quarter to date), `calendar_month` (current month to date), or an
    explicit `YYYY` / `YYYY-Qn` / `YYYY-MM`. Unknown / empty-range names skipped.
    """
    if not dates:
        return []
    last = dates[-1]
    last_y, last_m = int(last[:4]), int(last[5:7])
    out: list[Window] = []

    def add(key: str, label: str, start: str, end: str):
        rng = _clamp(dates, start, end)
        if rng:
            out.append(Window(key, label, rng[0], rng[1], _span_years(rng[0], rng[1])))

    for name in names:
        n = name.lower()
        if n == "ytd":
            add("ytd", f"YTD {last_y}", f"{last_y}-01-01", last)
        elif n in ("trailing_1y", "trailing_3y", "trailing_5y"):
            yrs = int(n.split("_")[1].rstrip("y"))
            start_y = last_y - yrs
            add(n, f"Trailing {yrs}y", f"{start_y}-{last[5:]}", last)
        elif n == "calendar_month":
            add(f"{last_y}-{last_m:02d}", f"{last_y}-{last_m:02d}",
                f"{last_y}-{last_m:02d}-01", last)
        elif n == "calendar_quarter":
            q = (last_m - 1) // 3 + 1
            qstart_m = (q - 1) * 3 + 1
            add(f"{last_y}-Q{q}", f"{last_y} Q{q}", f"{last_y}-{qstart_m:02d}-01", last)
        elif len(n) == 4 and n.isdigit():
            add(n, n, f"{n}-01-01", f"{n}-12-31")
        elif len(n) == 7 and n[4] == "-" and n[5] == "q":  # YYYY-Qn
            y, q = int(n[:4]), int(n[6])
            qs = (q - 1) * 3 + 1
            qe = qs + 2
            add(name, f"{y} Q{q}", f"{y}-{qs:02d}-01", f"{y}-{qe:02d}-31")
        elif len(n) == 7 and n[4] == "-":  # YYYY-MM
            add(name, name, f"{name}-01", f"{name}-31")
        # else: unknown → skip
    return out
