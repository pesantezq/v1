"""Exchange calendar → exact expected 5-minute session grids. Research-only.

Built on the repo-native `portfolio_automation.market_session` holiday data
rather than a new dependency (`exchange_calendars` / `pandas_market_calendars`
are not installed, and adding a market-data dependency is an operator decision).

TWO GAPS IN THE UNDERLYING DATA, both handled by failing closed:

1. `market_session` carries NO early-close data — its own docstring says
   "no early-close half-days". Session 1 proved early closes are real in the
   provider feed (2025-11-28 returned 42 bars ending 12:55), so this module
   adds an explicit early-close table.

2. `NYSE_HOLIDAYS` spans 2025-01-01 … 2027-12-24 ONLY. Five-minute bars are
   available back to 2017, but a session whose holiday status cannot be
   verified must never be admitted — expecting 78 bars on a day that was
   actually a holiday is exactly the silent corruption this lab exists to
   prevent. Outside the certified window `resolve_session` returns
   ``UNCERTIFIED`` and the dataset builder refuses it.

Both failure directions are safe. A missed early close yields expected=78 vs
observed=42 → rejected. A wrongly-declared early close yields expected=42 vs
observed=78 → surplus → rejected by the Session 1 hardening. Neither can admit
a corrupt session.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterator
from zoneinfo import ZoneInfo

from portfolio_automation.market_session import (
    NYSE_HOLIDAYS, HOLIDAY_COVERAGE_THROUGH, is_trading_day,
)
from portfolio_automation.intraday_lab.validation import (
    SESSION_REGULAR, SESSION_EARLY_CLOSE, SESSION_MARKET_CLOSED,
)

SCHEMA_VERSION = "1"
CALENDAR_SOURCE = "portfolio_automation.market_session + intraday_lab early-close table"
EXCHANGE = "XNYS"
EXCHANGE_TZ = ZoneInfo("America/New_York")

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE_TIME = time(13, 0)

# A session outside the holiday data's span cannot be certified.
SESSION_UNCERTIFIED = "UNCERTIFIED"

# Earliest date the repo's holiday data covers. Derived, not hardcoded, so it
# tracks the underlying table automatically if it is ever extended backwards.
HOLIDAY_COVERAGE_FROM: date = min(NYSE_HOLIDAYS)

# NYSE 13:00 ET early closes within the certified window. Deliberately limited
# to dates inside the holiday-coverage span — an early-close entry outside it
# would imply a certainty the holiday data itself does not provide.
EARLY_CLOSES: frozenset[date] = frozenset({
    date(2025, 7, 3),      # July 4 falls Friday
    date(2025, 11, 28),    # day after Thanksgiving — PROVEN by the Session 1 probe
    date(2025, 12, 24),    # Christmas Eve
    date(2026, 11, 27),    # day after Thanksgiving
    date(2026, 12, 24),    # Christmas Eve
    date(2027, 11, 26),    # day after Thanksgiving
})


class CalendarCoverageError(ValueError):
    """The requested date lies outside the certified calendar window."""


@dataclass(frozen=True)
class TradingSession:
    market_date: date
    exchange: str
    session_type: str
    market_open: datetime | None
    market_close: datetime | None
    expected_bar_starts: tuple[datetime, ...]
    timezone: str
    calendar_source: str
    certified: bool

    @property
    def expected_bar_count(self) -> int:
        return len(self.expected_bar_starts)

    def to_dict(self) -> dict:
        return {
            "market_date": self.market_date.isoformat(),
            "exchange": self.exchange,
            "session_type": self.session_type,
            "market_open": self.market_open.isoformat() if self.market_open else None,
            "market_close": self.market_close.isoformat() if self.market_close else None,
            "expected_bar_count": self.expected_bar_count,
            "timezone": self.timezone,
            "calendar_source": self.calendar_source,
            "certified": self.certified,
        }


def is_certified(d: date) -> bool:
    """True when the holiday data actually covers this date."""
    return HOLIDAY_COVERAGE_FROM <= d <= HOLIDAY_COVERAGE_THROUGH


def _grid(open_et: datetime, close_et: datetime, minutes: int) -> tuple[datetime, ...]:
    """Bar STARTS from open to close, exclusive of the close.

    A 16:00 close yields a final start of 15:55; a 13:00 close yields 12:55 —
    matching the BAR_OPEN semantics proven in Session 1. Computed from the
    calendar's open/close, never from hardcoded special dates.
    """
    step = timedelta(minutes=minutes)
    out, cursor = [], open_et
    while cursor + step <= close_et:
        out.append(cursor.astimezone(timezone.utc))
        cursor += step
    return tuple(out)


def resolve_session(d: date, *, timeframe_minutes: int = 5) -> TradingSession:
    """Resolve one market date to its session type and exact expected grid."""
    def _session(stype: str, open_t: time | None, close_t: time | None,
                 certified: bool) -> TradingSession:
        if open_t is None or close_t is None:
            o = c = None
            grid: tuple[datetime, ...] = ()
        else:
            o = datetime.combine(d, open_t, tzinfo=EXCHANGE_TZ)
            c = datetime.combine(d, close_t, tzinfo=EXCHANGE_TZ)
            grid = _grid(o, c, timeframe_minutes)
        return TradingSession(
            market_date=d, exchange=EXCHANGE, session_type=stype,
            market_open=o.astimezone(timezone.utc) if o else None,
            market_close=c.astimezone(timezone.utc) if c else None,
            expected_bar_starts=grid, timezone=str(EXCHANGE_TZ),
            calendar_source=CALENDAR_SOURCE, certified=certified)

    if not is_certified(d):
        # Weekends are unambiguous even without holiday data; everything else
        # outside the window is genuinely unknown and must not be admitted.
        if d.weekday() >= 5:
            return _session(SESSION_MARKET_CLOSED, None, None, False)
        return _session(SESSION_UNCERTIFIED, None, None, False)

    if not is_trading_day(d):
        return _session(SESSION_MARKET_CLOSED, None, None, True)
    if d in EARLY_CLOSES:
        return _session(SESSION_EARLY_CLOSE, REGULAR_OPEN, EARLY_CLOSE_TIME, True)
    return _session(SESSION_REGULAR, REGULAR_OPEN, REGULAR_CLOSE, True)


def sessions_in_range(start: date, end: date, *,
                      timeframe_minutes: int = 5) -> Iterator[TradingSession]:
    d = start
    while d <= end:
        yield resolve_session(d, timeframe_minutes=timeframe_minutes)
        d += timedelta(days=1)


def calendar_provenance() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "exchange": EXCHANGE,
        "timezone": str(EXCHANGE_TZ),
        "source": CALENDAR_SOURCE,
        "holiday_coverage_from": HOLIDAY_COVERAGE_FROM.isoformat(),
        "holiday_coverage_through": HOLIDAY_COVERAGE_THROUGH.isoformat(),
        "holiday_count": len(NYSE_HOLIDAYS),
        "early_close_count": len(EARLY_CLOSES),
        "early_close_time_et": EARLY_CLOSE_TIME.isoformat(),
        "limitation": (
            "Holiday data spans 2025-2027 only. Five-minute bars exist back to "
            "2017, but sessions before the coverage window are UNCERTIFIED and "
            "are refused by the dataset builder — an unverifiable expectation "
            "must never admit a session."
        ),
    }
