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
from portfolio_automation.vs002_evidence import snapshots as SN

SIGNALS_REL = "signals.json"
RETURNS_REL = "returns.json"
BARS_REL = "bars.json"
BARS_RAW_REL = "bars_raw.json"            # frozen provider input, pre-normalization
BARS_SNAPSHOTS_REL = "bars_snapshots.json"  # canonical evidence identities
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


class FMPDividendAdjustedProvider:
    """The ONE production provider for the adjusted-bar panel.

    Exists so the builder can demand an ENDPOINT IDENTITY rather than trust
    whatever rows it is handed: the identity is compared against the authorized
    endpoint before a single row is read. Tests inject synthetic providers with
    the same two-attribute surface; this class is the production binding and is
    NOT exercised offline (B3: no network, no credential, no fetch here).
    """

    endpoint = C.AUTHORIZED_ENDPOINT

    def __init__(self, client: Any) -> None:
        self._client = client

    def fetch(self, symbol: str) -> list[dict]:
        return self._client.get_historical_prices_dividend_adjusted(symbol)


def _normalize_bars(symbol: str, raw_rows: list[dict]) -> list[C.BarRow]:
    """Provider rows -> chronological BarRows. Every refusal is a BuildError.

    FAIL CLOSED, field by field: a row missing a required field, carrying a
    non-finite or non-positive price, or duplicating a session date is
    malformed evidence, and no version of it is admitted.
    """
    if not isinstance(raw_rows, list) or not raw_rows:
        raise BuildError(f"{symbol}: provider returned no bar rows")
    bars: list[C.BarRow] = []
    for row in raw_rows:
        if not isinstance(row, dict):
            raise BuildError(f"{symbol}: malformed provider row {type(row).__name__}")
        missing = [f for f in C.REQUIRED_PROVIDER_FIELDS if row.get(f) is None]
        if missing:
            raise BuildError(
                f"{symbol}: provider row missing required field(s) {missing} — "
                f"the claimed semantics cannot be established from weaker data")
        try:
            close = float(row["close"])
            adj_close = float(row["adjClose"])
            volume = int(row["volume"])
        except (TypeError, ValueError) as exc:
            raise BuildError(f"{symbol}: malformed value in provider row: {exc}")
        if not (close > 0 and adj_close > 0 and close == close
                and adj_close == adj_close and close not in (float("inf"),)
                and adj_close not in (float("inf"),)):
            raise BuildError(
                f"{symbol}: non-finite or non-positive price on "
                f"{str(row['date'])[:10]}")
        if volume < 0:
            raise BuildError(f"{symbol}: negative volume on {str(row['date'])[:10]}")
        bars.append(C.BarRow(symbol=symbol, session_date=str(row["date"])[:10],
                             close=close, adj_close=adj_close, volume=volume))
    bars.sort(key=lambda b: b.session_date)
    dates = [b.session_date for b in bars]
    if len(set(dates)) != len(dates):
        dupes = sorted({d for d in dates if dates.count(d) > 1})
        raise BuildError(f"{symbol}: duplicate session dates {dupes[:3]}")
    return bars


def verify_adjustment_semantics(bars: list[C.BarRow], *,
                                is_benchmark: bool) -> list[str]:
    """The DIJ-0011 battery, deterministic and re-runnable by any consumer.

    What each check PROVES, stated exactly:

    * ratio = adj_close/close is the cumulative adjustment factor for actions
      AFTER that session. For a genuinely dividend/split-adjusted series it is
      non-decreasing over time (every action shrinks earlier adjusted values
      relative to close) and ~1.0 on the latest session. A violated monotone
      is an artificial discontinuity; a final ratio away from 1 is a series
      adjusted to some other vintage.
    * splits and dividends both express as ratio steps, so the same two checks
      cover both behaviours; the synthetic fixtures exercise each separately.
    * the BENCHMARK must show at least one genuine adjustment over a long
      span. SPY pays quarterly dividends; 200+ sessions of ratio == 1.0 is how
      an unadjusted series masquerades as adjusted, and DIJ-0011 names exactly
      this as the mutation that must fail. Single names may legitimately show
      ratio == 1 (non-payers, no splits), so the witness is scoped to the
      benchmark rather than generalized into a false positive.
    """
    findings: list[str] = []
    if not bars:
        return ["empty bar series"]
    sym = bars[0].symbol
    ratios = [b.adj_close / b.close for b in bars]
    if abs(ratios[-1] - 1.0) > C.RATIO_FINAL_TOLERANCE:
        findings.append(
            f"{sym}: final adj_close/close ratio {ratios[-1]:.6f} is not ~1 — "
            f"the series is adjusted to some other vintage")
    for i in range(1, len(ratios)):
        if ratios[i] < ratios[i - 1] * (1.0 - C.RATIO_MONOTONE_TOLERANCE):
            findings.append(
                f"{sym}: adjustment ratio decreases at {bars[i].session_date} "
                f"({ratios[i - 1]:.6f} -> {ratios[i]:.6f}) — an artificial "
                f"discontinuity no real corporate action produces")
            break
    if is_benchmark and len(bars) >= 200:
        if all(abs(r - 1.0) <= C.RATIO_MONOTONE_TOLERANCE for r in ratios):
            findings.append(
                f"{sym}: benchmark shows NO adjustment over {len(bars)} "
                f"sessions — indistinguishable from an unadjusted series, so "
                f"the dividend-adjusted claim is not established")
    return findings


def _session_gaps(symbol_dates: list[str], benchmark_dates: list[str]) -> int:
    """Largest number of BENCHMARK sessions skipped between two consecutive
    symbol sessions. The benchmark is the session ruler: a symbol that misses
    more than MAX_SESSION_GAP of them inside its own span has a hole no
    'claimed continuous window' may paper over."""
    index = {d: i for i, d in enumerate(benchmark_dates)}
    positions = [index[d] for d in symbol_dates if d in index]
    worst = 0
    for a, b in zip(positions, positions[1:]):
        worst = max(worst, b - a - 1)
    return worst


def _utc_now() -> datetime:
    """The production acquisition clock. Never called in hermetic tests, which
    inject a deterministic clock so retrieval time is reproducible."""
    return datetime.now(timezone.utc)


def build_adjusted_bars(provider: Any, symbols: list[str], *,
                        clock: Any = _utc_now,
                        benchmark_first: bool = True) -> tuple[
                            dict[str, list[C.BarRow]], dict[str, list],
                            dict[str, str], dict[str, datetime]]:
    """Fetch + validate the panel through the provider boundary.

    Refuses, in order: a provider whose declared endpoint is not the authorized
    one (endpoint substitution is a refusal, never a fallback); an absent
    benchmark; any per-symbol normalization or semantics failure; a gap wider
    than the frozen contract allows.

    ``clock`` is called ONCE per symbol, immediately after that symbol's
    provider response returns, to record a truthful per-symbol retrieval time.
    It is a build-side acquisition fact: the production binding reads the real
    clock, tests inject a deterministic one, and the lab consumer never
    supplies it.

    Returns ``(panel, raw_responses, raw_digests, retrieved_at)``:
    the normalized bars, the EXACT parsed provider rows (frozen, pre-
    normalization), the per-symbol raw digest, and the per-symbol retrieval
    timestamp.
    """
    declared = getattr(provider, "endpoint", None)
    if declared != C.AUTHORIZED_ENDPOINT:
        raise BuildError(
            f"provider declares endpoint {declared!r}, not the authorized "
            f"{C.AUTHORIZED_ENDPOINT!r} — endpoint substitution is refused, "
            f"never adopted")

    ordered = sorted(set(symbols))
    if C.BENCHMARK not in ordered:
        raise BuildError("benchmark bars are mandatory")
    panel: dict[str, list[C.BarRow]] = {}
    raw_responses: dict[str, list] = {}
    raw_digests: dict[str, str] = {}
    retrieved_at: dict[str, datetime] = {}
    for sym in ([C.BENCHMARK] + [s for s in ordered if s != C.BENCHMARK]
                if benchmark_first else ordered):
        raw = provider.fetch(sym)
        stamp = clock()
        if not isinstance(stamp, datetime) or stamp.tzinfo is None:
            raise BuildError(
                f"{sym}: acquisition clock returned {stamp!r}, not a tz-aware "
                f"datetime — retrieval time must be a real, tz-aware fact")
        try:
            raw_digests[sym] = C.artifact_digest(raw)
        except Exception as exc:
            raise BuildError(
                f"{sym}: provider response is not canonicalizable "
                f"({type(exc).__name__}: {exc}) — malformed evidence is "
                f"refused, never coerced")
        bars = _normalize_bars(sym, raw)
        findings = verify_adjustment_semantics(bars,
                                               is_benchmark=sym == C.BENCHMARK)
        if findings:
            raise BuildError("; ".join(findings))
        panel[sym] = bars
        # The raw rows are frozen EXACTLY as returned — provider order,
        # unnormalized — so the consumer can recompute the digest and
        # re-derive the normalized bars from the same bytes.
        raw_responses[sym] = list(raw)
        retrieved_at[sym] = stamp

    bench_dates = [b.session_date for b in panel[C.BENCHMARK]]
    for sym, bars in panel.items():
        if sym == C.BENCHMARK:
            continue
        gap = _session_gaps([b.session_date for b in bars], bench_dates)
        if gap > C.MAX_SESSION_GAP:
            raise BuildError(
                f"{sym}: gap of {gap} benchmark sessions inside the series — "
                f"the frozen contract allows at most {C.MAX_SESSION_GAP}, and "
                f"forward-filling across it would invent sessions")
    return panel, raw_responses, raw_digests, retrieved_at


def window_bar_payload(panel: dict[str, list[C.BarRow]], kept: set[str],
                       start_date: str, end_date: str) -> list[dict[str, Any]]:
    """The ONE deterministic normalized-bar projection, shared by the builder
    and by the consumer's raw->normalized reconstruction so the two cannot
    diverge. Every bar in the inclusive [start, end] window, sorted."""
    out: list[dict[str, Any]] = []
    for sym in sorted(kept):
        for b in panel.get(sym, []):
            if start_date <= b.session_date <= end_date:
                out.append(b.to_dict())
    out.sort(key=lambda r: (r["symbol"], r["session_date"]))
    return out


def reconstruct_bars_from_raw(raw_responses: dict[str, list], kept: set[str],
                              start_date: str, end_date: str
                              ) -> list[dict[str, Any]]:
    """Re-derive the normalized bar payload from FROZEN raw provider input,
    using the same normalization and windowing the builder used. The consumer
    compares this against the persisted bars.json: equal means the normalized
    artifact was genuinely derived from the frozen raw response, not paired
    with it."""
    panel = {sym: _normalize_bars(sym, raw_responses[sym])
             for sym in kept if sym in raw_responses}
    return window_bar_payload(panel, kept, start_date, end_date)


def bar_eligibility(panel: dict[str, list[C.BarRow]],
                    earliest_signal: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """The universal >=252-prior-session rule, applied to the BAR panel.

    Same rule, same wording, no ticker exceptions — a short-history symbol is
    excluded by arithmetic, exactly as the returns-path eligibility does it.
    """
    eligible: list[str] = []
    excluded: dict[str, str] = {}
    for sym in sorted(k for k in panel if k != C.BENCHMARK):
        first = earliest_signal.get(sym)
        if first is None:
            excluded[sym] = "no matured signal"
            continue
        dates = [b.session_date for b in panel[sym]]
        prior = bisect_left(dates, first)
        if prior < C.MIN_PRIOR_SESSIONS:
            excluded[sym] = (
                f"only {prior} prior sessions before first signal {first}; "
                f"universal rule requires >= {C.MIN_PRIOR_SESSIONS}")
            continue
        eligible.append(sym)
    return eligible, excluded


def build(repo_root: Path, *, db_rel: str = DEFAULT_DB_REL,
          archive_rel: str = DEFAULT_ARCHIVE_REL,
          out_rel: str = DEFAULT_OUT_REL,
          code_sha: str = "UNAVAILABLE",
          generated_at: Optional[str] = None,
          bar_provider: Any = None,
          bar_clock: Any = None) -> dict[str, Any]:
    """Build the package. Returns the manifest.

    With ``bar_provider`` (the production path, via
    :class:`FMPDividendAdjustedProvider`) the package also carries the
    dividend-adjusted bar panel — the historical risk evidence VS-002 is
    blocked on. Without it the legacy three-artifact package is produced and
    readiness will truthfully report the risk evidence as missing.
    """
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

    # ---- the dividend-adjusted bar panel (bounded 0C prerequisite) --------
    bars_payload: list[dict[str, Any]] = []
    bars_raw_payload: dict[str, list] = {}
    bars_snapshots_payload: list[dict[str, Any]] = []
    bar_manifest: dict[str, Any] = {}
    if bar_provider is not None:
        earliest_by_symbol: dict[str, str] = {}
        for s_ in signals:
            d = s_.signal_time[:10]
            if s_.ticker not in earliest_by_symbol or d < earliest_by_symbol[s_.ticker]:
                earliest_by_symbol[s_.ticker] = d
        panel, raw_responses, raw_digests, retrieved_at = build_adjusted_bars(
            bar_provider, sorted(set(C.FROZEN_UNIVERSE) | {C.BENCHMARK}),
            clock=bar_clock or _utc_now)
        bar_eligible, bar_excluded = bar_eligibility(panel, earliest_by_symbol)
        if not bar_eligible:
            raise BuildError(
                "no symbol passes the universal history rule on the adjusted "
                "bar panel")
        kept_bars = set(bar_eligible) | {C.BENCHMARK}
        # Bounded like the return panel: the experiment needs the pre-signal
        # window plus the scored span, never the whole provider history.
        bench_bar_dates = [b.session_date for b in panel[C.BENCHMARK]]
        first_signal_date = min(earliest_by_symbol.values())
        bar_idx = bisect_left(bench_bar_dates, first_signal_date)
        bar_start_idx = max(0, bar_idx - C.MIN_PRIOR_SESSIONS
                            - LOOKBACK_BUFFER_SESSIONS)
        bar_start = bench_bar_dates[bar_start_idx]
        # ONE shared projection, reused by the consumer's raw->normalized check.
        bars_payload = window_bar_payload(panel, kept_bars, bar_start, cutoff_date)
        # Finding A: freeze the EXACT parsed provider rows, pre-normalization,
        # for every kept symbol. Deterministic symbol order; row order exactly
        # as the provider returned it.
        bars_raw_payload = {sym: list(raw_responses[sym])
                            for sym in sorted(kept_bars) if sym in raw_responses}
        # Finding B: per-symbol retrieval time is an immutable package fact.
        bar_retrieved_at = {
            sym: retrieved_at[sym].astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            for sym in sorted(kept_bars) if sym in retrieved_at}
        # Finding C: build the canonical snapshots HERE and freeze their
        # identities, using the package-bound retrieval time (never a caller
        # value). Snapshot identity is derived from the SAME second-precision
        # timestamp that is persisted -- parsed back from the manifest strings
        # -- so the build-side and lab-side identities are byte-identical
        # rather than differing by a discarded sub-second component.
        retrieved_bound = SN.parse_retrieved_at(bar_retrieved_at)
        snaps = SN.panel_snapshots_bound(
            bars_payload, retrieved_at=retrieved_bound, raw_digests=raw_digests)
        bars_snapshots_payload = [SN.snapshot_identity(sn) for sn in snaps]
        descriptor = C.data_source_descriptor()
        per_symbol_bars: dict[str, int] = {}
        for b in bars_payload:
            per_symbol_bars[b["symbol"]] = per_symbol_bars.get(b["symbol"], 0) + 1
        bar_manifest = {
            "bar_endpoint": C.AUTHORIZED_ENDPOINT,
            "bar_source_provider": C.SOURCE_PROVIDER,
            "bar_source_dataset": C.SOURCE_DATASET,
            "bar_source_id": descriptor.source_id,
            "bar_evidence_type": C.EVIDENCE_TYPE_BAR,
            "bar_eligible_universe": sorted(bar_eligible),
            "bar_excluded_symbols": bar_excluded,
            "bar_raw_response_digests": {
                k: raw_digests[k] for k in sorted(kept_bars)},
            "bar_retrieved_at": bar_retrieved_at,
            "bar_row_count": len(bars_payload),
            "bar_snapshot_count": len(bars_snapshots_payload),
            "per_symbol_bar_counts": per_symbol_bars,
            "bar_date_range": {"start": bar_start, "end": cutoff_date},
            "bar_max_session_gap": C.MAX_SESSION_GAP,
            "adjusted_return_derivation": C.ADJUSTED_BETA_FORMULA,
            "adjusted_beta_convention": C.ADJUSTED_BETA_CONVENTION,
            "adjusted_pit_certification": C.ADJUSTED_PIT_CERTIFICATION,
            "adjusted_pit_certification_scope":
                C.ADJUSTED_PIT_CERTIFICATION_SCOPE,
            "risk_free_rate_7d": dict(C.RISK_FREE_RATE_7D_ASSUMPTION),
        }

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
            **({BARS_REL: C.artifact_digest(bars_payload),
                BARS_RAW_REL: C.artifact_digest(bars_raw_payload),
                BARS_SNAPSHOTS_REL: C.artifact_digest(bars_snapshots_payload)}
               if bar_manifest else {}),
        },
        **bar_manifest,
    }
    manifest = dict(manifest_core)
    manifest["generated_at"] = generated_at or datetime.now(
        timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest["package_id"] = C.package_id(manifest_core)

    out_dir = root / out_rel
    out_dir.mkdir(parents=True, exist_ok=True)
    _write(out_dir / SIGNALS_REL, signals_payload)
    _write(out_dir / RETURNS_REL, returns_payload)
    if bar_manifest:
        _write(out_dir / BARS_REL, bars_payload)
        _write(out_dir / BARS_RAW_REL, bars_raw_payload)
        _write(out_dir / BARS_SNAPSHOTS_REL, bars_snapshots_payload)
    _write(out_dir / MANIFEST_REL, manifest)
    return manifest


def _write(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
