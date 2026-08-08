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
FINGERPRINT_SCHEMA = "intraday_canonical_v2"

ADMITTED = "ADMITTED"
REJECTED_MISSING_BARS = "REJECTED_MISSING_BARS"
REJECTED_SURPLUS_BARS = "REJECTED_SURPLUS_BARS"
REJECTED_OFF_GRID = "REJECTED_OFF_GRID"
REJECTED_CONFLICTING_DUPLICATE = "REJECTED_CONFLICTING_DUPLICATE"
REJECTED_CALENDAR_UNCERTIFIED = "REJECTED_CALENDAR_UNCERTIFIED"
REJECTED_CLOSED_SESSION_HAS_BARS = "REJECTED_CLOSED_SESSION_HAS_BARS"
REJECTED_MIXED_ADJUSTMENT = "REJECTED_MIXED_ADJUSTMENT"


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
    for bar in bars:
        prior = by_start.get(bar.bar_start_at)
        if prior is None:
            by_start[bar.bar_start_at] = bar
        elif (prior.open, prior.high, prior.low, prior.close, prior.volume) != (
                bar.open, bar.high, bar.low, bar.close, bar.volume):
            conflicts.append(bar.bar_start_at.isoformat())

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

    @property
    def admitted(self) -> tuple[SessionReconciliation, ...]:
        return tuple(r for r in self.reconciliations if r.admitted)

    @property
    def rejected(self) -> tuple[SessionReconciliation, ...]:
        return tuple(r for r in self.reconciliations if not r.admitted)

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
    rows = sorted(
        ([b.symbol, b.timeframe, b.bar_start_at.isoformat(),
          b.open, b.high, b.low, b.close, b.volume] for b in bars),
        key=lambda r: (r[0], r[1], r[2]))
    payload = {"schema": FINGERPRINT_SCHEMA, "timeframe": timeframe,
               "adjustment_state": adjustment_state, "rows": rows}
    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def build_canonical_dataset(
    bars_by_date: dict[tuple[str, date], Sequence[IntradayBar]], *,
    timeframe: str = "5min", adjustment_state: str = "split_adjusted",
) -> CanonicalDataset:
    """Reconcile every requested session; admit only exact matches.

    Rejected sessions contribute NO bars. Their reconciliation is retained so
    the manifest can disclose the exclusion — a dataset that silently omitted
    them would look complete while covering a different window than requested.
    """
    admitted_bars: list[IntradayBar] = []
    recs: list[SessionReconciliation] = []
    for (symbol, market_date) in sorted(bars_by_date):
        bars = bars_by_date[(symbol, market_date)]
        session = resolve_session(market_date)
        rec = reconcile_session(bars, session, symbol=symbol, timeframe=timeframe)
        recs.append(rec)
        if rec.admitted:
            admitted_bars.extend(bars)
    return CanonicalDataset(
        bars=tuple(sorted(admitted_bars, key=lambda b: (b.symbol, b.bar_start_at))),
        reconciliations=tuple(recs), timeframe=timeframe,
        adjustment_state=adjustment_state)


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
