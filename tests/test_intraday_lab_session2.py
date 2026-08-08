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
    a = DS.build_canonical_dataset({("SPY", NORMAL): _bars(s, adj="split_adjusted")})
    b = DS.build_canonical_dataset({("SPY", NORMAL): _bars(s, adj="unadjusted")})
    # Each dataset is internally uniform; the states differ between them. The
    # caller label is no longer an input at all -- identity comes from the bars.
    assert a.adjustment_state == "split_adjusted"
    assert b.adjustment_state == "unadjusted"
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


# ===========================================================================
# Session 2 HARDENING — five confirmed fail-open defects.
# Each test below reproduces a behaviour observed on b781dd28.
# ===========================================================================
def test_an_entirely_absent_requested_session_cannot_disappear():
    """Observed: 3 requested days, Aug 4 missing from the mapping, only 2
    reconciliations emitted. The window looked fully covered."""
    req = DS.DatasetRequest(symbols=("SPY",), start=date(2026, 8, 3),
                            end=date(2026, 8, 5))
    ds = DS.build_canonical_dataset({
        ("SPY", date(2026, 8, 3)): _bars(C.resolve_session(date(2026, 8, 3))),
        ("SPY", date(2026, 8, 5)): _bars(C.resolve_session(date(2026, 8, 5))),
    }, request=req)
    assert len(ds.reconciliations) == 3
    assert len(ds.admitted) == 2 and len(ds.rejected) == 1
    gone = [r for r in ds.rejected if r.market_date == date(2026, 8, 4)]
    assert gone and gone[0].admission_status == DS.REJECTED_MISSING_BARS


def test_request_matrix_covers_only_certified_trading_sessions():
    req = DS.DatasetRequest(symbols=("SPY", "AAPL"), start=date(2026, 8, 7),
                            end=date(2026, 8, 10))   # Fri, Sat, Sun, Mon
    dates = {d for _, d in req.certified_sessions()}
    assert dates == {date(2026, 8, 7), date(2026, 8, 10)}
    assert len(req.certified_sessions()) == 4        # 2 symbols x 2 sessions


def test_identical_duplicate_no_longer_inflates_the_canonical_dataset():
    """Observed: 78 expected, 78 unique observed, 79 canonical rows."""
    s = C.resolve_session(NORMAL)
    bars = _bars(s)
    bars.append(bars[0])
    r = DS.reconcile_session(bars, s, symbol="SPY")
    assert r.admission_status == DS.REJECTED_EXACT_DUPLICATE
    ds = DS.build_canonical_dataset({("SPY", NORMAL): bars})
    assert ds.bars == ()


def test_bar_symbol_must_match_the_request_key():
    """The outer dict key must never be trusted over the bar itself."""
    s = C.resolve_session(NORMAL)
    r = DS.reconcile_session(_bars(s, symbol="AAPL"), s, symbol="SPY")
    assert r.admission_status == DS.REJECTED_IDENTITY_MISMATCH


def test_cross_session_mixed_adjustment_admits_nothing():
    """Observed: dataset labelled split_adjusted while holding both regimes."""
    a, b = C.resolve_session(NORMAL), C.resolve_session(date(2026, 8, 4))
    bars_b = _bars(b)
    for x in bars_b:
        object.__setattr__(x, "adjustment_state", "unadjusted")
    ds = DS.build_canonical_dataset({("SPY", NORMAL): _bars(a),
                                     ("SPY", date(2026, 8, 4)): bars_b})
    assert ds.bars == () and len(ds.admitted) == 0
    assert all(r.admission_status == DS.REJECTED_MIXED_ADJUSTMENT
               for r in ds.reconciliations)


def test_dataset_adjustment_state_is_derived_not_taken_from_the_caller():
    ds = DS.build_canonical_dataset({("SPY", NORMAL): _bars(C.resolve_session(NORMAL))},
                                    adjustment_state="TOTALLY_WRONG")
    assert ds.adjustment_state == "split_adjusted"


def test_manifest_fingerprint_separates_meaning_from_bytes():
    """Same admitted bars, different requested window = different research
    question, so identical content must not share a manifest identity."""
    s = C.resolve_session(NORMAL)
    bars = {("SPY", NORMAL): _bars(s)}
    a = DS.build_canonical_dataset(bars, request=DS.DatasetRequest(
        symbols=("SPY",), start=NORMAL, end=NORMAL))
    b = DS.build_canonical_dataset(bars, request=DS.DatasetRequest(
        symbols=("SPY",), start=NORMAL, end=date(2026, 8, 5)))
    assert a.fingerprint() == b.fingerprint()               # same bytes
    assert a.manifest_fingerprint() != b.manifest_fingerprint()   # different meaning


# --------------------------- feature integrity ---------------------------
def test_feature_window_cannot_cross_symbols():
    """Observed: a 3-bar window over SPY+AAPL produced a value labelled AAPL."""
    s = C.resolve_session(NORMAL)
    mixed = _bars(s, symbol="SPY")[:3] + _bars(s, symbol="AAPL")[:3]
    with pytest.raises(F.SeriesIntegrityError):
        F.compute_return_nbar(mixed, 4, 3, dataset_id="d", fingerprint="f")


def test_group_series_splits_by_symbol_and_timeframe():
    s = C.resolve_session(NORMAL)
    groups = F.group_series(_bars(s, symbol="SPY")[:3] + _bars(s, symbol="AAPL")[:3])
    assert set(groups) == {("SPY", "5min"), ("AAPL", "5min")}
    assert all(len(v) == 3 for v in groups.values())


def test_feature_window_cannot_bridge_a_rejected_session():
    """Rejected sessions contribute no bars, so a positional window could
    otherwise treat Monday and Wednesday as adjacent."""
    mon = _bars(C.resolve_session(date(2026, 8, 3)))[-2:]
    wed = _bars(C.resolve_session(date(2026, 8, 5)))[:2]
    assert F.compute_return_nbar(mon + wed, 3, 3, dataset_id="d",
                                 fingerprint="f") is F.FEATURE_NOT_AVAILABLE


def test_feature_fingerprint_binds_to_the_source_dataset():
    """Numerically identical values from different datasets must not share an
    identity — otherwise an experiment cannot say which data produced it."""
    s = C.resolve_session(NORMAL)
    bars = _bars(s)
    vals_a = [F.compute_return_nbar(bars, i, 3, dataset_id="A", fingerprint="fp_A")
              for i in range(5, 9)]
    vals_b = [F.compute_return_nbar(bars, i, 3, dataset_id="B", fingerprint="fp_B")
              for i in range(5, 9)]
    assert [v.value for v in vals_a] == [v.value for v in vals_b]
    assert F.feature_fingerprint(vals_a) != F.feature_fingerprint(vals_b)


def test_every_enabled_feature_has_a_real_implementation():
    for fid in F.ENABLED_FEATURES:
        fn = F.IMPLEMENTATIONS.get(fid)
        assert fn and callable(getattr(F, fn, None)), f"{fid} ENABLED but unimplemented"


def test_session_progress_is_not_advertised_as_enabled_without_code():
    assert F.FEATURE_REGISTRY["session_progress"]["status"] == F.STATUS_NOT_IMPLEMENTED
    assert "session_progress" not in F.ENABLED_FEATURES


# --------------------------- status evidence ---------------------------
def test_readiness_requires_evidence_not_assertion():
    from portfolio_automation.intraday_lab import foundation as FD
    blank = FD.session2_status(None)
    assert blank["canonical_dataset_ready"] is False
    assert blank["feature_dataset_ready"] is False
    proven = FD.session2_status({"dataset_fingerprint": "x", "sessions_requested": 3,
                                 "sessions_admitted": 3, "feature_fingerprint": "y",
                                 "feature_observations": 10})
    assert proven["canonical_dataset_ready"] is True
    assert proven["feature_dataset_ready"] is True


def test_strategy_validation_stays_false_under_every_evidence_state():
    from portfolio_automation.intraday_lab import foundation as FD
    for pilot in (None, {}, {"dataset_fingerprint": "x", "sessions_requested": 1,
                            "sessions_admitted": 1, "feature_fingerprint": "y",
                            "feature_observations": 5}):
        assert FD.session2_status(pilot)["strategy_validation_allowed"] is False
