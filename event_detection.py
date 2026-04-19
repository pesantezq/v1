"""
Event Detection
===============
Detects meaningful market events from shallow scan results.

Pure function — no I/O, no API calls, no side effects.

Event types
-----------
STRONG_MOVE_UP        daily price change >= strong_move_pct
STRONG_MOVE_DOWN      daily price change <= -strong_move_pct
VOLUME_SPIKE          relative volume >= volume_spike_factor
BREAKOUT_PROXY        price within breakout_proximity_pct of 52-week high
                      AND pct_change_1d >= 1.0 (meaningful upward pressure)
                      AND rel_volume >= 0.8 (volume-confirmed, or data absent)
VOLATILITY_EXPANSION  intraday range (high−low)/price >= volatility_expansion_pct

strength field
--------------
Normalised 0.0–1.0 value indicating how extreme the event is relative
to the triggering threshold.  Computed per event type so values are
comparable within a type but not across types.

Config key: ``universal_scanner.event_thresholds``
  strong_move_pct          (default: 3.0)   % daily move to trigger
  volume_spike_factor      (default: 2.0)   relative-volume multiple
  breakout_proximity_pct   (default: 2.0)   % below 52-week high
  volatility_expansion_pct (default: 4.0)   % intraday range / price
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set

from universal_scanner import ScanResult

logger = logging.getLogger("portfolio_automation.event_detection")

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    STRONG_MOVE_UP = "STRONG_MOVE_UP"
    STRONG_MOVE_DOWN = "STRONG_MOVE_DOWN"
    VOLUME_SPIKE = "VOLUME_SPIKE"
    BREAKOUT_PROXY = "BREAKOUT_PROXY"
    VOLATILITY_EXPANSION = "VOLATILITY_EXPANSION"


@dataclass
class MarketEvent:
    symbol: str
    event_type: EventType
    strength: float          # 0.0–1.0 (capped)
    metadata: Dict = field(default_factory=dict)
    detected_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "event_type": self.event_type.value,
            "strength": self.strength,
            "metadata": self.metadata,
            "detected_at": self.detected_at,
        }


# ---------------------------------------------------------------------------
# Default thresholds
# ---------------------------------------------------------------------------

_DEFAULT_THRESHOLDS: Dict[str, float] = {
    "strong_move_pct": 3.0,
    "volume_spike_factor": 2.0,
    "breakout_proximity_pct": 2.0,
    "volatility_expansion_pct": 4.0,
}


def _resolve_thresholds(config: Optional[dict]) -> Dict[str, float]:
    """Merge user thresholds over defaults."""
    t = dict(_DEFAULT_THRESHOLDS)
    if config:
        for k in _DEFAULT_THRESHOLDS:
            if k in config:
                try:
                    value = float(config[k])
                    if value > 0:
                        t[k] = value
                except (TypeError, ValueError):
                    pass
    return t


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_events(
    scan_results: List[ScanResult],
    config: Optional[dict] = None,
) -> List[MarketEvent]:
    """
    Detect market events from shallow scan results.

    Args:
        scan_results: Output from ``UniversalScanner.scan()``.
        config:       ``event_thresholds`` dict (optional).
                      Keys: strong_move_pct, volume_spike_factor,
                      breakout_proximity_pct, volatility_expansion_pct,
                      theme_breadth_threshold (default 0.15).

    Returns:
        List of MarketEvent objects (may be empty).
        Never raises; per-symbol errors are logged and skipped.

    Side-effect:
        Enriches each ScanResult with a computed ``theme_support`` value
        (multi-symbol confirmation score, 0.0–1.0).  Only written where
        ``sr.theme_support is None`` so externally-supplied values are
        preserved.
    """
    thresholds = _resolve_thresholds(config)
    events: List[MarketEvent] = []

    for sr in scan_results:
        if not sr.has_price:
            continue
        try:
            _detect_for_symbol(sr, thresholds, events)
        except Exception as exc:
            logger.warning(
                "Event detection error for %s (skipped): %s", sr.symbol, exc
            )

    # Inject theme_support scores derived from multi-symbol confirmation.
    # Only fills in symbols that haven't received an external score already.
    theme_map = compute_theme_support(scan_results, events, config)
    for sr in scan_results:
        if sr.theme_support is None and sr.symbol in theme_map:
            sr.theme_support = theme_map[sr.symbol]

    n_symbols_with_events = len({e.symbol for e in events})
    logger.info(
        "EventDetection: %d events across %d symbols (scanned %d)",
        len(events),
        n_symbols_with_events,
        len(scan_results),
    )
    return events


def compute_theme_support(
    scan_results: List[ScanResult],
    events: List[MarketEvent],
    config: Optional[dict] = None,
) -> Dict[str, float]:
    """
    Compute per-symbol theme-support scores using multi-symbol confirmation.

    A symbol's score reflects two components:
      1. Broad-market confirmation — the fraction of priced symbols that have
         positive events (STRONG_MOVE_UP or BREAKOUT_PROXY).  When this
         fraction reaches ``theme_breadth_threshold`` (default 0.15, i.e. 15%)
         the broad score saturates at 1.0.
      2. Per-symbol event bonus — additional credit for symbols that themselves
         have positive events (BREAKOUT_PROXY +0.20, STRONG_MOVE_UP +0.10,
         VOLUME_SPIKE +0.05).

    Config keys (under the same event_thresholds namespace):
      theme_breadth_threshold  — fraction of priced symbols that must have
                                 positive events to reach broad_score = 1.0
                                 (default: 0.15)

    Returns:
        Dict mapping symbol → theme_support (0.0–1.0).
        Only symbols where has_price=True are included.
    """
    cfg = config or {}
    breadth_threshold = float(cfg.get("theme_breadth_threshold", 0.15))
    breadth_threshold = max(0.01, min(1.0, breadth_threshold))

    _POSITIVE_TYPES: Set[EventType] = {EventType.STRONG_MOVE_UP, EventType.BREAKOUT_PROXY}

    # Index events per symbol
    symbol_events: Dict[str, Set[EventType]] = {}
    for ev in events:
        symbol_events.setdefault(ev.symbol, set()).add(ev.event_type)

    # Broad confirmation: fraction of priced symbols with positive events
    priced_symbols = [sr.symbol for sr in scan_results if sr.has_price]
    total_priced = len(priced_symbols)
    if total_priced == 0:
        return {}

    positive_count = sum(
        1 for sym in priced_symbols
        if symbol_events.get(sym, set()) & _POSITIVE_TYPES
    )
    min_positive = int(cfg.get("min_positive_symbols_for_breadth", 3))
    if positive_count < min_positive:
        broad_score = 0.0
    else:
        broad_score = min(1.0, positive_count / max(1, total_priced * breadth_threshold))

    # Per-symbol bonuses
    result: Dict[str, float] = {}
    for sym in priced_symbols:
        sym_ev = symbol_events.get(sym, set())
        bonus = 0.0
        if EventType.BREAKOUT_PROXY in sym_ev:
            bonus += 0.20
        if EventType.STRONG_MOVE_UP in sym_ev:
            bonus += 0.10
        if EventType.VOLUME_SPIKE in sym_ev:
            bonus += 0.05
        result[sym] = round(min(1.0, max(0.0, broad_score + bonus)), 3)

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _strength(value: float, threshold: float) -> float:
    """
    Normalise a value into [0.5, 1.0] given a threshold.

    At threshold → 0.5; at 2× threshold → 1.0.
    Result is capped to [0.0, 1.0].
    """
    if threshold <= 0:
        return 0.5
    raw = (abs(value) - threshold) / threshold + 0.5
    return round(min(1.0, max(0.0, raw)), 3)


def _detect_for_symbol(
    sr: ScanResult,
    t: Dict[str, float],
    events: List[MarketEvent],
) -> None:
    """Append triggered events for a single ScanResult into ``events``."""

    # ── Strong daily move ──────────────────────────────────────────────
    if sr.pct_change_1d is not None:
        pct = sr.pct_change_1d
        thresh = t["strong_move_pct"]
        if pct >= thresh:
            events.append(MarketEvent(
                symbol=sr.symbol,
                event_type=EventType.STRONG_MOVE_UP,
                strength=_strength(pct, thresh),
                metadata={"pct_change_1d": pct, "threshold": thresh},
                detected_at=sr.timestamp,
            ))
        elif pct <= -thresh:
            events.append(MarketEvent(
                symbol=sr.symbol,
                event_type=EventType.STRONG_MOVE_DOWN,
                strength=_strength(pct, thresh),
                metadata={"pct_change_1d": pct, "threshold": -thresh},
                detected_at=sr.timestamp,
            ))

    # ── Volume spike ───────────────────────────────────────────────────
    if sr.rel_volume is not None:
        factor = t["volume_spike_factor"]
        if sr.rel_volume >= factor:
            events.append(MarketEvent(
                symbol=sr.symbol,
                event_type=EventType.VOLUME_SPIKE,
                strength=_strength(sr.rel_volume, factor),
                metadata={
                    "rel_volume": sr.rel_volume,
                    "volume": sr.volume,
                    "avg_volume": sr.avg_volume,
                    "threshold": factor,
                },
                detected_at=sr.timestamp,
            ))

    # ── Breakout proxy ─────────────────────────────────────────────────
    # Fires when price is within breakout_proximity_pct of 52-week high,
    # upward daily pressure is at least 1.0% (raised from 0.5% to reduce
    # false signals on low-momentum drift near the high), AND volume is at least
    # 80% of average (volume_ok is True when rel_volume is unavailable so
    # we do not penalise missing data).
    if (
        sr.pct_from_year_high is not None
        and sr.pct_change_1d is not None
    ):
        proximity_thresh = t["breakout_proximity_pct"]
        # pct_from_year_high is 0 at the high, negative below it
        pct_below = -min(0.0, sr.pct_from_year_high)  # 0 at high, positive below
        volume_ok = sr.rel_volume is None or sr.rel_volume >= 0.8
        if pct_below <= proximity_thresh and sr.pct_change_1d >= 1.0 and volume_ok:
            # Strength: 1.0 when at the high, ~0.5 when at the edge
            strength = round(
                max(0.0, 1.0 - pct_below / max(proximity_thresh, 1e-9)),
                3,
            )
            events.append(MarketEvent(
                symbol=sr.symbol,
                event_type=EventType.BREAKOUT_PROXY,
                strength=strength,
                metadata={
                    "pct_from_year_high": sr.pct_from_year_high,
                    "pct_change_1d": sr.pct_change_1d,
                    "year_high": sr.year_high,
                    "proximity_threshold_pct": proximity_thresh,
                },
                detected_at=sr.timestamp,
            ))

    # ── Volatility expansion ───────────────────────────────────────────
    if sr.day_range_pct is not None:
        vol_thresh = t["volatility_expansion_pct"]
        if sr.day_range_pct >= vol_thresh:
            events.append(MarketEvent(
                symbol=sr.symbol,
                event_type=EventType.VOLATILITY_EXPANSION,
                strength=_strength(sr.day_range_pct, vol_thresh),
                metadata={
                    "day_range_pct": sr.day_range_pct,
                    "day_high": sr.day_high,
                    "day_low": sr.day_low,
                    "threshold": vol_thresh,
                },
                detected_at=sr.timestamp,
            ))
