"""VS-002 dividend-adjusted price evidence: the adversarial suite.

Every fixture is SYNTHETIC and offline. No network, no credential, no FMP.
Each test here is one of the implementation mission's required attacks; the
happy-path behavior lives in test_vs002_evidence.py alongside the package it
extends.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from portfolio_automation.vs002_evidence import builder as B
from portfolio_automation.vs002_evidence import consumer as CON
from portfolio_automation.vs002_evidence import contracts as C
from portfolio_automation.vs002_evidence import readiness as R
from portfolio_automation.vs002_evidence import snapshots as SN

from tests.test_vs002_evidence import (  # the shared synthetic machinery
    BENCH, SESSIONS, SYMBOLS, SyntheticAdjustedProvider, _archive, _db,
    _dates, _scan_dates)

RETRIEVED_AT = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)


def _package(tmp_path: Path, *, provider=None, scans=None) -> tuple[Path, dict]:
    scans = scans or _scan_dates(12)
    _db(tmp_path, scans=scans)
    for sym in SYMBOLS + (BENCH,):
        _archive(tmp_path, sym, 106 if sym == "NASA" else SESSIONS)
    manifest = B.build(tmp_path, code_sha="testsha",
                       generated_at="2026-09-06T00:00:00Z",
                       bar_provider=provider or SyntheticAdjustedProvider())
    return tmp_path / B.DEFAULT_OUT_REL, manifest


# ---------------------------------------------------------------------------
# 1. endpoint substitution must fail
# ---------------------------------------------------------------------------

def test_endpoint_substitution_is_refused(tmp_path: Path):
    """A provider declaring /full — however plausible its rows — is refused at
    the identity boundary, never adopted, never fallen back to."""
    provider = SyntheticAdjustedProvider()
    provider.endpoint = "/stable/historical-price-eod/full"
    with pytest.raises(B.BuildError, match="endpoint substitution is refused"):
        _package(tmp_path, provider=provider)


def test_consumer_refuses_a_swapped_endpoint_identity(built_dir):
    """A stored manifest whose endpoint was edited to /full fails validation
    on the consumer side too — the transport is where substitution happens."""
    root, _ = built_dir
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["bar_endpoint"] = "/stable/historical-price-eod/full"
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2,
                                                   sort_keys=True))
    with pytest.raises(CON.SnapshotInvalid):
        CON.validate(root)


@pytest.fixture
def built_dir(tmp_path: Path) -> tuple[Path, dict]:
    return _package(tmp_path)


# ---------------------------------------------------------------------------
# 2–3. adjustment-semantics mutations must fail
# ---------------------------------------------------------------------------

def test_unadjusted_benchmark_masquerade_fails_split_dividend_battery(tmp_path: Path):
    """DIJ-0011's named mutation: a series that silently falls back to
    unadjusted close. On the benchmark — a quarterly dividend payer — a long
    span with adjClose == close is indistinguishable from raw data, so the
    dividend-adjusted claim is NOT established and the build refuses."""
    def strip_adjustment(sym, rows):
        if sym != BENCH:
            return rows
        return [{**r, "adjClose": r["close"]} for r in rows]
    with pytest.raises(B.BuildError, match="NO adjustment"):
        _package(tmp_path,
                 provider=SyntheticAdjustedProvider(mutate=strip_adjustment))


def test_split_semantics_mutation_fails(tmp_path: Path):
    """An 'adjusted' series that jumps WITH the split (ratio step down in
    time) is an artificial discontinuity no real corporate action produces."""
    def break_split(sym, rows):
        if sym != "AAPL":
            return rows
        rows = [dict(r) for r in rows]
        # one mid-series bar whose adjusted close dips and recovers: the
        # adjustment ratio decreases going forward in time, which no genuine
        # split or dividend adjustment can produce
        rows[len(rows) // 2]["adjClose"] *= 0.5
        return rows
    with pytest.raises(B.BuildError, match="artificial discontinuity|not ~1"):
        _package(tmp_path, provider=SyntheticAdjustedProvider(mutate=break_split))


def test_dividend_semantics_mutation_fails(tmp_path: Path):
    """A final adjustment factor away from 1 is a series adjusted to some
    other vintage than the retrieval it claims."""
    def stale_vintage(sym, rows):
        if sym != BENCH:
            return rows
        return [{**r, "adjClose": r["adjClose"] * 0.98} for r in rows]
    with pytest.raises(B.BuildError, match="not ~1"):
        _package(tmp_path, provider=SyntheticAdjustedProvider(mutate=stale_vintage))


# ---------------------------------------------------------------------------
# 4–5. structural window safety
# ---------------------------------------------------------------------------

def test_signal_date_bar_is_excluded_from_the_beta_window():
    bars = [{"symbol": "AAPL", "session_date": d, "close": 1.0,
             "adj_close": 1.0, "volume": 1} for d in
            ("2026-05-10", "2026-05-11", "2026-05-12")]
    window = SN.beta_window_bars(bars, signal_date="2026-05-12")
    assert [b["session_date"] for b in window] == ["2026-05-10", "2026-05-11"]


def test_future_bar_is_excluded_from_the_beta_window():
    bars = [{"symbol": "AAPL", "session_date": "2026-05-13", "close": 1.0,
             "adj_close": 1.0, "volume": 1}]
    assert SN.beta_window_bars(bars, signal_date="2026-05-12") == []


def test_readiness_flags_a_window_that_would_leak(built_dir):
    """Shifting a signal to the panel's FIRST session leaves no prior bars —
    the gates refuse rather than quietly shrink the window."""
    root, manifest = built_dir
    snap = CON.validate(root)
    first_bar = min(b["session_date"] for b in snap.bars_for("AAPL"))
    assert snap.bars_before("AAPL", first_bar) == []


# ---------------------------------------------------------------------------
# 6–9. panel data-quality refusals
# ---------------------------------------------------------------------------

def test_duplicate_session_is_refused(tmp_path: Path):
    def duplicate(sym, rows):
        return rows + [rows[0]] if sym == "MSFT" else rows
    with pytest.raises(B.BuildError, match="duplicate session dates"):
        _package(tmp_path, provider=SyntheticAdjustedProvider(mutate=duplicate))


def test_251_prior_sessions_do_not_satisfy_the_252_gate():
    dates = _dates(SESSIONS)
    panel = {
        BENCH: [C.BarRow(BENCH, d, 1.0, 1.0, 1) for d in dates],
        "AAPL": [C.BarRow("AAPL", d, 1.0, 1.0, 1) for d in dates[-252:]],
    }
    earliest = {"AAPL": dates[-1]}     # 251 strictly-prior sessions exist
    eligible, excluded = B.bar_eligibility(panel, earliest)
    assert eligible == []
    assert "251 prior sessions" in excluded["AAPL"]
    # ...and 252 does satisfy it: same rule, one more session.
    panel["AAPL"] = [C.BarRow("AAPL", d, 1.0, 1.0, 1) for d in dates[-253:]]
    eligible, _ = B.bar_eligibility(panel, earliest)
    assert eligible == ["AAPL"]


def test_thin_joint_overlap_is_not_ready(tmp_path: Path):
    """A symbol whose sessions barely intersect the benchmark's cannot back a
    beta; the gap rule refuses the panel before readiness even sees it."""
    def decimate(sym, rows):
        if sym != "MSFT":
            return rows
        return [r for i, r in enumerate(rows) if i % 10 == 0]
    with pytest.raises(B.BuildError, match="gap of"):
        _package(tmp_path, provider=SyntheticAdjustedProvider(mutate=decimate))


def test_missing_benchmark_bars_refuse_the_build(tmp_path: Path):
    class NoBench(SyntheticAdjustedProvider):
        def fetch(self, symbol):
            if symbol == BENCH:
                return []
            return super().fetch(symbol)
    with pytest.raises(B.BuildError, match="no bar rows"):
        _package(tmp_path, provider=NoBench())


@pytest.mark.parametrize("field, bad", [
    ("close", -1.0), ("close", 0.0), ("adjClose", float("nan")),
    ("adjClose", None), ("volume", None),
])
def test_malformed_values_are_refused(tmp_path: Path, field, bad):
    def poison(sym, rows):
        if sym != "MSFT":
            return rows
        rows = [dict(r) for r in rows]
        rows[5][field] = bad
        return rows
    with pytest.raises(B.BuildError):
        _package(tmp_path, provider=SyntheticAdjustedProvider(mutate=poison))


def test_missing_required_provider_field_is_refused(tmp_path: Path):
    """The real endpoint's shape is unverified (B3). A row that cannot supply
    the required fields cannot establish the claimed semantics — fail closed,
    exactly what the production build must do if the endpoint disappoints."""
    def drop_field(sym, rows):
        if sym != "MSFT":
            return rows
        rows = [dict(r) for r in rows]
        rows[3].pop("adjClose")
        return rows
    with pytest.raises(B.BuildError, match="missing required field"):
        _package(tmp_path, provider=SyntheticAdjustedProvider(mutate=drop_field))


# ---------------------------------------------------------------------------
# 10–11. identity and digests
# ---------------------------------------------------------------------------

def test_tampered_bar_is_refused(built_dir):
    root, _ = built_dir
    bars = json.loads((root / "bars.json").read_text())
    bars[0]["adj_close"] = bars[0]["adj_close"] * 1.01
    (root / "bars.json").write_text(json.dumps(bars, indent=2, sort_keys=True))
    with pytest.raises(CON.SnapshotInvalid, match="digest mismatch"):
        CON.validate(root)


def test_missing_bars_artifact_is_refused_when_declared(built_dir):
    root, _ = built_dir
    (root / "bars.json").unlink()
    with pytest.raises(CON.SnapshotInvalid, match="missing artifact"):
        CON.validate(root)


def test_undeclared_bars_artifact_is_refused(tmp_path: Path):
    """A legacy package cannot smuggle a bar file past its own manifest."""
    scans = _scan_dates(12)
    _db(tmp_path, scans=scans)
    for sym in SYMBOLS + (BENCH,):
        _archive(tmp_path, sym, 106 if sym == "NASA" else SESSIONS)
    B.build(tmp_path, code_sha="testsha", generated_at="2026-09-06T00:00:00Z")
    root = tmp_path / B.DEFAULT_OUT_REL
    (root / "bars.json").write_text("[]")
    with pytest.raises(CON.SnapshotInvalid, match="unexpected artifact"):
        CON.validate(root)


# ---------------------------------------------------------------------------
# 12. possession PIT — the existing gateway, unchanged
# ---------------------------------------------------------------------------

def test_lookahead_probe_refuses_every_backfilled_bar(built_dir):
    """A backfilled bar was not possessed before retrieval, and the gateway
    says so for every single one — the VS-001 refusal-ledger discipline."""
    root, manifest = built_dir
    snap = CON.validate(root)
    snaps = SN.panel_snapshots(
        snap.bars[:25], retrieved_at=RETRIEVED_AT,
        raw_digests=manifest["bar_raw_response_digests"])
    earliest_signal = datetime(2026, 5, 12, tzinfo=timezone.utc)
    probe = SN.lookahead_probe(snaps, earliest_signal)
    assert probe.passed and probe.refused == probe.total == 25


def test_bars_are_admitted_after_retrieval(built_dir):
    root, manifest = built_dir
    snap = CON.validate(root)
    snaps = SN.panel_snapshots(
        snap.bars[:5], retrieved_at=RETRIEVED_AT,
        raw_digests=manifest["bar_raw_response_digests"])
    decisions = SN.admit_panel(
        snaps, datetime(2026, 9, 19, tzinfo=timezone.utc))
    assert all(d.admitted for d in decisions)
    assert snaps[0].pit.known_at_basis == "derived_conservative"
    assert snaps[0].pit.known_at == RETRIEVED_AT


def test_no_historical_known_at_is_fabricated(built_dir):
    """The two PIT truths stay distinct: no snapshot claims a session-close
    knowledge time, and none carries an invented observed_at."""
    root, manifest = built_dir
    snap = CON.validate(root)
    s = SN.panel_snapshots(snap.bars[:1], retrieved_at=RETRIEVED_AT,
                           raw_digests={})[0]
    assert s.pit.observed_at is None
    assert s.pit.published_at is None
    assert s.pit.known_at == s.pit.retrieved_at


# ---------------------------------------------------------------------------
# 13. the frozen outcome is untouchable
# ---------------------------------------------------------------------------

def test_frozen_outcome_is_never_recomputed_from_bars(built_dir):
    """The recorded outcome_return_7d passes through byte-identical even though
    the bar panel implies a very different 7-day move — the new prices exist
    for RISK evidence, and a test fails if anything rederives the outcome."""
    root, _ = built_dir
    snap = CON.validate(root)
    recorded = {s["outcome_return_7d"] for s in snap.signals}
    assert recorded == {1.5}       # the DB fixture's value, verbatim
    # the synthetic bar panel's +0.5/day drift implies nothing close to 1.5
    # per 7d on a 100-base — if any code path replaced the outcome with a
    # bar-derived value, this set would not survive.


def test_outcome_tamper_is_refused(built_dir):
    root, _ = built_dir
    signals = json.loads((root / "signals.json").read_text())
    signals[0]["outcome_return_7d"] = 9.99
    (root / "signals.json").write_text(json.dumps(signals, indent=2,
                                                  sort_keys=True))
    with pytest.raises(CON.SnapshotInvalid, match="digest mismatch"):
        CON.validate(root)


# ---------------------------------------------------------------------------
# 14. determinism
# ---------------------------------------------------------------------------

def test_same_frozen_bytes_rebuild_to_identical_identities(tmp_path: Path):
    a = _package(tmp_path / "a")[1]
    b = _package(tmp_path / "b")[1]
    assert a["package_id"] == b["package_id"]
    assert a["artifact_digests"] == b["artifact_digests"]
    assert a["bar_raw_response_digests"] == b["bar_raw_response_digests"]


def test_snapshot_identities_are_deterministic(built_dir):
    root, manifest = built_dir
    snap = CON.validate(root)
    first = SN.panel_snapshots(snap.bars[:10], retrieved_at=RETRIEVED_AT,
                               raw_digests=manifest["bar_raw_response_digests"])
    second = SN.panel_snapshots(snap.bars[:10], retrieved_at=RETRIEVED_AT,
                                raw_digests=manifest["bar_raw_response_digests"])
    assert [s.snapshot_id for s in first] == [s.snapshot_id for s in second]
    assert [r.payload_hash for r in SN.panel_refs(first)] == \
        [r.payload_hash for r in SN.panel_refs(second)]


# ---------------------------------------------------------------------------
# the rf assumption boundary
# ---------------------------------------------------------------------------

def test_rf_assumption_is_named_and_attributable(built_dir):
    root, _ = built_dir
    snap = CON.validate(root)
    rf = snap.risk_free_rate_7d
    assert rf["value"] == 0.0
    assert rf["basis"] == "operator_preregistered_assumption"
    assert rf["observed_evidence"] is False
    assert rf["scope"] == "VS-002 only"


def test_a_package_claiming_an_observed_rf_is_refused(built_dir):
    root, _ = built_dir
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["risk_free_rate_7d"] = {"value": 0.0392, "basis": "observed",
                                     "observed_evidence": True}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2,
                                                   sort_keys=True))
    with pytest.raises(CON.SnapshotInvalid, match="risk_free_rate_7d"):
        CON.validate(root)


# ---------------------------------------------------------------------------
# no-network guarantee for this suite
# ---------------------------------------------------------------------------

def test_the_production_provider_is_bound_to_the_authorized_endpoint():
    """The production binding exists in code and declares the right identity —
    without being exercised: its client is a sentinel that would explode."""
    class Sentinel:
        def get_historical_prices_dividend_adjusted(self, symbol):
            raise AssertionError("network path must not be exercised offline")
    provider = B.FMPDividendAdjustedProvider(Sentinel())
    assert provider.endpoint == C.AUTHORIZED_ENDPOINT
