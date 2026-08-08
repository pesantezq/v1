"""Point-in-time enforcement, dataset canonicalization, quality profiling and
fingerprinting for the Intraday Strategy Lab. Research-only.

The point-in-time guard is deliberately generic (`admissible_inputs`) rather
than bar-specific: the inputs most likely to leak are news, sentiment, analyst
revisions and regime labels, not prices.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from portfolio_automation.intraday_lab.models import (
    IntradayBar, FeatureObservation, TemporalViolation, ensure_utc,
)

SCHEMA_VERSION = "1"

# SESSION TYPE — what the calendar says the session WAS. Caller-supplied; never
# inferred from the data, because a truncated normal session and a real early
# close are indistinguishable by bar count alone.
SESSION_REGULAR = "REGULAR"
SESSION_EARLY_CLOSE = "EARLY_CLOSE"
SESSION_MARKET_CLOSED = "MARKET_CLOSED"
SESSION_UNKNOWN = "UNKNOWN"

# GAP CLASSIFICATION — how complete the DATA is. A separate axis from session
# type. HALT and EARLY_CLOSE are intentionally absent: OHLCV alone cannot
# distinguish a halt from a provider gap, and an early close is calendar
# knowledge, not a data shape. Guessing either would manufacture evidence.
GAP_MISSING_BAR = "MISSING_BAR"
GAP_PROVIDER_GAP = "PROVIDER_GAP"
GAP_UNKNOWN = "UNKNOWN_GAP"


SESSION_TYPES = frozenset({SESSION_REGULAR, SESSION_EARLY_CLOSE,
                           SESSION_MARKET_CLOSED, SESSION_UNKNOWN})


class DuplicateBarError(ValueError):
    """Two different bars claim the same symbol+timeframe+bar_start_at."""


class SessionMetadataError(ValueError):
    """Calendar metadata and observed data contradict each other.

    Raised rather than returned because Session 2 will build the immutable
    canonical dataset on top of these profiles. A contradictory session that
    merely *reports* a bad status would still be admitted; one that raises
    cannot be. Fail closed at the boundary, not downstream.
    """


def _validate_session_metadata(session_type: str, expected_bars: Any,
                               observed: int) -> None:
    if session_type not in SESSION_TYPES:
        raise SessionMetadataError(
            f"unknown session_type {session_type!r}; expected one of "
            f"{sorted(SESSION_TYPES)}")

    if expected_bars is not None:
        if isinstance(expected_bars, bool) or not isinstance(expected_bars, int):
            raise SessionMetadataError(
                f"expected_bars must be None or a non-negative int, got "
                f"{expected_bars!r}")
        if expected_bars < 0:
            raise SessionMetadataError(f"negative expected_bars {expected_bars}")

    # A closed market cannot have traded.
    if session_type == SESSION_MARKET_CLOSED:
        if expected_bars not in (None, 0):
            raise SessionMetadataError(
                f"MARKET_CLOSED session expects 0 bars, calendar supplied "
                f"{expected_bars}")
        if observed > 0:
            raise SessionMetadataError(
                f"MARKET_CLOSED session carries {observed} observed bars — the "
                f"calendar and the data disagree about whether the market was "
                f"open; do not admit this session")

    # A trading session that expects nothing is a calendar contradiction.
    if session_type in (SESSION_REGULAR, SESSION_EARLY_CLOSE) and expected_bars == 0:
        raise SessionMetadataError(
            f"{session_type} session cannot expect 0 bars — the calendar "
            f"expectation contradicts the session type")

    # Surplus bars are never harmless. They signal extended-hours contamination,
    # a wrong calendar expectation, a timestamp/session normalization error, or
    # a provider change. Reporting >100% coverage as "complete" would let any of
    # those into the canonical dataset unnoticed.
    if expected_bars is not None and observed > expected_bars:
        raise SessionMetadataError(
            f"{observed} observed bars exceed the {expected_bars} the calendar "
            f"expects for a {session_type} session — possible extended-hours "
            f"contamination, wrong calendar expectation, timestamp "
            f"normalization error, or duplicated source data")


# ---------------------------------------------------------------------------
# Point-in-time enforcement
# ---------------------------------------------------------------------------

def admissible_inputs(inputs: Iterable[Any], decision_time: datetime) -> list[Any]:
    """Filter to inputs knowable at `decision_time`.

    Accepts anything exposing `is_known_at` — bars and feature observations
    alike. Anything without that method is REJECTED rather than assumed
    admissible: an input that cannot state when it became knowable cannot be
    proven leak-free, and defaulting to "allow" is how leakage enters.
    """
    decision_time = ensure_utc(decision_time)
    out = []
    for item in inputs:
        checker = getattr(item, "is_known_at", None)
        if checker is None:
            raise TemporalViolation(
                f"{type(item).__name__} exposes no is_known_at(); it cannot be "
                f"proven point-in-time safe")
        if checker(decision_time):
            out.append(item)
    return out


def assert_no_lookahead(inputs: Iterable[Any], decision_time: datetime) -> None:
    """Raise if ANY input postdates the decision. For adversarial tests and for
    guarding a future simulator's input set."""
    decision_time = ensure_utc(decision_time)
    for item in inputs:
        checker = getattr(item, "is_known_at", None)
        if checker is None or not checker(decision_time):
            known = getattr(item, "known_at", "unknown")
            raise TemporalViolation(
                f"look-ahead: {getattr(item, 'feature_id', None) or getattr(item, 'symbol', item)!r} "
                f"known_at={known} > decision_time={decision_time.isoformat()}")


def earliest_order_time(bar: IntradayBar) -> datetime:
    """The earliest instant a future simulator may fill a signal from `bar`.

    Session 1 does not implement fills. It fixes the boundary they must respect:
    a signal derived from the 10:00-10:05 bar cannot be filled before that bar
    was knowable. Any future simulator granting an earlier fill is producing an
    INVALID result regardless of profitability.
    """
    return bar.known_at


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------

def canonicalize(bars: Sequence[IntradayBar]) -> list[IntradayBar]:
    """Sort chronologically and reject conflicting duplicates.

    CONTRACT (documented choice): out-of-order input is SORTED, not rejected —
    providers legitimately return newest-first, and refusing would reject valid
    data. Duplicates are different: an exact repeat is collapsed, but two bars
    sharing an identity with different values are irreconcilable and RAISE,
    because silently keeping either one would make the dataset fingerprint
    depend on arrival order.
    """
    seen: dict[tuple, IntradayBar] = {}
    for bar in bars:
        key = bar.key()
        prior = seen.get(key)
        if prior is None:
            seen[key] = bar
            continue
        if (prior.open, prior.high, prior.low, prior.close, prior.volume) != (
                bar.open, bar.high, bar.low, bar.close, bar.volume):
            raise DuplicateBarError(
                f"conflicting duplicate for {key[0]} {key[1]} "
                f"{key[2].isoformat()}: {prior.close} vs {bar.close}")
    return sorted(seen.values(), key=lambda b: (b.symbol, b.timeframe, b.bar_start_at))


# ---------------------------------------------------------------------------
# Quality profiling
# ---------------------------------------------------------------------------

def profile_session(bars: Sequence[IntradayBar], *, expected_bars: int | None = None,
                    session_type: str = SESSION_UNKNOWN) -> dict:
    """Coverage/completeness profile for one symbol-session window.

    TWO INDEPENDENT AXES, deliberately not conflated:

    * ``session_type`` — what the CALENDAR says the session was (REGULAR /
      EARLY_CLOSE / MARKET_CLOSED / UNKNOWN). Supplied by the caller.
    * ``gap_classification`` — how complete the DATA is for that session.

    An earlier version inferred ``EARLY_CLOSE`` from the shape of the missing
    data (``observed >= expected * 0.4``). That was unsafe: a provider outage
    that truncated the tail of a normal 78-bar session would be relabelled as an
    exchange early close, converting a data defect into a "healthy" verdict.
    Early-close knowledge is calendar knowledge; it can never be recovered from
    the bar count. Both ``expected_bars`` and ``session_type`` are therefore
    caller-supplied, and neither is inferred from observation.

    An early-close session with missing bars stays ``EARLY_CLOSE`` *and*
    incomplete — the session type must never hide a coverage gap.

    FAILS CLOSED on contradictory metadata (``SessionMetadataError``): an
    unknown session type, a negative/non-integer expectation, a MARKET_CLOSED
    session that expects or carries bars, a trading session expecting zero
    bars, or MORE observed bars than the calendar expects. Session 2 builds the
    immutable canonical dataset from these profiles, so a contradiction must be
    impossible to admit, not merely reported.

    ``complete`` is ``None`` — not ``True`` — when no expectation was supplied.
    Absence of a calendar expectation is not evidence of completeness.
    """
    ordered = sorted(bars, key=lambda b: b.bar_start_at)
    counts = Counter(b.key() for b in bars)
    duplicates = sum(c - 1 for c in counts.values() if c > 1)
    observed = len(counts)

    _validate_session_metadata(session_type, expected_bars, observed)

    missing = None
    coverage = None
    gap = None
    if expected_bars is not None:
        # Surplus is impossible past _validate_session_metadata, so this
        # subtraction can no longer mask extra bars behind max(0, ...).
        missing = expected_bars - observed
        coverage = 100.0 if expected_bars == 0 else round(100.0 * observed / expected_bars, 2)
        if missing:
            # Conservative: we can prove bars are absent, not WHY. Never infer a
            # halt, and never infer an early close.
            gap = GAP_MISSING_BAR

    return {
        "schema_version": SCHEMA_VERSION,
        "session_type": session_type,
        "observed_bars": observed,
        "expected_bars": expected_bars,
        "missing_bars": missing,
        "coverage_pct": coverage,
        "complete": (missing == 0) if missing is not None else None,
        "duplicate_bars": duplicates,
        "zero_volume_bars": sum(1 for b in ordered if b.volume == 0),
        "first_bar": ordered[0].bar_start_at.isoformat() if ordered else None,
        "last_bar": ordered[-1].bar_start_at.isoformat() if ordered else None,
        "gap_classification": gap,
    }


# ---------------------------------------------------------------------------
# Dataset fingerprinting
# ---------------------------------------------------------------------------

def dataset_fingerprint(bars: Sequence[IntradayBar]) -> str:
    """Deterministic identity for a canonical dataset.

    Covers exactly what changes the research result: identity and OHLCV. It
    deliberately EXCLUDES `retrieved_at`, so re-fetching identical history
    reproduces the fingerprint — otherwise every experiment would be
    irreproducible by construction.
    """
    canonical = canonicalize(bars)
    payload = [
        [b.symbol, b.timeframe, b.bar_start_at.isoformat(),
         b.open, b.high, b.low, b.close, b.volume]
        for b in canonical
    ]
    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def dataset_manifest(bars: Sequence[IntradayBar], *, source: str = "",
                     window: tuple[str, str] | None = None) -> dict:
    canonical = canonicalize(bars)
    symbols = sorted({b.symbol for b in canonical})
    timeframes = sorted({b.timeframe for b in canonical})
    return {
        "schema_version": SCHEMA_VERSION,
        "fingerprint": dataset_fingerprint(canonical),
        "bar_count": len(canonical),
        "symbols": symbols,
        "timeframes": timeframes,
        "source": source,
        "window": list(window) if window else None,
        "first_bar_at": canonical[0].bar_start_at.isoformat() if canonical else None,
        "last_bar_at": canonical[-1].bar_start_at.isoformat() if canonical else None,
        "adjustment_states": sorted({b.adjustment_state for b in canonical}),
    }
