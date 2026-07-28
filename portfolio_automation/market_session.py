"""Shared NYSE trading-session helper (Reliability Program WS8 / Phase D2).

Single source of truth for calendar-aware trading-session logic. Before this
module existed, the only calendar-aware code in the repo was a **private**
``_NYSE_HOLIDAYS`` set inside ``resolution_due_probe.py``, hardcoded through
2027-12-24, used nowhere else — every other freshness check
(``daily_input_snapshot.py``, ``artifact_registry.py``, ``daily_run_status.py``)
reimplements a flat wall-clock window with zero calendar awareness. This
module promotes that private holiday set to a shared, importable helper so
future freshness/provenance work has one place to ask calendar questions.

Answers three questions:

  1. ``is_trading_day(d)``           -- is this date a NYSE trading day?
  2. ``latest_completed_session(ts)`` -- what is the most recent NYSE session
     that had already CLOSED as of this timestamp (not "today", if today's
     session hasn't opened/closed yet -- this is the fix for the pre-market
     conflation described in the WS8 audit: a 09:00 UTC run is ~04:00-05:00
     ET, well before the 09:30 ET open, so "today" has no completed session
     yet and the honest answer is still the PRIOR trading day)?
  3. ``sessions_between(start, end)`` -- how many NYSE sessions of new data
     lie between two points in time?

No network dependency, no new third-party package (repo convention: default
free / no new deps). The hardcoded holiday data is scoped EXACTLY to what
``resolution_due_probe.py`` already modeled (NYSE full-day closures only --
no early-close half-days) so moving it here changes nothing about existing
behaviour.

**Coverage horizon is explicit, not silent.** ``HOLIDAY_COVERAGE_THROUGH``
records the last date the hardcoded holiday list accounts for. Past that
date, ``is_trading_day``/``latest_completed_session``/``sessions_between``
still return an answer (weekday-only, no holiday awareness) rather than
raising, but every public function pairs its answer with a
``coverage_exceeded`` flag (via ``is_past_coverage_horizon`` or the
``session_provenance`` convenience wrapper) so a caller can detect and surface
degraded calendar precision instead of silently trusting a wrong answer.

DST caveat (read before using for intraday precision): this module does NOT
do timezone-aware DST conversion (no ``zoneinfo``/``tzdata`` dependency
assumed present on every platform this repo runs on, per the Windows-laptop +
Linux-VPS dual environment in CLAUDE.md). NYSE closes 16:00 ET, which is
21:00 UTC during EST and 20:00 UTC during EDT. ``_SESSION_CLOSE_UTC_HOUR``
uses the later (EST-equivalent) boundary deliberately -- the conservative
direction, so a session is never reported "completed" before it truly closed.
During EDT (roughly mid-March to early November) this can lag the true close
by up to one hour. The one production caller of this timestamp-level logic
(the daily cron) runs at 09:00 UTC, hours before ANY plausible close
boundary, so this simplification does not affect it. Do not reuse this
module for intraday session-boundary precision without revisiting that
assumption.

Observe-only: pure functions, no I/O, no clock calls (callers supply
timestamps), never raises.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

__all__ = [
    "NYSE_HOLIDAYS",
    "HOLIDAY_COVERAGE_THROUGH",
    "is_past_coverage_horizon",
    "is_trading_day",
    "previous_trading_day",
    "latest_completed_session",
    "sessions_between",
    "session_provenance",
]

# NYSE-closed dates (full-day closures only). Moved verbatim from the former
# ``resolution_due_probe._NYSE_HOLIDAYS`` -- same data, same source of truth,
# now shared.
NYSE_HOLIDAYS: frozenset[date] = frozenset({
    date(2025, 1, 1),    # New Year's Day
    date(2025, 1, 9),    # National Day of Mourning (Jimmy Carter)
    date(2025, 1, 20),   # MLK Jr Day
    date(2025, 2, 17),   # Presidents Day
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 26),   # Memorial Day
    date(2025, 6, 19),   # Juneteenth
    date(2025, 7, 4),    # Independence Day
    date(2025, 9, 1),    # Labor Day
    date(2025, 11, 27),  # Thanksgiving
    date(2025, 12, 25),  # Christmas
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Jr Day
    date(2026, 2, 16),   # Presidents Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth (Friday)
    date(2026, 7, 3),    # Independence Day observed (Jul 4 = Saturday)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
    date(2027, 1, 1),    # New Year's Day
    date(2027, 1, 18),   # MLK Jr Day
    date(2027, 2, 15),   # Presidents Day
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 18),   # Juneteenth observed (Jun 19 = Saturday)
    date(2027, 7, 5),    # Independence Day observed (Jul 4 = Sunday)
    date(2027, 9, 6),    # Labor Day
    date(2027, 11, 25),  # Thanksgiving
    date(2027, 12, 24),  # Christmas observed (Dec 25 = Saturday)
})

# Last date the hardcoded holiday data accounts for. Past this date,
# is_trading_day() degrades to weekday-only (no holiday awareness) -- this is
# the "landmine" the WS8 audit flagged (F3): make it an explicit, checkable
# constant instead of a silent cliff.
HOLIDAY_COVERAGE_THROUGH: date = date(2027, 12, 24)

# Conservative approximation of NYSE close (16:00 ET) in UTC. See module
# docstring's DST caveat.
_SESSION_CLOSE_UTC_HOUR = 21


def _to_utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)


def _coerce_date(d: "date | datetime") -> date:
    """Normalize a ``date`` OR ``datetime`` argument to a plain ``date``.

    ``datetime`` is a subclass of ``date`` in Python, so a caller passing a
    ``datetime`` into a ``d: date``-annotated function satisfies every static
    check and then crashes at runtime the moment the function compares *d*
    against a ``date`` constant (e.g. ``d > HOLIDAY_COVERAGE_THROUGH``). Every
    date-taking public function in this module routes through this coercion
    first so that failure mode cannot happen.

    **TZ policy (explicit, not implicit):** a timezone-AWARE datetime is
    converted to UTC before taking its calendar date (matching
    :func:`_to_utc`'s convention used elsewhere in this module for
    timestamp-taking functions). A timezone-NAIVE datetime is treated AS
    UTC (not local time) -- again matching :func:`_to_utc`. This means a
    naive and a UTC-aware datetime for "the same wall-clock instant" always
    agree on which calendar day they fall on. NOTE: the ``isinstance``
    check MUST test ``datetime`` before ``date`` -- since ``datetime`` is a
    ``date`` subclass, checking ``date`` first would make the ``datetime``
    branch unreachable.
    """
    if isinstance(d, datetime):
        return _to_utc(d).date()
    return d


def is_past_coverage_horizon(d: "date | datetime") -> bool:
    """True if *d* is beyond the hardcoded holiday data's coverage window.

    Accepts either a ``date`` or a ``datetime`` (see :func:`_coerce_date` for
    the datetime-to-date TZ policy).
    """
    return _coerce_date(d) > HOLIDAY_COVERAGE_THROUGH


def is_trading_day(d: "date | datetime") -> bool:
    """True if *d* is a NYSE trading day (Mon-Fri, not a known holiday).

    Accepts either a ``date`` or a ``datetime`` (see :func:`_coerce_date` for
    the datetime-to-date TZ policy). Past
    :data:`HOLIDAY_COVERAGE_THROUGH` this degrades to weekday-only (no
    holiday awareness) -- call :func:`is_past_coverage_horizon` to detect
    that condition explicitly rather than trusting the answer blindly.
    """
    dd = _coerce_date(d)
    return dd.weekday() < 5 and dd not in NYSE_HOLIDAYS


def previous_trading_day(d: "date | datetime") -> date:
    """The most recent NYSE trading day strictly before *d*.

    Accepts either a ``date`` or a ``datetime`` (see :func:`_coerce_date` for
    the datetime-to-date TZ policy). Always returns a plain ``date``.
    """
    cur = _coerce_date(d) - timedelta(days=1)
    while not is_trading_day(cur):
        cur -= timedelta(days=1)
    return cur


def latest_completed_session(ts: datetime) -> date:
    """The most recent NYSE trading day whose session had already closed as
    of *ts*.

    If *ts*'s own calendar date is a trading day AND *ts* is at/after the
    (conservative, UTC-approximated) close boundary, that date IS the latest
    completed session. Otherwise (pre-market, intraday, a weekend, or a
    holiday) this walks backward to the most recent PRIOR trading day -- this
    is the fix for the pre-market conflation in WS8-F2: a run at 09:00 UTC
    (~04:00-05:00 ET, pre-market) must not report "today" as a completed
    session just because today happens to be a weekday.
    """
    ts_utc = _to_utc(ts)
    d = ts_utc.date()
    if is_trading_day(d) and ts_utc.hour >= _SESSION_CLOSE_UTC_HOUR:
        return d
    return previous_trading_day(d)


def sessions_between(start: datetime, end: datetime) -> int:
    """Count of NYSE trading sessions strictly between the *completed*
    sessions represented by *start* and *end*.

    Both timestamps are first reduced via :func:`latest_completed_session` so
    this answers "how many sessions of new data could exist between these two
    points in time" -- e.g. an artifact captured Friday pre-market vs. one
    captured the following Monday pre-market spans zero NEW completed
    sessions (both still represent the prior Thursday close), while
    Friday-post-close vs. Monday-post-close spans exactly one (Friday's own
    close only becomes "new" once Monday's session also closes... concretely
    this returns the count of trading days strictly after the start session
    up to and including the end session). Returns 0 if end's completed
    session is not strictly after start's.
    """
    s = latest_completed_session(start)
    e = latest_completed_session(end)
    if e <= s:
        return 0
    count = 0
    cur = s
    while cur < e:
        cur += timedelta(days=1)
        if is_trading_day(cur):
            count += 1
    return count


def session_provenance(as_of: datetime) -> dict[str, Any]:
    """Convenience for producers stamping provenance fields on an artifact.

    Returns a small, additive dict:

    - ``latest_session_represented``: ISO date (``YYYY-MM-DD``) of the most
      recent NYSE session completed as of *as_of* -- the human/date label for
      "which close does this data reflect."
    - ``source_data_through``: ISO datetime marking the upper bound of data
      currency -- the (conservative, UTC-approximated) close instant of
      ``latest_session_represented``. Underlying source data cannot be
      assumed to reflect anything after this instant.
    - ``coverage_exceeded``: True if *as_of* (or the derived session date) is
      past :data:`HOLIDAY_COVERAGE_THROUGH`, meaning holiday awareness has
      silently degraded to weekday-only for this query.

    Never raises; pure function of its input.
    """
    ts_utc = _to_utc(as_of)
    session = latest_completed_session(ts_utc)
    close_instant = datetime(
        session.year, session.month, session.day, _SESSION_CLOSE_UTC_HOUR,
        tzinfo=timezone.utc,
    )
    return {
        "latest_session_represented": session.isoformat(),
        "source_data_through": close_instant.isoformat(),
        "coverage_exceeded": is_past_coverage_horizon(ts_utc.date()) or is_past_coverage_horizon(session),
    }
