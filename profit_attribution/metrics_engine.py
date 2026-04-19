"""
Profit Attribution — Metrics Engine
=====================================
Computes aggregated portfolio-learning metrics from the trade ledger.

Metrics computed (all explicit, never hidden):
  win_rate           = count(return_5d > 0) / count(attributable with 5d return)
  avg_gain           = mean(return_5d) for winning trades
  avg_loss           = mean(return_5d) for losing trades (negative value)
  risk_reward        = avg_gain / abs(avg_loss)
  expectancy         = win_rate * avg_gain + (1 - win_rate) * avg_loss
  capital_efficiency = sum(positive returns) / sum(abs(all returns))
                       Measures how concentrated the positive returns are.
                       1.0 = all returns were positive;
                       0.5 = gains equal losses in magnitude.
  avg_mfe            = mean(mfe) across attributable trades
  avg_mae            = mean(mae) across attributable trades
  avg_exit_quality   = mean(exit_quality) across trades where exit_quality is known
  avg_hold_days      = mean(hold_days) across attributable trades
  strong_win_rate    = count(return_5d >= +2%) / count(with 5d return)
  adverse_rate       = count(return_5d <= -2%) / count(with 5d return)

Primary horizon: T+5d (return_5d).  No IO — pure computation.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from profit_attribution.models import TradeLedgerEntry, AttributionMetrics

logger = logging.getLogger("profit_attribution.metrics_engine")

STRONG_WIN_THRESHOLD: float = 0.02   # +2%
ADVERSE_THRESHOLD: float = -0.02     # −2%


def compute_metrics(ledger: List[TradeLedgerEntry]) -> AttributionMetrics:
    """
    Compute overall attribution metrics from a trade ledger.

    Handles empty ledger and missing data gracefully — all Optional fields
    remain None when there is insufficient data.

    Args:
        ledger: List of TradeLedgerEntry records.

    Returns:
        AttributionMetrics with all computable fields populated.
    """
    total = len(ledger)
    attributable_list = [t for t in ledger if t.attributable]
    attributable = len(attributable_list)
    coverage_rate = round(attributable / total, 4) if total > 0 else 0.0

    with_5d = [t for t in attributable_list if t.return_5d is not None]
    n5 = len(with_5d)

    returns_5d = [t.return_5d for t in with_5d]   # type: ignore[misc]
    gains = [r for r in returns_5d if r > 0]
    losses = [r for r in returns_5d if r <= 0]
    hits = len(gains)
    strong_wins = sum(1 for r in returns_5d if r >= STRONG_WIN_THRESHOLD)
    adverse = sum(1 for r in returns_5d if r <= ADVERSE_THRESHOLD)

    win_rate = _safe_rate(hits, n5)
    avg_gain: Optional[float] = _safe_mean(gains)
    avg_loss: Optional[float] = _safe_mean(losses)
    risk_reward: Optional[float] = _safe_rr(avg_gain, avg_loss)
    expectancy: Optional[float] = _safe_expectancy(win_rate, avg_gain, avg_loss)
    capital_efficiency: Optional[float] = _safe_cap_eff(returns_5d)

    mfe_vals = [t.mfe for t in attributable_list if t.mfe is not None]
    mae_vals = [t.mae for t in attributable_list if t.mae is not None]
    eq_vals = [t.exit_quality for t in attributable_list if t.exit_quality is not None]
    hold_vals = [t.hold_days for t in attributable_list if t.hold_days is not None]

    avg_mfe = _safe_mean(mfe_vals)
    avg_mae = _safe_mean(mae_vals)
    avg_exit_quality = _safe_mean(eq_vals)
    avg_hold_days = _safe_mean(hold_vals)

    strong_win_rate = _safe_rate(strong_wins, n5)
    adverse_rate = _safe_rate(adverse, n5)

    logger.debug(
        "metrics_engine: %d trades, %d attr, %d with 5d | win=%.0f%% rr=%.2f exp=%.4f",
        total, attributable, n5,
        (win_rate or 0) * 100,
        risk_reward or 0,
        expectancy or 0,
    )

    return AttributionMetrics(
        total_entries=total,
        attributable_entries=attributable,
        entries_with_5d=n5,
        coverage_rate=coverage_rate,
        win_rate=win_rate,
        avg_gain=avg_gain,
        avg_loss=avg_loss,
        risk_reward=risk_reward,
        expectancy=expectancy,
        capital_efficiency=capital_efficiency,
        avg_mfe=avg_mfe,
        avg_mae=avg_mae,
        avg_exit_quality=avg_exit_quality,
        avg_hold_days=avg_hold_days,
        strong_win_rate=strong_win_rate,
        adverse_rate=adverse_rate,
    )


def notable_trades(
    ledger: List[TradeLedgerEntry],
    n: int = 5,
) -> tuple[list, list]:
    """
    Return top-n winning and bottom-n losing trades by 5d return.

    Returns:
        (best_trades, worst_trades) — each a list of dicts.
    """
    with_5d = [t for t in ledger if t.attributable and t.return_5d is not None]
    if not with_5d:
        return [], []

    ranked = sorted(with_5d, key=lambda t: t.return_5d or 0.0)

    def _fmt(t: TradeLedgerEntry) -> dict:
        return {
            "trade_id": t.trade_id,
            "symbol": t.symbol,
            "strategy_type": t.strategy_type,
            "entry_date": t.entry_date,
            "entry_score": t.entry_score,
            "entry_regime": t.entry_regime,
            "return_5d": t.return_5d,
            "mfe": t.mfe,
            "mae": t.mae,
            "exit_quality": t.exit_quality,
            "hold_days": t.hold_days,
        }

    best = [_fmt(t) for t in reversed(ranked[-n:])]
    worst = [_fmt(t) for t in ranked[:n]]
    return best, worst


# ---------------------------------------------------------------------------
# Helpers
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


def _safe_cap_eff(returns: list) -> Optional[float]:
    """Capital efficiency = sum(positive) / sum(abs(all))."""
    if not returns:
        return None
    total_abs = sum(abs(r) for r in returns)
    if total_abs == 0:
        return None
    positive_sum = sum(r for r in returns if r > 0)
    return round(positive_sum / total_abs, 4)
