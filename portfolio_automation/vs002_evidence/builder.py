"""Build the frozen VS-002 evidence package. PRODUCTION-SIDE.

This runs where the evidence lives -- the VPS -- because the research plane has
no vendor credential and must not acquire one. It reads two production sources
and writes one bounded package:

    data/portfolio.db :: watchlist_signal_feedback   (recorded signals)
    outputs/backtest/historical/<SYM>_5y.json        (daily price archive)
                    |
                    v
    outputs/vs002_evidence/{signals.json, returns.json, manifest.json}

WHAT IT REFUSES TO DO.

It does not reconstruct signals, does not fetch anything, does not widen the
universe beyond the frozen VS-002 set, and does not export price levels as the
consumer contract. It exports five years only if five years are needed; the
span is derived from the actual lookback requirement.

``experimental_noncanonical``.
"""
from __future__ import annotations

import json
import sqlite3
from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from portfolio_automation.vs002_evidence import contracts as C

SIGNALS_REL = "signals.json"
RETURNS_REL = "returns.json"
MANIFEST_REL = "manifest.json"

DEFAULT_DB_REL = "data/portfolio.db"
DEFAULT_ARCHIVE_REL = "outputs/backtest/historical"
DEFAULT_OUT_REL = "outputs/vs002_evidence"

#: Extra sessions beyond the 252 lookback, so a symbol sitting exactly on the
#: boundary is not disqualified by one holiday.
LOOKBACK_BUFFER_SESSIONS = 20

BUILDER_VERSION = "v1"


class BuildError(RuntimeError):
    """The build fails closed rather than emitting a partial package."""


def _read_signals(db_path: Path) -> list[C.SignalRow]:
    """Matured signals only. An unmatured outcome is excluded, never imputed."""
    if not db_path.is_file():
        raise BuildError(f"signal database absent: {db_path}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.execute(
            "SELECT ticker, signal_time, signal_score, price_at_signal, "
            "       outcome_return_7d, outcome_price_7d, evaluated_at_7d, "
            "       prediction_intent, data_mode "
            "FROM watchlist_signal_feedback "
            "WHERE outcome_return_7d IS NOT NULL "
            "  AND evaluated_at_7d IS NOT NULL "
            "  AND price_at_signal IS NOT NULL AND price_at_signal > 0 "
            "ORDER BY ticker, signal_time")
        rows = cur.fetchall()
    finally:
        con.close()

    universe = set(C.FROZEN_UNIVERSE) | {C.BENCHMARK}
    out: list[C.SignalRow] = []
    for (tick, stime, score, price, ret, oprice, evald, intent, mode) in rows:
        sym = str(tick).upper()
        if sym not in universe:
            continue
        out.append(C.SignalRow(
            ticker=sym, signal_time=str(stime),
            signal_score=(float(score) if score is not None else None),
            price_at_signal=float(price), outcome_return_7d=float(ret),
            outcome_price_7d=(float(oprice) if oprice is not None else None),
            evaluated_at_7d=str(evald),
            prediction_intent=str(intent or ""), data_mode=str(mode or "")))
    if not out:
        raise BuildError("no matured signals found for the frozen universe")
    return out


def _load_archive(archive_dir: Path, symbol: str) -> tuple[list[dict], str, str]:
    """Return (sessions_oldest_first, stored_at, file_digest)."""
    path = archive_dir / f"{symbol}_5y.json"
    if not path.is_file():
        raise BuildError(f"price archive absent for {symbol}: {path}")
    raw = path.read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    rows = doc.get("rows") or []
    if not rows:
        raise BuildError(f"price archive empty for {symbol}")
    sessions = sorted(
        ({"date": str(r["date"])[:10], "close": float(r["close"])}
         for r in rows if r.get("date") and r.get("close")),
        key=lambda r: r["date"])
    # A duplicated session date would make the return series ambiguous.
    dates = [s["date"] for s in sessions]
    if len(set(dates)) != len(dates):
        raise BuildError(f"duplicate session dates in archive for {symbol}")
    return sessions, str(doc.get("stored_at") or ""), C.artifact_digest(
        {"sha256_of_file": __import__("hashlib").sha256(raw).hexdigest()})


def greedy_cohorts(signal_dates: list[str], horizon_days: int = C.HORIZON_DAYS
                   ) -> list[str]:
    """Non-overlapping cohorts: a cohort may begin only once the previous
    cohort's outcome window has closed. This is the independent unit of
    observation -- the defect VS-001 shipped was counting overlapping scans."""
    chosen: list[str] = []
    for d in sorted(set(signal_dates)):
        if not chosen:
            chosen.append(d)
            continue
        prev = datetime.strptime(chosen[-1], "%Y-%m-%d")
        cur = datetime.strptime(d, "%Y-%m-%d")
        if (cur - prev).days >= horizon_days:
            chosen.append(d)
    return chosen


def eligible_symbols(signals: list[C.SignalRow],
                     archives: dict[str, list[dict]]) -> tuple[list[str], dict[str, str]]:
    """Apply the universal >=252-prior-session rule. No ticker exceptions."""
    eligible: list[str] = []
    excluded: dict[str, str] = {}
    earliest_signal: dict[str, str] = {}
    for s in signals:
        d = s.signal_time[:10]
        if s.ticker not in earliest_signal or d < earliest_signal[s.ticker]:
            earliest_signal[s.ticker] = d
    for sym in sorted(set(C.FROZEN_UNIVERSE)):
        sessions = archives.get(sym)
        if not sessions:
            excluded[sym] = "no price archive"
            continue
        first = earliest_signal.get(sym)
        if first is None:
            excluded[sym] = "no matured signal"
            continue
        dates = [s["date"] for s in sessions]
        prior = bisect_left(dates, first)
        if prior < C.MIN_PRIOR_SESSIONS:
            excluded[sym] = (
                f"only {prior} prior sessions before first signal {first}; "
                f"universal rule requires >= {C.MIN_PRIOR_SESSIONS}")
            continue
        eligible.append(sym)
    return eligible, excluded


def _return_panel(archives: dict[str, list[dict]], symbols: list[str],
                  start_date: str, end_date: str) -> list[C.ReturnRow]:
    out: list[C.ReturnRow] = []
    for sym in sorted(symbols):
        sessions = archives[sym]
        for i in range(1, len(sessions)):
            d = sessions[i]["date"]
            if d < start_date or d > end_date:
                continue
            prev = sessions[i - 1]
            out.append(C.ReturnRow(
                symbol=sym, session_date=d, prev_session_date=prev["date"],
                close=sessions[i]["close"], prev_close=prev["close"],
                daily_return=C.derive_return(sessions[i]["close"], prev["close"])))
    return out


def build(repo_root: Path, *, db_rel: str = DEFAULT_DB_REL,
          archive_rel: str = DEFAULT_ARCHIVE_REL,
          out_rel: str = DEFAULT_OUT_REL,
          code_sha: str = "UNAVAILABLE",
          generated_at: Optional[str] = None) -> dict[str, Any]:
    """Build the package. Returns the manifest. Writes three files."""
    root = Path(repo_root)
    signals = _read_signals(root / db_rel)

    # Cutoff derived from the source, never assumed.
    cutoff = max(s.signal_time for s in signals)
    cutoff_date = cutoff[:10]

    archives: dict[str, list[dict]] = {}
    stored_at: dict[str, str] = {}
    digests: dict[str, str] = {}
    for sym in sorted(set(C.FROZEN_UNIVERSE) | {C.BENCHMARK}):
        try:
            sessions, st, dig = _load_archive(root / archive_rel, sym)
        except BuildError:
            if sym == C.BENCHMARK:
                raise
            continue
        archives[sym], stored_at[sym], digests[sym] = sessions, st, dig
    if C.BENCHMARK not in archives:
        raise BuildError("benchmark archive is mandatory")

    eligible, excluded = eligible_symbols(signals, archives)
    if not eligible:
        raise BuildError("no eligible symbols after the universal history rule")

    kept = set(eligible) | {C.BENCHMARK}
    signals = [s for s in signals if s.ticker in kept]

    earliest_signal_date = min(s.signal_time[:10] for s in signals)
    bench_dates = [r["date"] for r in archives[C.BENCHMARK]]
    idx = bisect_left(bench_dates, earliest_signal_date)
    start_idx = max(0, idx - C.MIN_PRIOR_SESSIONS - LOOKBACK_BUFFER_SESSIONS)
    start_date = bench_dates[start_idx]

    returns = _return_panel(archives, sorted(kept), start_date, cutoff_date)
    if not returns:
        raise BuildError("empty return panel")

    signal_dates = sorted({s.signal_time[:10] for s in signals})
    cohorts = greedy_cohorts(signal_dates)

    signals_payload = [s.to_dict() for s in
                       sorted(signals, key=lambda s: (s.ticker, s.signal_time))]
    returns_payload = [r.to_dict() for r in
                       sorted(returns, key=lambda r: (r.symbol, r.session_date))]

    per_symbol_returns = {}
    for r in returns_payload:
        per_symbol_returns[r["symbol"]] = per_symbol_returns.get(r["symbol"], 0) + 1

    manifest_core = {
        "schema_version": C.SCHEMA_VERSION,
        "schema_kind": C.SCHEMA_KIND,
        "experiment": "VS-002",
        "purpose": (
            "Frozen production evidence enabling a risk-adjusted VS-002. "
            "Contains recorded signals and derived daily returns only; grants "
            "nothing and executes nothing."),
        "builder_version": BUILDER_VERSION,
        "code_sha": code_sha,
        "signal_source": f"{db_rel} :: watchlist_signal_feedback",
        "signal_evidence_cutoff": cutoff,
        "frozen_universe": list(C.FROZEN_UNIVERSE),
        "benchmark": C.BENCHMARK,
        "eligible_universe": sorted(eligible),
        "excluded_symbols": excluded,
        "eligibility_rule": (
            f"A security is eligible at signal time T only if >= "
            f"{C.MIN_PRIOR_SESSIONS} prior trading sessions exist in this "
            "frozen archive. Applied to every symbol identically, using only "
            "pre-signal information, frozen before execution."),
        "new_listing_bias": (
            "The eligibility rule systematically removes recent listings, which "
            "have distinct volatility and beta instability. VS-002 conclusions "
            "will not generalize to new listings. Declared, not hidden."),
        "signal_row_count": len(signals_payload),
        "matured_scan_count": len({s["signal_time"] for s in signals_payload}),
        "signal_date_count": len(signal_dates),
        "non_overlapping_cohort_count": len(cohorts),
        "non_overlapping_cohort_dates": cohorts,
        "return_date_range": {"start": start_date, "end": cutoff_date},
        "return_row_count": len(returns_payload),
        "per_symbol_return_counts": per_symbol_returns,
        "archive_stored_at": {k: stored_at[k] for k in sorted(stored_at) if k in kept},
        "archive_digests": {k: digests[k] for k in sorted(digests) if k in kept},
        "return_derivation": C.BETA_FORMULA,
        "beta_convention": C.BETA_CONVENTION,
        "dividend_semantics_limitation": C.DIVIDEND_LIMITATION,
        "pit_certification": C.PIT_CERTIFICATION,
        "pit_certification_scope": C.PIT_CERTIFICATION_SCOPE,
        "authority_statement": (
            "This package is evidence. It grants no authority, certifies no "
            "strategy, and does not execute VS-002."),
        "artifact_digests": {
            SIGNALS_REL: C.artifact_digest(signals_payload),
            RETURNS_REL: C.artifact_digest(returns_payload),
        },
    }
    manifest = dict(manifest_core)
    manifest["generated_at"] = generated_at or datetime.now(
        timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest["package_id"] = C.package_id(manifest_core)

    out_dir = root / out_rel
    out_dir.mkdir(parents=True, exist_ok=True)
    _write(out_dir / SIGNALS_REL, signals_payload)
    _write(out_dir / RETURNS_REL, returns_payload)
    _write(out_dir / MANIFEST_REL, manifest)
    return manifest


def _write(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
