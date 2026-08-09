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


def test_dates_outside_the_certified_window_are_uncertified():
    """The certified window widened to 2017 with an authoritative calendar, but
    the RULE is unchanged: an unverifiable expectation must never certify a
    session. Only the boundary moved."""
    s = C.resolve_session(date(2010, 1, 4))          # before CERTIFIED_FROM
    assert s.session_type == C.SESSION_UNCERTIFIED and s.certified is False
    assert C.resolve_session(NORMAL).certified is True
    assert C.resolve_session(date(2017, 1, 3)).certified is True


def test_calendar_provenance_discloses_coverage_and_backend():
    p = C.calendar_provenance()
    assert p["coverage_from"] == "2017-01-01"
    assert p["exchange"] == "XNYS"
    if p["authoritative"]:
        assert p["limitation"] is None
        assert p["backend"] == C.BACKEND_EXCHANGE_CALENDARS
    else:                                            # no-dependency fallback
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
    s = C.resolve_session(date(2010, 1, 4))
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
    assert m["calendar"]["coverage_from"] == "2017-01-01"
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
def test_readiness_requires_evidence_not_assertion(tmp_path):
    """Superseded premise: this once accepted metadata fields as proof. Since
    readiness is recomputed from persisted bytes, asserted-only metadata must
    now read FALSE. The positive case needs a real snapshot and lives in
    test_readiness_is_recomputed_from_persisted_bytes_not_metadata.

    Scoped to an EMPTY root. Readiness is now derived from durable graduation
    evidence, so leaving the root defaulted made this read the operator's real
    corpus and pass for the wrong reason — the test must supply the absence it
    claims to be testing.
    """
    from portfolio_automation.intraday_lab import foundation as FD
    blank = FD.session2_status(None, root=str(tmp_path))
    assert blank["canonical_dataset_ready"] is False
    assert blank["feature_dataset_ready"] is False
    assert blank["graduation_evidence_ready"] is False
    asserted_only = FD.session2_status({"dataset_fingerprint": "x",
                                        "sessions_reconciled": 3,
                                        "feature_fingerprint": "y",
                                        "feature_observations": 10},
                                       root=str(tmp_path))
    assert asserted_only["canonical_dataset_ready"] is False
    assert asserted_only["feature_dataset_ready"] is False


def test_strategy_validation_stays_false_under_every_evidence_state():
    from portfolio_automation.intraday_lab import foundation as FD
    for pilot in (None, {}, {"dataset_fingerprint": "x", "sessions_requested": 1,
                            "sessions_admitted": 1, "feature_fingerprint": "y",
                            "feature_observations": 5}):
        assert FD.session2_status(pilot)["strategy_validation_allowed"] is False


# ===========================================================================
# Session 2 COMPLETION — durable acquisition, immutable snapshots, provenance
# ===========================================================================
from portfolio_automation.intraday_lab import storage as ST      # noqa: E402
from portfolio_automation.intraday_lab import pipeline as PL      # noqa: E402
from portfolio_automation.intraday_lab import models as M         # noqa: E402


def _rows(session, n=None):
    """Provider-shaped rows (naive ET strings) for a session."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    starts = session.expected_bar_starts[:n] if n else session.expected_bar_starts
    return [{"date": t.astimezone(et).strftime("%Y-%m-%d %H:%M:%S"),
             "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
             "volume": 1000} for t in starts]


def _fetcher(mapping):
    def f(symbol, start, end):
        return mapping.get(symbol, []), 200
    return f


# --------------------------- request accounting ---------------------------
def test_uncertified_requested_date_still_produces_a_record():
    req = DS.DatasetRequest(symbols=("SPY",), start=date(2010, 1, 4),
                            end=date(2010, 1, 4))
    assert len(req.resolved_items()) == 1
    ds = DS.build_canonical_dataset({}, request=req)
    assert len(ds.reconciliations) == 1
    assert ds.reconciliations[0].admission_status == DS.REJECTED_CALENDAR_UNCERTIFIED


def test_requested_closed_date_is_accounted_but_not_a_rejection():
    req = DS.DatasetRequest(symbols=("SPY",), start=date(2026, 8, 8),
                            end=date(2026, 8, 8))          # Saturday
    summary = req.calendar_resolution_summary()
    assert summary["requested_symbol_date_count"] == 1
    assert summary["closed_dates"] == 1 and summary["provider_calls_planned"] == 0
    ds = DS.build_canonical_dataset({}, request=req)
    assert ds.reconciliations[0].admission_status == DS.NOT_A_TRADING_SESSION
    assert ds.rejected == ()


def test_unexpected_provider_result_cannot_be_silently_ignored():
    req = DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL)
    rogue = ("AAPL", NORMAL)
    ds = DS.build_canonical_dataset(
        {("SPY", NORMAL): _bars(C.resolve_session(NORMAL)),
         rogue: _bars(C.resolve_session(NORMAL), symbol="AAPL")}, request=req)
    statuses = {(r.symbol, r.admission_status) for r in ds.reconciliations}
    assert ("AAPL", DS.REJECTED_UNEXPECTED_PROVIDER_RESULT) in statuses


# --------------------------- calendar identity ---------------------------
def test_manifest_identity_changes_when_calendar_semantics_change(monkeypatch):
    """Same bars, different calendar meaning = different research question."""
    s = C.resolve_session(NORMAL)
    bars = {("SPY", NORMAL): _bars(s)}
    req = DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL)
    before = DS.build_canonical_dataset(bars, request=req)
    content_before, manifest_before = before.fingerprint(), before.manifest_fingerprint()

    # Simulate the real §25 risk: a calendar-data upgrade that changes a
    # HISTORICAL session. The bars are untouched, but they now answer a
    # different research question, so manifest identity must move.
    base = C._schedule_digest()
    monkeypatch.setattr(C, "_schedule_digest",
                        lambda: {**base, "schedule_digest": "a-different-schedule",
                                 "early_close_count": base["early_close_count"] + 1})
    after = DS.build_canonical_dataset(bars, request=req)
    assert after.fingerprint() == content_before            # bytes unchanged
    assert after.manifest_fingerprint() != manifest_before  # meaning changed


def test_a_dependency_version_bump_alone_does_not_change_calendar_identity():
    """§25: identity tracks SCHEDULE meaning, not the package version. A no-op
    upgrade must not mint a spurious research era (which would also make every
    archived manifest un-remintable)."""
    ident = C.calendar_identity()
    assert "implementation_version" not in ident
    assert C.calendar_provenance()["implementation_version"] == C._XCALS_VERSION
    assert "schedule_digest" in ident


# --------------------------- adjustment authority ---------------------------
def test_caller_cannot_supply_adjustment_state_at_all():
    ds = DS.build_canonical_dataset({("SPY", NORMAL): _bars(C.resolve_session(NORMAL))},
                                    adjustment_state="LIES")
    assert ds.adjustment_state == "split_adjusted"


def test_empty_dataset_adjustment_is_not_applicable_not_a_guess():
    ds = DS.build_canonical_dataset({}, request=DS.DatasetRequest(
        symbols=("SPY",), start=date(2026, 8, 8), end=date(2026, 8, 8)))
    assert ds.adjustment_state == "NOT_APPLICABLE"


# --------------------------- immutable storage ---------------------------
def test_identical_snapshot_is_verified_and_reused(tmp_path):
    files = {"payload.json": [{"a": 1}]}
    a = ST.write_snapshot(ST.RAW, "abc123", files, root=str(tmp_path))
    b = ST.write_snapshot(ST.RAW, "abc123", files, root=str(tmp_path))
    assert a == b and a.is_dir()


def test_same_identity_different_bytes_is_a_hard_failure(tmp_path):
    """Overwriting would invalidate every experiment bound to that identity."""
    ST.write_snapshot(ST.RAW, "abc123", {"payload.json": [{"a": 1}]}, root=str(tmp_path))
    with pytest.raises(ST.SnapshotCollisionError):
        ST.write_snapshot(ST.RAW, "abc123", {"payload.json": [{"a": 2}]},
                          root=str(tmp_path))


def test_raw_identity_ignores_retrieval_time_but_tracks_content():
    rows = [{"date": "2026-08-03 09:30:00", "open": 1, "high": 2, "low": 0.5,
             "close": 1.5, "volume": 10}]
    h1 = ST.raw_payload_hash(rows, symbol="SPY", timeframe="5min")
    h2 = ST.raw_payload_hash(rows, symbol="SPY", timeframe="5min")
    changed = ST.raw_payload_hash(rows + [dict(rows[0], close=9)],
                                  symbol="SPY", timeframe="5min")
    assert h1 == h2 and h1 != changed


def test_snapshots_live_under_historical_never_latest(tmp_path):
    ST.write_snapshot(ST.DATASETS, "fp1", {"x.json": {}}, root=str(tmp_path))
    assert (tmp_path / "outputs" / "backtest" / "intraday" / "datasets" / "content"
            / "fp1").is_dir()
    assert not (tmp_path / "outputs" / "latest").exists()


# --------------------------- end-to-end pipeline ---------------------------
def test_pipeline_persists_and_verifies_canonical_identity(tmp_path):
    req = DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL)
    out = PL.build_historical_research_dataset(
        req, _fetcher({"SPY": _rows(C.resolve_session(NORMAL))}), root=str(tmp_path))
    assert out["sessions_admitted"] == 1 and out["bars_admitted"] == 78
    assert out["canonical_verification"]["verified"] is True
    assert out["strategy_validation_allowed"] is False
    assert ST.snapshot_exists(ST.DATASETS, out["dataset_fingerprint"], root=str(tmp_path))
    assert ST.snapshot_exists(ST.FEATURES, out["feature_fingerprint"], root=str(tmp_path))


def test_pipeline_keeps_a_missing_middle_session_visible(tmp_path):
    """3 requested trading days, provider omits the middle one entirely."""
    s3, s5 = C.resolve_session(date(2026, 8, 3)), C.resolve_session(date(2026, 8, 5))
    req = DS.DatasetRequest(symbols=("SPY",), start=date(2026, 8, 3),
                            end=date(2026, 8, 5))
    out = PL.build_historical_research_dataset(
        req, _fetcher({"SPY": _rows(s3) + _rows(s5)}), root=str(tmp_path))
    assert out["expected_trading_sessions"] == 3
    assert out["sessions_admitted"] == 2 and out["sessions_rejected"] == 1


def test_pipeline_records_a_provider_error_without_losing_the_session(tmp_path):
    def boom(symbol, start, end):
        raise RuntimeError("provider exploded")
    req = DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL)
    out = PL.build_historical_research_dataset(req, boom, root=str(tmp_path))
    assert out["acquisitions"][0]["provider_status"] == "PROVIDER_ERROR"
    assert "provider exploded" in out["acquisitions"][0]["error_message_safe"]
    assert out["sessions_reconciled"] == 1 and out["sessions_rejected"] == 1


def test_dry_run_writes_nothing(tmp_path):
    req = DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL)
    out = PL.build_historical_research_dataset(
        req, _fetcher({"SPY": _rows(C.resolve_session(NORMAL))}),
        root=str(tmp_path), dry_run=True)
    assert out["dry_run"] is True and out["writes"] == []
    assert not (tmp_path / "outputs").exists()


def test_durable_feature_build_cannot_misbind_provenance(tmp_path):
    """build_features derives identity from the dataset object, so no caller can
    pair bars from one dataset with the identity of another."""
    import inspect
    sig = inspect.signature(PL.build_features)
    assert "dataset_id" not in sig.parameters
    assert "fingerprint" not in sig.parameters
    ds = DS.build_canonical_dataset({("SPY", NORMAL): _bars(C.resolve_session(NORMAL))})
    vals = PL.build_features(ds)
    assert vals and all(v.source_dataset_fingerprint == ds.fingerprint() for v in vals)
    assert all(v.source_dataset_manifest_fingerprint == ds.manifest_fingerprint()
               for v in vals)


def test_feature_identity_changes_with_manifest_identity_alone():
    """Same bars, same values, different research meaning -> different identity."""
    s = C.resolve_session(NORMAL)
    bars = {("SPY", NORMAL): _bars(s)}
    a = DS.build_canonical_dataset(bars, request=DS.DatasetRequest(
        symbols=("SPY",), start=NORMAL, end=NORMAL))
    b = DS.build_canonical_dataset(bars, request=DS.DatasetRequest(
        symbols=("SPY",), start=NORMAL, end=date(2026, 8, 5)))
    va, vb = PL.build_features(a), PL.build_features(b)
    assert [v.value for v in va] == [v.value for v in vb]
    assert a.fingerprint() == b.fingerprint()
    assert F.feature_fingerprint(va) != F.feature_fingerprint(vb)


def test_readiness_is_recomputed_from_persisted_bytes_not_metadata(tmp_path):
    """Fabricated metadata must not make the data product look ready."""
    from portfolio_automation.intraday_lab import foundation as FD
    fake = {"dataset_fingerprint": "deadbeef", "sessions_reconciled": 3,
            "feature_fingerprint": "nope", "feature_observations": 99}
    assert FD._canonical_ready(fake, str(tmp_path)) is False
    assert FD._feature_ready(fake, str(tmp_path)) is False

    req = DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL)
    out = PL.build_historical_research_dataset(
        req, _fetcher({"SPY": _rows(C.resolve_session(NORMAL))}), root=str(tmp_path))
    assert FD._canonical_ready(out, str(tmp_path)) is True
    assert FD._feature_ready(out, str(tmp_path)) is True


def test_tampered_snapshot_fails_verification(tmp_path):
    """A snapshot whose stored bars no longer hash to their directory name."""
    import json as _json
    req = DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL)
    out = PL.build_historical_research_dataset(
        req, _fetcher({"SPY": _rows(C.resolve_session(NORMAL))}), root=str(tmp_path))
    fp = out["dataset_fingerprint"]
    path = ST.intraday_root(str(tmp_path)) / ST.DATASETS / fp / "canonical_bars.json"
    rows = _json.loads(path.read_text())
    rows[0]["close"] = 999.99
    path.write_text(_json.dumps(rows, separators=(",", ":"), sort_keys=True))
    assert ST.verify_canonical_snapshot(fp, root=str(tmp_path))["verified"] is False
    from portfolio_automation.intraday_lab import foundation as FD
    assert FD._canonical_ready(out, str(tmp_path)) is False


# ===========================================================================
# PROVENANCE HARDENING — content objects vs research/acquisition events.
#
# Reproduced on 05f5003e: refetching identical observations an hour later
# raised SnapshotCollisionError, because acquisition_manifest.json (carrying
# retrieved_at) lived inside a content-addressed raw directory. The same defect
# existed one and two levels up: canonical_bars.json serialized retrieved_at,
# and the dataset manifest carried generated_at + per-run event ids.
# ===========================================================================
def _idempotent_runs(tmp_path, rows, req, times):
    import portfolio_automation.intraday_lab.pipeline as _PL
    orig, outs = _PL.acquire, []
    try:
        for t in times:
            _PL.acquire = (lambda tt: (lambda r, f, *, root=str(tmp_path), now=None:
                                       orig(r, f, root=root, now=tt)))(t)
            outs.append(_PL.build_historical_research_dataset(
                req, _fetcher({"SPY": rows}), root=str(tmp_path)))
    finally:
        _PL.acquire = orig
    return outs


def test_identical_refetch_reuses_content_and_records_two_events(tmp_path):
    """The headline regression: same observations, different retrieval time."""
    req = DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL)
    a, b = _idempotent_runs(
        tmp_path, _rows(C.resolve_session(NORMAL)), req,
        [datetime(2026, 8, 8, 10, 0, tzinfo=UTC),
         datetime(2026, 8, 8, 11, 0, tzinfo=UTC)])

    assert a["raw_content_fingerprints"] == b["raw_content_fingerprints"]
    assert a["dataset_fingerprint"] == b["dataset_fingerprint"]
    assert a["manifest_fingerprint"] == b["manifest_fingerprint"]
    assert a["feature_fingerprint"] == b["feature_fingerprint"]
    # ...but the retrievals remain individually auditable
    assert a["acquisition_event_ids"] != b["acquisition_event_ids"]
    root = ST.intraday_root(str(tmp_path))
    assert len(list((root / "raw" / "content").iterdir())) == 1
    assert len(list((root / "raw" / "events").iterdir())) == 2
    assert b["canonical_verification"]["verified"] is True
    assert b["feature_verification"]["verified"] is True


def test_two_requests_sharing_canonical_content_both_persist(tmp_path):
    """One canonical content object, two research manifests, no collision."""
    rows = _rows(C.resolve_session(NORMAL))
    a = PL.build_historical_research_dataset(
        DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL),
        _fetcher({"SPY": rows}), root=str(tmp_path))
    b = PL.build_historical_research_dataset(
        DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=date(2026, 8, 4)),
        _fetcher({"SPY": rows}), root=str(tmp_path))
    assert a["dataset_fingerprint"] == b["dataset_fingerprint"]
    assert a["manifest_fingerprint"] != b["manifest_fingerprint"]
    root = ST.intraday_root(str(tmp_path))
    assert len(list((root / "datasets" / "content").iterdir())) == 1
    assert len(list((root / "datasets" / "manifests").iterdir())) == 2


def test_provider_error_is_not_reported_as_missing_market_data(tmp_path):
    def boom(symbol, start, end):
        raise RuntimeError("provider exploded")
    out = PL.build_historical_research_dataset(
        DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL),
        boom, root=str(tmp_path))
    statuses = {r["admission_status"] for r in out["rejections"]["rejections"]}
    assert statuses == {DS.REJECTED_PROVIDER_ERROR}
    assert out["acquisitions"][0]["provider_status"] == "PROVIDER_ERROR"
    assert out["acquisitions"][0]["raw_payload_hash"] is None
    assert out["acquisitions"][0]["acquisition_event_id"]


def test_empty_successful_response_is_distinct_from_provider_failure(tmp_path):
    out = PL.build_historical_research_dataset(
        DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL),
        _fetcher({"SPY": []}), root=str(tmp_path))
    assert out["acquisitions"][0]["provider_status"] == "NO_DATA"
    statuses = {r["admission_status"] for r in out["rejections"]["rejections"]}
    assert statuses == {DS.REJECTED_MISSING_BARS}      # market data absent, not an outage


def test_raw_content_verification_detects_tampering(tmp_path):
    import json as _json
    out = PL.build_historical_research_dataset(
        DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL),
        _fetcher({"SPY": _rows(C.resolve_session(NORMAL))}), root=str(tmp_path))
    fp = out["raw_content_fingerprints"][0]
    assert ST.verify_raw_content(fp, root=str(tmp_path))["verified"] is True
    path = ST.intraday_root(str(tmp_path)) / "raw" / "content" / fp / "payload.json"
    rows = _json.loads(path.read_text())
    rows[0]["close"] = 12345.0
    path.write_text(_json.dumps(rows, separators=(",", ":"), sort_keys=True))
    assert ST.verify_raw_content(fp, root=str(tmp_path))["verified"] is False


def test_feature_tampering_flips_readiness_false(tmp_path):
    import json as _json
    from portfolio_automation.intraday_lab import foundation as FD
    out = PL.build_historical_research_dataset(
        DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL),
        _fetcher({"SPY": _rows(C.resolve_session(NORMAL))}), root=str(tmp_path))
    assert FD._feature_ready(out, str(tmp_path)) is True
    fp = out["feature_fingerprint"]
    path = ST.intraday_root(str(tmp_path)) / "features" / "content" / fp / "features.json"
    rows = _json.loads(path.read_text())
    rows[0]["value"] = 9.99
    path.write_text(_json.dumps(rows, separators=(",", ":"), sort_keys=True))
    assert ST.verify_feature_snapshot(fp, root=str(tmp_path))["verified"] is False
    assert FD._feature_ready(out, str(tmp_path)) is False


def test_volatile_keys_never_enter_an_immutable_content_object(tmp_path):
    out = PL.build_historical_research_dataset(
        DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL),
        _fetcher({"SPY": _rows(C.resolve_session(NORMAL))}), root=str(tmp_path))
    root = ST.intraday_root(str(tmp_path))
    for kind, fp in (("datasets/content", out["dataset_fingerprint"]),
                     ("features/content", out["feature_fingerprint"]),
                     ("raw/content", out["raw_content_fingerprints"][0])):
        for f in (root / kind / fp).iterdir():
            text = f.read_text()
            assert "retrieved_at" not in text and "generated_at" not in text, f


# ===========================================================================
# FINAL PROVENANCE SEMANTICS — PIT identity, source identity, causality, graph.
# ===========================================================================
def _pit_bar(delay_s, **kw):
    from datetime import timedelta as _td
    return M.IntradayBar(symbol="SPY", timeframe="5min",
                         bar_start_at=datetime(2026, 8, 3, 13, 30, tzinfo=UTC),
                         open=100, high=101, low=99, close=100.5, volume=1000,
                         adjustment_state="split_adjusted",
                         publication_delay=_td(seconds=delay_s), **kw)


def test_known_at_is_part_of_canonical_identity():
    """The critical PIT test. Two datasets identical in OHLCV and bar_start_at
    but where B publishes 60s EARLIER are not research-equivalent: B confers a
    look-ahead advantage. v2 gave them one identity."""
    a = _pit_bar(60)
    b = _pit_bar(0)
    assert a.known_at != b.known_at
    fa = DS.canonical_fingerprint([a], timeframe="5min", adjustment_state="split_adjusted")
    fb = DS.canonical_fingerprint([b], timeframe="5min", adjustment_state="split_adjusted")
    assert fa != fb


def test_bar_end_at_is_part_of_canonical_identity():
    """bar_end_at derives from timeframe, so a different timeframe on the same
    start instant must also change identity."""
    from datetime import timedelta as _td
    a = _pit_bar(60)
    original = dict(M.TIMEFRAMES)
    try:
        M.TIMEFRAMES["15min"] = _td(minutes=15)
        b = M.IntradayBar(symbol="SPY", timeframe="15min",
                          bar_start_at=a.bar_start_at, open=100, high=101, low=99,
                          close=100.5, volume=1000, adjustment_state="split_adjusted")
        assert a.bar_end_at != b.bar_end_at
        assert DS.canonical_fingerprint([a], timeframe="5min",
                                        adjustment_state="split_adjusted") != \
               DS.canonical_fingerprint([b], timeframe="15min",
                                        adjustment_state="split_adjusted")
    finally:
        M.TIMEFRAMES.clear()
        M.TIMEFRAMES.update(original)


def test_raw_identity_includes_source_semantics():
    """The content_manifest already recorded provider+endpoint, so identity had
    to cover them or two sources would collide on one hash while storing
    different manifests — identity narrower than its own content."""
    rows = [{"date": "2026-08-03 09:30:00", "open": 1, "high": 2, "low": 0.5,
             "close": 1.5, "volume": 10}]
    a = ST.raw_payload_hash(rows, symbol="SPY", timeframe="5min",
                            provider="fmp", endpoint="/stable/historical-chart/5min")
    b = ST.raw_payload_hash(rows, symbol="SPY", timeframe="5min",
                            provider="other", endpoint="/stable/historical-chart/5min")
    c = ST.raw_payload_hash(rows, symbol="SPY", timeframe="5min",
                            provider="fmp", endpoint="/v3/other-endpoint")
    assert a != b and a != c


def test_normalization_failure_has_its_own_causal_state(tmp_path):
    """Provider answered; WE could not parse it. Reporting missing market data
    would hide a provider schema change entirely."""
    bad = [{"date": "not-a-timestamp", "open": 1, "high": 2, "low": 0.5,
            "close": 1.5, "volume": 10}]
    out = PL.build_historical_research_dataset(
        DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL),
        _fetcher({"SPY": bad}), root=str(tmp_path))
    acq = out["acquisitions"][0]
    assert acq["provider_status"] == "OK"
    assert acq["normalization_status"].startswith("FAILED")
    assert acq["raw_payload_hash"]                      # raw evidence preserved
    statuses = {r["admission_status"] for r in out["rejections"]["rejections"]}
    assert statuses == {DS.REJECTED_NORMALIZATION_ERROR}
    assert DS.REJECTED_MISSING_BARS not in statuses
    assert DS.REJECTED_PROVIDER_ERROR not in statuses


def _built(tmp_path):
    return PL.build_historical_research_dataset(
        DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL),
        _fetcher({"SPY": _rows(C.resolve_session(NORMAL))}), root=str(tmp_path))


def test_valid_provenance_graph_verifies(tmp_path):
    out = _built(tmp_path)
    v = ST.verify_dataset_provenance(out["manifest_fingerprint"], root=str(tmp_path))
    assert v["verified"] is True
    assert v["canonical_content_fingerprint"] == out["dataset_fingerprint"]
    assert v["reconciled_items"] == 1 and v["raw_verified"]


def _tamper(path, mutate):
    import json as _json
    data = _json.loads(path.read_text())
    mutate(data)
    path.write_text(_json.dumps(data, separators=(",", ":"), sort_keys=True))


def test_raw_tampering_flips_dataset_readiness_false(tmp_path):
    """Proves readiness actually walks the chain rather than checking one file."""
    from portfolio_automation.intraday_lab import foundation as FD
    out = _built(tmp_path)
    assert FD._canonical_ready(out, str(tmp_path)) is True
    raw_fp = out["raw_content_fingerprints"][0]
    _tamper(ST.intraday_root(str(tmp_path)) / "raw" / "content" / raw_fp / "payload.json",
            lambda d: d.__setitem__(0, {**d[0], "close": 4242.0}))
    assert ST.verify_dataset_provenance(out["manifest_fingerprint"],
                                        root=str(tmp_path))["verified"] is False
    assert FD._canonical_ready(out, str(tmp_path)) is False
    assert FD._feature_ready(out, str(tmp_path)) is False


@pytest.mark.parametrize("field,value", [
    ("request_fingerprint", "tampered"),
    ("calendar_fingerprint", ""),
    ("canonical_content_fingerprint", "somethingelse"),
])
def test_manifest_tampering_flips_readiness_false(tmp_path, field, value):
    from portfolio_automation.intraday_lab import foundation as FD
    out = _built(tmp_path)
    path = (ST.intraday_root(str(tmp_path)) / "datasets" / "manifests"
            / out["manifest_fingerprint"] / "request_manifest.json")
    _tamper(path, lambda d: d.__setitem__(field, value))
    assert ST.verify_dataset_provenance(out["manifest_fingerprint"],
                                        root=str(tmp_path))["verified"] is False
    assert FD._canonical_ready(out, str(tmp_path)) is False


def test_a_disappeared_reconciliation_record_fails_verification(tmp_path):
    out = _built(tmp_path)
    path = (ST.intraday_root(str(tmp_path)) / "datasets" / "manifests"
            / out["manifest_fingerprint"] / "reconciliation.json")
    path.write_text("[]")
    v = ST.verify_dataset_provenance(out["manifest_fingerprint"], root=str(tmp_path))
    assert v["verified"] is False and "disappeared" in v["reason"]


def test_acquisition_event_verifies_and_references_raw(tmp_path):
    out = _built(tmp_path)
    v = ST.verify_acquisition_event(out["acquisition_event_ids"][0], root=str(tmp_path))
    assert v["verified"] is True
    assert v["raw_content_fingerprint"] == out["raw_content_fingerprints"][0]
