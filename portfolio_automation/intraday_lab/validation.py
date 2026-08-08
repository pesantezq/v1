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

# Gap classifications. HALT is intentionally absent from the automatic
# vocabulary — OHLCV alone cannot distinguish a halt from a provider gap, and
# guessing would manufacture evidence. Use UNKNOWN_GAP.
GAP_MARKET_CLOSED = "MARKET_CLOSED"
GAP_EARLY_CLOSE = "EARLY_CLOSE"
GAP_MISSING_BAR = "MISSING_BAR"
GAP_PROVIDER_GAP = "PROVIDER_GAP"
GAP_UNKNOWN = "UNKNOWN_GAP"


class DuplicateBarError(ValueError):
    """Two different bars claim the same symbol+timeframe+bar_start_at."""


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
                    session_label: str = "regular") -> dict:
    """Coverage/completeness profile for one symbol-session window.

    `expected_bars` is supplied by the caller (from the market calendar) rather
    than inferred, because inferring it from the observed data would make a
    truncated session look complete — the exact failure this profile exists to
    surface.
    """
    if not bars:
        return {"schema_version": SCHEMA_VERSION, "session_label": session_label,
                "observed_bars": 0, "expected_bars": expected_bars,
                "missing_bars": expected_bars, "coverage_pct": 0.0,
                "duplicate_bars": 0, "zero_volume_bars": 0,
                "first_bar": None, "last_bar": None,
                "gap_classification": GAP_UNKNOWN if expected_bars else GAP_MARKET_CLOSED}

    ordered = sorted(bars, key=lambda b: b.bar_start_at)
    counts = Counter(b.key() for b in bars)
    duplicates = sum(c - 1 for c in counts.values() if c > 1)
    zero_volume = sum(1 for b in ordered if b.volume == 0)
    observed = len(counts)

    gap = None
    missing = None
    coverage = None
    if expected_bars:
        missing = max(0, expected_bars - observed)
        coverage = round(100.0 * observed / expected_bars, 2)
        if missing == 0:
            gap = None
        elif observed and observed < expected_bars:
            # A session that starts on time but ends early is the shape of an
            # early close; anything else we refuse to name.
            gap = GAP_EARLY_CLOSE if observed >= expected_bars * 0.4 else GAP_UNKNOWN
        else:
            gap = GAP_UNKNOWN

    return {
        "schema_version": SCHEMA_VERSION,
        "session_label": session_label,
        "observed_bars": observed,
        "expected_bars": expected_bars,
        "missing_bars": missing,
        "coverage_pct": coverage,
        "duplicate_bars": duplicates,
        "zero_volume_bars": zero_volume,
        "first_bar": ordered[0].bar_start_at.isoformat(),
        "last_bar": ordered[-1].bar_start_at.isoformat(),
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
