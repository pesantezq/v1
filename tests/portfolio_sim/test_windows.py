"""Tests for the named-window resolver (different periods of the year)."""
from __future__ import annotations

from portfolio_automation.portfolio_sim.windows import resolve_windows

# A daily-ish calendar spanning 2024-01 .. 2026-06 (month-end sample is enough).
DATES = []
for y in (2024, 2025, 2026):
    for mo in range(1, 13):
        if y == 2026 and mo > 6:
            break
        DATES.append(f"{y}-{mo:02d}-15")
DATES.sort()


def _by_key(wins):
    return {w.key: w for w in wins}


def test_ytd_window():
    w = _by_key(resolve_windows(["ytd"], DATES))
    assert "ytd" in w
    assert w["ytd"].start.startswith("2026-01")
    assert w["ytd"].end == DATES[-1]


def test_trailing_windows():
    w = _by_key(resolve_windows(["trailing_1y", "trailing_3y"], DATES))
    assert w["trailing_1y"].start.startswith("2025-06")
    # trailing_3y clamps to earliest available (2024-01)
    assert w["trailing_3y"].start == DATES[0]
    assert w["trailing_1y"].years > 0


def test_calendar_quarter_and_month():
    w = _by_key(resolve_windows(["calendar_quarter", "calendar_month"], DATES))
    # last date is 2026-06-15 → Q2, month 2026-06
    assert "2026-Q2" in w
    assert w["2026-Q2"].start.startswith("2026-04")
    assert "2026-06" in w
    assert w["2026-06"].start == "2026-06-15"  # snapped to first cal date ≥ 2026-06-01


def test_explicit_year_and_quarter():
    w = _by_key(resolve_windows(["2025", "2025-Q3"], DATES))
    assert w["2025"].start.startswith("2025-01")
    assert w["2025"].end.startswith("2025-12")
    assert w["2025-Q3"].start.startswith("2025-07")


def test_unknown_and_empty_skipped():
    assert resolve_windows(["bogus"], DATES) == []
    assert resolve_windows(["ytd"], []) == []
