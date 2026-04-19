"""
Profit Attribution — Exit Quality Analyzer
==========================================
Classifies the exit quality of each TradeLedgerEntry and answers three
diagnostic questions:

  1. Did the exit protect profit?     → "protected" (retained ≥70% of peak)
  2. Did we exit too early?           → "protected" with high exit_quality (near 1.0)
  3. Did we give back gains?          → "gave_back" or "reversed"

Classification hierarchy (ordered, first match wins):
  unresolved  → no observations; cannot evaluate
  no_gain     → mfe == 0 (price never rose above entry in the window)
  protected   → exit_quality ≥ EXIT_QUALITY_PROTECTED (0.70)
  partial     → exit_quality ≥ EXIT_QUALITY_PARTIAL   (0.30)
  gave_back   → 0 < exit_quality < EXIT_QUALITY_PARTIAL
  reversed    → exit_quality ≤ 0 (gain turned to loss)

All thresholds are imported from models.py so only one canonical definition.
No IO — pure computation.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple

from profit_attribution.models import (
    TradeLedgerEntry,
    ExitClassification,
    EXIT_QUALITY_PROTECTED,
    EXIT_QUALITY_PARTIAL,
    EXIT_LABELS,
)

logger = logging.getLogger("profit_attribution.exit_analyzer")

# Fine-grained "protected" sub-label: held near peak
EARLY_EXIT_QUALITY_CEIL: float = 1.05   # exit_quality > 1.0 means re-entered after dip


def classify_exits(
    ledger: List[TradeLedgerEntry],
) -> Tuple[List[ExitClassification], Dict[str, int]]:
    """
    Classify exit quality for every trade in the ledger.

    Returns:
        classified  — one ExitClassification per trade
        summary     — {label: count} across all trades
    """
    classified: List[ExitClassification] = []
    summary: Dict[str, int] = {label: 0 for label in EXIT_LABELS}

    for trade in ledger:
        ec = _classify_one(trade)
        classified.append(ec)
        summary[ec.label] = summary.get(ec.label, 0) + 1

    return classified, summary


def _classify_one(trade: TradeLedgerEntry) -> ExitClassification:
    """Classify a single trade's exit quality."""
    if not trade.attributable or trade.mfe is None:
        return ExitClassification(
            trade_id=trade.trade_id,
            symbol=trade.symbol,
            exit_quality=None,
            label="unresolved",
            detail="No price observations — exit quality cannot be evaluated.",
        )

    if trade.mfe == 0.0:
        detail = (
            f"Price never exceeded entry within the tracking window "
            f"(MAE: {_pct(trade.mae)})."
        )
        return ExitClassification(
            trade_id=trade.trade_id,
            symbol=trade.symbol,
            exit_quality=trade.exit_quality,
            label="no_gain",
            detail=detail,
        )

    eq = trade.exit_quality
    if eq is None:
        return ExitClassification(
            trade_id=trade.trade_id,
            symbol=trade.symbol,
            exit_quality=None,
            label="unresolved",
            detail="Exit quality undefined (MFE > 0 but latest_return is missing).",
        )

    if eq >= EXIT_QUALITY_PROTECTED:
        if eq >= 1.0:
            detail = (
                f"Excellent: retained {eq:.0%} of peak gain "
                f"(MFE {_pct(trade.mfe)}, latest {_pct(trade.latest_return)})."
            )
        else:
            detail = (
                f"Protected: retained {eq:.0%} of peak gain "
                f"(MFE {_pct(trade.mfe)}, latest {_pct(trade.latest_return)})."
            )
        label = "protected"

    elif eq >= EXIT_QUALITY_PARTIAL:
        detail = (
            f"Partial: retained {eq:.0%} of peak gain "
            f"(MFE {_pct(trade.mfe)}, latest {_pct(trade.latest_return)})."
        )
        label = "partial"

    elif eq > 0:
        detail = (
            f"Gave back gains: retained only {eq:.0%} of peak "
            f"(MFE {_pct(trade.mfe)}, latest {_pct(trade.latest_return)})."
        )
        label = "gave_back"

    else:
        detail = (
            f"Reversed: peak gain of {_pct(trade.mfe)} turned to "
            f"{_pct(trade.latest_return)} (exit_quality {eq:.2f})."
        )
        label = "reversed"

    return ExitClassification(
        trade_id=trade.trade_id,
        symbol=trade.symbol,
        exit_quality=eq,
        label=label,
        detail=detail,
    )


def _pct(v) -> str:
    if v is None:
        return "—"
    return f"{float(v) * 100:+.1f}%"
