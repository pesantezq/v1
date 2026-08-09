"""Exact session reconciliation, fail-closed admission, immutable canonical
datasets and deterministic manifests. Research-only, HISTORICAL namespace.

THE SESSION 2 INVARIANT — bar-count equality is NOT completeness:

    observed_bar_start_times == expected_bar_start_times

A session with 78 observed and 78 expected bars is still REJECTED if the 10:05
bar is absent and an off-grid bar took its place. Counting would admit that
silently; set comparison cannot.

Nothing is ever repaired. No forward fill, no interpolation, no invented volume,
no dropping bad timestamps, no shifting bars onto the expected grid, no padding.
A data defect is evidence, and hiding it would corrupt every experiment built on
top of the dataset.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence

from portfolio_automation.intraday_lab.calendar import (
    TradingSession, resolve_session, calendar_provenance, SESSION_UNCERTIFIED,
)
from portfolio_automation.intraday_lab.models import IntradayBar
from portfolio_automation.intraday_lab.validation import (
    SESSION_MARKET_CLOSED, DuplicateBarError,
)

SCHEMA_VERSION = "2"
# v3 (2026-08-09): adds bar_end_at and known_at. v2 hashed only OHLCV +
# bar_start_at, so two datasets whose bars became knowable at DIFFERENT times
# shared one identity. A dataset that publishes a bar 60s earlier confers a
# look-ahead advantage and is NOT research-equivalent -- verified before the
# change. Temporal knowability is the core PIT contract; it must be part of
# canonical identity, not metadata beside it.
FINGERPRINT_SCHEMA = "intraday_canonical_v3"

ADMITTED = "ADMITTED"
REJECTED_MISSING_BARS = "REJECTED_MISSING_BARS"
REJECTED_SURPLUS_BARS = "REJECTED_SURPLUS_BARS"
REJECTED_OFF_GRID = "REJECTED_OFF_GRID"
REJECTED_CONFLICTING_DUPLICATE = "REJECTED_CONFLICTING_DUPLICATE"
REJECTED_EXACT_DUPLICATE = "REJECTED_EXACT_DUPLICATE"
REJECTED_IDENTITY_MISMATCH = "REJECTED_IDENTITY_MISMATCH"
REJECTED_PROVIDER_ERROR = "REJECTED_PROVIDER_ERROR"
REJECTED_UNEXPECTED_PROVIDER_RESULT = "REJECTED_UNEXPECTED_PROVIDER_RESULT"
REJECTED_NORMALIZATION_ERROR = "REJECTED_NORMALIZATION_ERROR"
NOT_A_TRADING_SESSION = "NOT_A_TRADING_SESSION"

# How the calendar resolved a REQUESTED date. Distinct from admission: a
# requested Saturday is correctly not-a-session, which is not a rejection.
CAL_EXPECTED_TRADING_SESSION = "EXPECTED_TRADING_SESSION"
CAL_MARKET_CLOSED = "MARKET_CLOSED"
CAL_UNCERTIFIED = "CALENDAR_UNCERTIFIED"
REJECTED_CALENDAR_UNCERTIFIED = "REJECTED_CALENDAR_UNCERTIFIED"
REJECTED_CLOSED_SESSION_HAS_BARS = "REJECTED_CLOSED_SESSION_HAS_BARS"
REJECTED_MIXED_ADJUSTMENT = "REJECTED_MIXED_ADJUSTMENT"


def _calendar_identity() -> dict:
    """Deterministic calendar semantics for the manifest hash.

    The manifest fingerprint must change when calendar MEANING changes, even if
    the admitted bars are byte-identical: the same bars interpreted under a
    different holiday/early-close table answer a different research question.
    Transient generation timestamps are excluded.
    """
    from portfolio_automation.intraday_lab import calendar as _cal
    # Delegated so there is ONE definition of calendar meaning. Enumerating the
    # holiday/early-close tables here worked only while they were hand-written;
    # an authoritative calendar spans thousands of sessions, so identity is now
    # a digest of the schedule itself (see calendar.calendar_identity).
    return _cal.calendar_identity()


def calendar_fingerprint() -> str:
    return hashlib.sha256(
        json.dumps(_calendar_identity(), separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()[:32]


def manifest_fingerprint_from_parts(*, content_fingerprint: str,
                                    request: dict | None, calendar: dict,
                                    timeframe: str, adjustment_state: str,
                                    sessions: list) -> str:
    """The manifest identity algorithm, as a pure function of its parts.

    Extracted so that reminting a manifest during identity migration uses THE
    SAME algorithm as minting one, rather than a hand-rolled copy that could
    drift. `calendar` is a parameter, not a lookup: a remint must reproduce the
    calendar meaning the original manifest was built under, never silently
    reinterpret an archived manifest under a newer calendar.
    """
    payload = {
        "schema": "intraday_manifest_v1",
        "content_fingerprint": content_fingerprint,
        "request": request,
        "calendar": calendar,
        "timeframe": timeframe,
        "adjustment_state": adjustment_state,
        "sessions": sessions,
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()[:32]


@dataclass(frozen=True)
class SessionReconciliation:
    symbol: str
    market_date: date
    timeframe: str
    session_type: str
    expected_count: int
    observed_count: int
    missing_timestamps: tuple[datetime, ...]
    unexpected_timestamps: tuple[datetime, ...]
    conflicting_duplicates: tuple[str, ...]
    admission_status: str
    rejection_reasons: tuple[str, ...]

    @property
    def admitted(self) -> bool:
        return self.admission_status == ADMITTED

    @property
    def coverage_pct(self) -> float | None:
        if not self.expected_count:
            return 100.0 if not self.observed_count else 0.0
        return round(100.0 * (self.expected_count - len(self.missing_timestamps))
                     / self.expected_count, 2)

    def summary(self) -> dict:
        """Compact form for artifacts — full timestamp arrays stay in the
        rejection report rather than bloating every manifest row."""
        return {
            "symbol": self.symbol,
            "market_date": self.market_date.isoformat(),
            "timeframe": self.timeframe,
            "session_type": self.session_type,
            "expected_count": self.expected_count,
            "observed_count": self.observed_count,
            "missing_count": len(self.missing_timestamps),
            "unexpected_count": len(self.unexpected_timestamps),
            "coverage_pct": self.coverage_pct,
            "admission_status": self.admission_status,
            "rejection_reasons": list(self.rejection_reasons),
        }

    def detail(self, limit: int = 25) -> dict:
        d = self.summary()
        d["missing_timestamps"] = [t.isoformat() for t in self.missing_timestamps[:limit]]
        d["unexpected_timestamps"] = [t.isoformat() for t in self.unexpected_timestamps[:limit]]
        d["conflicting_duplicates"] = list(self.conflicting_duplicates[:limit])
        d["truncated"] = (len(self.missing_timestamps) > limit
                          or len(self.unexpected_timestamps) > limit)
        return d


def reconcile_session(bars: Sequence[IntradayBar], session: TradingSession, *,
                      symbol: str, timeframe: str = "5min") -> SessionReconciliation:
    """Compare the EXACT observed timestamp set against the calendar grid."""
    reasons: list[str] = []
    status = ADMITTED

    # Conflicting duplicates first: two bars claiming one slot with different
    # values make the whole session unreconcilable.
    by_start: dict[datetime, IntradayBar] = {}
    conflicts: list[str] = []
    exact_dupes: list[str] = []
    for bar in bars:
        prior = by_start.get(bar.bar_start_at)
        if prior is None:
            by_start[bar.bar_start_at] = bar
        elif (prior.open, prior.high, prior.low, prior.close, prior.volume) != (
                bar.open, bar.high, bar.low, bar.close, bar.volume):
            conflicts.append(bar.bar_start_at.isoformat())
        else:
            # An EXACT duplicate also rejects. It collapses in the observed SET
            # while the underlying sequence still carries it, so a 78-bar
            # session was producing 79 canonical rows. Research input is never
            # silently deduplicated.
            exact_dupes.append(bar.bar_start_at.isoformat())

    # Trust the BAR, not the outer dict key: a mislabelled mapping would
    # otherwise admit another symbol's or another day's bars under this key.
    identity_problems: list[str] = []
    wrong_symbol = sorted({b.symbol for b in bars if b.symbol != symbol})
    if wrong_symbol:
        identity_problems.append(
            f"bars carry symbol(s) {wrong_symbol} but were requested as {symbol}")
    wrong_tf = sorted({b.timeframe for b in bars if b.timeframe != timeframe})
    if wrong_tf:
        identity_problems.append(
            f"bars carry timeframe(s) {wrong_tf} but were requested as {timeframe}")

    observed = set(by_start)
    expected = set(session.expected_bar_starts)
    missing = tuple(sorted(expected - observed))
    unexpected = tuple(sorted(observed - expected))

    # Adjustment integrity is checked FIRST and never overwritten: mixing
    # regimes changes what a return means, so it outranks any grid defect. An
    # earlier version ran this last and clobbered whatever status the grid
    # checks had set, making the reported reason depend on check order.
    states = {b.adjustment_state for b in bars}
    if len(states) > 1:
        return SessionReconciliation(
            symbol=symbol, market_date=session.market_date, timeframe=timeframe,
            session_type=session.session_type,
            expected_count=len(session.expected_bar_starts),
            observed_count=len({b.bar_start_at for b in bars}),
            missing_timestamps=(), unexpected_timestamps=(),
            conflicting_duplicates=(),
            admission_status=REJECTED_MIXED_ADJUSTMENT,
            rejection_reasons=(f"mixed adjustment states {sorted(states)}",))

    if session.session_type == SESSION_UNCERTIFIED:
        status = REJECTED_CALENDAR_UNCERTIFIED
        reasons.append(
            f"{session.market_date} lies outside the certified holiday window; "
            f"its expected grid cannot be verified")
    elif conflicts:
        status = REJECTED_CONFLICTING_DUPLICATE
        reasons.append(f"{len(conflicts)} conflicting duplicate bar(s)")
    elif exact_dupes:
        status = REJECTED_EXACT_DUPLICATE
        reasons.append(
            f"{len(exact_dupes)} exact duplicate bar(s) — the observed SET hides "
            f"them but the sequence would inflate the canonical dataset")
    elif identity_problems:
        status = REJECTED_IDENTITY_MISMATCH
        reasons.extend(identity_problems)
    elif session.session_type == SESSION_MARKET_CLOSED and not observed:
        # Correctly not a trading session; no provider call was owed.
        status = NOT_A_TRADING_SESSION
    elif session.session_type == SESSION_MARKET_CLOSED and observed:
        status = REJECTED_CLOSED_SESSION_HAS_BARS
        reasons.append(
            f"calendar says the market was closed but {len(observed)} bars were "
            f"returned — calendar and data disagree")
    elif missing and unexpected:
        # Equal counts with a swapped slot land here: the defect a count check
        # cannot see.
        status = REJECTED_OFF_GRID
        reasons.append(
            f"{len(missing)} expected timestamp(s) absent while "
            f"{len(unexpected)} off-grid timestamp(s) present")
    elif missing:
        status = REJECTED_MISSING_BARS
        reasons.append(f"{len(missing)} expected timestamp(s) absent")
    elif unexpected:
        status = REJECTED_SURPLUS_BARS
        reasons.append(
            f"{len(unexpected)} timestamp(s) outside the expected grid — "
            f"possible extended-hours contamination or normalization error")

    return SessionReconciliation(
        symbol=symbol, market_date=session.market_date, timeframe=timeframe,
        session_type=session.session_type, expected_count=len(expected),
        observed_count=len(observed), missing_timestamps=missing,
        unexpected_timestamps=unexpected, conflicting_duplicates=tuple(conflicts),
        admission_status=status, rejection_reasons=tuple(reasons))


@dataclass(frozen=True)
class CanonicalDataset:
    """Immutable admitted-bars-only dataset."""
    bars: tuple[IntradayBar, ...]
    reconciliations: tuple[SessionReconciliation, ...]
    timeframe: str
    adjustment_state: str
    request: "DatasetRequest | None" = None

    def manifest_fingerprint(self) -> str:
        """Identity of the dataset's MEANING, not just its bytes.

        "Aug 1-10 with 3 rejections" and "only the 7 admitted days" can produce
        byte-identical bars yet answer different research questions. Experiments
        bind here; storage dedupes on the content fingerprint.
        """
        return manifest_fingerprint_from_parts(
            content_fingerprint=self.fingerprint(),
            request=self.request.to_dict() if self.request else None,
            calendar=_calendar_identity(),
            timeframe=self.timeframe,
            adjustment_state=self.adjustment_state,
            sessions=[[r.symbol, r.market_date.isoformat(), r.admission_status]
                      for r in self.reconciliations])

    @property
    def admitted(self) -> tuple[SessionReconciliation, ...]:
        return tuple(r for r in self.reconciliations if r.admitted)

    @property
    def rejected(self) -> tuple[SessionReconciliation, ...]:
        """Genuine rejections. A requested non-trading date is accounted for but
        is not a failure — it is the calendar answering correctly."""
        return tuple(r for r in self.reconciliations
                     if not r.admitted and r.admission_status != NOT_A_TRADING_SESSION)

    @property
    def not_trading(self) -> tuple[SessionReconciliation, ...]:
        return tuple(r for r in self.reconciliations
                     if r.admission_status == NOT_A_TRADING_SESSION)

    @property
    def symbols(self) -> list[str]:
        return sorted({b.symbol for b in self.bars})

    def fingerprint(self) -> str:
        return canonical_fingerprint(self.bars, timeframe=self.timeframe,
                                     adjustment_state=self.adjustment_state)

    def dataset_id(self) -> str:
        return f"intraday-{self.timeframe}-{self.fingerprint()[:16]}"


def canonical_fingerprint(bars: Iterable[IntradayBar], *, timeframe: str,
                          adjustment_state: str) -> str:
    """Deterministic research identity.

    v2 extends the Session 1 hash to cover `adjustment_state`, `timeframe` and
    the schema tag, because mixing adjustment regimes changes what a return
    MEANS while leaving OHLCV byte-identical — two datasets that differ only in
    adjustment must not share an identity.

    `retrieved_at` is still excluded: re-fetching identical history must
    reproduce the fingerprint, or every experiment is irreproducible by
    construction.
    """
    # `source` / `source_endpoint` are deliberately EXCLUDED from canonical
    # identity: they describe where observations came from, not what they mean
    # for research. Two providers reporting the identical bar with identical
    # knowability yield the same canonical research object. Source semantics are
    # not lost -- they are part of RAW content identity (storage.raw_payload_hash)
    # and of the acquisition event, so a source change still changes an
    # authoritative upstream identity and is fully auditable.
    rows = sorted(
        ([b.symbol, b.timeframe, b.bar_start_at.isoformat(),
          b.bar_end_at.isoformat(), b.known_at.isoformat(),
          b.open, b.high, b.low, b.close, b.volume] for b in bars),
        key=lambda r: (r[0], r[1], r[2]))
    payload = {"schema": FINGERPRINT_SCHEMA, "timeframe": timeframe,
               "adjustment_state": adjustment_state, "rows": rows}
    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class DatasetRequest:
    """Authoritative statement of what was ASKED FOR.

    Requested coverage is never inferred from what the provider returned. An
    entire session that came back empty, errored, or was simply absent from the
    result mapping must still appear in the reconciliation trail — otherwise it
    silently disappears and the dataset looks complete while covering a
    narrower window than requested. Observed live before this change: three
    requested days produced two reconciliations.
    """
    symbols: tuple[str, ...]
    start: date
    end: date
    timeframe: str = "5min"

    def resolved_items(self) -> list[tuple[str, date, str]]:
        """EVERY requested (symbol, date) with its calendar resolution.

        Nothing is filtered out. An earlier version kept only dates with
        expected bars, so a requested 2023 weekday (UNCERTIFIED) or a requested
        Saturday (CLOSED) vanished from the request record entirely — the
        manifest could not show that they had been asked for at all.
        """
        from portfolio_automation.intraday_lab.calendar import (
            sessions_in_range, SESSION_UNCERTIFIED,
        )
        out = []
        for session in sessions_in_range(self.start, self.end):
            if session.session_type == SESSION_UNCERTIFIED:
                status = CAL_UNCERTIFIED
            elif session.expected_bar_count:
                status = CAL_EXPECTED_TRADING_SESSION
            else:
                status = CAL_MARKET_CLOSED
            for symbol in self.symbols:
                out.append((symbol, session.market_date, status))
        return sorted(out)

    def certified_sessions(self) -> list[tuple[str, date]]:
        """Only the (symbol, date) pairs that require a provider call."""
        return [(s, d) for s, d, st in self.resolved_items()
                if st == CAL_EXPECTED_TRADING_SESSION]

    def calendar_resolution_summary(self) -> dict:
        items = self.resolved_items()
        return {
            "requested_symbol_date_count": len(items),
            "expected_trading_sessions": sum(1 for *_, s in items
                                             if s == CAL_EXPECTED_TRADING_SESSION),
            "closed_dates": sum(1 for *_, s in items if s == CAL_MARKET_CLOSED),
            "uncertified_dates": sum(1 for *_, s in items if s == CAL_UNCERTIFIED),
            "provider_calls_planned": len(self.certified_sessions()),
        }

    def fingerprint(self) -> str:
        payload = {"schema": "intraday_request_v1", "symbols": sorted(self.symbols),
                   "start": self.start.isoformat(), "end": self.end.isoformat(),
                   "timeframe": self.timeframe}
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()[:32]

    def to_dict(self) -> dict:
        return {"symbols": sorted(self.symbols), "start": self.start.isoformat(),
                "end": self.end.isoformat(), "timeframe": self.timeframe,
                "request_fingerprint": self.fingerprint(),
                **self.calendar_resolution_summary()}


def build_canonical_dataset(
    bars_by_date: dict[tuple[str, date], Sequence[IntradayBar]], *,
    request: "DatasetRequest | None" = None,
    timeframe: str = "5min", adjustment_state: str | None = None,
    provider_failures: set[str] | None = None,
    normalization_failures: set[str] | None = None,
) -> CanonicalDataset:
    """Reconcile EVERY requested session; admit only exact matches.

    When `request` is supplied its certified session matrix drives the loop, so
    a session absent from `bars_by_date` still yields a reconciliation (with an
    empty bar list → REJECTED_MISSING_BARS) instead of vanishing.

    `adjustment_state` is DERIVED from the admitted bars, never taken from the
    caller. A caller label could otherwise disagree with the data — a dataset
    was observed labelled `split_adjusted` while containing both regimes.
    """
    if request:
        timeframe = request.timeframe
        keys = [(sym, d) for sym, d, _ in request.resolved_items()]
        # Provider results outside the authorized request matrix are drift, not
        # a bonus. They must surface rather than be quietly ignored.
        keys = keys + sorted(set(bars_by_date) - set(keys))
    else:
        keys = sorted(bars_by_date)
    unauthorized = (set(bars_by_date) - {(s, d) for s, d, _ in request.resolved_items()}
                    if request else set())

    admitted_bars: list[IntradayBar] = []
    recs: list[SessionReconciliation] = []
    for (symbol, market_date) in keys:
        bars = bars_by_date.get((symbol, market_date), [])
        session = resolve_session(market_date)
        if (symbol, market_date) in unauthorized:
            recs.append(SessionReconciliation(
                symbol=symbol, market_date=market_date, timeframe=timeframe,
                session_type=session.session_type, expected_count=0,
                observed_count=len(bars), missing_timestamps=(),
                unexpected_timestamps=(), conflicting_duplicates=(),
                admission_status=REJECTED_UNEXPECTED_PROVIDER_RESULT,
                rejection_reasons=(
                    f"provider returned {symbol} {market_date} which is outside "
                    f"the authorized request matrix — query or normalization drift",)))
            continue
        rec = reconcile_session(bars, session, symbol=symbol, timeframe=timeframe)
        # A provider outage is NOT a market-data defect. Collapsing it into
        # REJECTED_MISSING_BARS would blame the market for our own failed call
        # and destroy the causal trail.
        if (normalization_failures and symbol in normalization_failures
                and rec.admission_status == REJECTED_MISSING_BARS):
            # The provider answered; WE could not interpret it. Reporting this
            # as missing market data would blame the market for our own schema
            # break, and would hide a provider-format change entirely.
            rec = SessionReconciliation(
                symbol=rec.symbol, market_date=rec.market_date,
                timeframe=rec.timeframe, session_type=rec.session_type,
                expected_count=rec.expected_count, observed_count=rec.observed_count,
                missing_timestamps=rec.missing_timestamps,
                unexpected_timestamps=(), conflicting_duplicates=(),
                admission_status=REJECTED_NORMALIZATION_ERROR,
                rejection_reasons=(
                    f"provider returned rows for {symbol} but normalization "
                    f"failed — response schema drift, not absent market data",))
        elif (provider_failures and symbol in provider_failures
                and rec.admission_status == REJECTED_MISSING_BARS):
            rec = SessionReconciliation(
                symbol=rec.symbol, market_date=rec.market_date,
                timeframe=rec.timeframe, session_type=rec.session_type,
                expected_count=rec.expected_count, observed_count=rec.observed_count,
                missing_timestamps=rec.missing_timestamps,
                unexpected_timestamps=(), conflicting_duplicates=(),
                admission_status=REJECTED_PROVIDER_ERROR,
                rejection_reasons=(
                    f"acquisition failed for {symbol}; no provider response to "
                    f"reconcile — distinct from the market having no data",))
        recs.append(rec)
        if rec.admitted:
            admitted_bars.extend(bars)

    # Cross-session adjustment integrity. Each session can be internally
    # uniform while the DATASET mixes regimes; a return computed across that
    # boundary is meaningless.
    states = {b.adjustment_state for b in admitted_bars}
    if len(states) > 1:
        recs = [
            SessionReconciliation(
                symbol=r.symbol, market_date=r.market_date, timeframe=r.timeframe,
                session_type=r.session_type, expected_count=r.expected_count,
                observed_count=r.observed_count, missing_timestamps=(),
                unexpected_timestamps=(), conflicting_duplicates=(),
                admission_status=REJECTED_MIXED_ADJUSTMENT,
                rejection_reasons=(
                    f"dataset mixes adjustment states {sorted(states)} across "
                    f"sessions; no session may be admitted",))
            if r.admitted else r
            for r in recs
        ]
        admitted_bars = []
        states = set()

    # Caller has NO authority over canonical adjustment identity.
    derived = states.pop() if len(states) == 1 else "NOT_APPLICABLE"
    return CanonicalDataset(
        bars=tuple(sorted(admitted_bars, key=lambda b: (b.symbol, b.bar_start_at))),
        reconciliations=tuple(recs), timeframe=timeframe,
        adjustment_state=derived, request=request)


def dataset_manifest(ds: CanonicalDataset, *, source: str = "fmp",
                     endpoint: str = "/stable/historical-chart/5min",
                     raw_snapshot_hash: str | None = None) -> dict:
    """Machine-readable identity + disclosure of every exclusion."""
    reasons: dict[str, int] = {}
    for r in ds.rejected:
        reasons[r.admission_status] = reasons.get(r.admission_status, 0) + 1
    dates = [r.market_date for r in ds.reconciliations]
    return {
        "schema_version": SCHEMA_VERSION,
        "source_module": "intraday_lab.dataset",
        "observe_only": True,
        "dataset_id": ds.dataset_id(),
        "dataset_fingerprint": ds.fingerprint(),
        "manifest_fingerprint": ds.manifest_fingerprint(),
        "request": ds.request.to_dict() if ds.request else None,
        "fingerprint_schema": FINGERPRINT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "endpoint": endpoint,
        "timeframe": ds.timeframe,
        "adjustment_state": ds.adjustment_state,
        "symbols": ds.symbols,
        "market_date_start": min(dates).isoformat() if dates else None,
        "market_date_end": max(dates).isoformat() if dates else None,
        "raw_snapshot_hash": raw_snapshot_hash,
        "calendar": calendar_provenance(),
        "calendar_fingerprint": calendar_fingerprint(),
        "not_trading_count": len(ds.not_trading),
        "session_count_requested": len(ds.reconciliations),
        "session_count_admitted": len(ds.admitted),
        "session_count_rejected": len(ds.rejected),
        "rejection_summary": reasons,
        "bar_count": len(ds.bars),
        "first_bar_at": ds.bars[0].bar_start_at.isoformat() if ds.bars else None,
        "last_bar_at": ds.bars[-1].bar_start_at.isoformat() if ds.bars else None,
        "admitted_sessions": [r.summary() for r in ds.admitted],
        "limitations": [
            "Rejected sessions contribute no bars; the requested window is NOT "
            "necessarily fully covered — read session_count_rejected.",
            "Split back-adjusted: absolute-price features are not PIT-safe.",
            "Calendar certifies 2025-2027 only; earlier sessions are refused.",
        ],
    }


def rejection_report(ds: CanonicalDataset) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "source_module": "intraday_lab.dataset",
        "observe_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_id": ds.dataset_id(),
        "rejected_count": len(ds.rejected),
        "rejections": [r.detail() for r in ds.rejected],
        "note": "A rejected session is evidence, not something to repair. "
                "Nothing is forward-filled, interpolated, shifted or padded.",
    }
