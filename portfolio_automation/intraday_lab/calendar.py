"""Exchange calendar → exact expected 5-minute session grids. Research-only.

AUTHORITATIVE SOURCE
====================

Sessions come from `exchange_calendars` (Apache-2.0), the maintained successor
to Quantopian's `trading_calendars`, using its XNYS calendar. It was chosen over
`pandas_market_calendars` because the latter DEPENDS ON this package — it is a
strict superset, so `exchange_calendars` is the smaller maintained dependency
that satisfies the contract.

It replaces a hand-maintained holiday table that spanned 2025-2027 only, which
made every session before 2025 UNCERTIFIED and therefore unusable for research.
Hand-maintaining nine years of holidays and early closes would be a standing
correctness liability; the observed schedule (2017-2026: all opens 09:30 ET,
closes only 16:00 or 13:00, 20 early closes) matches this module's REGULAR /
EARLY_CLOSE model exactly.

Session TYPE is derived from the calendar's actual close time, never from a
hardcoded date list. An early close is simply a session closing before 16:00 ET,
so a newly announced half-day is handled by upgrading the calendar data rather
than by editing this file.

FAIL CLOSED, IN BOTH DIRECTIONS
===============================

If `exchange_calendars` is unavailable the module falls back to the repo-native
`portfolio_automation.market_session` table and its narrow window. That is NOT
silent: the backend in force is part of the CALENDAR IDENTITY, so a dataset built
without the authoritative calendar is a different research object, not a
lookalike. Outside the certified window `resolve_session` returns UNCERTIFIED and
the dataset builder refuses the session — an unverifiable expectation must never
admit data.

Both error directions remain safe. A missed early close yields expected=78 vs
observed=42 → rejected. A wrongly-declared early close yields expected=42 vs
observed=78 → surplus → rejected. Neither can admit a corrupt session.

CALENDAR IDENTITY = SCHEDULE MEANING, NOT PACKAGE VERSION
=========================================================

`calendar_identity()` hashes a digest of the ACTUAL certified schedule (every
session date with its open and close), plus the exchange, timezone, backend and
semantics version. The dependency's version string is deliberately NOT in the
identity, and is disclosed separately instead:

* an upgrade that changes a historical session CHANGES the digest, and therefore
  mints a new calendar era — research meaning can never be rewritten silently;
* an upgrade that changes nothing leaves the digest identical, so archived
  research does not churn through spurious eras for a no-op bump.

Version-in-identity would get the second case wrong, and would make every
archived manifest un-remintable after any routine dependency update.
"""
from __future__ import annotations

import hashlib
import json
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

SCHEMA_VERSION = "2"

# Bumped when the MEANING of a session changes (grid derivation, session typing,
# open/close semantics) — independently of the calendar data itself.
CALENDAR_SEMANTICS_VERSION = "intraday_calendar_semantics_v2"

EXCHANGE = "XNYS"
EXCHANGE_TZ = ZoneInfo("America/New_York")

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE_TIME = time(13, 0)

SESSION_UNCERTIFIED = "UNCERTIFIED"

BACKEND_EXCHANGE_CALENDARS = "exchange_calendars:XNYS"
BACKEND_REPO_NATIVE = "portfolio_automation.market_session + intraday_lab early-close table"

try:                                            # authoritative source
    import exchange_calendars as _xcals
    _XCALS_VERSION = getattr(_xcals, "__version__", "unknown")
except Exception:                               # pragma: no cover - env dependent
    _xcals = None
    _XCALS_VERSION = None

# The certified research window. FIXED constants, deliberately NOT derived from
# the library's own bounds: `exchange_calendars` advances `last_session` with the
# wall clock (today + 1 year), so deriving from it would silently mint a new
# calendar identity — and therefore new research meaning — every single day.
# Widening this window is an explicit, reviewable edit that mints a new era.
CERTIFIED_FROM: date = date(2017, 1, 1)
CERTIFIED_THROUGH: date = date(2027, 6, 30)

# Repo-native fallback data, retained for the no-dependency path.
HOLIDAY_COVERAGE_FROM: date = min(NYSE_HOLIDAYS)
EARLY_CLOSES: frozenset[date] = frozenset({
    date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24),
    date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26),
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


def _calendar():
    """The XNYS calendar object, or None when the dependency is absent."""
    if _xcals is None:
        return None
    global _CAL
    try:
        return _CAL
    except NameError:
        pass
    try:
        _CAL = _xcals.get_calendar("XNYS")
    except Exception:                           # pragma: no cover - env dependent
        _CAL = None
    return _CAL


def backend() -> str:
    return BACKEND_EXCHANGE_CALENDARS if _calendar() is not None else BACKEND_REPO_NATIVE


CALENDAR_SOURCE = BACKEND_EXCHANGE_CALENDARS if _xcals is not None else BACKEND_REPO_NATIVE


def coverage() -> tuple[date, date]:
    """The window this build can actually certify."""
    cal = _calendar()
    if cal is None:
        return HOLIDAY_COVERAGE_FROM, HOLIDAY_COVERAGE_THROUGH
    return CERTIFIED_FROM, CERTIFIED_THROUGH


def is_certified(d: date) -> bool:
    lo, hi = coverage()
    return lo <= d <= hi


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


def _session(d: date, stype: str, open_dt: datetime | None, close_dt: datetime | None,
             certified: bool, minutes: int) -> TradingSession:
    grid = _grid(open_dt, close_dt, minutes) if open_dt and close_dt else ()
    return TradingSession(
        market_date=d, exchange=EXCHANGE, session_type=stype,
        market_open=open_dt.astimezone(timezone.utc) if open_dt else None,
        market_close=close_dt.astimezone(timezone.utc) if close_dt else None,
        expected_bar_starts=grid, timezone=str(EXCHANGE_TZ),
        calendar_source=backend(), certified=certified)


def _resolve_authoritative(d: date, minutes: int) -> TradingSession:
    import pandas as pd

    cal = _calendar()
    ts = pd.Timestamp(d)
    if not cal.is_session(ts):
        return _session(d, SESSION_MARKET_CLOSED, None, None, True, minutes)
    open_dt = cal.session_open(ts).to_pydatetime()
    close_dt = cal.session_close(ts).to_pydatetime()
    # Session TYPE follows the actual close, so an early close needs no table.
    close_et = close_dt.astimezone(EXCHANGE_TZ).time()
    stype = SESSION_REGULAR if close_et >= REGULAR_CLOSE else SESSION_EARLY_CLOSE
    return _session(d, stype, open_dt, close_dt, True, minutes)


def _resolve_repo_native(d: date, minutes: int) -> TradingSession:
    def mk(stype, open_t, close_t, certified):
        o = datetime.combine(d, open_t, tzinfo=EXCHANGE_TZ) if open_t else None
        c = datetime.combine(d, close_t, tzinfo=EXCHANGE_TZ) if close_t else None
        return _session(d, stype, o, c, certified, minutes)

    if not is_certified(d):
        if d.weekday() >= 5:
            return mk(SESSION_MARKET_CLOSED, None, None, False)
        return mk(SESSION_UNCERTIFIED, None, None, False)
    if not is_trading_day(d):
        return mk(SESSION_MARKET_CLOSED, None, None, True)
    if d in EARLY_CLOSES:
        return mk(SESSION_EARLY_CLOSE, REGULAR_OPEN, EARLY_CLOSE_TIME, True)
    return mk(SESSION_REGULAR, REGULAR_OPEN, REGULAR_CLOSE, True)


def resolve_session(d: date, *, timeframe_minutes: int = 5) -> TradingSession:
    """Resolve one market date to its session type and exact expected grid."""
    if _calendar() is not None:
        if not is_certified(d):
            # Weekends are unambiguous even outside the window; a weekday is
            # genuinely unknown there and must not be admitted.
            if d.weekday() >= 5:
                return _session(d, SESSION_MARKET_CLOSED, None, None, False,
                                timeframe_minutes)
            return _session(d, SESSION_UNCERTIFIED, None, None, False,
                            timeframe_minutes)
        return _resolve_authoritative(d, timeframe_minutes)
    return _resolve_repo_native(d, timeframe_minutes)


def sessions_in_range(start: date, end: date, *,
                      timeframe_minutes: int = 5) -> Iterator[TradingSession]:
    d = start
    while d <= end:
        yield resolve_session(d, timeframe_minutes=timeframe_minutes)
        d += timedelta(days=1)


def trading_sessions(start: date, end: date) -> list[date]:
    """Certified trading dates in range. Empty outside the certified window."""
    return [s.market_date for s in sessions_in_range(start, end)
            if s.session_type in (SESSION_REGULAR, SESSION_EARLY_CLOSE)]


# ── Calendar identity ──────────────────────────────────────────────────────
_DIGEST_CACHE: dict[str, dict] = {}


def _schedule_digest() -> dict:
    """Hash the certified schedule itself: every session with its open + close.

    This — not the package version — is what research meaning depends on. Cached
    because the window spans thousands of sessions and the identity is consulted
    on every pipeline run.
    """
    key = f"{backend()}|{CERTIFIED_FROM}|{CERTIFIED_THROUGH}"
    if key in _DIGEST_CACHE:
        return _DIGEST_CACHE[key]

    lo, hi = coverage()
    rows, early = [], 0
    cal = _calendar()
    if cal is not None:
        import pandas as pd

        sch = cal.schedule.loc[str(lo):str(hi)]
        for idx, row in zip(sch.index, sch.itertuples()):
            o = row.open.tz_convert(EXCHANGE_TZ)
            c = row.close.tz_convert(EXCHANGE_TZ)
            rows.append([idx.date().isoformat(), o.strftime("%H:%M"), c.strftime("%H:%M")])
            if c.hour < REGULAR_CLOSE.hour:
                early += 1
    else:
        for s in sessions_in_range(lo, hi):
            if s.session_type not in (SESSION_REGULAR, SESSION_EARLY_CLOSE):
                continue
            o = s.market_open.astimezone(EXCHANGE_TZ)
            c = s.market_close.astimezone(EXCHANGE_TZ)
            rows.append([s.market_date.isoformat(), o.strftime("%H:%M"), c.strftime("%H:%M")])
            if c.hour < REGULAR_CLOSE.hour:
                early += 1

    digest = hashlib.sha256(
        json.dumps(rows, separators=(",", ":")).encode()).hexdigest()[:32]
    out = {"schedule_digest": digest, "session_count": len(rows),
           "early_close_count": early}
    _DIGEST_CACHE[key] = out
    return out


def calendar_identity() -> dict:
    """Deterministic calendar MEANING. Hashed into every dataset manifest."""
    lo, hi = coverage()
    d = _schedule_digest()
    return {
        "exchange": EXCHANGE,
        "timezone": str(EXCHANGE_TZ),
        "backend": backend(),
        "semantics_version": CALENDAR_SEMANTICS_VERSION,
        "coverage_from": lo.isoformat(),
        "coverage_through": hi.isoformat(),
        "session_count": d["session_count"],
        "early_close_count": d["early_close_count"],
        "schedule_digest": d["schedule_digest"],
        "regular_open_et": REGULAR_OPEN.isoformat(),
        "regular_close_et": REGULAR_CLOSE.isoformat(),
    }


def certified_schedule_rows() -> list[dict]:
    """The full certified schedule, one row per session.

    `session_type` is DERIVED from the close time, exactly as `resolve_session`
    derives it, so the archived rows carry no independent claim that could drift
    away from the open/close they sit beside.
    """
    lo, hi = coverage()
    out = []
    cal = _calendar()
    if cal is not None:
        sch = cal.schedule.loc[str(lo):str(hi)]
        for idx, row in zip(sch.index, sch.itertuples()):
            o = row.open.tz_convert(EXCHANGE_TZ)
            c = row.close.tz_convert(EXCHANGE_TZ)
            out.append({"market_date": idx.date().isoformat(),
                        "open_et": o.strftime("%H:%M"), "close_et": c.strftime("%H:%M"),
                        "session_type": (SESSION_REGULAR if c.hour >= REGULAR_CLOSE.hour
                                         else SESSION_EARLY_CLOSE)})
    else:
        for s in sessions_in_range(lo, hi):
            if s.session_type not in (SESSION_REGULAR, SESSION_EARLY_CLOSE):
                continue
            o = s.market_open.astimezone(EXCHANGE_TZ)
            c = s.market_close.astimezone(EXCHANGE_TZ)
            out.append({"market_date": s.market_date.isoformat(),
                        "open_et": o.strftime("%H:%M"), "close_et": c.strftime("%H:%M"),
                        "session_type": s.session_type})
    return out


def _digest_of_rows(rows: list[dict]) -> str:
    """The digest projection: exactly the triples `_schedule_digest` hashes."""
    payload = [[r["market_date"], r["open_et"], r["close_et"]] for r in rows]
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":")).encode()).hexdigest()[:32]


def persist_certified_schedule(*, root: str = ".") -> str:
    """Archive the certified schedule as an immutable content object.

    A digest detects that a calendar changed; it cannot tell you what the old
    calendar SAID. Research meaning depends on the schedule itself, so the
    schedule is archived under the digest that already appears in every dataset
    manifest's calendar identity — no new identity is introduced, and existing
    manifests therefore already point at this object.
    """
    from portfolio_automation.intraday_lab import storage as _st

    rows = certified_schedule_rows()
    digest = _digest_of_rows(rows)
    lo, hi = coverage()
    _st.write_snapshot(_st.CALENDARS, digest, {
        "schedule.json": rows,
        "calendar_manifest.json": {
            "schema_version": SCHEMA_VERSION,
            "identity_schema": CALENDAR_SEMANTICS_VERSION,
            "schedule_digest": digest,
            "exchange": EXCHANGE,
            "timezone": str(EXCHANGE_TZ),
            "backend": backend(),
            "coverage_from": lo.isoformat(),
            "coverage_through": hi.isoformat(),
            "session_count": len(rows),
            "early_close_count": sum(1 for r in rows
                                     if r["session_type"] == SESSION_EARLY_CLOSE),
            # Disclosure, not identity — see the module docstring.
            "implementation_version": _XCALS_VERSION,
        },
    }, root=root)
    return digest


def verify_certified_schedule(digest: str, *, root: str = ".") -> dict:
    """Recompute the digest from the ARCHIVED rows, never from the live calendar."""
    from portfolio_automation.intraday_lab import storage as _st

    rows = _st.read_snapshot(_st.CALENDARS, digest, "schedule.json", root=root)
    man = _st.read_snapshot(_st.CALENDARS, digest, "calendar_manifest.json", root=root)
    if rows is None or man is None:
        return {"verified": False, "reason": "missing schedule.json or calendar_manifest"}
    recomputed = _digest_of_rows(rows)
    if recomputed != digest or man.get("schedule_digest") != digest:
        return {"verified": False, "recomputed": recomputed, "identity": digest,
                "reason": "archived schedule does not hash to its identity"}
    bad = [r["market_date"] for r in rows
           if r["session_type"] != (SESSION_REGULAR if r["close_et"] >= "16:00"
                                    else SESSION_EARLY_CLOSE)]
    if bad:
        return {"verified": False, "identity": digest,
                "reason": f"session_type disagrees with close time on {bad[:3]}"}
    return {"verified": True, "identity": digest, "reason": None,
            "session_count": len(rows),
            "coverage_from": man.get("coverage_from"),
            "coverage_through": man.get("coverage_through"),
            "backend": man.get("backend")}


def calendar_provenance() -> dict:
    """Identity plus disclosures that are NOT part of research meaning."""
    lo, hi = coverage()
    authoritative = _calendar() is not None
    return {
        "schema_version": SCHEMA_VERSION,
        **calendar_identity(),
        # Disclosure only. Deliberately outside the identity: a dependency bump
        # that changes no session must not mint a new research era, and one that
        # does change a session is already caught by schedule_digest.
        "implementation_version": _XCALS_VERSION,
        "authoritative": authoritative,
        "limitation": None if authoritative else (
            f"exchange_calendars is not installed; falling back to the "
            f"repo-native holiday table covering {lo} to {hi} only. Sessions "
            f"outside that window are UNCERTIFIED and are refused by the "
            f"dataset builder."
        ),
    }
