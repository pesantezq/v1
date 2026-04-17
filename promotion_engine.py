"""
Promotion Engine
================
Selects the top slice of ranked opportunities for deeper downstream analysis.

The engine applies three sequential filters:
  1. Minimum score     — drop anything below min_score threshold
  2. Top-N by score    — keep at most top_n candidates
  3. Hard cap          — final ceiling of max_promoted

Label assignment
----------------
  "compounder"   — near 52-week high (BREAKOUT_PROXY event) with strong RS score
  "momentum"     — strong daily move or volume spike (STRONG_MOVE_UP / VOLUME_SPIKE)
  "watchlist"    — everything else that passed the score filters

Output is a list of PromotedCandidate objects ready for downstream deep
analysis or email digest inclusion.

Pure function — no I/O, no API calls.

Config key: ``promotion_engine``
  top_n         — keep top N by score (default: 20)
  min_score     — minimum composite score to promote (default: 35.0)
  max_promoted  — hard ceiling after all other filters (default: 30)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional

from opportunity_ranker import RankedOpportunity
from event_detection import EventType

logger = logging.getLogger("portfolio_automation.promotion_engine")

_MOMENTUM_EVENTS = frozenset({EventType.STRONG_MOVE_UP, EventType.VOLUME_SPIKE})
_COMPOUNDER_EVENTS = frozenset({EventType.BREAKOUT_PROXY})

# Minimum RS raw score (0–100) to qualify for "compounder" label
_COMPOUNDER_RS_MIN = 75.0


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PromotedCandidate:
    symbol: str
    rank: int               # rank from opportunity_ranker (lower = better)
    score: float            # total_score from opportunity_ranker
    label: str              # "compounder" | "momentum" | "watchlist"
    events: List[str]       # EventType string values
    reasons: List[str]      # factor explanation strings
    promoted_at: str        # ISO-8601 UTC timestamp
    theme_support: Optional[float] = None   # 0.0–1.0 propagated from RankedOpportunity
    portfolio_context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "rank": self.rank,
            "score": self.score,
            "label": self.label,
            "events": self.events,
            "reasons": self.reasons,
            "promoted_at": self.promoted_at,
            "theme_support": self.theme_support,
            "portfolio_context": self.portfolio_context,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def promote_candidates(
    ranked: List[RankedOpportunity],
    config: Optional[dict] = None,
) -> List[PromotedCandidate]:
    """
    Select the top slice of ranked opportunities.

    Args:
        ranked: Output from ``opportunity_ranker.rank_opportunities()``.
                Expected to already be sorted by total_score descending.
        config: ``promotion_engine`` config dict (optional).
                Keys: top_n, min_score, max_promoted, compounder_rs_min.

    Returns:
        List of PromotedCandidate, sorted by score descending.
        Returns empty list if no candidates pass the filters.
    """
    if not ranked:
        logger.info("PromotionEngine: no ranked candidates to promote")
        return []

    cfg = config or {}
    top_n = _config_int(cfg.get("top_n"), 15, minimum=0)
    min_score = _config_float(cfg.get("min_score"), 45.0, minimum=0.0)
    max_promoted = _config_int(cfg.get("max_promoted"), 30, minimum=0)
    compounder_rs_min = min(100.0, _config_float(cfg.get("compounder_rs_min"), _COMPOUNDER_RS_MIN, minimum=0.0))

    now_iso = datetime.now(timezone.utc).isoformat()

    # Step 1: minimum score filter
    filtered = [o for o in ranked if o.total_score >= min_score]
    # Step 2: top-N
    filtered = filtered[:top_n]
    # Step 3: hard cap
    filtered = filtered[:max_promoted]

    promoted: List[PromotedCandidate] = []
    for opp in filtered:
        label = _assign_label(opp, compounder_rs_min)
        promoted.append(PromotedCandidate(
            symbol=opp.symbol,
            rank=opp.rank,
            score=opp.total_score,
            label=label,
            events=list(opp.events),
            reasons=list(opp.reasons),
            promoted_at=now_iso,
            theme_support=opp.theme_support,
        ))

    logger.info(
        "PromotionEngine: %d/%d candidates promoted "
        "(min_score=%.1f, top_n=%d, max_promoted=%d)",
        len(promoted),
        len(ranked),
        min_score,
        top_n,
        max_promoted,
    )
    return promoted


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _assign_label(opp: RankedOpportunity, compounder_rs_min: float = _COMPOUNDER_RS_MIN) -> str:
    """
    Classify an opportunity as 'compounder', 'momentum', or 'watchlist'.

    Compounder: has a BREAKOUT_PROXY event AND a strong RS score (>= compounder_rs_min),
    indicating the stock is near its high with upward pressure — a potential
    long-term entry.

    Momentum: has a STRONG_MOVE_UP or VOLUME_SPIKE event but does not qualify
    as a compounder — typically a short-term tactical trade.

    Watchlist: passed the score bar but triggered no decisive events.
    """
    try:
        event_set = {
            EventType(e)
            for e in opp.events
            if e in EventType._value2member_map_  # type: ignore[attr-defined]
        }
    except Exception:
        event_set = set()

    rs_score = (
        opp.factor_breakdown.relative_strength
        if opp.factor_breakdown.relative_strength is not None
        else 0.0
    )

    has_breakout = bool(event_set & _COMPOUNDER_EVENTS)
    has_momentum = bool(event_set & _MOMENTUM_EVENTS)

    if has_breakout and rs_score >= compounder_rs_min:
        return "compounder"
    if has_momentum:
        return "momentum"
    return "watchlist"


def build_portfolio_review(
    promoted: List[PromotedCandidate],
    *,
    holdings: Optional[Iterable[Any]] = None,
    scanner_candidates: Optional[Iterable[dict[str, Any]]] = None,
    cash_available: Optional[float] = None,
) -> dict[str, Any]:
    """
    Build a portfolio-oriented review of promoted broad-market candidates.

    This keeps the shallow market-coverage pipeline advisory-only while
    translating the promoted list into portfolio decision context:
    confirmation of existing holdings, confirmation of the core scanner,
    or potential rotation candidates for review.
    """
    holding_symbols = _extract_holding_symbols(holdings or [])
    scanner_symbols = _extract_scanner_symbols(scanner_candidates or [])
    reviewed_candidates: list[dict[str, Any]] = []
    held_count = 0
    scanner_overlap_count = 0
    rotation_count = 0

    for candidate in promoted:
        symbol = str(candidate.symbol or "").upper()
        already_held = symbol in holding_symbols
        scanner_overlap = symbol in scanner_symbols

        if already_held:
            action_bucket = "existing_holding_confirmation"
            action_hint = (
                "Existing holding with fresh broad-market confirmation. "
                "Review add/hold conviction before looking elsewhere."
            )
            held_count += 1
        elif scanner_overlap:
            action_bucket = "scanner_confirmation"
            action_hint = (
                "Also surfaced in the core scanner. "
                "Prioritize this symbol for deeper diligence and capital deployment review."
            )
            scanner_overlap_count += 1
        else:
            action_bucket = "rotation_candidate"
            action_hint = (
                "New broad-market leader not already in the portfolio or core scanner. "
                "Review as a rotation candidate if cash is available or lower-conviction exposure needs replacing."
            )
            rotation_count += 1

        portfolio_context = {
            "already_held": already_held,
            "scanner_overlap": scanner_overlap,
            "action_bucket": action_bucket,
            "action_hint": action_hint,
            "cash_available": float(cash_available or 0.0),
        }
        candidate.portfolio_context = portfolio_context
        reviewed_candidates.append(
            {
                **candidate.to_dict(),
                "portfolio_action_bucket": action_bucket,
                "portfolio_action_hint": action_hint,
            }
        )

    if not reviewed_candidates:
        return {
            "available": False,
            "summary_line": "No promoted broad-market candidates met the review threshold.",
            "reviewed_candidates": [],
            "existing_holding_confirmations": 0,
            "scanner_confirmation_count": 0,
            "new_rotation_candidates": 0,
            "top_rotation_candidates": [],
        }

    summary_parts: list[str] = []
    if held_count:
        summary_parts.append(f"{held_count} holding confirmation")
    if scanner_overlap_count:
        summary_parts.append(f"{scanner_overlap_count} scanner-confirmed idea")
    if rotation_count:
        summary_parts.append(f"{rotation_count} new rotation candidate")
    summary_line = (
        "Portfolio review: " + ", ".join(summary_parts) + "."
        if summary_parts
        else "Portfolio review available."
    )

    top_rotation_candidates = [
        {
            "symbol": row["symbol"],
            "score": row["score"],
            "label": row["label"],
            "action_bucket": row["portfolio_action_bucket"],
            "action_hint": row["portfolio_action_hint"],
        }
        for row in reviewed_candidates
        if row["portfolio_action_bucket"] in {"scanner_confirmation", "rotation_candidate"}
    ][:3]

    return {
        "available": True,
        "summary_line": summary_line,
        "reviewed_candidates": reviewed_candidates,
        "existing_holding_confirmations": held_count,
        "scanner_confirmation_count": scanner_overlap_count,
        "new_rotation_candidates": rotation_count,
        "top_rotation_candidates": top_rotation_candidates,
    }


def _config_float(value: object, default: float, *, minimum: float | None = None) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = default
    if numeric != numeric or numeric == float("inf") or numeric == float("-inf"):
        numeric = default
    if minimum is not None:
        numeric = max(minimum, numeric)
    return numeric


def _config_int(value: object, default: int, *, minimum: int | None = None) -> int:
    try:
        numeric = int(float(value))
    except (TypeError, ValueError):
        numeric = default
    if minimum is not None:
        numeric = max(minimum, numeric)
    return numeric


def _extract_holding_symbols(holdings: Iterable[Any]) -> set[str]:
    symbols: set[str] = set()
    for holding in holdings:
        if isinstance(holding, dict):
            symbol = holding.get("symbol")
        else:
            symbol = getattr(holding, "symbol", None)
        normalized = str(symbol or "").strip().upper()
        if normalized:
            symbols.add(normalized)
    return symbols


def _extract_scanner_symbols(rows: Iterable[dict[str, Any]]) -> set[str]:
    symbols: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
        if normalized:
            symbols.add(normalized)
    return symbols
