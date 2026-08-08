"""Intraday bar contract + temporal model. Research-only, no production path.

TEMPORAL MODEL — the three times are NOT interchangeable
=======================================================

``bar_start_at``  The instant the bar's interval opens. FMP's ``date`` field is
                  this value (proven: a 13:00 early close yields a last bar of
                  12:55, and a normal session yields 09:30…15:55).
``bar_end_at``    ``bar_start_at + timeframe``. The bar is COMPLETE here.
``known_at``      The first instant a decision-maker could have acted on the
                  bar. For a completed bar this is ``bar_end_at`` plus a
                  conservative publication delay — never earlier.
``retrieved_at``  When *we* fetched the row. Bookkeeping only.
``event_at``      Generic: when the underlying event occurred (for a bar,
                  ``bar_start_at``).

The invariant that makes research honest:

    known_at <= decision_time        (an input must be knowable)
    known_at >= bar_end_at           (a bar is not knowable before it closes)

And the one that is easiest to get wrong:

    known_at != retrieved_at

Fetching a 2017 bar in 2026 does not mean it became knowable in 2026. Using
``retrieved_at`` as ``known_at`` would make every historical bar look unknowable
and silently empty every backtest; using ``bar_start_at`` would let a strategy
trade on a bar before it finished forming. Both are leakage-class errors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

SCHEMA_VERSION = "1"

# Supported research timeframes -> bar duration.
#
# ONLY 5min. Entitlement was PROVEN for 5min and DISPROVEN for 1min (HTTP 402)
# on 2026-08-08. 15min/30min/1hour were never probed, so declaring them would
# advertise capability that was not demonstrated — the same error class this
# lab exists to prevent. A later session that wants them must probe first and
# record the result in the provider assessment.
TIMEFRAMES: dict[str, timedelta] = {
    "5min": timedelta(minutes=5),
}

# Probed and refused by the configured account. Kept as documentation so the
# refusal is visible, NOT as a supported timeframe.
NOT_ENTITLED_TIMEFRAMES: dict[str, str] = {
    "1min": "HTTP 402 Payment Required (probed 2026-08-08)",
}

# Conservative publication delay applied on top of bar_end_at when the provider
# does not expose its own emission timestamp — which FMP does not. A real feed
# is not on your screen the instant the bar closes. This is deliberately a
# floor, not a measurement: if the true latency is ever measured it may only
# move LATER (more conservative), never earlier.
DEFAULT_PUBLICATION_DELAY = timedelta(seconds=60)


class TemporalViolation(ValueError):
    """Raised when a temporal invariant is broken. Never caught silently."""


class BarValidationError(ValueError):
    """Raised when a bar cannot be trusted as market data."""


def ensure_utc(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime, or refuse.

    A naive datetime is rejected rather than assumed-UTC: the provider returns
    naive US/Eastern strings, so silently stamping them UTC would shift every
    bar by 4-5 hours and quietly corrupt every session boundary.
    """
    if not isinstance(value, datetime):
        raise TemporalViolation(f"expected datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise TemporalViolation(
            "naive datetime rejected — attach the source timezone explicitly "
            "(provider timestamps are US/Eastern wall-clock, not UTC)")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class IntradayBar:
    """One completed intraday OHLCV bar, normalized to UTC.

    Frozen: a bar is an observation. Later sessions must not mutate history in
    place — a corrected bar is a new bar with a new fingerprint.
    """

    symbol: str
    timeframe: str
    bar_start_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str = "unknown"
    source_endpoint: str = ""
    retrieved_at: Optional[datetime] = None
    adjustment_state: str = "unknown"
    quality_flags: tuple[str, ...] = field(default_factory=tuple)
    publication_delay: timedelta = DEFAULT_PUBLICATION_DELAY

    def __post_init__(self) -> None:
        if self.timeframe not in TIMEFRAMES:
            raise BarValidationError(
                f"unsupported timeframe {self.timeframe!r}; "
                f"supported: {sorted(TIMEFRAMES)}")
        object.__setattr__(self, "bar_start_at", ensure_utc(self.bar_start_at))
        if self.retrieved_at is not None:
            object.__setattr__(self, "retrieved_at", ensure_utc(self.retrieved_at))
        _validate_ohlcv(self)

    @property
    def duration(self) -> timedelta:
        return TIMEFRAMES[self.timeframe]

    @property
    def bar_end_at(self) -> datetime:
        """The instant the bar completes. Exclusive end of the interval."""
        return self.bar_start_at + self.duration

    @property
    def event_at(self) -> datetime:
        return self.bar_start_at

    @property
    def known_at(self) -> datetime:
        """First instant this bar could have been acted upon.

        Never earlier than bar_end_at. Never derived from retrieved_at.
        """
        return self.bar_end_at + self.publication_delay

    def is_known_at(self, decision_time: datetime) -> bool:
        return self.known_at <= ensure_utc(decision_time)

    def key(self) -> tuple[str, str, datetime]:
        """Canonical identity: symbol + timeframe + bar start."""
        return (self.symbol, self.timeframe, self.bar_start_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bar_start_at": self.bar_start_at.isoformat(),
            "bar_end_at": self.bar_end_at.isoformat(),
            "known_at": self.known_at.isoformat(),
            "open": self.open, "high": self.high, "low": self.low,
            "close": self.close, "volume": self.volume,
            "source": self.source, "source_endpoint": self.source_endpoint,
            "retrieved_at": self.retrieved_at.isoformat() if self.retrieved_at else None,
            "adjustment_state": self.adjustment_state,
            "quality_flags": list(self.quality_flags),
        }


def _validate_ohlcv(bar: IntradayBar) -> None:
    import math

    for name in ("open", "high", "low", "close", "volume"):
        value = getattr(bar, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BarValidationError(f"{name} must be numeric, got {value!r}")
        if math.isnan(value) or math.isinf(value):
            raise BarValidationError(f"{name} must be finite, got {value!r}")
    if bar.volume < 0:
        raise BarValidationError(f"negative volume {bar.volume}")
    if bar.high < bar.low:
        raise BarValidationError(f"high {bar.high} < low {bar.low}")
    for name in ("open", "close"):
        value = getattr(bar, name)
        if value > bar.high:
            raise BarValidationError(f"{name} {value} > high {bar.high}")
        if value < bar.low:
            raise BarValidationError(f"{name} {value} < low {bar.low}")
    if bar.open <= 0 or bar.close <= 0:
        raise BarValidationError("non-positive price")


@dataclass(frozen=True)
class FeatureObservation:
    """Any non-bar research input, carrying its own knowability.

    Deliberately generic: leakage protection must not be bar-specific, because
    the inputs most likely to leak are news, sentiment, analyst revisions and
    regime labels — not prices.
    """

    feature_id: str
    value: Any
    event_at: datetime
    known_at: datetime
    source: str = "unknown"
    provenance: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_at", ensure_utc(self.event_at))
        object.__setattr__(self, "known_at", ensure_utc(self.known_at))
        if self.known_at < self.event_at:
            raise TemporalViolation(
                f"{self.feature_id}: known_at {self.known_at.isoformat()} precedes "
                f"event_at {self.event_at.isoformat()} — nothing is knowable "
                f"before it happens")

    def is_known_at(self, decision_time: datetime) -> bool:
        return self.known_at <= ensure_utc(decision_time)
