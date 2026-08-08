"""Intraday Strategy Lab — Session 1 foundation tests.

Adversarial by design: each leakage test encodes a mistake a later session
could plausibly make. If a protection is removed, a test here must go red.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from portfolio_automation.intraday_lab import models as M
from portfolio_automation.intraday_lab import validation as V
from portfolio_automation.intraday_lab import data as D

ET = ZoneInfo("America/New_York")
UTC = timezone.utc


def _bar(symbol="SPY", tf="5min", start=None, o=100.0, h=101.0, lo=99.0, c=100.5,
         vol=1000.0, **kw):
    start = start or datetime(2026, 8, 3, 13, 30, tzinfo=UTC)
    return M.IntradayBar(symbol=symbol, timeframe=tf, bar_start_at=start,
                         open=o, high=h, low=lo, close=c, volume=vol, **kw)


# ===========================================================================
# Bar model
# ===========================================================================
def test_valid_bar_accepted():
    assert _bar().close == 100.5


@pytest.mark.parametrize("kw", [
    {"h": 98.0},                       # high < low
    {"o": 105.0},                      # open above high
    {"c": 98.0, "lo": 99.0},           # close below low
    {"vol": -1.0},                     # negative volume
    {"o": float("nan")},               # non-finite
    {"c": 0.0},                        # non-positive price
])
def test_malformed_ohlcv_rejected(kw):
    with pytest.raises(M.BarValidationError):
        _bar(**kw)


def test_unsupported_timeframe_rejected():
    with pytest.raises(M.BarValidationError):
        _bar(tf="7min")


def test_1min_is_not_a_supported_timeframe():
    """The configured account returns HTTP 402 for 1min. Declaring it would let
    a later session request data this account cannot serve."""
    assert "1min" not in M.TIMEFRAMES
    assert "5min" in M.TIMEFRAMES


def test_bars_are_immutable():
    with pytest.raises(Exception):
        _bar().close = 1.0


# ===========================================================================
# Timezone discipline
# ===========================================================================
def test_naive_datetime_is_rejected_not_assumed_utc():
    """Assuming UTC would shift every provider bar by 4-5 hours."""
    with pytest.raises(M.TemporalViolation):
        _bar(start=datetime(2026, 8, 3, 9, 30))


def test_eastern_to_utc_preserves_the_instant():
    et = datetime(2026, 8, 3, 9, 30, tzinfo=ET)
    bar = _bar(start=et)
    assert bar.bar_start_at == et
    assert bar.bar_start_at.hour == 13          # EDT = UTC-4


def test_dst_boundary_uses_the_calendar_not_a_fixed_offset():
    """November (EST, -5) and August (EDT, -4) must differ."""
    summer = D.parse_provider_timestamp("2026-08-03 09:30:00")
    winter = D.parse_provider_timestamp("2026-11-30 09:30:00")
    assert summer.hour == 13
    assert winter.hour == 14


# ===========================================================================
# Temporal model / point-in-time
# ===========================================================================
def test_known_at_is_never_before_bar_end():
    bar = _bar()
    assert bar.known_at >= bar.bar_end_at


def test_known_at_is_not_retrieved_at():
    """Fetching a 2017 bar in 2026 does not make it knowable only in 2026."""
    bar = _bar(retrieved_at=datetime(2026, 8, 8, tzinfo=UTC))
    assert bar.known_at.year == 2026 and bar.known_at.month == 8 and bar.known_at.day == 3
    assert bar.known_at != bar.retrieved_at


def test_incomplete_bar_is_not_knowable_mid_interval():
    """A decision at 10:02 cannot use the 10:00-10:05 bar's close."""
    bar = _bar(start=datetime(2026, 8, 3, 14, 0, tzinfo=UTC))   # 10:00 ET
    assert not bar.is_known_at(datetime(2026, 8, 3, 14, 2, tzinfo=UTC))
    assert bar.is_known_at(datetime(2026, 8, 3, 14, 10, tzinfo=UTC))


def test_future_bar_is_excluded_from_admissible_inputs():
    early = _bar(start=datetime(2026, 8, 3, 14, 0, tzinfo=UTC))
    later = _bar(start=datetime(2026, 8, 3, 15, 0, tzinfo=UTC))
    admissible = V.admissible_inputs([early, later],
                                     datetime(2026, 8, 3, 14, 30, tzinfo=UTC))
    assert admissible == [early]


def test_future_feature_is_rejected():
    feat = M.FeatureObservation(
        feature_id="news_sentiment", value=0.9,
        event_at=datetime(2026, 8, 3, 16, 0, tzinfo=UTC),
        known_at=datetime(2026, 8, 3, 16, 5, tzinfo=UTC))
    assert V.admissible_inputs([feat], datetime(2026, 8, 3, 14, 0, tzinfo=UTC)) == []


def test_feature_known_before_it_happened_is_rejected():
    with pytest.raises(M.TemporalViolation):
        M.FeatureObservation(
            feature_id="x", value=1,
            event_at=datetime(2026, 8, 3, 16, 0, tzinfo=UTC),
            known_at=datetime(2026, 8, 3, 15, 0, tzinfo=UTC))


def test_eod_aggregate_is_not_available_to_a_morning_decision():
    """Full-session volume is only knowable after the close."""
    full_day_volume = M.FeatureObservation(
        feature_id="session_total_volume", value=8.1e7,
        event_at=datetime(2026, 8, 3, 20, 0, tzinfo=UTC),      # 16:00 ET close
        known_at=datetime(2026, 8, 3, 20, 0, tzinfo=UTC))
    morning = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)          # 10:00 ET
    assert V.admissible_inputs([full_day_volume], morning) == []
    with pytest.raises(M.TemporalViolation):
        V.assert_no_lookahead([full_day_volume], morning)


def test_hindsight_regime_label_is_not_intraday_eligible():
    regime = M.FeatureObservation(
        feature_id="daily_regime_label", value="risk_on",
        event_at=datetime(2026, 8, 3, 20, 0, tzinfo=UTC),
        known_at=datetime(2026, 8, 3, 20, 0, tzinfo=UTC))
    assert V.admissible_inputs([regime], datetime(2026, 8, 3, 15, 0, tzinfo=UTC)) == []


def test_input_without_known_at_is_refused_not_assumed_safe():
    """Defaulting to 'allow' is how leakage enters."""
    with pytest.raises(M.TemporalViolation):
        V.admissible_inputs([{"close": 1.0}], datetime(2026, 8, 3, 14, 0, tzinfo=UTC))


def test_earliest_order_time_forbids_a_fill_before_the_bar_was_known():
    bar = _bar(start=datetime(2026, 8, 3, 14, 0, tzinfo=UTC))
    assert V.earliest_order_time(bar) == bar.known_at
    assert V.earliest_order_time(bar) > bar.bar_start_at


# ===========================================================================
# Canonicalization: ordering + duplicates
# ===========================================================================
def test_out_of_order_input_is_sorted_per_contract():
    """Providers return newest-first; rejecting would refuse valid data."""
    a = _bar(start=datetime(2026, 8, 3, 14, 0, tzinfo=UTC))
    b = _bar(start=datetime(2026, 8, 3, 13, 30, tzinfo=UTC))
    assert [x.bar_start_at for x in V.canonicalize([a, b])] == [b.bar_start_at, a.bar_start_at]


def test_identical_duplicate_is_collapsed():
    a = _bar()
    assert len(V.canonicalize([a, _bar()])) == 1


def test_conflicting_duplicate_raises():
    """Keeping either would make the fingerprint depend on arrival order."""
    with pytest.raises(V.DuplicateBarError):
        V.canonicalize([_bar(c=100.5), _bar(c=100.9)])


# ===========================================================================
# Quality profiling
# ===========================================================================
def test_missing_bars_detected_against_supplied_expectation():
    bars = [_bar(start=datetime(2026, 8, 3, 13, 30, tzinfo=UTC) + timedelta(minutes=5 * i))
            for i in range(70)]
    p = V.profile_session(bars, expected_bars=78)
    assert p["observed_bars"] == 70 and p["missing_bars"] == 8
    assert p["coverage_pct"] == pytest.approx(89.74, abs=0.01)


def test_early_close_shape_is_classified_conservatively():
    bars = [_bar(start=datetime(2025, 11, 28, 14, 30, tzinfo=UTC) + timedelta(minutes=5 * i))
            for i in range(42)]                    # the real 2025-11-28 session
    assert V.profile_session(bars, expected_bars=78)["gap_classification"] == V.GAP_EARLY_CLOSE


def test_severe_shortfall_is_unknown_not_named():
    """OHLCV cannot distinguish a halt from a provider gap; do not guess."""
    bars = [_bar(start=datetime(2026, 8, 3, 13, 30, tzinfo=UTC) + timedelta(minutes=5 * i))
            for i in range(5)]
    assert V.profile_session(bars, expected_bars=78)["gap_classification"] == V.GAP_UNKNOWN


def test_empty_window_reports_zero_not_crash():
    p = V.profile_session([], expected_bars=78)
    assert p["observed_bars"] == 0 and p["coverage_pct"] == 0.0


def test_zero_volume_bars_are_counted_not_fatal():
    assert V.profile_session([_bar(vol=0.0)], expected_bars=1)["zero_volume_bars"] == 1


# ===========================================================================
# Fingerprinting / reproducibility
# ===========================================================================
def test_same_dataset_same_fingerprint_regardless_of_order():
    a = _bar(start=datetime(2026, 8, 3, 13, 30, tzinfo=UTC))
    b = _bar(start=datetime(2026, 8, 3, 13, 35, tzinfo=UTC))
    assert V.dataset_fingerprint([a, b]) == V.dataset_fingerprint([b, a])


@pytest.mark.parametrize("mutation", [
    {"c": 100.6},                                                   # changed bar
    {"symbol": "AAPL"},                                             # symbol set
    {"tf": "15min"},                                                # timeframe
])
def test_meaningful_change_alters_the_fingerprint(mutation):
    assert V.dataset_fingerprint([_bar()]) != V.dataset_fingerprint([_bar(**mutation)])


def test_added_and_removed_bars_alter_the_fingerprint():
    a, b = _bar(), _bar(start=datetime(2026, 8, 3, 13, 35, tzinfo=UTC))
    assert V.dataset_fingerprint([a]) != V.dataset_fingerprint([a, b])


def test_retrieved_at_does_not_affect_the_fingerprint():
    """Otherwise every experiment is irreproducible by construction."""
    assert V.dataset_fingerprint([_bar(retrieved_at=datetime(2026, 8, 8, tzinfo=UTC))]) == \
           V.dataset_fingerprint([_bar(retrieved_at=datetime(2026, 1, 1, tzinfo=UTC))])


def test_manifest_reports_identity_and_adjustment_state():
    m = V.dataset_manifest([_bar(adjustment_state="split_adjusted")], source="fmp")
    assert m["symbols"] == ["SPY"] and m["bar_count"] == 1
    assert m["adjustment_states"] == ["split_adjusted"]


# ===========================================================================
# Provider adapter (fixtures/mocks — no network)
# ===========================================================================
_ROW = {"date": "2026-08-03 09:30:00", "open": 100.0, "high": 101.0,
        "low": 99.0, "close": 100.5, "volume": 1000}


def test_valid_response_normalized_to_utc_bar_open():
    bar = D.normalize_fmp_rows([_ROW], symbol="SPY", timeframe="5min")[0]
    assert bar.bar_start_at == datetime(2026, 8, 3, 13, 30, tzinfo=UTC)
    assert bar.source == "fmp"
    assert bar.adjustment_state == "split_adjusted"
    assert bar.source_endpoint == "/stable/historical-chart/5min"


def test_missing_field_raises_rather_than_dropping_the_row():
    bad = {k: v for k, v in _ROW.items() if k != "volume"}
    with pytest.raises(D.IntradayDataError):
        D.normalize_fmp_rows([bad], symbol="SPY", timeframe="5min")


def test_malformed_timestamp_raises():
    with pytest.raises(M.BarValidationError):
        D.normalize_fmp_rows([{**_ROW, "date": "not-a-date"}], symbol="SPY",
                             timeframe="5min")


def test_1min_request_is_refused_at_the_adapter():
    with pytest.raises(D.IntradayDataError):
        D.normalize_fmp_rows([_ROW], symbol="SPY", timeframe="1min")


@pytest.mark.parametrize("rows,http,expected", [
    ([_ROW], 200, D.STATUS_OK),
    ([], 200, D.STATUS_NO_DATA),
    (None, 200, D.STATUS_DATA_UNAVAILABLE),
    ({"Error": "x"}, 200, D.STATUS_MALFORMED),
    (None, 402, D.STATUS_NOT_ENTITLED),
    (None, 500, D.STATUS_DATA_UNAVAILABLE),
])
def test_response_states_are_explicit_never_silent_zeros(rows, http, expected):
    assert D.fetch_status(rows, http_status=http) == expected


# ===========================================================================
# Governance / production isolation
# ===========================================================================
def test_no_broker_execution_or_decision_plan_path_in_the_lab():
    import pathlib
    forbidden = ("schwab", "broker", "place_order", "submit_order",
                 "decision_plan.json", "signal_registry")
    pkg = pathlib.Path("portfolio_automation/intraday_lab")
    for path in pkg.glob("*.py"):
        src = path.read_text(encoding="utf-8")
        # strip the module docstring's prose disclaimers before scanning
        body = src.split('"""', 2)[-1]
        for token in forbidden:
            assert token not in body, f"{path.name} references {token!r}"


def test_lab_imports_nothing_from_production_decision_layers():
    import pathlib
    pkg = pathlib.Path("portfolio_automation/intraday_lab")
    for path in pkg.glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(("import ", "from ")):
                assert "decision_engine" not in line
                assert "capital_plan" not in line


# ===========================================================================
# Foundation / provider assessment artifacts
# ===========================================================================
def test_foundation_artifacts_land_in_historical_namespace_only(tmp_path):
    from portfolio_automation.intraday_lab import foundation as F
    paths = F.write_foundation_artifacts(str(tmp_path))
    assert paths and all("backtest" in p for p in paths)
    assert not (tmp_path / "outputs" / "latest").exists(), \
        "research artifacts must never touch the live namespace"


def test_health_separates_source_limitation_from_system_failure():
    """An unentitled account is a correct diagnosis, not a crash."""
    from portfolio_automation.intraday_lab import foundation as F
    assert F.assess_foundation_health()["overall"] == "HEALTHY"


def test_probe_evidence_records_the_1min_entitlement_refusal():
    from portfolio_automation.intraday_lab import foundation as F
    assert any(e["timeframe"] == "1min" and e["http"] == 402 for e in F.PROBE_EVIDENCE)


def test_assessment_states_adjustment_and_timestamp_semantics_with_evidence():
    from portfolio_automation.intraday_lab import foundation as F
    a = F.provider_assessment()
    assert a["timestamp_semantics"] == "BAR_OPEN" and a["timestamp_semantics_evidence"]
    assert a["adjustment_semantics"] == "SPLIT_BACK_ADJUSTED" and a["adjustment_evidence"]
    assert a["registry_status"] == "REGISTERED"


def test_intraday_endpoint_is_registered_and_1min_is_not_declared():
    from fmp_endpoint_registry import REGISTRY
    assert REGISTRY["intraday_chart"]["endpoint"] == "/stable/historical-chart/5min"
    assert REGISTRY["intraday_chart"]["required_daily"] is False
    assert not any("1min" in str(v.get("endpoint", "")) for v in REGISTRY.values())
