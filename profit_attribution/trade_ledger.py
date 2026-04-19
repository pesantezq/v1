"""
Profit Attribution — Trade Ledger
===================================
Converts CoverageOutcome objects (from coverage_evaluator) into
TradeLedgerEntry records for trade-level profit attribution analysis.

This is a pure transformation layer: read-only, no IO.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from coverage_evaluator import CoverageOutcome, build_coverage_outcomes
from profit_attribution.models import TradeLedgerEntry

logger = logging.getLogger("profit_attribution.trade_ledger")


def build_trade_ledger(history_path=None) -> List[TradeLedgerEntry]:
    """
    Build a trade ledger from coverage history.

    Each CoverageOutcome (one per scanner promotion event) becomes one
    TradeLedgerEntry.  Entries without a valid price are silently skipped.

    Args:
        history_path: Optional override for coverage_history.jsonl path.

    Returns:
        List of TradeLedgerEntry, sorted by entry_date ascending.
    """
    outcomes: List[CoverageOutcome] = build_coverage_outcomes(history_path)
    if not outcomes:
        logger.debug("trade_ledger: no coverage outcomes available")
        return []

    entries: List[TradeLedgerEntry] = []
    for o in outcomes:
        entry = _outcome_to_entry(o)
        if entry is not None:
            entries.append(entry)

    entries.sort(key=lambda e: e.entry_date)
    logger.debug("trade_ledger: built %d entries from %d outcomes", len(entries), len(outcomes))
    return entries


def _outcome_to_entry(o: CoverageOutcome) -> Optional[TradeLedgerEntry]:
    """Map a single CoverageOutcome → TradeLedgerEntry.  Returns None on bad data."""
    try:
        if not o.symbol or not o.entry_date:
            return None

        hold_days: Optional[int] = None
        if o.observations:
            last_obs = max(o.observations, key=lambda obs: obs.obs_date)
            hold_days = (last_obs.obs_date - o.entry_date).days

        action_bucket = str(o.action_bucket or "")

        return TradeLedgerEntry(
            trade_id=f"{o.symbol}_{o.entry_run_id}",
            symbol=o.symbol,
            strategy_type=str(o.label or "watchlist"),
            entry_date=o.entry_date.isoformat(),
            entry_price=o.entry_price,
            entry_score=o.score,
            entry_events=list(o.events or []),
            entry_regime=str(o.drawdown_regime or "normal"),
            action_bucket=action_bucket,
            observation_count=len(o.observations),
            hold_days=hold_days,
            return_1d=o.forward_return_1d,
            return_3d=o.forward_return_3d,
            return_5d=o.forward_return_5d,
            return_10d=o.forward_return_10d,
            latest_return=o.latest_return,
            mfe=o.mfe,
            mae=o.mae,
            exit_quality=o.exit_quality,
            hit=o.hit,
            attributable=o.attributable,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("trade_ledger: skipping outcome for %s — %s", getattr(o, "symbol", "?"), exc)
        return None
