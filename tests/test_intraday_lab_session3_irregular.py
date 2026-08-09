"""Session 3.0 — halt-aware classification, segmentation and population policy.

THE DISTINCTION THIS FREEZES
============================

    a market halt is not corrupted market data
    an unexplained gap is not automatically a market halt

Session 2 rejects a halted session because its observed bars do not equal the
calendar grid. That stays frozen and correct. Session 3 may re-admit such a
session ONLY when an AUTHORITATIVE registry event explains every single missing
bar. Missing bars — even identical ones across symbols — never confer admission
by themselves, and a halt never repairs a different defect.

Everything below is designed to fail closed. The tests that matter most are the
ones proving a session is NOT re-admitted.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from portfolio_automation.intraday_lab import calendar as C
from portfolio_automation.intraday_lab import irregular_sessions as IR
from portfolio_automation.intraday_lab import population_audit as PA
from portfolio_automation.intraday_lab import storage as ST
from portfolio_automation.intraday_lab.models import IntradayBar

UTC = timezone.utc
HALT_DATE = date(2020, 3, 9)          # halt 09:34:13 -> 09:49:13 ET
CONTROL_DATE = date(2020, 3, 17)      # violent, but NO circuit breaker


def _et(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=C.EXCHANGE_TZ).astimezone(UTC)


def _classify(missing_et, *, market_date=HALT_DATE, state="REJECTED_MISSING_BARS",
              unexpected=(), symbol="SPY", timeframe="5min"):
    return IR.classify_session(
        symbol=symbol, market_date=market_date, timeframe=timeframe,
        session2_state=state,
        missing_timestamps=[_et(market_date, h, m) for h, m in missing_et],
        unexpected_timestamps=unexpected,
        session_type=C.SESSION_REGULAR)


# ═══════════════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════════════
def test_registry_holds_exactly_the_four_verified_march_2020_events():
    assert len(IR.MWCB_EVENTS) == 4
    assert [e.market_date.isoformat() for e in IR.MWCB_EVENTS] == [
        "2020-03-09", "2020-03-12", "2020-03-16", "2020-03-18"]
    assert all(e.level == 1 and e.scope == IR.MWCB_SCOPE for e in IR.MWCB_EVENTS)
    # Verified against the primary document, not a summary.
    prov = IR.registry_provenance()
    assert "Market-Wide Circuit Breaker" in prov["source_title"]
    assert prov["source_document_identifier"].startswith("SEC Release")
    assert prov["source_publication_date"] == "2021-03-31"
    assert prov["timezone"] == "America/New_York"


def test_registry_identity_changes_when_an_event_changes(monkeypatch):
    """A later source correction must mint a DIFFERENT research object rather
    than silently reinterpreting already-published results."""
    before = IR.registry_fingerprint()
    shifted = tuple(
        IR.MWCBEvent(e.market_date, time(9, 34, 14), e.reopen_start_et, e.level)
        if e.market_date == HALT_DATE else e for e in IR.MWCB_EVENTS)
    monkeypatch.setattr(IR, "MWCB_EVENTS", shifted)
    assert IR.registry_fingerprint() != before


def test_registry_identity_excludes_retrieval_facts():
    """When we looked is not what the registry means."""
    prov = IR.registry_provenance()
    for volatile in ("reviewed_at", "generated_at", "retrieved_at", "fetched_at"):
        assert volatile not in prov


# ═══════════════════════════════════════════════════════════════════════════
# §12 exact containment / §13 partial overlap
# ═══════════════════════════════════════════════════════════════════════════
def test_only_fully_contained_bars_are_halt_explained():
    ev = IR.mwcb_event_for(HALT_DATE)
    hs, rs = ev.window_utc()
    expect = {"09:30": False, "09:35": True, "09:40": True,
              "09:45": False, "09:50": False}
    for hhmm, want in expect.items():
        h, m = map(int, hhmm.split(":"))
        start = _et(HALT_DATE, h, m)
        got = IR.bar_fully_inside_halt(start, start + timedelta(minutes=5), hs, rs)
        assert got is want, f"{hhmm}: expected fully_inside={want}"


def test_fully_contained_missing_bars_are_admitted_as_a_halt_session():
    c = _classify([(9, 35), (9, 40)])
    assert c.state == IR.VALID_MARKET_WIDE_HALT_SESSION
    assert len(c.explained_missing) == 2 and not c.unexplained_missing
    assert c.mwcb_event["level"] == 1
    assert c.in_halt_aware_cohort and not c.in_continuous_cohort


def test_a_partially_overlapped_missing_bar_is_not_explained():
    """09:30-09:35 contained tradable time before the 09:34:13 trigger, so its
    absence is NOT explained by the halt. Fail closed."""
    c = _classify([(9, 30), (9, 35), (9, 40)])
    assert c.state == IR.REJECTED_UNEXPLAINED_GAP
    assert "not fully contained" in c.reason
    assert c.unexplained_missing and len(c.explained_missing) == 2


def test_one_extra_unexplained_gap_rejects_the_whole_session():
    """Two explained absences do not license a third, unrelated one."""
    c = _classify([(9, 35), (9, 40), (14, 30)])
    assert c.state == IR.REJECTED_UNEXPLAINED_GAP
    assert len(c.explained_missing) == 2
    assert len(c.unexplained_missing) == 1


def test_halt_windows_are_not_rounded_to_convenient_boundaries():
    """A 15-minute halt starting mid-bar explains only 10 minutes of absence.
    Rounding 09:34:13 down to 09:30 would silently admit a tradable interval."""
    c = _classify([(9, 35), (9, 40)])
    assert len(c.explained_missing) == 2          # 10 minutes, not 15


# ═══════════════════════════════════════════════════════════════════════════
# §6/§10 — inference is never authority
# ═══════════════════════════════════════════════════════════════════════════
def test_identical_missing_bars_without_a_registry_event_stay_rejected():
    """THE central rule. The same gap shape on a non-event date must not be
    rescued, however suggestive it looks across symbols."""
    for symbol in ("SPY", "AAPL"):
        c = _classify([(9, 35), (9, 40)], market_date=CONTROL_DATE, symbol=symbol)
        assert c.state == IR.REJECTED_UNEXPLAINED_GAP
        assert "not by themselves evidence of a halt" in c.reason
        assert c.mwcb_event is None


def test_a_non_market_wide_event_cannot_explain_an_arbitrary_symbol(monkeypatch):
    scoped = tuple(
        IR.MWCBEvent(e.market_date, e.halt_start_et, e.reopen_start_et, e.level,
                     scope="SINGLE_SYMBOL_LULD")
        if e.market_date == HALT_DATE else e for e in IR.MWCB_EVENTS)
    monkeypatch.setattr(IR, "MWCB_EVENTS", scoped)
    monkeypatch.setattr(IR, "_BY_DATE", {e.market_date: e for e in scoped})
    c = _classify([(9, 35), (9, 40)])
    assert c.state == IR.REJECTED_UNEXPLAINED_GAP
    assert "not market-wide" in c.reason


@pytest.mark.parametrize("state", [
    "REJECTED_PROVIDER_ERROR", "REJECTED_NORMALIZATION_ERROR",
    "REJECTED_SURPLUS_BARS", "REJECTED_OFF_GRID",
    "REJECTED_CONFLICTING_DUPLICATE", "REJECTED_EXACT_DUPLICATE",
    "REJECTED_IDENTITY_MISMATCH", "REJECTED_MIXED_ADJUSTMENT",
    "REJECTED_CALENDAR_UNCERTIFIED",
])
def test_a_halt_never_repairs_a_different_defect(state):
    """Only REJECTED_MISSING_BARS is eligible. A halt cannot make a surplus bar,
    an off-grid timestamp or a mixed adjustment regime correct."""
    c = _classify([(9, 35), (9, 40)], state=state)
    assert c.state != IR.VALID_MARKET_WIDE_HALT_SESSION
    assert c.state in (IR.REJECTED_SOURCE_ERROR, IR.REJECTED_OTHER_DATA_DEFECT)


def test_source_errors_are_kept_separate_from_market_structure():
    """A provider outage says nothing about the market and must not pollute the
    data-defect bucket."""
    assert _classify([], state="REJECTED_PROVIDER_ERROR").state == \
        IR.REJECTED_SOURCE_ERROR
    assert _classify([], state="REJECTED_NORMALIZATION_ERROR").state == \
        IR.REJECTED_SOURCE_ERROR


def test_unexpected_timestamps_block_halt_recovery():
    c = _classify([(9, 35), (9, 40)], unexpected=[_et(HALT_DATE, 16, 30)])
    assert c.state == IR.REJECTED_OTHER_DATA_DEFECT


def test_admitted_sessions_are_continuous_and_closed_days_are_not_sessions():
    assert _classify([], state="ADMITTED").state == IR.VALID_CONTINUOUS_SESSION
    c = IR.classify_session(symbol="SPY", market_date=date(2020, 3, 14),
                            timeframe="5min", session2_state="NOT_A_TRADING_SESSION",
                            session_type=C.SESSION_MARKET_CLOSED)
    assert c.state == IR.NOT_A_TRADING_SESSION


# ═══════════════════════════════════════════════════════════════════════════
# §19/§20 feature segmentation
# ═══════════════════════════════════════════════════════════════════════════
def _bar_at(start, close: float) -> IntradayBar:
    return IntradayBar(symbol="SPY", timeframe="5min", bar_start_at=start,
                       open=close, high=close + 0.5, low=close - 0.5, close=close,
                       volume=1000, adjustment_state="split_adjusted")


def _bar(d: date, hh: int, mm: int, close: float) -> IntradayBar:
    return _bar_at(_et(d, hh, mm), close)


def _seq(d: date, hh: int, mm: int, n: int, first_close: float):
    """n contiguous 5-minute bars starting at hh:mm."""
    base = _et(d, hh, mm)
    return [_bar_at(base + timedelta(minutes=5 * i), first_close + i)
            for i in range(n)]


def _halted_session_bars():
    """09:30 observed, 09:35 + 09:40 absent (halt), 09:45.. observed."""
    return [_bar(HALT_DATE, 9, 30, 100.0)] + _seq(HALT_DATE, 9, 45, 5, 101.0)


def test_segmentation_splits_at_the_discontinuity():
    segs = IR.segment_bars(_halted_session_bars())
    assert len(segs) == 2
    assert len(segs[0]) == 1 and len(segs[1]) == 5


def test_the_frozen_session2_engine_already_refuses_a_bridged_window():
    """Stated accurately rather than flatteringly: Session 2 ALREADY prevents
    this. `features._contiguous` checks adjacency in TIME, so a window spanning
    the halt returns explicit absence. Session 3 did not close a leak here, and
    claiming otherwise would misrepresent where the guarantee lives."""
    from portfolio_automation.intraday_lab import features as F

    bars = _halted_session_bars()
    # index 3 is 09:55; its 4-bar window is 09:30, 09:45, 09:50, 09:55 — bridged.
    assert F.compute_return_nbar(bars, 3, 3, dataset_id="d", fingerprint="f",
                                 manifest_fingerprint="m") is None


def test_segmented_features_agree_with_the_frozen_engine_across_a_halt():
    """Segmentation is an INDEPENDENT expression of the same invariant, and the
    structure Session 3.1 needs. Both paths must agree exactly."""
    from portfolio_automation.intraday_lab import pipeline as PL

    bars = _halted_session_bars()
    seg = IR.segmented_features(bars, dataset_id="d", fingerprint="f",
                                manifest_fingerprint="m", lookback=3)
    unseg = PL.features_from_bars(bars, dataset_id="d", fingerprint="f",
                                  manifest_fingerprint="m", lookback=3)
    assert [v.to_dict() for v in seg] == [v.to_dict() for v in unseg]

    starts = {v.event_at for v in seg}
    assert _et(HALT_DATE, 9, 30) not in starts     # pre-halt bar anchors nothing
    # Rolling state RESETS: the 5-bar post-halt segment yields features only
    # once three contiguous bars exist after the reopen.
    assert len(seg) == 2
    assert min(starts) == _et(HALT_DATE, 9, 45) + timedelta(minutes=15)


def test_segmentation_of_a_continuous_session_is_a_single_segment():
    bars = _seq(HALT_DATE, 10, 0, 6, 100.0)
    assert len(IR.segment_bars(bars)) == 1


def test_segmented_features_reuse_the_frozen_session2_algorithm():
    """Session 3 decides which bars are consecutive; Session 2 decides what a
    feature means. The values must be identical on a continuous session."""
    from portfolio_automation.intraday_lab import pipeline as PL

    bars = _seq(HALT_DATE, 10, 0, 6, 100.0)
    a = PL.features_from_bars(bars, dataset_id="d", fingerprint="f",
                              manifest_fingerprint="m", lookback=3)
    b = IR.segmented_features(bars, dataset_id="d", fingerprint="f",
                              manifest_fingerprint="m", lookback=3)
    assert [v.to_dict() for v in a] == [v.to_dict() for v in b]


# ═══════════════════════════════════════════════════════════════════════════
# §14/§21 no synthetic bars, temporal contract preserved
# ═══════════════════════════════════════════════════════════════════════════
def test_no_code_path_fills_interpolates_or_pads_a_halt():
    """A halted interval is absence plus an authoritative event. Manufacturing a
    price across it would destroy the very thing this session measures.

    Parsed with `ast`, not text: a plain-string scan also matches the PROSE
    stating the rule, and the first version of this test failed on its own
    docstring — the same false-positive class this lab keeps having to remove.
    What is asserted is that no fill/interpolate operation is ever CALLED, and
    that Session 3 constructs no bars at all.
    """
    import ast
    import pathlib

    banned = {"ffill", "fillna", "interpolate", "bfill", "reindex", "resample",
              "pad", "asfreq"}
    for mod in (IR, PA):
        tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))
        called, constructed = set(), 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = (fn.attr if isinstance(fn, ast.Attribute)
                        else fn.id if isinstance(fn, ast.Name) else None)
                if name:
                    called.add(name)
                    if name == "IntradayBar":
                        constructed += 1
        assert not (called & banned), \
            f"{mod.__name__} calls a fill/resample operation: {called & banned}"
        assert constructed == 0, (
            f"{mod.__name__} constructs {constructed} bar(s); Session 3 only "
            f"ever reads bars produced by the frozen Session 2 chain")


def test_derived_view_preserves_every_temporal_field_exactly():
    bars = _halted_session_bars()
    c = _classify([(9, 35), (9, 40)])
    payload = IR.irregular_view_payload(
        classification=c, source_manifest_fingerprint="m",
        source_dataset_fingerprint="d", raw_content_fingerprints=["r"],
        calendar_identity={"exchange": "XNYS"}, bars=bars)
    assert len(payload["observed_bars"]) == len(bars)      # nothing added
    for row, bar in zip(payload["observed_bars"], ST.bars_to_rows(bars)):
        assert row["bar_start_at"] == bar["bar_start_at"]
        assert row["bar_end_at"] == bar["bar_end_at"]
        assert row["known_at"] == bar["known_at"]
        assert (row["open"], row["high"], row["low"], row["close"],
                row["volume"]) == (bar["open"], bar["high"], bar["low"],
                                   bar["close"], bar["volume"])


def test_derived_view_identity_binds_to_registry_and_policy(tmp_path):
    bars = _halted_session_bars()
    c = _classify([(9, 35), (9, 40)])
    kw = dict(classification=c, source_manifest_fingerprint="m",
              source_dataset_fingerprint="d", raw_content_fingerprints=["r"],
              calendar_identity={"exchange": "XNYS"}, bars=bars)
    base = IR.irregular_view_payload(**kw)
    other = {**base, "registry_fingerprint": "different"}
    assert ST.content_hash(base) != ST.content_hash(other)
    other2 = {**base, "policy_fingerprint": "different"}
    assert ST.content_hash(base) != ST.content_hash(other2)
    other3 = {**base, "source_manifest_fingerprint": "different"}
    assert ST.content_hash(base) != ST.content_hash(other3)


# ═══════════════════════════════════════════════════════════════════════════
# Population accounting + prevalence
# ═══════════════════════════════════════════════════════════════════════════
def test_mwcb_prevalence_is_exact_and_needs_no_provider_calls():
    """The registry is complete for the window, so halt prevalence is a
    closed-form fact about the calendar rather than a sampled estimate."""
    p = PA.mwcb_prevalence()
    assert p["registry_complete_for_window"] is True
    assert p["mwcb_dates"] == ["2020-03-09", "2020-03-12", "2020-03-16",
                               "2020-03-18"]
    assert p["certified_trading_dates"] > 2000
    assert 0.0 < p["mwcb_share_of_symbol_sessions_pct"] < 1.0


def test_audit_budget_is_preflighted_and_refuses_an_oversized_run():
    """A mid-run governor refusal returns [] and would be recorded as absent
    market data — in the audit whose subject is absent market data."""
    ok = PA.audit_budget_headroom(chunks=PA.sample_windows() + PA.mwcb_windows())
    assert ok["fits"] is True and ok["planned_calls"] < ok["call_budget"]
    huge = PA.audit_budget_headroom(chunks=PA.sample_windows() * 10)
    assert huge["fits"] is False and "budget" in huge["reason"]


def test_sample_windows_are_deterministic_and_provider_compatible():
    a, b = PA.sample_windows(), PA.sample_windows()
    assert a == b, "sampling must be reproducible, never random"
    years = {y for y, _, _ in a}
    assert years == set(range(2017, 2027)), "every year must be represented"
    for _, lo, hi in a:
        n = len(PA.certified_sessions_in(lo, hi))
        assert n <= PA.PROVIDER_MAX_SESSIONS_PER_CALL, \
            "a window wider than the provider cap would silently return the tail"


def test_every_population_state_is_accounted_for():
    classifications = [
        _classify([], state="ADMITTED"),
        _classify([(9, 35), (9, 40)]),
        _classify([(9, 30)]),
        _classify([], state="REJECTED_PROVIDER_ERROR"),
        _classify([(9, 35)], state="REJECTED_OFF_GRID"),
    ]
    counts = PA._count_states(classifications)
    assert sum(counts.values()) == len(classifications)
    assert counts[IR.VALID_CONTINUOUS_SESSION] == 1
    assert counts[IR.VALID_MARKET_WIDE_HALT_SESSION] == 1
    assert counts[IR.REJECTED_UNEXPLAINED_GAP] == 1
    assert counts[IR.REJECTED_SOURCE_ERROR] == 1
    assert counts[IR.REJECTED_OTHER_DATA_DEFECT] == 1


# ═══════════════════════════════════════════════════════════════════════════
# §30/§31 metric semantics
# ═══════════════════════════════════════════════════════════════════════════
def test_a_reopening_jump_is_never_counted_as_a_five_minute_return():
    """A 20-minute discontinuity is economically important and is NOT an
    ordinary 5-minute return. Folding it into realized vol would claim equal
    spacing that did not exist."""
    bars = [_bar(HALT_DATE, 9, 30, 100.0), _bar(HALT_DATE, 9, 45, 90.0),
            _bar(HALT_DATE, 9, 50, 90.5), _bar(HALT_DATE, 9, 55, 91.0)]
    m = PA.session_metrics("SPY", HALT_DATE, bars, IR.VALID_MARKET_WIDE_HALT_SESSION)
    assert m.segments == 2
    assert m.discontinuity_return == pytest.approx(90.0 / 100.0 - 1.0)
    # The -10% jump must NOT appear as a within-segment step.
    assert m.largest_step_return < 0.02
    assert m.within_segment_realized_vol < 0.02
    assert m.halt_minutes == pytest.approx(10.0)


def test_continuous_session_metrics_have_no_discontinuity():
    bars = _seq(HALT_DATE, 10, 0, 6, 100.0)
    m = PA.session_metrics("SPY", HALT_DATE, bars, IR.VALID_CONTINUOUS_SESSION)
    assert m.segments == 1
    assert m.discontinuity_return is None and m.halt_minutes is None


# ═══════════════════════════════════════════════════════════════════════════
# Governance
# ═══════════════════════════════════════════════════════════════════════════
def test_session3_never_enables_strategy_validation():
    assert IR.policy_provenance()["strategy_validation_allowed"] is False
    audit = {"accounting_exact": True, "counts": {IR.VALID_MARKET_WIDE_HALT_SESSION: 8,
                                                  IR.REJECTED_SOURCE_ERROR: 0},
             "comparison": {"n_continuous": 5, "n_halt": 8},
             "exact_mwcb_prevalence": {"registry_complete_for_window": True}}
    st = PA.session3_0_status(audit, root=".")
    assert st["strategy_validation_allowed"] is False
    assert st["status"] in (PA.SESSION_3_0_POLICY_READY, PA.SESSION_3_0_LIMITED)


def test_session3_status_is_not_the_strategy_flag():
    """Reusing strategy_validation_allowed as a completion marker is how a
    governance gate loses its meaning."""
    assert PA.SESSION_3_0_POLICY_READY != "strategy_validation_allowed"
    audit = {"accounting_exact": False, "counts": {}, "comparison":
             {"n_continuous": 0, "n_halt": 0}, "exact_mwcb_prevalence": {}}
    st = PA.session3_0_status(audit, root=".")
    assert st["status"] == PA.SESSION_3_0_LIMITED
    assert st["session_3_1_gate"] == PA.SESSION_3_1_NO_GO
    assert st["blockers"]


def test_session3_writes_only_into_the_historical_research_area(tmp_path):
    audit = {"accounting_exact": True, "counts": {IR.VALID_CONTINUOUS_SESSION: 1},
             "rates": {IR.VALID_CONTINUOUS_SESSION: 100.0},
             "comparison": {"n_continuous": 1, "n_halt": 0, "metrics": {},
                            "discontinuity_returns": [], "halt_minutes": [],
                            "note": "n/a"},
             "exact_mwcb_prevalence": PA.mwcb_prevalence(),
             "policy_id": IR.POLICY_ID, "registry_version": IR.MWCB_REGISTRY_VERSION,
             "registry_fingerprint": IR.registry_fingerprint(),
             "metric_definitions_version": IR.METRIC_DEFINITIONS_VERSION,
             "symbols": ["SPY"], "coverage_note": "n/a",
             "accounted_symbol_sessions": 1,
             "requested_certified_symbol_sessions": 1}
    st = {"status": PA.SESSION_3_0_POLICY_READY,
          "session_3_1_gate": PA.SESSION_3_1_GO,
          "research_population_policy": PA.POLICY_HALT_AWARE_PRIMARY,
          "policy_rationale": "n/a"}
    written = PA.write_session3_artifacts(audit, st, root=str(tmp_path))
    for p in written:
        assert "outputs/backtest/intraday/session3" in p
        assert "outputs/latest" not in p
    assert {p.relative_to(tmp_path).parts[0] for p in tmp_path.rglob("*") if p.is_file()} \
        == {"outputs"}
