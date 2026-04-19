"""
Profit Attribution — Missed Opportunity Tracker
=================================================
Identifies high-scored candidates that the portfolio did NOT act on
and measures how they performed — i.e., the cost of inaction.

"Not acted on" heuristic:
  action_bucket in ("", "watchonly", "unclassified", "hold") AND
  entry_score >= MISSED_HIGH_SCORE_THRESHOLD (default 70)

This is a signal, not a guarantee.  action_bucket is populated by the
scanner pipeline's portfolio_context; empty/unclassified = no active
portfolio decision was recorded at promotion time.

No IO — pure computation.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from profit_attribution.models import (
    TradeLedgerEntry,
    OpportunityRecord,
    MISSED_HIGH_SCORE_THRESHOLD,
)

logger = logging.getLogger("profit_attribution.opportunity_tracker")

# Action bucket values that suggest the candidate was not acted on
_INACTIVE_BUCKETS = frozenset({"", "watchonly", "unclassified", "hold", "monitor"})


def find_missed_opportunities(
    ledger: List[TradeLedgerEntry],
    score_threshold: float = MISSED_HIGH_SCORE_THRESHOLD,
) -> Tuple[List[OpportunityRecord], Optional[float]]:
    """
    Find high-scored trades that were not acted on and compute opportunity cost.

    Args:
        ledger:           All TradeLedgerEntry records.
        score_threshold:  Minimum score to be considered "notable" (default 70).

    Returns:
        (opportunities, total_opportunity_cost)
        total_opportunity_cost = sum of positive forward_return_5d values
        for missed trades (i.e., the upside that was left on the table).
    """
    opportunities: List[OpportunityRecord] = []

    for trade in ledger:
        if trade.entry_score < score_threshold:
            continue
        if trade.action_bucket.lower().strip() not in _INACTIVE_BUCKETS:
            continue

        r5 = trade.return_5d
        outcome, opp_cost = _classify_opportunity(r5)

        opportunities.append(OpportunityRecord(
            symbol=trade.symbol,
            entry_date=trade.entry_date,
            strategy_type=trade.strategy_type,
            score=trade.entry_score,
            action_bucket=trade.action_bucket,
            forward_return_5d=r5,
            mfe=trade.mfe,
            outcome=outcome,
            opportunity_cost=opp_cost,
        ))

    # Sort by opportunity_cost descending (largest missed upside first)
    opportunities.sort(key=lambda o: -(o.opportunity_cost or 0.0))

    total: Optional[float] = None
    costs = [o.opportunity_cost for o in opportunities if o.opportunity_cost is not None]
    if costs:
        total = round(sum(costs), 6)

    logger.debug(
        "opportunity_tracker: %d missed opportunities (score ≥ %.0f), total_cost=%s",
        len(opportunities), score_threshold, total,
    )
    return opportunities, total


def _classify_opportunity(
    forward_return_5d: Optional[float],
) -> Tuple[str, Optional[float]]:
    """Return (outcome_label, opportunity_cost) for a single missed trade."""
    if forward_return_5d is None:
        return "unknown", None
    if forward_return_5d > 0:
        return "would_have_won", round(forward_return_5d, 6)
    return "would_have_lost", 0.0
