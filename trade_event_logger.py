"""
Trade Event Logger
==================
Append-only JSONL log of finalized portfolio actions (BUY, SELL, TRIM,
PROMOTE_TO_PORTFOLIO).  Written after generate_portfolio_actions() returns
in main.py — no business logic is modified.

Output: outputs/policy/trade_events.jsonl
  One JSON object per line, one record per logged action per run.

Public API:
  append_trade_events(actions, *, run_id, ...) → int   (records written)
  load_trade_events(path, *, action_filter)    → list[dict]
  iter_trade_events(path, *, action_filter)    → Iterator[dict]

Logged actions (default):  BUY | SELL | TRIM | PROMOTE_TO_PORTFOLIO
Skipped (informational):   HOLD | ADD_TO_WATCHLIST
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Any, Iterator, List, Optional, Set

logger = logging.getLogger("trade_event_logger")

DEFAULT_PATH = Path("outputs/policy/trade_events.jsonl")

# Actions that represent capital-allocation or position-change decisions.
# HOLD and ADD_TO_WATCHLIST are informational — they produce no event record.
LOGGABLE_ACTIONS: frozenset[str] = frozenset(
    {"BUY", "SELL", "TRIM", "PROMOTE_TO_PORTFOLIO"}
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TradeEvent:
    # Run context
    run_id: str
    timestamp: str
    run_mode: str
    portfolio_value: Optional[float]
    cash_available: Optional[float]
    drawdown_regime: str
    degraded_mode: bool
    degraded_reason: Optional[str]

    # Action core
    symbol: str
    action: str                          # BUY | SELL | TRIM | PROMOTE_TO_PORTFOLIO
    strategy_type: Optional[str]         # compounder | momentum | watchlist
    score: Optional[float]               # 0-100
    confidence: Optional[float]          # 0-1

    # Sizing (advisory — not executed shares/price)
    suggested_allocation_pct: Optional[float]
    suggested_allocation_amount: Optional[float]

    # Decision detail
    rationale: List[str] = field(default_factory=list)
    related_symbol: Optional[str] = None    # e.g. position being replaced
    exit_plan: Optional[dict] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["exit_plan"] = dict(self.exit_plan) if self.exit_plan else None
        return d


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def append_trade_events(
    actions: List[dict[str, Any]],
    *,
    run_id: str,
    run_mode: str,
    portfolio_value: Optional[float] = None,
    cash_available: Optional[float] = None,
    drawdown_regime: str = "normal",
    degraded_mode: bool = False,
    degraded_reason: Optional[str] = None,
    timestamp: Optional[str] = None,
    loggable_actions: Optional[Set[str]] = None,
    history_path: Optional[Path] = None,
    dry_run: bool = False,
) -> int:
    """
    Append trade event records for all finalized loggable actions.

    Non-fatal: any serialization or IO failure is logged as a warning and
    returns 0 without raising.

    Args:
        actions:           List of PortfolioAction dicts from generate_portfolio_actions().
        run_id:            Unique run identifier (e.g. "2026-04-16_daily").
        run_mode:          "daily" | "weekly" | "monthly".
        portfolio_value:   Total portfolio value at decision time.
        cash_available:    Available cash at decision time.
        drawdown_regime:   Market regime label.
        degraded_mode:     Whether data quality is degraded.
        degraded_reason:   Reason for degraded mode (optional).
        timestamp:         ISO datetime string; defaults to now.
        loggable_actions:  Override which action types are recorded.
        history_path:      Override output file path.
        dry_run:           If True, build records but skip writing.

    Returns:
        Number of records written (or would-be-written in dry_run mode).
    """
    if not actions:
        return 0

    allowed = loggable_actions if loggable_actions is not None else LOGGABLE_ACTIONS
    ts = timestamp or datetime.now().isoformat()

    records: list[dict[str, Any]] = []
    for action in actions:
        raw_action = str(action.get("action", "")).upper().strip()
        if raw_action not in allowed:
            continue
        try:
            event = _build_event(
                action=action,
                run_id=run_id,
                run_mode=run_mode,
                timestamp=ts,
                portfolio_value=portfolio_value,
                cash_available=cash_available,
                drawdown_regime=drawdown_regime,
                degraded_mode=degraded_mode,
                degraded_reason=degraded_reason,
            )
            records.append(event.to_dict())
        except Exception as exc:
            logger.warning(
                "trade_event_logger: failed to serialize action %s for %s — %s",
                action.get("action"), action.get("symbol"), exc,
            )

    if not records:
        return 0

    if dry_run:
        logger.debug("trade_event_logger: dry_run=True — skipping write (%d records)", len(records))
        return len(records)

    out_path = Path(history_path) if history_path else DEFAULT_PATH
    try:
        written = _append_jsonl(records, out_path)
        logger.info("trade_event_logger: appended %d events to %s", written, out_path)
        return written
    except Exception as exc:
        logger.warning("trade_event_logger: write failed (non-fatal) — %s", exc)
        return 0


# ---------------------------------------------------------------------------
# Reader helpers
# ---------------------------------------------------------------------------

def load_trade_events(
    path: Optional[Path] = None,
    *,
    action_filter: Optional[Set[str]] = None,
) -> list[dict[str, Any]]:
    """
    Load all trade events from JSONL into a list of dicts.

    Args:
        path:          Override default file path.
        action_filter: If provided, only return records whose "action" matches.

    Returns:
        List of event dicts, oldest first.  Empty list if file absent.
    """
    return list(iter_trade_events(path, action_filter=action_filter))


def iter_trade_events(
    path: Optional[Path] = None,
    *,
    action_filter: Optional[Set[str]] = None,
) -> Iterator[dict[str, Any]]:
    """
    Iterate over trade events from JSONL one dict at a time.

    Skips malformed lines with a warning rather than raising.
    """
    src = Path(path) if path else DEFAULT_PATH
    if not src.exists():
        return

    af = {a.upper() for a in action_filter} if action_filter else None
    with src.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning("trade_event_logger: malformed line %d in %s — %s", lineno, src, exc)
                continue
            if af and record.get("action", "").upper() not in af:
                continue
            yield record


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_event(
    action: dict[str, Any],
    *,
    run_id: str,
    run_mode: str,
    timestamp: str,
    portfolio_value: Optional[float],
    cash_available: Optional[float],
    drawdown_regime: str,
    degraded_mode: bool,
    degraded_reason: Optional[str],
) -> TradeEvent:
    pv = _safe_float(portfolio_value)
    ca = _safe_float(cash_available)
    return TradeEvent(
        run_id=run_id,
        timestamp=timestamp,
        run_mode=run_mode,
        portfolio_value=round(pv, 2) if pv is not None else None,
        cash_available=round(ca, 2) if ca is not None else None,
        drawdown_regime=str(drawdown_regime or "normal"),
        degraded_mode=bool(degraded_mode),
        degraded_reason=str(degraded_reason) if degraded_reason else None,
        symbol=str(action.get("symbol") or "UNKNOWN").upper().strip(),
        action=str(action.get("action") or "").upper().strip(),
        strategy_type=action.get("strategy_type") or None,
        score=_safe_float(action.get("score")),
        confidence=_safe_float(action.get("confidence")),
        suggested_allocation_pct=_safe_float(action.get("suggested_allocation_pct")),
        suggested_allocation_amount=_safe_float(action.get("suggested_allocation_amount")),
        rationale=list(action.get("rationale") or []),
        related_symbol=action.get("related_symbol") or None,
        exit_plan=action.get("exit_plan") or None,
    )


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _append_jsonl(records: list[dict[str, Any]], path: Path) -> int:
    """Atomic batch append — single write + fsync, no torn records."""
    if not records:
        return 0
    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd: Optional[int] = None
    try:
        fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY)
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
        return len(records)
    finally:
        if fd is not None:
            os.close(fd)
