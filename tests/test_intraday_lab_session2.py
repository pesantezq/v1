"""Intraday Lab Session 2 — calendar, admission, immutability, PIT features."""
from datetime import date, datetime, timedelta, timezone

import pytest

from portfolio_automation.intraday_lab import calendar as C
from portfolio_automation.intraday_lab import dataset as DS
from portfolio_automation.intraday_lab import features as F
from portfolio_automation.intraday_lab.models import IntradayBar
from portfolio_automation.intraday_lab.validation import (
    SESSION_REGULAR, SESSION_EARLY_CLOSE, SESSION_MARKET_CLOSED,
)

UTC = timezone.utc
NORMAL = date(2026, 8, 3)
EARLY = date(2025, 11, 28)


def _bars(session, symbol="SPY", adj="split_adjusted", drop=(), extra=()):
    out = []
    for i, t in enumerate(session.expected_bar_starts):
        if i in drop:
            continue
        out.append(IntradayBar(symbol=symbol, timeframe="5min", bar_start_at=t,
                               open=100 + i * 0.1, high=101 + i * 0.1,
                               low=99 + i * 0.1, close=100.5 + i * 0.1,
                               volume=1000 + i, adjustment_state=adj))
    for t in extra:
        out.append(IntradayBar(symbol=symbol, timeframe="5min", bar_start_at=t,
                               open=100, high=101, low=99, close=100.5,
                               volume=1, adjustment_state=adj))
    return out


# ===========================================================================
# Calendar
# ===========================================================================
def test_regular_session_grid_is_78_bars_0930_to_1555():
    s = C.resolve_session(NORMAL)
    assert s.session_type == SESSION_REGULAR and s.expected_bar_count == 78
    assert s.expected_bar_starts[0] == datetime(2026, 8, 3, 13, 30, tzinfo=UTC)
    assert s.expected_bar_starts[-1] == datetime(2026, 8, 3, 19, 55, tzinfo=UTC)


def test_early_close_grid_terminates_at_1255_from_calendar_not_hardcoding():
    s = C.resolve_session(EARLY)
    assert s.session_type == SESSION_EARLY_CLOSE and s.expected_bar_count == 42
    assert s.expected_bar_starts[-1] == datetime(2025, 11, 28, 17, 55, tzinfo=UTC)


def test_holiday_and_weekend_are_market_closed():
    assert C.resolve_session(date(2025, 11, 27)).session_type == SESSION_MARKET_CLOSED
    assert C.resolve_session(date(2026, 8, 8)).session_type == SESSION_MARKET_CLOSED
    assert C.resolve_session(date(2026, 8, 8)).expected_bar_count == 0


def test_dst_summer_and_winter_grids_differ_in_utc():
    summer = C.resolve_session(date(2026, 8, 3)).expected_bar_starts[0]
    winter = C.resolve_session(date(2026, 11, 30)).expected_bar_starts[0]
    assert summer.hour == 13 and winter.hour == 14      # EDT -4, EST -5
    assert C.resolve_session(date(2026, 11, 30)).expected_bar_count == 78


def test_dates_outside_holiday_coverage_are_uncertified():
    """5min bars exist back to 2017 but the holiday table starts 2025-01-01.
    An unverifiable expectation must never certify a session."""
    s = C.resolve_session(date(2023, 8, 7))
    assert s.session_type == C.SESSION_UNCERTIFIED and s.certified is False
    assert C.resolve_session(NORMAL).certified is True


def test_calendar_provenance_discloses_the_coverage_limitation():
    p = C.calendar_provenance()
    assert p["holiday_coverage_from"] == "2025-01-01"
    assert "UNCERTIFIED" in p["limitation"]


# ===========================================================================
# Exact reconciliation — count equality is NOT completeness
# ===========================================================================
def test_exact_grid_match_is_admitted():
    s = C.resolve_session(NORMAL)
    r = DS.reconcile_session(_bars(s), s, symbol="SPY")
    assert r.admission_status == DS.ADMITTED and r.coverage_pct == 100.0


def test_equal_count_but_wrong_timestamp_is_rejected():
    """THE Session 2 invariant: 78 observed vs 78 expected, one bar swapped for
    an off-grid one. A count check admits this; a set comparison cannot."""
    s = C.resolve_session(NORMAL)
    off_grid = s.expected_bar_starts[-1] + timedelta(minutes=5)   # 16:00, past close
    bars = _bars(s, drop=(10,), extra=(off_grid,))
    r = DS.reconcile_session(bars, s, symbol="SPY")
    assert r.observed_count == r.expected_count == 78
    assert r.admission_status == DS.REJECTED_OFF_GRID
    assert len(r.missing_timestamps) == 1 and len(r.unexpected_timestamps) == 1


def test_missing_bar_is_rejected():
    s = C.resolve_session(NORMAL)
    r = DS.reconcile_session(_bars(s, drop=(3, 4)), s, symbol="SPY")
    assert r.admission_status == DS.REJECTED_MISSING_BARS
    assert r.observed_count == 76


def test_surplus_extended_hours_bar_is_rejected():
    s = C.resolve_session(NORMAL)
    after_hours = datetime(2026, 8, 3, 20, 30, tzinfo=UTC)   # 16:30 ET
    r = DS.reconcile_session(_bars(s, extra=(after_hours,)), s, symbol="SPY")
    assert r.admission_status == DS.REJECTED_SURPLUS_BARS


def test_bars_on_a_closed_day_are_rejected():
    s = C.resolve_session(date(2025, 11, 27))
    bar = IntradayBar(symbol="SPY", timeframe="5min",
                      bar_start_at=datetime(2025, 11, 27, 14, 30, tzinfo=UTC),
                      open=100, high=101, low=99, close=100.5, volume=1)
    r = DS.reconcile_session([bar], s, symbol="SPY")
    assert r.admission_status == DS.REJECTED_CLOSED_SESSION_HAS_BARS


def test_conflicting_duplicate_is_rejected():
    s = C.resolve_session(NORMAL)
    bars = _bars(s)
    bars.append(IntradayBar(symbol="SPY", timeframe="5min",
                            bar_start_at=bars[0].bar_start_at,
                            open=100, high=101, low=99, close=100.7, volume=1,
                            adjustment_state="split_adjusted"))
    r = DS.reconcile_session(bars, s, symbol="SPY")
    assert r.admission_status == DS.REJECTED_CONFLICTING_DUPLICATE


def test_uncertified_session_is_rejected_even_with_perfect_looking_data():
    s = C.resolve_session(date(2023, 8, 7))
    r = DS.reconcile_session([], s, symbol="SPY")
    assert r.admission_status == DS.REJECTED_CALENDAR_UNCERTIFIED


def test_mixed_adjustment_states_are_rejected():
    s = C.resolve_session(NORMAL)
    bars = _bars(s)
    object.__setattr__(bars[0], "adjustment_state", "unadjusted")
    r = DS.reconcile_session(bars, s, symbol="SPY")
    assert r.admission_status == DS.REJECTED_MIXED_ADJUSTMENT


def test_mixed_adjustment_outranks_a_grid_defect_deterministically():
    """Mixing regimes changes what a return MEANS, so it must outrank any grid
    defect rather than depending on which check happened to run last."""
    s = C.resolve_session(NORMAL)
    bars = _bars(s, drop=(4,))                     # also a missing-bar defect
    object.__setattr__(bars[0], "adjustment_state", "unadjusted")
    assert DS.reconcile_session(bars, s, symbol="SPY").admission_status == \
        DS.REJECTED_MIXED_ADJUSTMENT


# ===========================================================================
# Canonical dataset + manifest
# ===========================================================================
def _dataset(**kw):
    s = C.resolve_session(NORMAL)
    return DS.build_canonical_dataset({("SPY", NORMAL): _bars(s, **kw)})


def test_rejected_sessions_contribute_no_bars_but_are_disclosed():
    s_ok, s_bad = C.resolve_session(NORMAL), C.resolve_session(date(2026, 8, 4))
    ds = DS.build_canonical_dataset({
        ("SPY", NORMAL): _bars(s_ok),
        ("SPY", date(2026, 8, 4)): _bars(s_bad, drop=(0,)),
    })
    assert len(ds.admitted) == 1 and len(ds.rejected) == 1
    assert len(ds.bars) == 78
    m = DS.dataset_manifest(ds)
    assert m["session_count_requested"] == 2 and m["session_count_rejected"] == 1
    assert m["rejection_summary"] == {DS.REJECTED_MISSING_BARS: 1}


def test_rejection_report_never_repairs_anything():
    s = C.resolve_session(NORMAL)
    ds = DS.build_canonical_dataset({("SPY", NORMAL): _bars(s, drop=(7,))})
    rep = DS.rejection_report(ds)
    assert rep["rejected_count"] == 1
    assert "forward-filled" in rep["note"]


def test_manifest_records_calendar_provenance_and_limitations():
    m = DS.dataset_manifest(_dataset())
    assert m["calendar"]["holiday_coverage_from"] == "2025-01-01"
    assert any("Rejected sessions" in s for s in m["limitations"])


# ===========================================================================
# Fingerprints / reproducibility
# ===========================================================================
def test_same_data_same_fingerprint():
    assert _dataset().fingerprint() == _dataset().fingerprint()


def test_changed_bar_changes_the_fingerprint():
    a = _dataset()
    s = C.resolve_session(NORMAL)
    bars = _bars(s)
    object.__setattr__(bars[5], "close", 123.456)
    b = DS.build_canonical_dataset({("SPY", NORMAL): bars})
    assert a.fingerprint() != b.fingerprint()


def test_retrieval_time_does_not_change_research_identity():
    """Refetching identical history must reproduce the fingerprint."""
    s = C.resolve_session(NORMAL)
    def mk(when):
        bars = _bars(s)
        for b in bars:
            object.__setattr__(b, "retrieved_at", when)
        return DS.build_canonical_dataset({("SPY", NORMAL): bars})
    a = mk(datetime(2026, 8, 8, tzinfo=UTC))
    b = mk(datetime(2027, 1, 1, tzinfo=UTC))
    assert a.fingerprint() == b.fingerprint()


def test_adjustment_state_is_part_of_dataset_identity():
    """Two datasets with byte-identical OHLCV but different adjustment regimes
    mean different things and must not share an identity."""
    s = C.resolve_session(NORMAL)
    a = DS.build_canonical_dataset({("SPY", NORMAL): _bars(s)},
                                   adjustment_state="split_adjusted")
    b = DS.build_canonical_dataset({("SPY", NORMAL): _bars(s)},
                                   adjustment_state="unadjusted")
    assert a.fingerprint() != b.fingerprint()


# ===========================================================================
# PIT-safe features
# ===========================================================================
def _fx(ds=None):
    ds = ds or _dataset()
    return ds, ds.dataset_id(), ds.fingerprint()


def test_feature_known_at_derives_from_the_newest_input_bar():
    ds, did, fp = _fx()
    fv = F.compute_return_nbar(ds.bars, 5, 3, dataset_id=did, fingerprint=fp)
    newest = max(ds.bars[3:6], key=lambda b: b.known_at)
    assert fv.known_at == newest.known_at
    assert fv.known_at > fv.event_at


def test_feature_is_not_knowable_before_its_newest_input():
    ds, did, fp = _fx()
    fv = F.compute_return_nbar(ds.bars, 5, 3, dataset_id=did, fingerprint=fp)
    assert not fv.is_known_at(fv.known_at - timedelta(seconds=1))
    assert fv.is_known_at(fv.known_at)


def test_insufficient_history_yields_explicit_absence_not_a_padded_value():
    ds, did, fp = _fx()
    assert F.compute_return_nbar(ds.bars, 1, 20, dataset_id=did,
                                 fingerprint=fp) is F.FEATURE_NOT_AVAILABLE
    assert F.compute_range_position(ds.bars, 2, 20, dataset_id=did,
                                    fingerprint=fp) is F.FEATURE_NOT_AVAILABLE


def test_feature_window_is_strictly_backward_looking():
    ds, did, fp = _fx()
    fv = F.compute_realized_vol(ds.bars, 10, 5, dataset_id=did, fingerprint=fp)
    assert fv.input_window_end == ds.bars[10].bar_start_at
    assert fv.input_window_start < fv.input_window_end
    assert fv.input_window_end <= fv.event_at


def test_every_feature_traces_to_its_exact_source_dataset():
    ds, did, fp = _fx()
    fv = F.compute_normalized_range(ds.bars, 4, dataset_id=did, fingerprint=fp)
    assert fv.source_dataset_id == did and fv.source_dataset_fingerprint == fp


def test_features_cannot_consume_bars_from_a_rejected_session():
    """A rejected session contributes no bars, so nothing downstream can read
    it — enforced structurally rather than by convention."""
    s = C.resolve_session(NORMAL)
    ds = DS.build_canonical_dataset({("SPY", NORMAL): _bars(s, drop=(9,))})
    assert ds.bars == () and ds.rejected


def test_normalized_range_is_dimensionless():
    ds, did, fp = _fx()
    fv = F.compute_normalized_range(ds.bars, 3, dataset_id=did, fingerprint=fp)
    bar = ds.bars[3]
    assert fv.value == pytest.approx((bar.high - bar.low) / bar.close)


# ===========================================================================
# Feature registry / blocked semantics
# ===========================================================================
@pytest.mark.parametrize("fid,status", [
    ("vwap", F.STATUS_BLOCKED_VOLUME),
    ("rvol", F.STATUS_BLOCKED_VOLUME),
    ("dollar_volume", F.STATUS_BLOCKED_VOLUME),
    ("absolute_atr", F.STATUS_BLOCKED_ADJUSTMENT),
    ("sector_relative_return", F.STATUS_DEFERRED),
])
def test_unproven_features_are_blocked_not_enabled_with_a_warning(fid, status):
    assert F.FEATURE_REGISTRY[fid]["status"] == status
    assert fid in F.BLOCKED_FEATURES and fid not in F.ENABLED_FEATURES


def test_no_enabled_feature_requires_absolute_price_or_volume():
    """Split back-adjustment makes absolute levels unsafe; volume semantics are
    unproven. Neither may leak into an enabled feature."""
    for fid in F.ENABLED_FEATURES:
        spec = F.FEATURE_REGISTRY[fid]
        assert spec["requires_absolute_price"] is False, fid
        assert spec["requires_volume"] is False, fid


def test_no_compute_function_exists_for_a_blocked_feature():
    for fid in F.BLOCKED_FEATURES:
        assert not hasattr(F, f"compute_{fid}"), f"blocked {fid} has an implementation"


# ===========================================================================
# Feature fingerprints
# ===========================================================================
def _values(n=3, lookback=3):
    ds, did, fp = _fx()
    return [v for v in (F.compute_return_nbar(ds.bars, i, lookback,
                                              dataset_id=did, fingerprint=fp)
                        for i in range(10, 10 + n)) if v]


def test_same_inputs_same_feature_fingerprint():
    assert F.feature_fingerprint(_values()) == F.feature_fingerprint(_values())


def test_changed_lookback_changes_the_feature_fingerprint():
    assert F.feature_fingerprint(_values(lookback=3)) != \
           F.feature_fingerprint(_values(lookback=5))


def test_feature_manifest_binds_to_its_source_dataset():
    ds, did, fp = _fx()
    m = F.feature_manifest(_values(), dataset_id=did, dataset_fingerprint=fp)
    assert m["source_dataset_fingerprint"] == fp
    assert set(m["features_blocked"]) == set(F.BLOCKED_FEATURES)


# ===========================================================================
# Governance
# ===========================================================================
def test_no_strategy_or_execution_module_exists():
    import pathlib
    pkg = pathlib.Path("portfolio_automation/intraday_lab")
    forbidden = {"strategies.py", "simulator.py", "cost_model.py", "risk.py",
                 "trades.py", "trade_ledger.py", "walk_forward.py", "oos.py"}
    assert not {p.name for p in pkg.rglob("*.py")} & forbidden
