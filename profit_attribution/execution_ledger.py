"""
Profit Attribution — Execution Ledger
=======================================
Loads trade_events.jsonl (advisory execution log) and enriches each event
with forward-return data from the matched coverage outcome.

Matching strategy:
  1. Exact  — event run_id prefix matches coverage outcome entry_run_id exactly.
  2. Nearest — symbol matches AND entry_date gap ≤ MAX_MATCH_DAYS (default 7).
  3. None   — no coverage outcome found; outcome fields remain None.

Only LOGGABLE_ACTIONS are included in the ledger (same set as trade_event_logger).

No IO side-effects beyond reading the two input files.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coverage_evaluator import CoverageOutcome, build_coverage_outcomes
from profit_attribution.models import ExecutionLedgerEntry

logger = logging.getLogger("profit_attribution.execution_ledger")

DEFAULT_EVENTS_PATH = Path("outputs/policy/trade_events.jsonl")
MAX_MATCH_DAYS: int = 7
LOGGABLE_ACTIONS: frozenset[str] = frozenset(
    {"BUY", "SELL", "TRIM", "PROMOTE_TO_PORTFOLIO"}
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_execution_ledger(
    events_path: Optional[Path] = None,
    history_path: Optional[Path] = None,
) -> List[ExecutionLedgerEntry]:
    """
    Build an execution ledger from trade_events.jsonl.

    Each event is enriched with forward-return data from the nearest matching
    coverage outcome (by symbol + date).  Events without a match are still
    included with matched=False and all outcome fields as None.

    Args:
        events_path:  Override path to trade_events.jsonl.
        history_path: Override path to coverage_history.jsonl.

    Returns:
        List of ExecutionLedgerEntry sorted by timestamp ascending.
        Empty list if trade_events.jsonl is absent or contains no loggable events.
    """
    raw_events = _load_events(events_path)
    if not raw_events:
        logger.debug("execution_ledger: no trade events found")
        return []

    outcomes = _load_outcomes(history_path)
    outcome_index = _build_outcome_index(outcomes)

    entries: List[ExecutionLedgerEntry] = []
    for ev in raw_events:
        entry = _event_to_entry(ev, outcome_index)
        if entry is not None:
            entries.append(entry)

    entries.sort(key=lambda e: e.timestamp)
    logger.debug(
        "execution_ledger: %d events → %d entries (%d matched)",
        len(raw_events),
        len(entries),
        sum(1 for e in entries if e.matched),
    )
    return entries


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_events(path: Optional[Path]) -> List[dict]:
    src = Path(path) if path else DEFAULT_EVENTS_PATH
    if not src.exists():
        return []
    events: List[dict] = []
    with src.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning("execution_ledger: malformed line %d in %s — %s", lineno, src, exc)
                continue
            action = str(record.get("action", "")).upper().strip()
            if action in LOGGABLE_ACTIONS:
                events.append(record)
    return events


def _load_outcomes(path: Optional[Path]) -> List[CoverageOutcome]:
    try:
        return build_coverage_outcomes(path)
    except Exception as exc:
        logger.warning("execution_ledger: could not load coverage outcomes — %s", exc)
        return []


def _build_outcome_index(
    outcomes: List[CoverageOutcome],
) -> Dict[str, List[CoverageOutcome]]:
    """Index outcomes by uppercase symbol → list sorted by entry_date ascending."""
    index: Dict[str, List[CoverageOutcome]] = {}
    for o in outcomes:
        sym = str(o.symbol or "").upper().strip()
        if not sym:
            continue
        index.setdefault(sym, []).append(o)
    for lst in index.values():
        lst.sort(key=lambda o: o.entry_date)
    return index


def _event_to_entry(
    ev: dict,
    outcome_index: Dict[str, List[CoverageOutcome]],
) -> Optional[ExecutionLedgerEntry]:
    """Convert one raw event dict into an ExecutionLedgerEntry."""
    try:
        symbol = str(ev.get("symbol") or "").upper().strip()
        if not symbol:
            return None

        action = str(ev.get("action") or "").upper().strip()
        run_id = str(ev.get("run_id") or "")
        timestamp = str(ev.get("timestamp") or "")
        run_mode = str(ev.get("run_mode") or "daily")

        event_date = _parse_event_date(run_id, timestamp)

        outcome, match_quality = _find_best_match(symbol, event_date, outcome_index)

        entry = ExecutionLedgerEntry(
            event_id=f"{symbol}_{run_id}",
            symbol=symbol,
            action=action,
            run_id=run_id,
            timestamp=timestamp,
            run_mode=run_mode,
            strategy_type=ev.get("strategy_type") or None,
            score=_safe_float(ev.get("score")),
            confidence=_safe_float(ev.get("confidence")),
            suggested_allocation_pct=_safe_float(ev.get("suggested_allocation_pct")),
            suggested_allocation_amount=_safe_float(ev.get("suggested_allocation_amount")),
            drawdown_regime=str(ev.get("drawdown_regime") or "normal"),
            degraded_mode=bool(ev.get("degraded_mode", False)),
        )

        if outcome is not None:
            entry.return_1d = outcome.forward_return_1d
            entry.return_3d = outcome.forward_return_3d
            entry.return_5d = outcome.forward_return_5d
            entry.return_10d = outcome.forward_return_10d
            entry.mfe = outcome.mfe
            entry.mae = outcome.mae
            entry.exit_quality = outcome.exit_quality
            if outcome.observations:
                last = max(outcome.observations, key=lambda obs: obs.obs_date)
                entry.hold_days = (last.obs_date - outcome.entry_date).days
            entry.matched = True
            entry.match_quality = match_quality

        return entry

    except Exception as exc:
        logger.warning(
            "execution_ledger: skipping event for %s — %s",
            ev.get("symbol", "?"), exc,
        )
        return None


def _find_best_match(
    symbol: str,
    event_date: Optional[date],
    index: Dict[str, List[CoverageOutcome]],
    max_days: int = MAX_MATCH_DAYS,
) -> Tuple[Optional[CoverageOutcome], str]:
    """
    Find the best matching CoverageOutcome for a given symbol + event date.
    Returns (outcome, match_quality) where match_quality ∈ {"exact","nearest","none"}.
    """
    candidates = index.get(symbol.upper(), [])
    if not candidates:
        return None, "none"

    if event_date is None:
        # No date — pick the most recent outcome as best guess
        return candidates[-1], "nearest"

    # 1. Exact run_id date match
    event_date_prefix = event_date.isoformat()
    for o in candidates:
        if o.entry_run_id and o.entry_run_id.startswith(event_date_prefix):
            return o, "exact"

    # 2. Nearest by entry_date within tolerance
    best = min(candidates, key=lambda o: abs((o.entry_date - event_date).days))
    gap = abs((best.entry_date - event_date).days)
    if gap <= max_days:
        return best, "nearest"

    return None, "none"


def _parse_event_date(run_id: str, timestamp: str) -> Optional[date]:
    """Extract a date from run_id (e.g. '2026-04-16_daily') or ISO timestamp."""
    if run_id:
        date_part = run_id.split("_")[0]
        try:
            return date.fromisoformat(date_part)
        except ValueError:
            pass
    if timestamp:
        try:
            return datetime.fromisoformat(timestamp).date()
        except ValueError:
            pass
    return None


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
