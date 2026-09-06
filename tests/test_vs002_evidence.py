"""VS-002 frozen evidence: build, validate, readiness.

The fixtures here are SYNTHETIC and say so. They are not disguised as live FMP
evidence: the real package can only be built on the production host, where the
signal database and the price archive actually exist.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from portfolio_automation.vs002_evidence import contracts as C
from portfolio_automation.vs002_evidence import builder as B
from portfolio_automation.vs002_evidence import consumer as CON
from portfolio_automation.vs002_evidence import readiness as R

SYMBOLS = ("AAPL", "MSFT", "NASA")          # NASA = deliberately short history
BENCH = C.BENCHMARK
SESSIONS = 400                               # > 252 + buffer for the long ones
SHORT_SESSIONS = 106                         # NASA, mirrors production


def _dates(n: int, start_ord: int = 739000) -> list[str]:
    """Synthetic consecutive session dates. Calendar realism is irrelevant here;
    ordering and count are what the contracts depend on."""
    import datetime as dt
    return [(dt.date.fromordinal(start_ord + i)).isoformat() for i in range(n)]


def _archive(tmp: Path, symbol: str, n: int, *, scale: float = 1.0,
             base: float = 100.0) -> None:
    d = _dates(SESSIONS)[-n:]
    rows = []
    for i, day in enumerate(d):
        price = (base + i * 0.5) * scale
        rows.append({"date": day, "open": price, "high": price, "low": price,
                     "close": price, "volume": 1000, "vwap": price,
                     "change": 0.0, "changePercent": 0.0, "symbol": symbol})
    out = tmp / "outputs" / "backtest" / "historical"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{symbol}_5y.json").write_text(json.dumps({
        "symbol": symbol, "years": 5, "row_count": len(rows),
        "stored_at": "2026-09-01T09:50:01Z", "rows": list(reversed(rows)),
        "schema_version": "1", "source": "historical_backfill",
        "observe_only": True}), encoding="utf-8")


def _db(tmp: Path, *, scans: list[str], symbols=SYMBOLS + (BENCH,),
        score: float | None = 0.5) -> None:
    p = tmp / "data"
    p.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p / "portfolio.db")
    con.execute("""CREATE TABLE watchlist_signal_feedback (
        ticker TEXT, signal_time TEXT, signal_score REAL, price_at_signal REAL,
        outcome_return_7d REAL, outcome_price_7d REAL, evaluated_at_7d TEXT,
        prediction_intent TEXT, data_mode TEXT)""")
    for s in scans:
        for sym in symbols:
            con.execute("INSERT INTO watchlist_signal_feedback VALUES (?,?,?,?,?,?,?,?,?)",
                        (sym, s + "T09:00:00", score, 100.0, 1.5, 101.5,
                         s + "T09:00:00", "up", "live"))
    con.commit()
    con.close()


def _scan_dates(n: int, step: int = 8) -> list[str]:
    """`step` days apart so each scan is its own non-overlapping cohort."""
    all_days = _dates(SESSIONS)
    start = SESSIONS - 1 - (n - 1) * step
    return [all_days[start + i * step] for i in range(n)]


@pytest.fixture
def built(tmp_path: Path) -> tuple[Path, dict]:
    scans = _scan_dates(12)
    _db(tmp_path, scans=scans)
    for sym in SYMBOLS + (BENCH,):
        _archive(tmp_path, sym, SHORT_SESSIONS if sym == "NASA" else SESSIONS)
    manifest = B.build(tmp_path, code_sha="testsha",
                       generated_at="2026-09-06T00:00:00Z")
    return tmp_path / B.DEFAULT_OUT_REL, manifest


# ── the load-bearing invariance property ──────────────────────────────────


def test_scaling_invariance_of_derived_returns():
    """A later whole-series adjustment must not change a derived return.

    This identity is the entire justification for exporting returns instead of
    price levels, and therefore for the narrow PIT certification. If it ever
    stops holding, the certification is void."""
    prices = [100.0, 101.5, 99.25, 103.0, 102.5]
    for c in (0.1, 0.25, 1.0, 4.0, 10.0):
        base = [C.derive_return(prices[i], prices[i - 1])
                for i in range(1, len(prices))]
        scaled = [C.derive_return(prices[i] * c, prices[i - 1] * c)
                  for i in range(1, len(prices))]
        for a, b in zip(base, scaled):
            assert abs(a - b) < 1e-12, c


def test_scaling_invariance_end_to_end_through_the_builder(tmp_path: Path):
    """The same property through the real build path, not just the helper.

    A 10:1 split applied to the whole archive is exactly the post-signal
    corporate action the PIT analysis was worried about."""
    scans = _scan_dates(12)
    for scale, sub in ((1.0, "a"), (0.1, "b")):
        root = tmp_path / sub
        root.mkdir()
        _db(root, scans=scans)
        for sym in SYMBOLS + (BENCH,):
            _archive(root, sym, SHORT_SESSIONS if sym == "NASA" else SESSIONS,
                     scale=scale)
        B.build(root, code_sha="testsha", generated_at="2026-09-06T00:00:00Z")

    def rets(sub):
        p = tmp_path / sub / B.DEFAULT_OUT_REL / B.RETURNS_REL
        return [(r["symbol"], r["session_date"], round(r["daily_return"], 12))
                for r in json.loads(p.read_text())]

    assert rets("a") == rets("b")


def test_price_levels_are_not_invariant(tmp_path: Path):
    """The contrast that makes the point: levels DO move under adjustment, which
    is why they are not the consumer contract."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    _archive(a, "AAPL", 10, scale=1.0)
    _archive(b, "AAPL", 10, scale=0.1)
    la = json.loads((a / "outputs/backtest/historical/AAPL_5y.json").read_text())
    lb = json.loads((b / "outputs/backtest/historical/AAPL_5y.json").read_text())
    assert la["rows"][0]["close"] != lb["rows"][0]["close"]


# ── universe / eligibility ────────────────────────────────────────────────


def test_nasa_excluded_by_the_universal_rule_not_by_ticker(built):
    _, m = built
    assert "NASA" not in m["eligible_universe"]
    assert str(C.MIN_PRIOR_SESSIONS) in m["excluded_symbols"]["NASA"]
    assert "prior sessions" in m["excluded_symbols"]["NASA"]
    # The rule text must be universal, never naming a symbol.
    assert "NASA" not in m["eligibility_rule"]
    assert "AAPL" in m["eligible_universe"] and "MSFT" in m["eligible_universe"]


def test_new_listing_bias_is_declared(built):
    _, m = built
    assert "new listings" in m["new_listing_bias"]


def test_only_matured_signals_are_exported(tmp_path: Path):
    scans = _scan_dates(12)
    _db(tmp_path, scans=scans)
    con = sqlite3.connect(tmp_path / "data" / "portfolio.db")
    con.execute("INSERT INTO watchlist_signal_feedback VALUES "
                "('AAPL','2030-01-01T09:00:00',0.5,100.0,NULL,NULL,NULL,'up','live')")
    con.commit(); con.close()
    for sym in SYMBOLS + (BENCH,):
        _archive(tmp_path, sym, SHORT_SESSIONS if sym == "NASA" else SESSIONS)
    m = B.build(tmp_path, code_sha="t", generated_at="2026-09-06T00:00:00Z")
    rows = json.loads((tmp_path / B.DEFAULT_OUT_REL / B.SIGNALS_REL).read_text())
    assert all(r["outcome_return_7d"] is not None for r in rows)
    assert m["signal_evidence_cutoff"] < "2030"


def test_cutoff_is_derived_from_the_source(built):
    root, m = built
    rows = json.loads((root / B.SIGNALS_REL).read_text())
    assert m["signal_evidence_cutoff"] == max(r["signal_time"] for r in rows)


def test_cohorts_are_non_overlapping(built):
    _, m = built
    import datetime as dt
    dates = [dt.date.fromisoformat(d) for d in m["non_overlapping_cohort_dates"]]
    for a, b in zip(dates, dates[1:]):
        assert (b - a).days >= C.HORIZON_DAYS


def test_greedy_cohorts_rejects_overlapping_scans():
    """The VS-001 defect, as a unit: daily scans are not daily cohorts."""
    daily = [f"2026-05-{d:02d}" for d in range(1, 21)]
    assert len(B.greedy_cohorts(daily)) == 3


# ── determinism / canonical output ────────────────────────────────────────


def test_build_is_deterministic(tmp_path: Path):
    scans = _scan_dates(12)
    outs = []
    for sub in ("a", "b"):
        root = tmp_path / sub
        root.mkdir()
        _db(root, scans=scans)
        for sym in SYMBOLS + (BENCH,):
            _archive(root, sym, SHORT_SESSIONS if sym == "NASA" else SESSIONS)
        m = B.build(root, code_sha="t", generated_at="2026-09-06T00:00:00Z")
        outs.append(m)
    assert outs[0]["package_id"] == outs[1]["package_id"]
    assert outs[0]["artifact_digests"] == outs[1]["artifact_digests"]


def test_returns_are_chronological_per_symbol(built):
    root, _ = built
    rows = json.loads((root / B.RETURNS_REL).read_text())
    per: dict[str, list[str]] = {}
    for r in rows:
        per.setdefault(r["symbol"], []).append(r["session_date"])
    for sym, dates in per.items():
        assert dates == sorted(dates), sym


def test_export_span_is_bounded_not_the_whole_archive(built):
    """Five years are not exported merely because five years exist.

    The bound is lookback + buffer + the span the signals themselves cover; an
    earlier version of this test forgot the third term and was simply wrong."""
    import datetime as dt
    root, m = built
    rows = json.loads((root / B.RETURNS_REL).read_text())
    spy = [r for r in rows if r["symbol"] == BENCH]

    dates = m["non_overlapping_cohort_dates"]
    signal_span = (dt.date.fromisoformat(m["return_date_range"]["end"])
                   - dt.date.fromisoformat(dates[0])).days
    ceiling = C.MIN_PRIOR_SESSIONS + B.LOOKBACK_BUFFER_SESSIONS + signal_span + 2
    assert len(spy) <= ceiling, (len(spy), ceiling)
    # And materially less than the whole archive it was cut from.
    assert len(spy) < SESSIONS


# ── consumer validation ───────────────────────────────────────────────────


def test_valid_snapshot_is_accepted(built):
    root, _ = built
    snap = CON.validate(root)
    assert snap.eligible_universe
    assert snap.manifest["pit_certification"] == C.PIT_CERTIFICATION


def test_modified_content_is_rejected(built):
    root, _ = built
    rows = json.loads((root / B.SIGNALS_REL).read_text())
    rows[0]["outcome_return_7d"] = 999.0
    (root / B.SIGNALS_REL).write_text(json.dumps(rows), encoding="utf-8")
    with pytest.raises(CON.SnapshotInvalid, match="digest mismatch"):
        CON.validate(root)


def test_missing_artifact_is_rejected(built):
    root, _ = built
    (root / B.RETURNS_REL).unlink()
    with pytest.raises(CON.SnapshotInvalid, match="missing artifact"):
        CON.validate(root)


def test_unexpected_artifact_is_rejected(built):
    root, _ = built
    (root / "extra.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CON.SnapshotInvalid, match="unexpected artifact"):
        CON.validate(root)


def test_tampered_manifest_is_rejected(built):
    root, _ = built
    m = json.loads((root / B.MANIFEST_REL).read_text())
    m["eligible_universe"] = m["eligible_universe"] + ["NASA"]
    (root / B.MANIFEST_REL).write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(CON.SnapshotInvalid, match="package_id mismatch"):
        CON.validate(root)


def test_wrong_cutoff_is_rejected(built):
    root, _ = built
    m = json.loads((root / B.MANIFEST_REL).read_text())
    m["signal_evidence_cutoff"] = "2000-01-01T00:00:00"
    (root / B.MANIFEST_REL).write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(CON.SnapshotInvalid):
        CON.validate(root)


def test_consumer_pre_signal_boundary_is_strict(built):
    root, _ = built
    snap = CON.validate(root)
    sym = snap.eligible_universe[0]
    sig = snap.signals_for(sym)[0]
    boundary = sig["signal_time"][:10]
    assert all(r["session_date"] < boundary
               for r in snap.returns_before(sym, boundary))


# ── readiness ─────────────────────────────────────────────────────────────


def test_valid_fixture_is_ready(built):
    root, _ = built
    res = R.evaluate(CON.validate(root))
    assert res.status == R.READY, res.reasons


def test_too_few_cohorts_is_not_ready(tmp_path: Path):
    scans = _scan_dates(3)
    _db(tmp_path, scans=scans)
    for sym in SYMBOLS + (BENCH,):
        _archive(tmp_path, sym, SHORT_SESSIONS if sym == "NASA" else SESSIONS)
    B.build(tmp_path, code_sha="t", generated_at="2026-09-06T00:00:00Z")
    res = R.evaluate(CON.validate(tmp_path / B.DEFAULT_OUT_REL))
    assert res.status == R.NOT_READY
    assert any("cohort" in r for r in res.reasons)


def test_absent_signal_score_is_not_ready(tmp_path: Path):
    """VS-002 H2 tests ranking; a reconstructed or null score cannot support it."""
    scans = _scan_dates(12)
    _db(tmp_path, scans=scans, score=None)
    for sym in SYMBOLS + (BENCH,):
        _archive(tmp_path, sym, SHORT_SESSIONS if sym == "NASA" else SESSIONS)
    B.build(tmp_path, code_sha="t", generated_at="2026-09-06T00:00:00Z")
    res = R.evaluate(CON.validate(tmp_path / B.DEFAULT_OUT_REL))
    assert res.status == R.NOT_READY
    assert any("signal_score" in r for r in res.reasons)


def test_missing_benchmark_archive_fails_the_build(tmp_path: Path):
    scans = _scan_dates(12)
    _db(tmp_path, scans=scans)
    for sym in SYMBOLS:
        _archive(tmp_path, sym, SHORT_SESSIONS if sym == "NASA" else SESSIONS)
    with pytest.raises(B.BuildError, match="benchmark"):
        B.build(tmp_path, code_sha="t")


def test_readiness_computes_no_experiment_result():
    """The evaluator must be unable to answer the VS-002 question early."""
    import inspect
    src = inspect.getsource(R)
    tokens = {t for name in dir(R) if not name.startswith("_")
              for t in name.lower().split("_")}
    assert tokens.isdisjoint({"beta", "excess", "alpha", "ic", "sharpe"}), tokens
    assert "Cov(" not in src


# ── security ──────────────────────────────────────────────────────────────


def test_exported_artifacts_contain_no_secret_shapes(built):
    import re
    root, _ = built
    pat = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{16,}|AKIA[0-9A-Z]{16}|"
                     r"BEGIN [A-Z ]*PRIVATE KEY|FMP_API_KEY\s*[:=]")
    for name in (B.SIGNALS_REL, B.RETURNS_REL, B.MANIFEST_REL):
        assert not pat.search((root / name).read_text(encoding="utf-8")), name


def test_allowlist_exposes_exact_paths_not_the_archive_tree():
    from portfolio_automation.agent_export import ALLOWLIST
    entries = {e.logical_name: e.source_relpath for e in ALLOWLIST
               if e.logical_name.startswith("vs002_")}
    assert entries == {
        "vs002_signals": "vs002_evidence/signals.json",
        "vs002_returns": "vs002_evidence/returns.json",
        "vs002_manifest": "vs002_evidence/manifest.json"}
    # The raw price archive must never be generically allowlisted.
    assert not any("backtest" in e.source_relpath for e in ALLOWLIST)
    assert not any(e.source_relpath.endswith("**") for e in ALLOWLIST)
