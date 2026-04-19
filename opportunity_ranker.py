"""
Opportunity Ranker
==================
Multi-factor composite scoring and ranking of broad-universe scan candidates.

Scoring model (0–100 points, weights configurable)
---------------------------------------------------
  Momentum           (weight default 0.40)
    Daily % change normalised to 5% ceiling.
    +5% or better → 100 raw pts.  Negative → 0 raw pts.

  Relative Strength proxy  (weight default 0.25)
    Distance of price from 52-week high.
    At the high (pct_from_year_high = 0) → 100 raw pts.
    20%+ below high                       → 0 raw pts.  Linear between.

  Volume Confirmation  (weight default 0.25)
    Relative volume normalised to 3× average ceiling.
    3× or higher → 100 raw pts.  Linear below.

  Volatility Sanity  (weight default 0.10)
    Penalises extreme intraday range.
    Range < 3%  → 100 raw pts (no penalty).
    Range = 12% → 0 raw pts.  Linear between 3–12%.
    Range > 12% → 0 raw pts.

Weights are re-normalised to sum=1.0 if the user provides a partial override.

Each RankedOpportunity carries:
  total_score      — weighted composite, 0–100
  factor_breakdown — raw score per factor (before weighting), for transparency
  reasons          — human-readable strings explaining each contributing factor
  events           — EventType names that fired for this symbol

Pure function — no I/O, no API calls.

Config key: ``opportunity_ranker``
  weights.momentum             (default: 0.40)
  weights.relative_strength    (default: 0.25)
  weights.volume_confirmation  (default: 0.25)
  weights.volatility_sanity    (default: 0.10)
  min_score                    (default: 0.0) — filter applied in ranker
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from universal_scanner import ScanResult
from event_detection import MarketEvent, EventType

logger = logging.getLogger("portfolio_automation.opportunity_ranker")

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_WEIGHTS: Dict[str, float] = {
    "momentum": 0.30,
    "relative_strength": 0.30,
    "volume_confirmation": 0.30,
    "volatility_sanity": 0.10,
}

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class FactorBreakdown:
    """Raw (pre-weight) scores per factor, 0–100."""
    momentum: Optional[float] = None
    relative_strength: Optional[float] = None
    volume_confirmation: Optional[float] = None
    volatility_sanity: Optional[float] = None

    def to_dict(self) -> Dict:
        return {
            "momentum": self.momentum,
            "relative_strength": self.relative_strength,
            "volume_confirmation": self.volume_confirmation,
            "volatility_sanity": self.volatility_sanity,
        }


@dataclass
class RankedOpportunity:
    symbol: str
    total_score: float           # 0–100
    factor_breakdown: FactorBreakdown
    reasons: List[str]
    events: List[str]            # EventType string values
    rank: int = 0                # populated after sorting
    theme_support: Optional[float] = None   # 0.0–1.0, from ScanResult
    scan_result: Optional[ScanResult] = field(default=None, repr=False)

    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "rank": self.rank,
            "total_score": self.total_score,
            "factor_breakdown": self.factor_breakdown.to_dict(),
            "reasons": self.reasons,
            "events": self.events,
            "theme_support": self.theme_support,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rank_opportunities(
    scan_results: List[ScanResult],
    events: List[MarketEvent],
    config: Optional[dict] = None,
) -> List[RankedOpportunity]:
    """
    Score and rank scan results by composite opportunity score.

    Args:
        scan_results: Output from ``UniversalScanner.scan()``.
        events:       Output from ``event_detection.detect_events()``.
        config:       ``opportunity_ranker`` config dict (optional).
                      Keys: weights (dict), min_score (float).

    Returns:
        List of RankedOpportunity sorted by total_score descending
        (alphabetical tiebreak).  Rank field is populated starting at 1.
        Returns empty list if scan_results is empty.
    """
    cfg = config or {}
    weights = _resolve_weights(cfg.get("weights", {}))
    min_score = _safe_float(cfg.get("min_score"), default=0.0)

    # Build event lookup: symbol → {EventType, ...}
    event_map: Dict[str, set] = {}
    for ev in events:
        event_type = getattr(ev, "event_type", ev)
        try:
            normalized_event = EventType(event_type)
        except ValueError:
            continue
        event_map.setdefault(ev.symbol, set()).add(normalized_event)

    ranked: List[RankedOpportunity] = []

    for sr in scan_results:
        if not sr.has_price:
            continue
        try:
            opp = _score_symbol(sr, event_map.get(sr.symbol, set()), weights)
        except Exception as exc:
            logger.warning("Scoring error for %s (skipped): %s", sr.symbol, exc)
            continue

        if opp.total_score >= min_score:
            ranked.append(opp)

    # Sort descending by score, then alphabetically as stable tiebreak
    ranked.sort(key=lambda o: (-o.total_score, o.symbol))

    for i, opp in enumerate(ranked, start=1):
        opp.rank = i

    logger.info(
        "OpportunityRanker: %d symbols scored, %d above min_score=%.1f",
        sum(1 for sr in scan_results if sr.has_price),
        len(ranked),
        min_score,
    )
    return ranked


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_weights(weight_cfg: dict) -> Dict[str, float]:
    """Merge user weights over defaults and re-normalise to sum=1.0."""
    w = dict(_DEFAULT_WEIGHTS)
    for k in _DEFAULT_WEIGHTS:
        if k in weight_cfg:
            try:
                val = float(weight_cfg[k])
                if val >= 0:
                    w[k] = val
            except (TypeError, ValueError):
                pass
    total = sum(w.values())
    if total <= 0:
        return dict(_DEFAULT_WEIGHTS)
    return {k: v / total for k, v in w.items()}


def _score_symbol(
    sr: ScanResult,
    triggered_events: set,
    weights: Dict[str, float],
) -> RankedOpportunity:
    """Compute factor scores and composite for one ScanResult."""
    reasons: List[str] = []
    fb = FactorBreakdown()

    # ── Momentum ──────────────────────────────────────────────────────
    # Normalised: +5% daily move → 100 pts; negative → 0 pts.
    # Below-average volume (rel_volume < 1.0) discounts the score — unconfirmed
    # moves on thin volume are weaker signals than volume-backed advances.
    momentum_raw = 0.0
    if sr.pct_change_1d is not None:
        pct = sr.pct_change_1d
        if pct > 0:
            momentum_raw = min(100.0, pct / 5.0 * 100)
            if sr.rel_volume is not None and sr.rel_volume < 1.0:
                vol_adj = max(0.2, sr.rel_volume)
                momentum_raw *= vol_adj
                reasons.append(f"momentum: +{pct:.2f}% today (light volume: {sr.rel_volume:.2f}x avg)")
            else:
                reasons.append(f"momentum: +{pct:.2f}% today")
        else:
            reasons.append(f"momentum: {pct:.2f}% today")
    fb.momentum = round(momentum_raw, 1)

    # ── Relative Strength proxy ───────────────────────────────────────
    # Normalised: at year high → 100 pts; 20%+ below → 0 pts.
    rs_raw = 0.0
    if sr.pct_from_year_high is not None:
        pct_below = abs(min(0.0, sr.pct_from_year_high))  # 0 at high
        rs_raw = max(0.0, 100.0 - (pct_below / 20.0) * 100)
        if pct_below < 5.0:
            reasons.append(
                f"RS: near 52wk high ({sr.pct_from_year_high:+.1f}%)"
            )
        elif pct_below < 15.0:
            reasons.append(
                f"RS: moderate ({sr.pct_from_year_high:+.1f}% vs high)"
            )
    fb.relative_strength = round(rs_raw, 1)

    # ── Volume Confirmation ───────────────────────────────────────────
    # Normalised: 3× average → 100 pts.
    # Volume on a down-move is sell pressure, not confirmation — suppress credit.
    vol_raw = 0.0
    if sr.rel_volume is not None and EventType.STRONG_MOVE_DOWN not in triggered_events:
        vol_raw = min(100.0, sr.rel_volume / 3.0 * 100)
        if sr.rel_volume >= 2.0:
            reasons.append(
                f"vol: {sr.rel_volume:.1f}x avg"
                + (f" ({sr.volume:,} shares)" if sr.volume is not None else "")
            )
    fb.volume_confirmation = round(vol_raw, 1)

    # ── Volatility Sanity ─────────────────────────────────────────────
    # Range < 3% → 100 pts (clean); range 3–12% → linear decay; > 12% → 0.
    # The upper boundary was extended from 10% to 12% to eliminate the abrupt
    # zero-score cliff that penalised 10.1%-range stocks as harshly as 20%-range ones.
    sanity_raw = 100.0
    if sr.day_range_pct is not None:
        drp = sr.day_range_pct
        if drp > 12.0:
            sanity_raw = 0.0
            reasons.append(f"risk: wide range {drp:.1f}%")
        elif drp > 3.0:
            sanity_raw = max(0.0, 100.0 - (drp - 3.0) / 9.0 * 100)
    fb.volatility_sanity = round(sanity_raw, 1)

    # ── Composite ─────────────────────────────────────────────────────
    total = (
        momentum_raw * weights["momentum"]
        + rs_raw * weights["relative_strength"]
        + vol_raw * weights["volume_confirmation"]
        + sanity_raw * weights["volatility_sanity"]
    )
    total = round(total, 1)

    if not reasons:
        reasons.append("limited market data left the opportunity score reliant on partial inputs")

    event_labels = sorted(ev.value for ev in triggered_events)

    return RankedOpportunity(
        symbol=sr.symbol,
        total_score=total,
        factor_breakdown=fb,
        reasons=reasons,
        events=event_labels,
        theme_support=getattr(sr, "theme_support", None),
        scan_result=sr,
    )


def _safe_float(value, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if numeric != numeric or numeric == float("inf") or numeric == float("-inf"):
        return default
    return numeric
