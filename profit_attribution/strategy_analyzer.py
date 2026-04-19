"""
Profit Attribution — Strategy Analyzer
========================================
Aggregates TradeLedgerEntry records by strategy type, score band, and
market regime to answer: "which strategy type / context actually made money?"

All constants are explicit.  No IO — pure computation.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from profit_attribution.models import (
    TradeLedgerEntry,
    StrategyPerformance,
    MISSED_HIGH_SCORE_THRESHOLD,
)

logger = logging.getLogger("profit_attribution.strategy_analyzer")

# Score-band boundaries (mirrors coverage_evaluator.SCORE_BANDS)
SCORE_BANDS = (
    ("low",    0,  40),
    ("medium", 41, 70),
    ("high",   71, 100),
)

SMALL_SAMPLE: int = 5
STRONG_WIN_THRESHOLD: float = 0.02   # +2%
ADVERSE_THRESHOLD: float = -0.02     # −2%


def analyze_by_strategy(
    ledger: List[TradeLedgerEntry],
) -> List[StrategyPerformance]:
    """
    Group trades by strategy_type (compounder / momentum / watchlist).
    Returns buckets sorted by descending win_rate (None last).
    """
    return _analyze_by(ledger, key_fn=lambda t: t.strategy_type, dimension="strategy")


def analyze_by_score_band(
    ledger: List[TradeLedgerEntry],
) -> List[StrategyPerformance]:
    """
    Group trades by score band: low (0-40) / medium (41-70) / high (71-100).
    Returns buckets in band order.
    """
    buckets: Dict[str, StrategyPerformance] = {
        name: StrategyPerformance(name=name, dimension="score_band")
        for name, _, _ in SCORE_BANDS
    }
    for trade in ledger:
        band = _score_band(trade.entry_score)
        _accumulate(buckets[band], trade)
    for b in buckets.values():
        b.small_sample = b.attributable < SMALL_SAMPLE
    return [buckets[name] for name, _, _ in SCORE_BANDS]


def analyze_by_regime(
    ledger: List[TradeLedgerEntry],
) -> List[StrategyPerformance]:
    """
    Group trades by entry drawdown regime.
    Returns buckets sorted by descending win_rate (None last).
    """
    return _analyze_by(ledger, key_fn=lambda t: t.entry_regime or "unknown", dimension="regime")


def analyze_by_confidence_tier(
    ledger: List[TradeLedgerEntry],
) -> List[StrategyPerformance]:
    """
    Group trades by score-tier acting as a confidence proxy.
    Reuses SCORE_BANDS since coverage_history has no separate confidence field.
    """
    return analyze_by_score_band(ledger)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _analyze_by(
    ledger: List[TradeLedgerEntry],
    key_fn,
    dimension: str,
) -> List[StrategyPerformance]:
    buckets: Dict[str, StrategyPerformance] = {}
    for trade in ledger:
        key = str(key_fn(trade) or "unknown")
        if key not in buckets:
            buckets[key] = StrategyPerformance(name=key, dimension=dimension)
        _accumulate(buckets[key], trade)
    for b in buckets.values():
        b.small_sample = b.attributable < SMALL_SAMPLE
    return sorted(
        buckets.values(),
        key=lambda b: (b.win_rate is None, -(b.win_rate or 0.0)),
    )


def _accumulate(perf: StrategyPerformance, trade: TradeLedgerEntry) -> None:
    """Add one TradeLedgerEntry into a StrategyPerformance bucket (in-place)."""
    perf.total_entries += 1
    if not trade.attributable:
        return
    perf.attributable += 1

    r5 = trade.return_5d
    if r5 is not None:
        perf.entries_with_5d += 1
        perf.returns_5d.append(r5)
        if r5 > 0:
            perf.hit_count += 1
            perf.gains.append(r5)
        else:
            perf.losses.append(r5)
        if r5 >= STRONG_WIN_THRESHOLD:
            perf.strong_win_count += 1
        if r5 <= ADVERSE_THRESHOLD:
            perf.adverse_count += 1

    if trade.mfe is not None:
        perf.mfe_values.append(trade.mfe)
    if trade.mae is not None:
        perf.mae_values.append(trade.mae)
    if trade.exit_quality is not None:
        perf.eq_values.append(trade.exit_quality)
    if trade.hold_days is not None:
        perf.hold_days_values.append(trade.hold_days)


def _score_band(score: float) -> str:
    for name, lo, hi in SCORE_BANDS:
        if lo <= score <= hi:
            return name
    return "medium"
