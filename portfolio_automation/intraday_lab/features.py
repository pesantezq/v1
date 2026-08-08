"""PIT-safe price-derived features + registry. Research-only.

TWO RULES GOVERN EVERYTHING HERE.

1. ``feature.known_at >= max(input_bar.known_at)`` — a feature cannot become
   knowable before its newest required input. Every window is BACKWARD-looking;
   there is no centred window, no negative lag, no forward fill.

2. Only dimensionless / relative quantities are enabled. Session 1 proved the
   history is SPLIT BACK-ADJUSTED, so absolute price levels were retroactively
   rewritten using a corporate action that had not occurred at the bar's own
   timestamp. Returns survive that; price thresholds do not.

Volume-dependent features (VWAP, RVOL, dollar volume) are BLOCKED, not
implemented-and-warned-about. Session 1 never established whether historical
volume is adjusted consistently with the adjusted prices, so
``price * volume`` cannot be shown to be point-in-time meaningful. A feature
whose semantics cannot be proven must be unavailable, not merely flagged.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from portfolio_automation.intraday_lab.models import IntradayBar, TemporalViolation

SCHEMA_VERSION = "1"
FEATURE_SET_VERSION = "1"

STATUS_ENABLED = "ENABLED"
STATUS_BLOCKED_ADJUSTMENT = "BLOCKED_ADJUSTMENT_SEMANTICS"
STATUS_BLOCKED_VOLUME = "BLOCKED_VOLUME_SEMANTICS"
STATUS_DEFERRED = "DEFERRED"
STATUS_NOT_IMPLEMENTED = "NOT_IMPLEMENTED"

FEATURE_NOT_AVAILABLE = None


@dataclass(frozen=True)
class FeatureValue:
    """A computed feature with full provenance back to its canonical dataset."""
    feature_id: str
    feature_version: str
    symbol: str
    timeframe: str
    value: float
    event_at: datetime
    known_at: datetime
    source_dataset_id: str
    source_dataset_fingerprint: str
    input_window_start: datetime
    input_window_end: datetime
    parameters: dict[str, Any]

    def is_known_at(self, decision_time: datetime) -> bool:
        if decision_time.tzinfo is None:
            raise TemporalViolation("decision_time must be timezone-aware")
        return self.known_at <= decision_time.astimezone(timezone.utc)

    def to_dict(self) -> dict:
        return {
            "feature_id": self.feature_id, "feature_version": self.feature_version,
            "symbol": self.symbol, "timeframe": self.timeframe, "value": self.value,
            "event_at": self.event_at.isoformat(), "known_at": self.known_at.isoformat(),
            "source_dataset_id": self.source_dataset_id,
            "source_dataset_fingerprint": self.source_dataset_fingerprint,
            "input_window_start": self.input_window_start.isoformat(),
            "input_window_end": self.input_window_end.isoformat(),
            "parameters": self.parameters,
        }


FEATURE_REGISTRY: dict[str, dict] = {
    "return_1bar": {
        "version": "1", "lookback_bars": 2, "status": STATUS_ENABLED,
        "description": "Close-to-close return over one bar.",
        "known_at_rule": "max(input_bar.known_at)",
        "requires_volume": False, "requires_absolute_price": False,
        "adjustment_compatibility": "SAFE — ratio cancels a uniform split factor",
    },
    "return_nbar": {
        "version": "1", "lookback_bars": None, "status": STATUS_ENABLED,
        "description": "Backward close-to-close return over N bars.",
        "known_at_rule": "max(input_bar.known_at)",
        "requires_volume": False, "requires_absolute_price": False,
        "adjustment_compatibility": "SAFE — ratio",
    },
    "realized_vol": {
        "version": "1", "lookback_bars": None, "status": STATUS_ENABLED,
        "description": "Population stdev of the last N one-bar returns.",
        "known_at_rule": "max(input_bar.known_at)",
        "requires_volume": False, "requires_absolute_price": False,
        "adjustment_compatibility": "SAFE — computed from returns",
    },
    "normalized_range": {
        "version": "1", "lookback_bars": 1, "status": STATUS_ENABLED,
        "description": "(high - low) / close for the bar. Dimensionless.",
        "known_at_rule": "bar.known_at",
        "requires_volume": False, "requires_absolute_price": False,
        "adjustment_compatibility": "SAFE — ratio",
    },
    "range_position": {
        "version": "1", "lookback_bars": None, "status": STATUS_ENABLED,
        "description": "Close's position in the prior N-bar high/low range, 0-1.",
        "known_at_rule": "max(input_bar.known_at)",
        "requires_volume": False, "requires_absolute_price": False,
        "adjustment_compatibility": "SAFE — normalized within one adjustment regime",
    },
    "session_progress": {
        "version": "0", "lookback_bars": 1, "status": STATUS_NOT_IMPLEMENTED,
        "limitations": "Registry claimed ENABLED with no compute function. "
                       "Downgraded rather than rushed in — an ENABLED feature "
                       "with no implementation is a false capability claim.",
        "description": "Fraction of the calendar session elapsed at bar end, 0-1.",
        "known_at_rule": "bar.known_at",
        "requires_volume": False, "requires_absolute_price": False,
        "adjustment_compatibility": "SAFE — derived from the calendar, not prices",
    },
    # ---------------- blocked ----------------
    "vwap": {
        "version": "0", "lookback_bars": None, "status": STATUS_BLOCKED_VOLUME,
        "description": "Volume-weighted average price.",
        "known_at_rule": "n/a", "requires_volume": True,
        "requires_absolute_price": True,
        "adjustment_compatibility": "UNPROVEN — needs price AND volume adjusted "
                                    "consistently; Session 1 established neither",
        "limitations": "Blocked until volume adjustment semantics are proven.",
    },
    "rvol": {
        "version": "0", "lookback_bars": None, "status": STATUS_BLOCKED_VOLUME,
        "description": "Relative volume vs a historical baseline.",
        "known_at_rule": "n/a", "requires_volume": True,
        "requires_absolute_price": False,
        "adjustment_compatibility": "UNPROVEN — a split changes share counts; "
                                    "comparing across a split needs proven semantics",
        "limitations": "Blocked until volume adjustment semantics are proven.",
    },
    "dollar_volume": {
        "version": "0", "lookback_bars": 1, "status": STATUS_BLOCKED_VOLUME,
        "description": "price * volume.",
        "known_at_rule": "n/a", "requires_volume": True,
        "requires_absolute_price": True,
        "adjustment_compatibility": "UNPROVEN",
        "limitations": "Must NOT be used as a liquidity admission threshold.",
    },
    "absolute_atr": {
        "version": "0", "lookback_bars": None, "status": STATUS_BLOCKED_ADJUSTMENT,
        "description": "Average true range in dollars.",
        "known_at_rule": "n/a", "requires_volume": False,
        "requires_absolute_price": True,
        "adjustment_compatibility": "UNSAFE — back-adjusted dollar levels",
        "limitations": "Use normalized_range instead.",
    },
    "sector_relative_return": {
        "version": "0", "lookback_bars": None, "status": STATUS_DEFERRED,
        "description": "Return relative to a sector benchmark.",
        "known_at_rule": "max(stock, benchmark) known_at",
        "requires_volume": False, "requires_absolute_price": False,
        "requires_sector_context": True,
        "adjustment_compatibility": "SAFE in principle",
        "limitations": "SECTOR_CONTEXT_DEFERRED — no point-in-time-safe "
                       "symbol->sector mapping was established.",
    },
}

# Explicit registry -> implementation map. Without it "ENABLED" is an
# unverifiable claim: session_progress shipped ENABLED with no compute function.
IMPLEMENTATIONS: dict[str, str] = {
    "return_1bar": "compute_return_nbar",
    "return_nbar": "compute_return_nbar",
    "realized_vol": "compute_realized_vol",
    "normalized_range": "compute_normalized_range",
    "range_position": "compute_range_position",
}

ENABLED_FEATURES = tuple(k for k, v in FEATURE_REGISTRY.items()
                         if v["status"] == STATUS_ENABLED)
BLOCKED_FEATURES = tuple(k for k, v in FEATURE_REGISTRY.items()
                         if v["status"] != STATUS_ENABLED)


class SeriesIntegrityError(ValueError):
    """A window crossed a symbol, timeframe or session boundary."""


def group_series(bars: Sequence[IntradayBar]) -> dict[tuple[str, str], list[IntradayBar]]:
    """Group canonical bars into single-symbol, single-timeframe series.

    Feature functions take positional windows, so an ungrouped mixed sequence
    lets a rolling window straddle two symbols. Observed live: a 3-bar window
    over [SPY, SPY, SPY, AAPL, AAPL, AAPL] produced a value labelled AAPL that
    was computed partly from SPY.
    """
    out: dict[tuple[str, str], list[IntradayBar]] = {}
    for b in bars:
        out.setdefault((b.symbol, b.timeframe), []).append(b)
    for key in out:
        out[key].sort(key=lambda b: b.bar_start_at)
    return out


def _contiguous(window: Sequence[IntradayBar]) -> bool:
    """True when every step is exactly one bar apart.

    Rejected sessions contribute no bars, so a naive positional window can
    bridge Monday->Wednesday as if adjacent. Adjacency is checked in time, not
    by index. A window spanning a session boundary also fails, which is the
    intended default: cross-session rolling features are DEFERRED.
    """
    step = window[0].duration
    return all(window[i].bar_start_at - window[i - 1].bar_start_at == step
               for i in range(1, len(window)))


def _window(bars: Sequence[IntradayBar], end_index: int, lookback: int
            ) -> list[IntradayBar] | None:
    """The `lookback` bars ending at `end_index`, or None if history is short.

    Returning None — never a partial or padded window — is what stops a
    20-bar feature from quietly becoming a 3-bar feature near the session open.
    """
    start = end_index - lookback + 1
    if start < 0:
        return None
    window = list(bars[start:end_index + 1])
    if len({(b.symbol, b.timeframe) for b in window}) > 1:
        raise SeriesIntegrityError(
            "feature window spans multiple symbols/timeframes — group with "
            "group_series() before computing features")
    if not _contiguous(window):
        # Explicit absence, never a silently shortened lookback.
        return None
    return window


def _emit(feature_id: str, window: Sequence[IntradayBar], value: float, *,
          dataset_id: str, fingerprint: str, parameters: dict) -> FeatureValue:
    newest = max(window, key=lambda b: b.known_at)
    return FeatureValue(
        feature_id=feature_id,
        feature_version=FEATURE_REGISTRY[feature_id]["version"],
        symbol=window[-1].symbol, timeframe=window[-1].timeframe, value=value,
        event_at=window[-1].bar_start_at,
        # The core invariant: never earlier than the newest input.
        known_at=newest.known_at,
        source_dataset_id=dataset_id, source_dataset_fingerprint=fingerprint,
        input_window_start=window[0].bar_start_at,
        input_window_end=window[-1].bar_start_at, parameters=parameters)


def compute_return_nbar(bars: Sequence[IntradayBar], index: int, n: int, *,
                        dataset_id: str, fingerprint: str) -> FeatureValue | None:
    window = _window(bars, index, n + 1)
    if window is None or window[0].close == 0:
        return FEATURE_NOT_AVAILABLE
    value = (window[-1].close / window[0].close) - 1.0
    fid = "return_1bar" if n == 1 else "return_nbar"
    return _emit(fid, window, value, dataset_id=dataset_id,
                 fingerprint=fingerprint, parameters={"n": n})


def compute_realized_vol(bars: Sequence[IntradayBar], index: int, n: int, *,
                         dataset_id: str, fingerprint: str) -> FeatureValue | None:
    window = _window(bars, index, n + 1)
    if window is None:
        return FEATURE_NOT_AVAILABLE
    rets = [(window[i].close / window[i - 1].close) - 1.0
            for i in range(1, len(window)) if window[i - 1].close]
    if len(rets) < 2:
        return FEATURE_NOT_AVAILABLE
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return _emit("realized_vol", window, var ** 0.5, dataset_id=dataset_id,
                 fingerprint=fingerprint, parameters={"n": n})


def compute_normalized_range(bars: Sequence[IntradayBar], index: int, *,
                             dataset_id: str, fingerprint: str) -> FeatureValue | None:
    window = _window(bars, index, 1)
    if window is None or window[-1].close == 0:
        return FEATURE_NOT_AVAILABLE
    bar = window[-1]
    return _emit("normalized_range", window, (bar.high - bar.low) / bar.close,
                 dataset_id=dataset_id, fingerprint=fingerprint, parameters={})


def compute_range_position(bars: Sequence[IntradayBar], index: int, n: int, *,
                           dataset_id: str, fingerprint: str) -> FeatureValue | None:
    window = _window(bars, index, n)
    if window is None:
        return FEATURE_NOT_AVAILABLE
    hi = max(b.high for b in window)
    lo = min(b.low for b in window)
    if hi == lo:
        return FEATURE_NOT_AVAILABLE
    return _emit("range_position", window, (window[-1].close - lo) / (hi - lo),
                 dataset_id=dataset_id, fingerprint=fingerprint, parameters={"n": n})


def feature_fingerprint(values: Sequence[FeatureValue]) -> str:
    """Deterministic identity for a feature set. Excludes computation time."""
    # Bind to the SOURCE DATASET. Two different datasets can yield numerically
    # identical values; without this they would share a feature identity and an
    # experiment could not tell which data produced it.
    rows = sorted(
        [v.feature_id, v.feature_version, v.symbol, v.timeframe,
         v.event_at.isoformat(), v.known_at.isoformat(),
         v.input_window_start.isoformat(), v.input_window_end.isoformat(),
         round(v.value, 12), json.dumps(v.parameters, sort_keys=True),
         v.source_dataset_fingerprint]
        for v in values)
    payload = {"schema": "intraday_features_v1",
               "feature_set_version": FEATURE_SET_VERSION, "rows": rows}
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()[:32]


def feature_manifest(values: Sequence[FeatureValue], *, dataset_id: str,
                     dataset_fingerprint: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "source_module": "intraday_lab.features",
        "observe_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "feature_set_id": f"featureset-{feature_fingerprint(values)[:16]}",
        "feature_set_version": FEATURE_SET_VERSION,
        "feature_fingerprint": feature_fingerprint(values),
        "source_dataset_id": dataset_id,
        "source_dataset_fingerprint": dataset_fingerprint,
        "features_enabled": list(ENABLED_FEATURES),
        "features_blocked": {k: FEATURE_REGISTRY[k]["status"] for k in BLOCKED_FEATURES},
        "symbol_count": len({v.symbol for v in values}),
        "observation_count": len(values),
        "first_feature_event_at": min((v.event_at for v in values), default=None)
        and min(v.event_at for v in values).isoformat(),
        "last_feature_event_at": max((v.event_at for v in values), default=None)
        and max(v.event_at for v in values).isoformat(),
        "pit_validation": "known_at >= max(input.known_at) enforced at construction",
    }


def feature_registry_artifact() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "source_module": "intraday_lab.features",
        "observe_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "feature_set_version": FEATURE_SET_VERSION,
        "features": FEATURE_REGISTRY,
        "enabled": list(ENABLED_FEATURES),
        "blocked": list(BLOCKED_FEATURES),
        "policy": "A feature whose semantics cannot be proven point-in-time safe "
                  "is BLOCKED, not enabled with a warning.",
    }
