"""VS-002 dividend-adjusted PROVIDER-CONTRACT reconciliation (post-B3).

B3 proved the authorized ``/stable/historical-price-eod/dividend-adjusted``
endpoint returns ``adjOpen/adjHigh/adjLow/adjClose/volume`` and NO raw ``close``.
This suite pins the reconciliation: the adjusted endpoint stays authoritative for
returns/eligibility/cohorts, and the dividend-adjustment SEMANTICS are witnessed
independently against the benchmark's companion ``/full`` ``close`` — 22 adjusted
acquisitions + 1 SPY ``/full`` witness, a hard cap of 23. Every fixture is
SYNTHETIC and offline: no network, no credential, no FMP.
"""
from __future__ import annotations

import dataclasses
import json
import urllib.error
from pathlib import Path

import pytest

from portfolio_automation.vs002_evidence import builder as B
from portfolio_automation.vs002_evidence import consumer as CON
from portfolio_automation.vs002_evidence import contracts as C
from portfolio_automation.vs002_evidence import production_runner as PR

from tests.test_vs002_evidence import (
    BENCH, SESSIONS, SYMBOLS, SyntheticAdjustedProvider, _db, _scan_dates,
    fixed_clock)
from tests.test_vs002_production_runner import FakeStrictClient


def _pkg(tmp: Path, *, provider=None, scans: int = 12):
    """Build an adjusted-mode package. The legacy /full archive is deliberately
    absent — adjusted mode never consults it."""
    _db(tmp, scans=_scan_dates(scans))
    manifest = B.build(tmp, code_sha="t", generated_at="2026-09-06T00:00:00Z",
                       bar_provider=provider or SyntheticAdjustedProvider(),
                       bar_clock=fixed_clock())
    return tmp / B.DEFAULT_OUT_REL, manifest


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")
    def read(self):
        return self._b
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


# 20) the old impossible contract cannot come back --------------------------

def test_20_required_provider_fields_do_not_demand_raw_close():
    assert "close" not in C.REQUIRED_PROVIDER_FIELDS
    assert C.REQUIRED_PROVIDER_FIELDS == ("date", "adjClose", "volume")
    assert "close" not in {f.name for f in dataclasses.fields(C.BarRow)}


# 1 + 12 + 13) documented adjusted shape WITHOUT close is accepted; no raw
# close is fabricated anywhere; adjClose is never aliased to a `close`. --------

def test_1_documented_dividend_adjusted_shape_without_close_is_accepted(tmp_path):
    class RealShape(SyntheticAdjustedProvider):
        def fetch(self, symbol):
            # exactly FMP's dividend-adjusted schema: adj* fields, no raw close
            return [{"date": r["date"], "adjOpen": r["adjClose"],
                     "adjHigh": r["adjClose"], "adjLow": r["adjClose"],
                     "adjClose": r["adjClose"], "volume": r["volume"]}
                    for r in self._rows(symbol)]
    root, m = _pkg(tmp_path, provider=RealShape())
    snap = CON.validate(root)
    assert snap.has_bars and snap.bars
    # 12/13: no bar and no snapshot payload carries a raw close
    bars = json.loads((root / "bars.json").read_text())
    assert bars and all("close" not in b for b in bars)
    # returns exist and are the adjusted-close ratio, never a fabricated close
    assert m["risk_return_source"] == "dividend_adjusted_bars"


# 2) missing adjClose refused -----------------------------------------------

def test_2_missing_adjclose_is_refused(tmp_path):
    def drop(sym, rows):
        if sym != "MSFT":
            return rows
        rows = [dict(r) for r in rows]
        rows[3].pop("adjClose")
        return rows
    with pytest.raises(B.BuildError, match="missing required field"):
        _pkg(tmp_path, provider=SyntheticAdjustedProvider(mutate=drop))


# 3) malformed adjusted values refused --------------------------------------

@pytest.mark.parametrize("field, bad", [
    ("adjClose", -1.0), ("adjClose", 0.0), ("adjClose", float("nan")),
    ("adjClose", None), ("volume", None)])
def test_3_malformed_adjusted_values_refused(tmp_path, field, bad):
    def poison(sym, rows):
        if sym != "MSFT":
            return rows
        rows = [dict(r) for r in rows]
        rows[5][field] = bad
        return rows
    with pytest.raises(B.BuildError):
        _pkg(tmp_path, provider=SyntheticAdjustedProvider(mutate=poison))


# 4) the companion witness consumes /full close ------------------------------

def test_4_companion_witness_consumes_full_close(tmp_path):
    root, m = _pkg(tmp_path)
    assert m["companion_endpoint"] == C.COMPANION_ENDPOINT
    assert m["companion_symbol"] == "SPY"
    assert m["witness_result"] == "PASS"
    assert m["witness_overlap"]["sessions"] >= C.WITNESS_MIN_OVERLAP_SESSIONS
    wit = json.loads((root / "bars_witness_raw.json").read_text())
    assert wit["observe_only"] is True
    assert wit["rows"] and all("close" in r for r in wit["rows"])


def test_4b_companion_without_close_fails_the_witness(tmp_path):
    class NoClose(SyntheticAdjustedProvider):
        def fetch_companion(self, symbol):
            return [{k: v for k, v in r.items() if k != "close"}
                    for r in self._rows(symbol)]
    with pytest.raises(B.BuildError, match="missing required field"):
        _pkg(tmp_path, provider=NoClose())


# 5) witness PASS allows the remaining symbols -------------------------------

def test_5_witness_pass_allows_remaining_symbols(tmp_path):
    root, m = _pkg(tmp_path)
    assert set(m["bar_fetched_universe"]) == set(C.FROZEN_UNIVERSE) | {BENCH}
    assert m["bar_eligible_universe"]        # a real panel was built


# 6) witness failure stops BEFORE the second adjusted symbol -----------------

def test_6_witness_failure_stops_before_second_symbol(tmp_path):
    def mask(sym, rows):
        return ([{**r, "adjClose": r["close"]} for r in rows]
                if sym == BENCH else rows)
    prov = SyntheticAdjustedProvider(mutate=mask)
    fetched: list[str] = []
    orig = prov.fetch
    prov.fetch = lambda s: (fetched.append(s), orig(s))[1]
    with pytest.raises(B.BuildError, match="NO dividend adjustment"):
        _pkg(tmp_path, provider=prov)
    assert fetched == [BENCH]     # only SPY adjusted; no 2nd symbol acquired


# 7) the adjusted endpoint is authoritative for returns ----------------------

def test_7_returns_are_derived_from_adjusted_close(tmp_path):
    root, m = _pkg(tmp_path)
    assert m["return_derivation"] == C.ADJUSTED_BETA_FORMULA
    bars = json.loads((root / "bars.json").read_text())
    returns = json.loads((root / "returns.json").read_text())
    sym = m["bar_eligible_universe"][0]
    series = {b["session_date"]: b["adj_close"]
              for b in bars if b["symbol"] == sym}
    rr = [r for r in returns if r["symbol"] == sym]
    r0 = rr[len(rr) // 2]
    expected = series[r0["session_date"]] / series[r0["prev_session_date"]] - 1
    assert abs(r0["daily_return"] - expected) < 1e-9


# 8 + 9) companion evidence changes nothing consumed ------------------------

def test_8_9_companion_change_leaves_returns_eligibility_cohorts_identical(tmp_path):
    class Extra(SyntheticAdjustedProvider):
        def fetch_companion(self, symbol):
            # a witness-valid companion difference (extra field only)
            return [{**r, "vwap": 1.23} for r in self._rows(symbol)]
    a, ma = _pkg(tmp_path / "a")
    b, mb = _pkg(tmp_path / "b", provider=Extra())
    assert json.loads((a / "returns.json").read_text()) == \
        json.loads((b / "returns.json").read_text())
    assert ma["bar_eligible_universe"] == mb["bar_eligible_universe"]
    assert ma["non_overlapping_cohort_dates"] == mb["non_overlapping_cohort_dates"]
    # the companion IS still bound into package identity (it is evidence)
    assert ma["companion_raw_digest"] != mb["companion_raw_digest"]


# 10) the lab consumer replays the witness from frozen bytes, no credential --

def test_10_consumer_replays_witness_from_frozen_bytes(tmp_path):
    root, _ = _pkg(tmp_path)
    snap = CON.validate(root)     # runs the witness replay; no FMP, no network
    assert snap.bars_witness_raw and snap.has_bars


# 11) tampering with the witness evidence is rejected ------------------------

def test_11_witness_evidence_tamper_is_refused(tmp_path):
    root, _ = _pkg(tmp_path)
    wit = json.loads((root / "bars_witness_raw.json").read_text())
    wit["rows"][0]["close"] = wit["rows"][0]["close"] * 1.5
    (root / "bars_witness_raw.json").write_text(
        json.dumps(wit, indent=2, sort_keys=True))
    with pytest.raises(CON.SnapshotInvalid):
        CON.validate(root)


def test_11b_resealed_witness_tamper_still_breaks_the_replay(tmp_path):
    """Refresh the witness artifact digest AND package_id after scaling the
    companion close: the digest check now passes, but recomputing the companion
    digest against the manifest, and replaying the factor, still refuse it."""
    root, _ = _pkg(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text())
    wit = json.loads((root / "bars_witness_raw.json").read_text())
    for r in wit["rows"]:
        r["close"] = r["close"] * 1.5      # shifts the factor away from ~1
    (root / "bars_witness_raw.json").write_text(
        json.dumps(wit, indent=2, sort_keys=True))
    manifest["artifact_digests"]["bars_witness_raw.json"] = C.artifact_digest(wit)
    core = {k: v for k, v in manifest.items()
            if k not in ("generated_at", "package_id")}
    manifest["package_id"] = C.package_id(core)
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True))
    with pytest.raises(CON.SnapshotInvalid):
        CON.validate(root)


# 14 + 15) companion strict-live: no cache, no stale, no retry ---------------

def _fmp(tmp_path, *, budget=230, retry_max=1):
    from fmp_client import FMPClient
    return FMPClient(api_key="TEST_KEY", daily_budget=budget, retry_max=retry_max,
                     cache_dir=tmp_path / "fmp_cache")


def test_15_companion_strict_live_makes_one_attempt_no_retry(tmp_path, monkeypatch):
    calls = []
    def boom(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 500, "err", {}, None)
    monkeypatch.setattr("urllib.request.urlopen", boom)
    from fmp_client import FMPError
    cl = _fmp(tmp_path, retry_max=3)
    with pytest.raises(FMPError):
        cl.get_full_bars_strict_live("SPY")
    assert len(calls) == 1


def test_14_companion_strict_live_ignores_fresh_and_stale_cache(tmp_path, monkeypatch):
    cl = _fmp(tmp_path)
    cl._cache.set("hist_stable_SPY_5y", [{"date": "1999-01-01", "close": 1.0,
                                          "adjClose": 1.0, "volume": 1}])
    live = [{"date": "2020-01-02", "close": 200.0, "adjClose": 200.0, "volume": 5}]
    calls = []
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: calls.append(1) or _Resp(live))
    out = cl.get_full_bars_strict_live("SPY")
    assert len(calls) == 1 and out == live


def test_14b_companion_budget_exhaustion_fails_closed_not_stale(tmp_path, monkeypatch):
    cl = _fmp(tmp_path, budget=3)
    cl._cache.set("hist_stable_SPY_5y", [{"date": "2020-01-02", "close": 1.0,
                                          "adjClose": 1.0, "volume": 1}])
    cl._counter.increment(3)
    called = []
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: called.append(1) or _Resp([]))
    from fmp_client import CallBudgetExceeded
    with pytest.raises(CallBudgetExceeded):
        cl.get_full_bars_strict_live("SPY")
    assert called == []


def test_companion_strict_live_uses_the_full_endpoint(tmp_path, monkeypatch):
    captured = {}
    def spy_once(self, endpoint, params, *, base_url):
        captured["endpoint"] = endpoint
        self._counter.increment()
        return [{"date": "2020-01-02", "close": 1.0, "adjClose": 1.0, "volume": 1}]
    monkeypatch.setattr("fmp_client.FMPClient._raw_get_once", spy_once)
    cl = _fmp(tmp_path)
    cl.get_full_bars_strict_live("SPY")
    from fmp_client import _EP_HISTORICAL
    assert captured["endpoint"] == _EP_HISTORICAL
    assert C.COMPANION_ENDPOINT.endswith(_EP_HISTORICAL)


# 16 + 19) the hard 23-request cap is load-bearing; no 24th ------------------

def test_16_19_hard_cap_23_no_24th_request():
    plan = PR.acquisition_plan()
    acq = PR.StrictLiveAcquirer(FakeStrictClient(), plan)
    acq.get_historical_prices_dividend_adjusted("SPY")     # 1 (adjusted)
    acq.get_historical_prices("SPY")                       # 2 (companion)
    for sym in [s for s in plan if s != "SPY"]:
        acq.get_historical_prices_dividend_adjusted(sym)   # 3..23
    assert acq.attempted_http_requests == 23
    with pytest.raises(PR.StrictLiveAcquisitionError):
        acq.get_historical_prices_dividend_adjusted(plan[1])   # a 24th adjusted
    with pytest.raises(PR.StrictLiveAcquisitionError):
        acq.get_historical_prices("SPY")                       # a 24th companion
    assert acq.attempted_http_requests == 23


# 17) SPY adjusted first, companion second ----------------------------------

def test_17_spy_adjusted_first_then_companion_ordering():
    acq = PR.StrictLiveAcquirer(FakeStrictClient(), PR.acquisition_plan())
    with pytest.raises(PR.StrictLiveAcquisitionError, match="must be acquired first"):
        acq.get_historical_prices_dividend_adjusted("AAPL")
    with pytest.raises(PR.StrictLiveAcquisitionError, match="before the /full companion"):
        acq.get_historical_prices("SPY")            # companion before SPY adjusted
    acq.get_historical_prices_dividend_adjusted("SPY")
    acq.get_historical_prices("SPY")                # now allowed (2nd)
    with pytest.raises(PR.StrictLiveAcquisitionError, match="more than once"):
        acq.get_historical_prices("SPY")


def test_companion_is_benchmark_only():
    acq = PR.StrictLiveAcquirer(FakeStrictClient(), PR.acquisition_plan())
    acq.get_historical_prices_dividend_adjusted("SPY")
    with pytest.raises(PR.StrictLiveAcquisitionError, match="benchmark-only"):
        acq.get_historical_prices("AAPL")


def test_companion_must_precede_remaining_adjusted():
    acq = PR.StrictLiveAcquirer(FakeStrictClient(), PR.acquisition_plan())
    acq.get_historical_prices_dividend_adjusted("SPY")
    acq.get_historical_prices_dividend_adjusted("AAPL")   # a remaining adjusted
    with pytest.raises(PR.StrictLiveAcquisitionError, match="must precede"):
        acq.get_historical_prices("SPY")


# 18) companion endpoint substitution is refused ----------------------------

def test_18_companion_endpoint_substitution_is_refused(tmp_path):
    prov = SyntheticAdjustedProvider()
    prov.companion_endpoint = C.AUTHORIZED_ENDPOINT       # not /full
    with pytest.raises(B.BuildError, match="companion substitution is refused"):
        _pkg(tmp_path, provider=prov)


def test_consumer_refuses_a_swapped_companion_endpoint(tmp_path):
    root, _ = _pkg(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["companion_endpoint"] = C.AUTHORIZED_ENDPOINT
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2,
                                                   sort_keys=True))
    with pytest.raises(CON.SnapshotInvalid):
        CON.validate(root)


# ===========================================================================
# observe_only ENVELOPE governance (PR #54 closeout)
# ===========================================================================

def _reseal_pkg(root, manifest):
    core = {k: v for k, v in manifest.items()
            if k not in ("generated_at", "package_id")}
    manifest["package_id"] = C.package_id(core)
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True))


def test_witness_artifact_carries_observe_only_true(tmp_path):
    root, _ = _pkg(tmp_path)
    wit = json.loads((root / "bars_witness_raw.json").read_text())
    assert isinstance(wit, dict) and wit["observe_only"] is True
    assert isinstance(wit["rows"], list) and wit["rows"]


def test_witness_rows_are_the_exact_provider_rows(tmp_path):
    prov = SyntheticAdjustedProvider()
    root, m = _pkg(tmp_path, provider=prov)
    wit = json.loads((root / "bars_witness_raw.json").read_text())
    # rows are the exact companion /full provider response, unmutated
    assert wit["rows"] == prov.fetch_companion(BENCH)
    # the raw-response digest is over the ROWS, never the envelope metadata
    assert m["companion_raw_digest"] == C.artifact_digest(wit["rows"])
    assert m["companion_raw_digest"] != C.artifact_digest(wit)
    # the whole-envelope artifact digest is a distinct scope
    assert m["artifact_digests"]["bars_witness_raw.json"] == C.artifact_digest(wit)


@pytest.mark.parametrize("bad", [False, "true", 1, None, "__DELETE__"])
def test_false_missing_or_nonbool_observe_only_is_refused(tmp_path, bad):
    root, _ = _pkg(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text())
    wit = json.loads((root / "bars_witness_raw.json").read_text())
    if bad == "__DELETE__":
        wit.pop("observe_only")
    else:
        wit["observe_only"] = bad
    (root / "bars_witness_raw.json").write_text(
        json.dumps(wit, indent=2, sort_keys=True))
    # reseal the whole-envelope artifact digest + package id so the tamper must
    # be caught by the EXPLICIT observe_only check, not merely the digest check
    manifest["artifact_digests"]["bars_witness_raw.json"] = C.artifact_digest(wit)
    _reseal_pkg(root, manifest)
    with pytest.raises(CON.SnapshotInvalid):
        CON.validate(root)


def test_flipping_observe_only_cannot_alter_returns(tmp_path):
    root, _ = _pkg(tmp_path)
    good_returns = (root / "returns.json").read_bytes()
    manifest = json.loads((root / "manifest.json").read_text())
    wit = json.loads((root / "bars_witness_raw.json").read_text())
    wit["observe_only"] = False
    (root / "bars_witness_raw.json").write_text(
        json.dumps(wit, indent=2, sort_keys=True))
    manifest["artifact_digests"]["bars_witness_raw.json"] = C.artifact_digest(wit)
    _reseal_pkg(root, manifest)
    with pytest.raises(CON.SnapshotInvalid):   # observe_only must be true
        CON.validate(root)
    # returns.json is byte-identical: observe_only lives only in the witness
    # envelope and never feeds the calculated return series
    assert (root / "returns.json").read_bytes() == good_returns


def test_witness_cannot_affect_eligibility_or_cohorts_via_envelope(tmp_path):
    """The envelope wrapper (and its metadata) is not consulted for the
    consumed surface: eligibility and cohorts come only from the adjusted
    panel."""
    root, m = _pkg(tmp_path)
    # nothing in the eligibility/cohort manifest surface references the witness
    for key in ("bar_eligible_universe", "bar_excluded_symbols",
                "non_overlapping_cohort_dates"):
        blob = json.dumps(m[key])
        assert "observe_only" not in blob and "companion" not in blob


def test_v1_schema_and_exact_seven_artifacts(tmp_path):
    root, m = _pkg(tmp_path)
    assert m["schema_version"] == "engineering.vs002_evidence.v1"
    names = {p.name for p in root.iterdir() if p.is_file()}
    assert names == {"signals.json", "returns.json", "bars.json", "bars_raw.json",
                     "bars_witness_raw.json", "bars_snapshots.json",
                     "manifest.json"}


def test_v0_labeled_manifest_is_refused(tmp_path):
    root, _ = _pkg(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["schema_version"] = "engineering.vs002_evidence.v0"
    _reseal_pkg(root, manifest)
    with pytest.raises(CON.SnapshotInvalid):
        CON.validate(root)
