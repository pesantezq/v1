"""Focused tests for the governed VS-002 production evidence runner.

Every test is offline: acquisition goes through an injected strict-live client
or a monkeypatched ``urllib.request.urlopen``. No real network, no credential,
no VS-002 execution.
"""
from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from portfolio_automation.vs002_evidence import production_runner as PR
from portfolio_automation.vs002_evidence import contracts as C
from portfolio_automation.vs002_evidence import consumer as CON
from portfolio_automation.vs002_evidence import readiness as R

from tests.test_vs002_evidence import (
    _db, _scan_dates, fixed_clock, SyntheticAdjustedProvider)

_ARTIFACTS = {"signals.json", "returns.json", "bars.json", "bars_raw.json",
              "bars_snapshots.json", "manifest.json"}


class FakeStrictClient:
    """Mimics FMPClient's strict-live surface: returns synthetic rows and calls
    on_attempt(symbol) immediately before 'the request' — no cache, no retry."""

    def __init__(self, *, fail=frozenset(), empty=frozenset()):
        self._rows = SyntheticAdjustedProvider()._rows
        self._fail = set(fail)
        self._empty = set(empty)
        self.calls: list[str] = []

    def get_dividend_adjusted_bars_strict_live(self, symbol, *, years=5,
                                               on_attempt=None):
        sym = symbol.upper()
        if on_attempt is not None:
            on_attempt(sym)
        self.calls.append(sym)
        if sym in self._fail:
            from fmp_client import FMPError
            raise FMPError(f"synthetic transport failure for {sym}")
        if sym in self._empty:
            return []
        return self._rows(sym)


def _seed(tmp_path: Path, scans: int = 12):
    if not (tmp_path / "data" / "portfolio.db").exists():
        _db(tmp_path, scans=_scan_dates(scans))


def _run(tmp_path, *, client=None, scans=12, run_id="run1", **kw):
    _seed(tmp_path, scans)
    return PR.run(tmp_path, client=client or FakeStrictClient(),
                  credential_present=lambda: True, clock=fixed_clock(),
                  code_sha="testsha", run_id=run_id, **kw)


# ===========================================================================
# G12 — the acquisition contract
# ===========================================================================

def test_1_frozen_universe_is_21_plus_spy():
    assert len(C.FROZEN_UNIVERSE) == 21
    assert C.BENCHMARK == "SPY"
    plan = PR.acquisition_plan()
    assert plan[0] == "SPY" and len(plan) == 22 and len(set(plan)) == 22


def test_2_spy_is_request_one(tmp_path):
    res = _run(tmp_path)
    assert res.result == PR.PASS, res.exact_blockers
    assert res.acquisition_order[0] == "SPY"
    assert res.attempts[0]["symbol"] == "SPY"


def test_3_complete_mission_is_exactly_22_requests(tmp_path):
    res = _run(tmp_path)
    assert res.attempted_http_requests == 22
    assert len(res.acquisition_order) == 22
    assert len(set(res.acquisition_order)) == 22


def test_4_no_23rd_request_is_possible():
    plan = PR.acquisition_plan()
    acq = PR.StrictLiveAcquirer(FakeStrictClient(), plan)
    for sym in plan:
        acq.get_historical_prices_dividend_adjusted(sym)
    assert acq.attempted_http_requests == 22
    with pytest.raises(PR.StrictLiveAcquisitionError):
        acq.get_historical_prices_dividend_adjusted(plan[1])   # a 23rd request
    assert acq.attempted_http_requests == 22


def test_5_one_failing_symbol_stops_later_acquisition(tmp_path):
    res = _run(tmp_path, client=FakeStrictClient(fail={"MSFT"}))
    assert res.result == PR.FAIL_PROVIDER
    assert res.acquisition_order[-1] == "MSFT"       # nothing acquired after it
    assert "MSFT" in res.acquisition_order


def test_6_spy_failure_stops_after_request_one(tmp_path):
    res = _run(tmp_path, client=FakeStrictClient(fail={"SPY"}))
    assert res.result == PR.FAIL_PROVIDER
    assert res.acquisition_order == ["SPY"]
    assert res.attempted_http_requests == 1


def test_12_duplicate_symbol_acquisition_impossible():
    acq = PR.StrictLiveAcquirer(FakeStrictClient(), PR.acquisition_plan())
    acq.get_historical_prices_dividend_adjusted("SPY")
    with pytest.raises(PR.StrictLiveAcquisitionError, match="more than once"):
        acq.get_historical_prices_dividend_adjusted("SPY")


def test_spy_first_ordering_is_enforced():
    acq = PR.StrictLiveAcquirer(FakeStrictClient(), PR.acquisition_plan())
    with pytest.raises(PR.StrictLiveAcquisitionError, match="must be acquired first"):
        acq.get_historical_prices_dividend_adjusted("AAPL")


def test_symbol_outside_plan_is_refused():
    acq = PR.StrictLiveAcquirer(FakeStrictClient(), PR.acquisition_plan())
    acq.get_historical_prices_dividend_adjusted("SPY")
    with pytest.raises(PR.StrictLiveAcquisitionError, match="not in the frozen"):
        acq.get_historical_prices_dividend_adjusted("ZZZZ")


# ===========================================================================
# G12 — strict-live FMP surface (real FMPClient, monkeypatched urlopen)
# ===========================================================================

class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")
    def read(self):
        return self._b
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _client(tmp_path, *, budget=230, retry_max=1):
    from fmp_client import FMPClient
    return FMPClient(api_key="TEST_KEY", daily_budget=budget, retry_max=retry_max,
                     cache_dir=tmp_path / "fmp_cache")


_GOOD = [{"date": "2020-01-02", "close": 100.0, "adjClose": 100.0, "volume": 1000}]


def test_7_no_retry_on_transient_http_failure(tmp_path, monkeypatch):
    calls = []
    def boom(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 500, "err", {}, None)
    monkeypatch.setattr("urllib.request.urlopen", boom)
    from fmp_client import FMPError
    cl = _client(tmp_path, retry_max=3)          # the flagged default
    with pytest.raises(FMPError):
        cl.get_dividend_adjusted_bars_strict_live("SPY")
    assert len(calls) == 1               # ONE attempt despite retry_max=3


def test_8_stale_cache_plus_live_failure_still_fails(tmp_path, monkeypatch):
    cl = _client(tmp_path)
    cl._cache.set("hist_divadj_SPY_5y", _GOOD)    # cache present
    def boom(req, timeout=None):
        raise urllib.error.URLError("down")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    from fmp_client import FMPError
    with pytest.raises(FMPError):                 # must NOT serve the cache
        cl.get_dividend_adjusted_bars_strict_live("SPY")


def test_9_fresh_cache_does_not_satisfy_strict_live(tmp_path, monkeypatch):
    cl = _client(tmp_path)
    cl._cache.set("hist_divadj_SPY_5y", [{"date": "1999-01-01", "close": 1.0,
                                          "adjClose": 1.0, "volume": 1}])
    live = [{"date": "2020-01-02", "close": 200.0, "adjClose": 200.0,
             "volume": 5}]
    calls = []
    def ok(req, timeout=None):
        calls.append(1)
        return _Resp(live)
    monkeypatch.setattr("urllib.request.urlopen", ok)
    out = cl.get_dividend_adjusted_bars_strict_live("SPY")
    assert len(calls) == 1 and out == live        # live, not the cached row


def test_10_budget_exhaustion_fails_not_stale(tmp_path, monkeypatch):
    cl = _client(tmp_path, budget=3)
    cl._cache.set("hist_divadj_SPY_5y", _GOOD)
    cl._counter.increment(3)                      # at budget
    calls = []
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: calls.append(1) or _Resp(_GOOD))
    from fmp_client import CallBudgetExceeded
    with pytest.raises(CallBudgetExceeded):
        cl.get_dividend_adjusted_bars_strict_live("SPY")
    assert calls == []                            # no outbound request at all


def test_11_endpoint_is_the_authorized_one_only(tmp_path, monkeypatch):
    captured = {}
    def spy_once(self, endpoint, params, *, base_url):
        captured["endpoint"] = endpoint
        captured["base_url"] = base_url
        self._counter.increment()
        return _GOOD
    monkeypatch.setattr("fmp_client.FMPClient._raw_get_once", spy_once)
    cl = _client(tmp_path)
    cl.get_dividend_adjusted_bars_strict_live("SPY")
    from fmp_client import _EP_HISTORICAL_DIVADJ, FMP_STABLE_BASE_URL
    assert captured["endpoint"] == _EP_HISTORICAL_DIVADJ
    assert captured["base_url"] == FMP_STABLE_BASE_URL
    assert C.AUTHORIZED_ENDPOINT.endswith(_EP_HISTORICAL_DIVADJ)


def test_11b_non_list_response_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr("fmp_client.FMPClient._raw_get_once",
                        lambda self, e, p, *, base_url: {"historical": _GOOD})
    from fmp_client import FMPError
    cl = _client(tmp_path)
    with pytest.raises(FMPError, match="not a list"):
        cl.get_dividend_adjusted_bars_strict_live("SPY")


# ===========================================================================
# G12 — build wiring, output freshness, validation, readiness
# ===========================================================================

def test_13_builder_receives_the_dividend_adjusted_provider(tmp_path, monkeypatch):
    seen = {}
    orig = PR.B.build
    def spy(root, **kw):
        seen["provider"] = kw.get("bar_provider")
        return orig(root, **kw)
    monkeypatch.setattr(PR.B, "build", spy)
    _run(tmp_path)
    prov = seen["provider"]
    assert isinstance(prov, PR.B.FMPDividendAdjustedProvider)
    assert prov.endpoint == C.AUTHORIZED_ENDPOINT
    assert isinstance(prov._client, PR.StrictLiveAcquirer)


def test_14_builder_receives_a_fresh_per_run_output_path(tmp_path, monkeypatch):
    seen = {}
    orig = PR.B.build
    def spy(root, **kw):
        seen["out_rel"] = kw.get("out_rel")
        return orig(root, **kw)
    monkeypatch.setattr(PR.B, "build", spy)
    _run(tmp_path, run_id="fresh1")
    assert ".staging-fresh1" in seen["out_rel"]
    assert seen["out_rel"] != PR.B.DEFAULT_OUT_REL


def test_15a_preexisting_staging_blocks_before_network(tmp_path):
    _seed(tmp_path)
    staging = tmp_path / PR.B.DEFAULT_OUT_REL / ".staging-collide"
    staging.mkdir(parents=True)
    client = FakeStrictClient()
    res = PR.run(tmp_path, client=client, credential_present=lambda: True,
                 clock=fixed_clock(), code_sha="x", run_id="collide")
    assert res.result == PR.BLOCKED_PREFLIGHT
    assert any("staging" in b for b in res.exact_blockers)
    assert client.calls == []                      # no acquisition happened


def test_15b_existing_final_package_is_never_overwritten(tmp_path):
    res1 = _run(tmp_path, run_id="first")
    assert res1.result == PR.PASS and res1.package_published
    pkg = tmp_path / res1.package_path
    manifest = (pkg / "manifest.json").read_bytes()
    # a second identical run yields the same package_id -> destination exists
    res2 = _run(tmp_path, run_id="second")
    assert res2.package_id == res1.package_id
    assert res2.package_published is False
    assert res2.package_overwrote_existing is False
    assert (pkg / "manifest.json").read_bytes() == manifest   # unchanged


def test_16_consumer_validation_passes_on_the_happy_path(tmp_path):
    assert _run(tmp_path, run_id="v1").consumer_validation == "PASS"


def test_17_snapshotinvalid_prevents_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(PR.CON, "validate",
                        lambda p: (_ for _ in ()).throw(CON.SnapshotInvalid("boom")))
    res = _run(tmp_path, run_id="v2")
    assert res.result == PR.FAIL_PACKAGE_VALIDATION
    assert res.package_published is False
    assert not (tmp_path / PR.B.DEFAULT_OUT_REL / "packages").exists()
    assert not list((tmp_path / PR.B.DEFAULT_OUT_REL).glob(".staging-*"))


def test_18_readiness_receives_a_validated_snapshot(tmp_path, monkeypatch):
    seen = {}
    orig = PR.R.evaluate
    def spy(snap):
        seen["type"] = type(snap).__name__
        return orig(snap)
    monkeypatch.setattr(PR.R, "evaluate", spy)
    _run(tmp_path)
    assert seen["type"] == "ValidatedSnapshot"


def test_19_ready_with_cohorts_is_pass(tmp_path):
    res = _run(tmp_path, scans=12)
    assert res.result == PR.PASS
    assert res.readiness_status == R.READY
    assert (res.cohort_count or 0) >= C.MIN_COHORTS
    assert res.readiness_reasons == []


def test_20_only_cohort_deficit_is_not_ready(tmp_path):
    res = _run(tmp_path, scans=3)
    assert res.result == PR.NOT_READY
    assert res.exact_blockers == ["INSUFFICIENT_NON_OVERLAPPING_COHORTS"]
    assert (res.cohort_count or 0) < C.MIN_COHORTS
    assert res.package_published is True           # the package is still valid


def test_21_other_readiness_reason_is_fail_contract(tmp_path, monkeypatch):
    bad = R.Readiness(status=R.NOT_READY,
                      reasons=("benchmark SPY absent from the return panel",),
                      detail={})
    monkeypatch.setattr(PR.R, "evaluate", lambda snap: bad)
    res = _run(tmp_path)
    assert res.result == PR.FAIL_READINESS_CONTRACT
    assert "benchmark SPY absent from the return panel" in res.exact_blockers


def test_22_no_db_write(tmp_path):
    _seed(tmp_path)
    db = tmp_path / PR.B.DEFAULT_DB_REL
    before = (db.stat().st_size, db.stat().st_mtime_ns)
    PR.run(tmp_path, client=FakeStrictClient(), credential_present=lambda: True,
           clock=fixed_clock(), code_sha="x", run_id="nodb")
    assert (db.stat().st_size, db.stat().st_mtime_ns) == before


def test_23_credentials_are_not_printed(tmp_path, capsys):
    res = _run(tmp_path)
    blob = json.dumps(res.to_dict())
    assert "FMP_API_KEY" not in blob and "apikey" not in blob


def test_24_25_package_has_exactly_the_declared_artifacts(tmp_path):
    res = _run(tmp_path)
    pkg = tmp_path / res.package_path
    names = {p.name for p in pkg.iterdir()}
    assert names == _ARTIFACTS                     # no extra runner metadata


def test_preflight_blocks_missing_credential(tmp_path):
    _seed(tmp_path)
    client = FakeStrictClient()
    res = PR.run(tmp_path, client=client, credential_present=lambda: False,
                 clock=fixed_clock(), code_sha="x", run_id="nocred")
    assert res.result == PR.BLOCKED_PREFLIGHT
    assert any("credential" in b for b in res.exact_blockers)
    assert client.calls == []


def test_preflight_blocks_missing_signal_db(tmp_path):
    # no _seed -> no data/portfolio.db
    res = PR.run(tmp_path, client=FakeStrictClient(), credential_present=lambda: True,
                 clock=fixed_clock(), code_sha="x", run_id="nodb2")
    assert res.result == PR.BLOCKED_PREFLIGHT
    assert any("signal database" in b for b in res.exact_blockers)


class _BudgetRefusingClient:
    def can_admit(self, n):
        return False
    def get_dividend_adjusted_bars_strict_live(self, *a, **k):
        raise AssertionError("must not acquire when the budget preflight refuses")


def test_preflight_blocks_when_budget_cannot_admit_mission(tmp_path):
    _seed(tmp_path)
    client = _BudgetRefusingClient()
    res = PR.run(tmp_path, client=client, credential_present=lambda: True,
                 clock=fixed_clock(), code_sha="x", run_id="budget")
    assert res.result == PR.BLOCKED_PREFLIGHT
    assert any("budget" in b for b in res.exact_blockers)
    assert res.attempted_http_requests == 0


def test_exit_codes_are_deterministic():
    assert PR.EXIT_CODES[PR.PASS] == 0
    assert PR.EXIT_CODES[PR.NOT_READY] == 10
    assert PR.EXIT_CODES[PR.BLOCKED_PREFLIGHT] == 20
    assert len(set(PR.EXIT_CODES.values())) == len(PR.EXIT_CODES)
