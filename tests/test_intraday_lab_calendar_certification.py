"""Historical XNYS calendar certification matrix. Research-only.

The Intraday Lab admits a session ONLY when the observed bar-start timestamps
equal the expected grid exactly. That makes the calendar the single most
load-bearing input in the data foundation: a wrong holiday silently expects 78
bars on a day that never traded, and a missed early close expects 78 where 42
exist. Both corrupt research rather than failing loudly.

So the calendar is certified here against known NYSE history — holidays, early
closes, unscheduled closures, DST transitions and year boundaries — across the
whole research window rather than the handful of dates that happened to be
convenient. Counts alone are never sufficient: the grid is asserted as an exact
timestamp set wherever the distinction matters.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from portfolio_automation.intraday_lab import calendar as C
from portfolio_automation.intraday_lab.validation import (
    SESSION_REGULAR, SESSION_EARLY_CLOSE, SESSION_MARKET_CLOSED,
)

UTC = timezone.utc
ET = C.EXCHANGE_TZ

authoritative = pytest.mark.skipif(
    C._calendar() is None,
    reason="exchange_calendars not installed; repo-native fallback covers 2025-2027 only")


# ═══════════════════════════════════════════════════════════════════════════
# Coverage
# ═══════════════════════════════════════════════════════════════════════════
@authoritative
def test_research_window_reaches_at_least_2017():
    lo, hi = C.coverage()
    assert lo <= date(2017, 1, 1)
    assert hi >= date(2026, 8, 1)
    assert C.resolve_session(date(2017, 1, 3)).certified is True


@authoritative
def test_certified_window_is_fixed_not_derived_from_the_library_bounds():
    """The library advances `last_session` with the wall clock. Deriving the
    window from it would mint a new calendar identity — and therefore new
    research meaning — every day."""
    cal = C._calendar()
    assert C.CERTIFIED_THROUGH < cal.last_session.date()
    assert isinstance(C.CERTIFIED_FROM, date) and isinstance(C.CERTIFIED_THROUGH, date)


@authoritative
def test_outside_the_window_fails_closed_rather_than_guessing():
    before = C.resolve_session(date(2016, 12, 30))     # a real trading day
    assert before.session_type == C.SESSION_UNCERTIFIED
    assert before.certified is False and before.expected_bar_count == 0


# ═══════════════════════════════════════════════════════════════════════════
# Normal sessions across years
# ═══════════════════════════════════════════════════════════════════════════
@authoritative
@pytest.mark.parametrize("d", [
    date(2017, 3, 15), date(2018, 6, 13), date(2019, 9, 11), date(2020, 10, 14),
    date(2021, 4, 14), date(2022, 8, 10), date(2023, 5, 17), date(2024, 2, 14),
    date(2025, 6, 11), date(2026, 8, 3),
])
def test_normal_session_is_exactly_78_bars_0930_to_1555(d):
    s = C.resolve_session(d)
    assert s.session_type == SESSION_REGULAR
    assert s.expected_bar_count == 78
    first, last = s.expected_bar_starts[0], s.expected_bar_starts[-1]
    assert first.astimezone(ET).strftime("%H:%M") == "09:30"
    assert last.astimezone(ET).strftime("%H:%M") == "15:55"
    # Exact grid, not just endpoints and a count.
    assert s.expected_bar_starts == tuple(
        first + timedelta(minutes=5 * i) for i in range(78))


# ═══════════════════════════════════════════════════════════════════════════
# Holidays across years
# ═══════════════════════════════════════════════════════════════════════════
@authoritative
@pytest.mark.parametrize("d,label", [
    (date(2017, 1, 2), "New Year's Day observed (Mon)"),
    (date(2017, 11, 23), "Thanksgiving"),
    (date(2018, 1, 15), "MLK Day"),
    (date(2018, 3, 30), "Good Friday"),
    (date(2019, 5, 27), "Memorial Day"),
    (date(2019, 7, 4), "Independence Day"),
    (date(2020, 9, 7), "Labor Day"),
    (date(2021, 12, 24), "Christmas Day observed (Fri)"),
    (date(2022, 6, 20), "Juneteenth observed — first year as a market holiday"),
    (date(2023, 2, 20), "Presidents' Day"),
    (date(2024, 3, 29), "Good Friday"),
    (date(2025, 1, 1), "New Year's Day"),
    (date(2025, 12, 25), "Christmas Day"),
])
def test_holiday_is_market_closed_with_an_empty_grid(d, label):
    s = C.resolve_session(d)
    assert s.session_type == SESSION_MARKET_CLOSED, label
    assert s.expected_bar_count == 0, label
    assert s.certified is True, label


@authoritative
@pytest.mark.parametrize("d,label", [
    (date(2018, 12, 5), "national day of mourning — George H. W. Bush"),
    (date(2025, 1, 9), "national day of mourning — Jimmy Carter"),
])
def test_unscheduled_closures_are_known(d, label):
    """The class of date a hand-maintained holiday table reliably misses: an
    unscheduled closure announced weeks ahead. Expecting 78 bars here would
    reject the session rather than corrupt it, but only because the grid check
    is exact — the calendar should simply know."""
    assert C.resolve_session(d).session_type == SESSION_MARKET_CLOSED, label


@authoritative
def test_juneteenth_was_a_trading_day_before_it_was_a_holiday():
    """A calendar must be right about WHEN a rule started, not just that it
    exists. Juneteenth first closed the NYSE in 2022."""
    assert C.resolve_session(date(2021, 6, 18)).session_type == SESSION_REGULAR
    assert C.resolve_session(date(2022, 6, 20)).session_type == SESSION_MARKET_CLOSED


# ═══════════════════════════════════════════════════════════════════════════
# Early closes across years
# ═══════════════════════════════════════════════════════════════════════════
@authoritative
@pytest.mark.parametrize("d,label", [
    (date(2017, 7, 3), "day before Independence Day"),
    (date(2017, 11, 24), "day after Thanksgiving"),
    (date(2018, 12, 24), "Christmas Eve"),
    (date(2019, 11, 29), "day after Thanksgiving"),
    (date(2020, 11, 27), "day after Thanksgiving"),
    (date(2021, 11, 26), "day after Thanksgiving"),
    (date(2022, 11, 25), "day after Thanksgiving"),
    (date(2023, 7, 3), "day before Independence Day"),
    (date(2024, 11, 29), "day after Thanksgiving"),
    (date(2025, 11, 28), "day after Thanksgiving — PROVEN by the Session 1 probe"),
])
def test_early_close_is_exactly_42_bars_ending_1255(d, label):
    s = C.resolve_session(d)
    assert s.session_type == SESSION_EARLY_CLOSE, label
    assert s.expected_bar_count == 42, label
    assert s.expected_bar_starts[0].astimezone(ET).strftime("%H:%M") == "09:30"
    assert s.expected_bar_starts[-1].astimezone(ET).strftime("%H:%M") == "12:55"
    assert s.market_close.astimezone(ET).strftime("%H:%M") == "13:00"


@authoritative
def test_early_close_is_derived_from_the_close_time_not_a_date_list():
    """No hardcoded early-close table is consulted under the authoritative
    backend, so a newly announced half-day arrives with the calendar data."""
    d = date(2024, 7, 3)
    s = C.resolve_session(d)
    assert s.session_type == SESSION_EARLY_CLOSE
    assert d not in C.EARLY_CLOSES          # absent from the fallback table
    assert s.calendar_source == C.BACKEND_EXCHANGE_CALENDARS


@authoritative
def test_the_day_after_an_early_close_is_a_full_session():
    """Guards against an early close leaking into neighbouring days."""
    assert C.resolve_session(date(2025, 12, 26)).expected_bar_count == 78
    assert C.resolve_session(date(2017, 7, 5)).expected_bar_count == 78


# ═══════════════════════════════════════════════════════════════════════════
# DST — local ET is invariant, the UTC offset is not
# ═══════════════════════════════════════════════════════════════════════════
@authoritative
@pytest.mark.parametrize("year,spring,fall", [
    (2017, date(2017, 3, 12), date(2017, 11, 5)),
    (2020, date(2020, 3, 8), date(2020, 11, 1)),
    (2023, date(2023, 3, 12), date(2023, 11, 5)),
    (2025, date(2025, 3, 9), date(2025, 11, 2)),
])
def test_sessions_around_dst_transitions_keep_0930_et(year, spring, fall):
    """The US equity session is 09:30-16:00 LOCAL. Only the UTC offset moves."""
    for transition, before_off, after_off in ((spring, 5, 4), (fall, 4, 5)):
        prev = _prev_session(transition)
        nxt = _next_session(transition)
        for d, offset in ((prev, before_off), (nxt, after_off)):
            s = C.resolve_session(d)
            first = s.expected_bar_starts[0]
            assert first.astimezone(ET).strftime("%H:%M") == "09:30", d
            assert first.hour == 9 + offset, (d, offset)   # 14:30Z EST / 13:30Z EDT
            assert s.expected_bar_count in (42, 78)


def _prev_session(d: date) -> date:
    c = d - timedelta(days=1)
    while C.resolve_session(c).session_type == SESSION_MARKET_CLOSED:
        c -= timedelta(days=1)
    return c


def _next_session(d: date) -> date:
    c = d + timedelta(days=1)
    while C.resolve_session(c).session_type == SESSION_MARKET_CLOSED:
        c += timedelta(days=1)
    return c


@authoritative
def test_no_session_grid_ever_spans_a_dst_boundary_incorrectly():
    """Every bar in a session must be exactly 5 minutes after the previous one
    in REAL elapsed time — a naive local-time grid would gain or lose an hour."""
    for d in (date(2017, 3, 13), date(2017, 11, 6), date(2025, 3, 10), date(2025, 11, 3)):
        starts = C.resolve_session(d).expected_bar_starts
        deltas = {starts[i + 1] - starts[i] for i in range(len(starts) - 1)}
        assert deltas == {timedelta(minutes=5)}, d


# ═══════════════════════════════════════════════════════════════════════════
# Leap years and year boundaries
# ═══════════════════════════════════════════════════════════════════════════
@authoritative
@pytest.mark.parametrize("d", [date(2020, 2, 28), date(2024, 2, 29)])
def test_leap_day_and_neighbours_resolve(d):
    s = C.resolve_session(d)
    assert s.session_type in (SESSION_REGULAR, SESSION_EARLY_CLOSE, SESSION_MARKET_CLOSED)
    if s.session_type == SESSION_REGULAR:
        assert s.expected_bar_count == 78


@authoritative
@pytest.mark.parametrize("year", [2017, 2019, 2021, 2023, 2025])
def test_year_boundary_sessions_resolve(year):
    last = C.resolve_session(date(year, 12, 31))
    first = C.resolve_session(date(year + 1, 1, 2))
    for s in (last, first):
        assert s.certified is True
        assert s.session_type in (SESSION_REGULAR, SESSION_EARLY_CLOSE,
                                  SESSION_MARKET_CLOSED)


@authoritative
@pytest.mark.parametrize("year", range(2017, 2026))
def test_every_year_has_a_plausible_session_count(year):
    """Roughly 250 sessions a year. A gross miscount means the calendar data is
    wrong in a way individual spot checks would not reveal."""
    n = len(C.trading_sessions(date(year, 1, 1), date(year, 12, 31)))
    assert 245 <= n <= 254, f"{year}: {n} sessions"


@authoritative
def test_weekends_are_never_trading_days_across_the_window():
    d, checked = date(2017, 1, 1), 0
    while d <= date(2026, 8, 1):
        if d.weekday() >= 5:
            assert C.resolve_session(d).session_type == SESSION_MARKET_CLOSED, d
            checked += 1
        d += timedelta(days=29)        # sample, not exhaustive
    assert checked > 0


# ═══════════════════════════════════════════════════════════════════════════
# Grid rule — counts are never sufficient on their own
# ═══════════════════════════════════════════════════════════════════════════
@authoritative
def test_grid_is_bar_open_and_excludes_the_close():
    """A 16:00 close yields a final START of 15:55; a 13:00 close yields 12:55.
    Including the close instant would create a 79th/43rd phantom bar."""
    reg = C.resolve_session(date(2024, 6, 12))
    early = C.resolve_session(date(2024, 11, 29))
    assert reg.expected_bar_starts[-1] + timedelta(minutes=5) == reg.market_close
    assert early.expected_bar_starts[-1] + timedelta(minutes=5) == early.market_close


@authoritative
def test_only_two_session_shapes_exist_in_the_window():
    """Pins the model: every certified session is 78 or 42 bars. A third shape
    would mean the calendar disagrees with this module's REGULAR/EARLY_CLOSE
    assumption, which must fail loudly rather than be averaged over."""
    counts = set()
    d = date(2017, 1, 1)
    while d <= date(2026, 8, 1):
        s = C.resolve_session(d)
        if s.session_type in (SESSION_REGULAR, SESSION_EARLY_CLOSE):
            counts.add(s.expected_bar_count)
        d += timedelta(days=1)
    assert counts == {42, 78}, counts


# ═══════════════════════════════════════════════════════════════════════════
# Calendar identity
# ═══════════════════════════════════════════════════════════════════════════
def test_calendar_identity_is_deterministic():
    assert C.calendar_identity() == C.calendar_identity()


def test_calendar_identity_covers_the_schedule_and_the_backend():
    ident = C.calendar_identity()
    for key in ("exchange", "timezone", "backend", "semantics_version",
                "coverage_from", "coverage_through", "schedule_digest",
                "session_count"):
        assert key in ident, key


@authoritative
def test_a_changed_historical_session_changes_calendar_identity(monkeypatch):
    """§25: if a dependency upgrade can change historical schedules, that must
    mint a NEW identity rather than silently rewriting research meaning."""
    before = C.calendar_identity()
    base = C._schedule_digest()
    monkeypatch.setattr(C, "_schedule_digest",
                        lambda: {**base, "schedule_digest": "changed"})
    assert C.calendar_identity() != before


@authoritative
def test_the_certified_schedule_digest_is_stable_across_calls():
    """Identity must not drift with the wall clock or cache state."""
    a = C._schedule_digest()
    C._DIGEST_CACHE.clear()
    b = C._schedule_digest()
    assert a == b
