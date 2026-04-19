"""
Profit Attribution — Execution Metrics
========================================
Computes action-level metrics from ExecutionLedgerEntry records.

BUY / PROMOTE_TO_PORTFOLIO — primary question: "did our buys work?"
  win_rate, avg_gain, avg_loss, risk_reward, expectancy (via return_5d)

SELL / TRIM — primary question: "did we exit well?"
  avg_exit_quality (retained what % of peak gain)

All actions are also grouped by strategy_type, score_band, and regime
using the same StrategyPerformance buckets as coverage attribution, so
the two layers can be compared side-by-side.

No IO — pure computation.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from profit_attribution.models import (
    ExecutionActionMetrics,
    ExecutionAttributionSummary,
    ExecutionLedgerEntry,
    StrategyPerformance,
)
from profit_attribution.confidence_calibration import calibrate_confidence_bands

logger = logging.getLogger("profit_attribution.execution_metrics")

SCORE_BANDS = (
    ("low",    0,  40),
    ("medium", 41, 70),
    ("high",   71, 100),
)
CONFIDENCE_BANDS = (
    ("low",    0.0,  0.65),   # confidence < 0.65
    ("medium", 0.65, 0.80),   # 0.65 ≤ confidence ≤ 0.80
    ("high",   0.80, 1.0),    # confidence > 0.80
)
SMALL_SAMPLE: int = 5
STRONG_WIN_THRESHOLD: float = 0.02
ADVERSE_THRESHOLD: float = -0.02


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_execution_attribution(
    ledger: List[ExecutionLedgerEntry],
) -> ExecutionAttributionSummary:
    """
    Build the full ExecutionAttributionSummary from an execution ledger.

    Args:
        ledger: List of ExecutionLedgerEntry from execution_ledger.build_execution_ledger().

    Returns:
        ExecutionAttributionSummary with all metrics, breakdowns, and notes.
    """
    now_str = datetime.now().isoformat()
    notes: List[str] = []

    total = len(ledger)
    matched = sum(1 for e in ledger if e.matched)
    match_rate = round(matched / total, 4) if total > 0 else 0.0

    if total == 0:
        notes.append("No trade events found in trade_events.jsonl.")
    elif matched == 0:
        notes.append(
            f"No execution events matched to coverage outcomes "
            f"({total} events logged, 0 matched).  "
            "Coverage history and trade events must overlap in date and symbol."
        )
    elif match_rate < 0.3:
        notes.append(
            f"Low match rate: only {matched}/{total} events matched to coverage outcomes "
            f"({match_rate * 100:.0f}%).  Results may be incomplete."
        )

    by_action = _compute_by_action(ledger)
    by_strategy = _analyze_by(ledger, key_fn=lambda e: e.strategy_type or "unknown", dimension="exec_strategy")
    by_score_band = _analyze_by_score_band(ledger)
    by_regime = _analyze_by(ledger, key_fn=lambda e: e.drawdown_regime or "normal", dimension="exec_regime")
    by_confidence_band = _analyze_by_confidence_band(ledger)
    confidence_calibration = calibrate_confidence_bands(by_confidence_band)

    logger.debug(
        "confidence_calibration: status=%s strongest=%s weakest=%s",
        confidence_calibration.status,
        confidence_calibration.strongest_band,
        confidence_calibration.weakest_band,
    )

    return ExecutionAttributionSummary(
        generated_at=now_str,
        total_events=total,
        matched_events=matched,
        match_rate=match_rate,
        by_action=by_action,
        by_strategy=by_strategy,
        by_score_band=by_score_band,
        by_regime=by_regime,
        by_confidence_band=by_confidence_band,
        confidence_calibration=confidence_calibration,
        execution_ledger=ledger,
        data_quality_notes=notes,
    )


# ---------------------------------------------------------------------------
# Per-action breakdown
# ---------------------------------------------------------------------------

def _compute_by_action(
    ledger: List[ExecutionLedgerEntry],
) -> List[ExecutionActionMetrics]:
    """Compute ExecutionActionMetrics for each distinct action type."""
    buckets: Dict[str, list] = {}
    for entry in ledger:
        buckets.setdefault(entry.action, []).append(entry)

    result: List[ExecutionActionMetrics] = []
    for action in sorted(buckets):
        entries = buckets[action]
        result.append(_action_metrics(action, entries))
    return result


def _action_metrics(
    action: str,
    entries: List[ExecutionLedgerEntry],
) -> ExecutionActionMetrics:
    matched_entries = [e for e in entries if e.matched]
    with_5d = [e for e in matched_entries if e.return_5d is not None]

    returns = [e.return_5d for e in with_5d]  # type: ignore[misc]
    gains = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    hits = len(gains)

    win_rate = _safe_rate(hits, len(returns))
    avg_gain = _safe_mean(gains)
    avg_loss = _safe_mean(losses)
    risk_reward = _safe_rr(avg_gain, avg_loss)
    expectancy = _safe_expectancy(win_rate, avg_gain, avg_loss)

    eq_vals = [e.exit_quality for e in matched_entries if e.exit_quality is not None]
    avg_exit_quality = _safe_mean(eq_vals)

    return ExecutionActionMetrics(
        action=action,
        total_events=len(entries),
        matched_events=len(matched_entries),
        entries_with_5d=len(with_5d),
        win_rate=win_rate,
        avg_gain=avg_gain,
        avg_loss=avg_loss,
        risk_reward=risk_reward,
        expectancy=expectancy,
        avg_exit_quality=avg_exit_quality,
    )


# ---------------------------------------------------------------------------
# Dimensional breakdowns (reuses StrategyPerformance model)
# ---------------------------------------------------------------------------

def _analyze_by(
    ledger: List[ExecutionLedgerEntry],
    key_fn,
    dimension: str,
) -> List[StrategyPerformance]:
    buckets: Dict[str, StrategyPerformance] = {}
    for entry in ledger:
        key = str(key_fn(entry) or "unknown")
        if key not in buckets:
            buckets[key] = StrategyPerformance(name=key, dimension=dimension)
        _accumulate(buckets[key], entry)
    for b in buckets.values():
        b.small_sample = b.attributable < SMALL_SAMPLE
    return sorted(
        buckets.values(),
        key=lambda b: (b.win_rate is None, -(b.win_rate or 0.0)),
    )


def _analyze_by_score_band(
    ledger: List[ExecutionLedgerEntry],
) -> List[StrategyPerformance]:
    buckets: Dict[str, StrategyPerformance] = {
        name: StrategyPerformance(name=name, dimension="exec_score_band")
        for name, _, _ in SCORE_BANDS
    }
    for entry in ledger:
        band = _score_band(entry.score or 0.0)
        _accumulate(buckets[band], entry)
    for b in buckets.values():
        b.small_sample = b.attributable < SMALL_SAMPLE
    return [buckets[name] for name, _, _ in SCORE_BANDS]


def _analyze_by_confidence_band(
    ledger: List[ExecutionLedgerEntry],
) -> List[StrategyPerformance]:
    buckets: Dict[str, StrategyPerformance] = {
        name: StrategyPerformance(name=name, dimension="exec_confidence_band")
        for name, _, _ in CONFIDENCE_BANDS
    }
    for entry in ledger:
        band = _confidence_band(entry.confidence if entry.confidence is not None else 0.0)
        _accumulate(buckets[band], entry)
    for b in buckets.values():
        b.small_sample = b.attributable < SMALL_SAMPLE
    return [buckets[name] for name, _, _ in CONFIDENCE_BANDS]


def _accumulate(perf: StrategyPerformance, entry: ExecutionLedgerEntry) -> None:
    """Add one ExecutionLedgerEntry into a StrategyPerformance bucket."""
    perf.total_entries += 1
    if not entry.matched:
        return
    perf.attributable += 1

    r5 = entry.return_5d
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

    if entry.mfe is not None:
        perf.mfe_values.append(entry.mfe)
    if entry.mae is not None:
        perf.mae_values.append(entry.mae)
    if entry.exit_quality is not None:
        perf.eq_values.append(entry.exit_quality)
    if entry.hold_days is not None:
        perf.hold_days_values.append(entry.hold_days)


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

def _safe_rate(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator > 0 else None


def _safe_mean(values: list) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def _safe_rr(avg_gain: Optional[float], avg_loss: Optional[float]) -> Optional[float]:
    if avg_gain is None or avg_loss is None or avg_loss == 0:
        return None
    return round(avg_gain / abs(avg_loss), 4)


def _safe_expectancy(
    win_rate: Optional[float],
    avg_gain: Optional[float],
    avg_loss: Optional[float],
) -> Optional[float]:
    if win_rate is None or avg_gain is None or avg_loss is None:
        return None
    return round(win_rate * avg_gain + (1 - win_rate) * avg_loss, 6)


def _score_band(score: float) -> str:
    for name, lo, hi in SCORE_BANDS:
        if lo <= score <= hi:
            return name
    return "medium"


def _confidence_band(confidence: float) -> str:
    if confidence < 0.65:
        return "low"
    if confidence <= 0.80:
        return "medium"
    return "high"
