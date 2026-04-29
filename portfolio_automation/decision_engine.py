"""
portfolio_automation/decision_engine.py

Central advisory decision layer for the Portfolio Automation System.
Unifies structural violations, portfolio adjustments, watchlist scanner signals,
market opportunities, and finance recommendations into a single ranked action plan.

ADVISORY ONLY — never executes trades. Output is an operator-readable plan.

Design:
  Each input source feeds a dedicated converter (decision_from_*) that produces a
  DecisionRecord dict. apply_decision_overrides then applies portfolio-level safety
  rules (degraded data, guardrail conflicts). build_decision_plan orchestrates all
  sources and delegates final ranking to rank_decisions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Closed-set constants
# ---------------------------------------------------------------------------

DECISION_BUY = "BUY"
DECISION_SELL = "SELL"
DECISION_SCALE = "SCALE"
DECISION_HOLD = "HOLD"
DECISION_WAIT = "WAIT"
DECISION_AVOID = "AVOID"

URGENCY_CRITICAL = "critical"
URGENCY_HIGH = "high"
URGENCY_MEDIUM = "medium"
URGENCY_LOW = "low"

SOURCE_STRUCTURAL = "structural"
SOURCE_PORTFOLIO = "portfolio"
SOURCE_WATCHLIST = "watchlist"
SOURCE_MARKET = "market"
SOURCE_FINANCE = "finance"

# ---------------------------------------------------------------------------
# Internal lookup tables
# ---------------------------------------------------------------------------

# Conviction band strength — higher is stronger.
_BAND_RANK: dict[str, int] = {
    "defer": 0,
    "observe": 1,
    "starter": 2,
    "normal": 3,
    "high_conviction": 4,
}

# Lowest band that may trigger an actionable decision.
_MIN_ACTIONABLE_BAND = "starter"

# Confidence floor below which a decision is capped at WAIT/HOLD.
_CONFIDENCE_FLOOR = 0.60

# Decision strength used for capping overrides.
_DECISION_RANK: dict[str, int] = {
    DECISION_AVOID: 0,
    DECISION_WAIT: 1,
    DECISION_HOLD: 2,
    DECISION_SCALE: 3,
    DECISION_BUY: 4,
    DECISION_SELL: 5,  # SELL is authoritative — never downgraded by overrides.
}
_RANK_TO_DECISION: dict[int, str] = {v: k for k, v in _DECISION_RANK.items()}

# Maximum priority score each source type may produce.
# Structural always outranks opportunity signals.
_SOURCE_PRIORITY_CEILING: dict[str, float] = {
    SOURCE_STRUCTURAL: 1.00,
    SOURCE_PORTFOLIO: 0.90,
    SOURCE_FINANCE: 0.80,
    SOURCE_MARKET: 0.65,
    SOURCE_WATCHLIST: 0.65,
}

# Hardcoded priority anchors for structural violation severity.
_VIOLATION_PRIORITY: dict[str, float] = {
    "leverage": 0.95,
    "concentration": 0.88,
    "drift": 0.76,
}

# Symbols that represent a portfolio-aggregate placeholder rather than a real ticker.
# Used to detect generic violations that need resolving to specific holdings.
_GENERIC_SYMBOLS: frozenset = frozenset({"PORTFOLIO", "UNKNOWN", ""})

# Finance action_level → urgency + priority band.
_FINANCE_URGENCY: dict[str, tuple[str, float]] = {
    "ACTION_REQUIRED": (URGENCY_CRITICAL, 0.78),
    "RECOMMENDED": (URGENCY_HIGH, 0.65),
    "MONITOR": (URGENCY_MEDIUM, 0.48),
    "FYI": (URGENCY_LOW, 0.28),
}

# Market opportunity_type → baseline priority.
_MARKET_OPP_PRIORITY: dict[str, float] = {
    "underweight_target": 0.62,
    "contribution_target": 0.58,
    "rebalance_target": 0.55,
}

# ---------------------------------------------------------------------------
# Defensive field-access helpers
# ---------------------------------------------------------------------------


def _safe_float(d: dict, key: str, default: float = 0.0) -> float:
    """Return float from dict, falling back to default on missing/None/invalid."""
    try:
        v = d.get(key)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _safe_str(d: dict, key: str, default: str = "") -> str:
    """Return str from dict, falling back to default."""
    v = d.get(key)
    return str(v).strip() if v is not None else default


def _safe_bool(d: dict, key: str, default: bool = False) -> bool:
    """Return bool from dict, falling back to default."""
    v = d.get(key)
    return bool(v) if v is not None else default


def _safe_list(d: dict, key: str) -> list:
    """Return list from dict, falling back to empty list."""
    v = d.get(key)
    return list(v) if isinstance(v, (list, tuple)) else []


def _conviction_rank(band: str) -> int:
    """Map a conviction band string to its numeric rank (0 = weakest)."""
    return _BAND_RANK.get(band, 0)


def _cap_decision(current: str, maximum: str) -> str:
    """Return the weaker of *current* and *maximum* decisions."""
    return _RANK_TO_DECISION[min(_DECISION_RANK[current], _DECISION_RANK[maximum])]


def _is_existing_holding(symbol: str, portfolio_context: dict) -> bool:
    """Return True if symbol is in the current portfolio holdings."""
    if not symbol:
        return False
    holdings = portfolio_context.get("current_holdings") or {}
    return symbol in holdings


def _compute_recommended_amount(
    suggested_pct: float,
    suggested_amount: float,
    portfolio_context: dict,
) -> Optional[float]:
    """Derive a dollar amount from pct × portfolio value or the explicit amount."""
    if suggested_amount:
        return round(suggested_amount, 2)
    total_value = _safe_float(portfolio_context, "total_portfolio_value")
    if suggested_pct and total_value:
        return round(suggested_pct * total_value, 2)
    return None


def _dedup_flags(flags: list[str]) -> list[str]:
    """Remove duplicates while preserving insertion order."""
    seen: set[str] = set()
    out: list[str] = []
    for f in flags:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


# ---------------------------------------------------------------------------
# Source converters
# ---------------------------------------------------------------------------


def decision_from_structural_violation(
    violation: dict,
    portfolio_context: Optional[dict] = None,
) -> dict:
    """
    Convert a guardrail structural violation into a decision record.

    Expected violation fields (all optional — missing fields are handled defensively):
      symbol         str
      violation_type str  — "leverage" | "concentration" | "drift"
      current_pct    float
      cap_pct        float
      required_action str

    Structural violations always produce SELL decisions and rank above all
    opportunity signals. Leverage breaches are critical; concentration breaches
    are high urgency.
    """
    portfolio_context = portfolio_context or {}

    symbol = _safe_str(violation, "symbol", "UNKNOWN")
    vtype = _safe_str(violation, "violation_type", "concentration")
    current_pct = _safe_float(violation, "current_pct")
    cap_pct = _safe_float(violation, "cap_pct")
    required_action = _safe_str(violation, "required_action", "trim")

    urgency = URGENCY_CRITICAL if vtype == "leverage" else URGENCY_HIGH
    priority = _VIOLATION_PRIORITY.get(vtype, 0.80)

    excess_pct = round(current_pct - cap_pct, 4) if current_pct and cap_pct else None
    excess_str = f" ({excess_pct:+.1%} over cap)" if excess_pct else ""

    reason = (
        f"Structural {vtype} violation on {symbol}: {required_action} required"
        f"{excess_str}. Cap={cap_pct:.0%}, current={current_pct:.0%}."
        if current_pct and cap_pct
        else f"Structural {vtype} violation on {symbol}: {required_action} required."
    )

    recommended_action = (
        f"Reduce {symbol} position to restore {vtype} compliance."
    )

    return {
        "symbol": symbol,
        "decision": DECISION_SELL,
        "priority": priority,
        "urgency": urgency,
        "source": SOURCE_STRUCTURAL,
        "recommended_action": recommended_action,
        "recommended_amount": None,
        "recommended_allocation_pct": cap_pct or None,
        "reason": reason,
        "risk_flags": [f"{vtype}_breach"],
        "confidence": 1.0,
        "inputs_used": {
            "violation_type": vtype,
            "current_pct": current_pct,
            "cap_pct": cap_pct,
            "required_action": required_action,
        },
    }


def decision_from_portfolio_adjustment(
    adjustment: dict,
    portfolio_context: Optional[dict] = None,
) -> dict:
    """
    Convert a PortfolioAdjustment (or serialised dict) into a decision record.

    Expected fields (all optional):
      symbol               str
      recommendation_type  str   — "sell" | "buy" | "rebalance" | "hold"
      action_level         str   — "ACTION_REQUIRED" | "RECOMMENDED" | "MONITOR" | "FYI"
      is_leveraged         bool
      amount               float
      drift                float
      title                str
      do                   str   — operator action string
      why                  str
    """
    portfolio_context = portfolio_context or {}

    portfolio_context = portfolio_context or {}

    symbol = _safe_str(adjustment, "symbol", "PORTFOLIO")
    adj_mode = _safe_str(adjustment, "adjustment_mode", "NO_ACTION").upper()
    action_level = _safe_str(adjustment, "action_level", "MONITOR")
    is_leveraged = _safe_bool(adjustment, "is_leveraged")
    amount = _safe_float(adjustment, "amount")
    drift = _safe_float(adjustment, "drift")
    title = _safe_str(adjustment, "title", f"Portfolio adjustment — {symbol}")
    do_str = _safe_str(adjustment, "do", "Review and act if warranted.")
    why_str = _safe_str(adjustment, "why", "")

    is_existing = _is_existing_holding(symbol, portfolio_context)

    # Decision mapping driven by adjustment_mode (the execution intent):
    #   CONTRIBUTE_ONLY / USE_CASH_EXCESS → capital deployment (BUY or SCALE)
    #   SELL_TO_REBALANCE / TRIM_LEVERAGE_FIRST → reduce position
    #   NO_ACTION / unknown → hold current position
    if adj_mode in ("SELL_TO_REBALANCE", "TRIM_LEVERAGE_FIRST"):
        decision = DECISION_SELL
    elif adj_mode in ("CONTRIBUTE_ONLY", "USE_CASH_EXCESS"):
        decision = DECISION_SCALE if is_existing else DECISION_BUY
    else:
        decision = DECISION_HOLD

    # Urgency
    if is_leveraged and decision == DECISION_SELL:
        urgency = URGENCY_CRITICAL
    elif action_level == "ACTION_REQUIRED":
        urgency = URGENCY_HIGH
    elif action_level == "RECOMMENDED":
        urgency = URGENCY_MEDIUM
    else:
        urgency = URGENCY_LOW

    # Priority: leverage gets a premium; otherwise scaled by action_level.
    if is_leveraged and decision == DECISION_SELL:
        priority = 0.87
    elif action_level == "ACTION_REQUIRED":
        priority = 0.80
    elif action_level == "RECOMMENDED":
        priority = 0.70
    elif action_level == "MONITOR":
        priority = 0.55
    else:
        priority = 0.38

    drift_str = f" (drift {drift:+.1%})" if drift else ""
    reason = f"{title}{drift_str}. {why_str}".strip().rstrip(".") + "."

    return {
        "symbol": symbol,
        "decision": decision,
        "priority": priority,
        "urgency": urgency,
        "source": SOURCE_PORTFOLIO,
        "recommended_action": do_str,
        "recommended_amount": amount or None,
        "recommended_allocation_pct": None,
        "reason": reason,
        "risk_flags": (["leveraged_exposure"] if is_leveraged else []),
        "confidence": 0.90,
        "inputs_used": {
            "adjustment_mode": adj_mode,
            "action_level": action_level,
            "is_leveraged": is_leveraged,
            "drift": drift,
            "is_existing_holding": is_existing,
        },
    }


def decision_from_watchlist_signal(
    signal: dict,
    portfolio_context: Optional[dict] = None,
) -> dict:
    """
    Convert a watchlist scanner alert into a decision record.

    Expected fields (all optional — conviction/allocation fields may be absent):
      ticker / symbol          str
      conviction_band          str
      conviction_score         float  0–1
      confidence_score         float  0–1
      signal_score             float  0–1
      effective_score          float  0–1
      sizing_multiplier        float  0–1
      suggested_allocation     float  decimal pct
      suggested_amount         float  dollar amount
      cooldown_active          bool
      alert_priority           str    — "high"|"normal"|"watch"|None
      data_mode                str    — "live"|"fallback"

    Decision depends on conviction band, confidence, and whether the symbol is
    already held. New positions use BUY/WAIT/AVOID; existing use SCALE/HOLD.
    """
    portfolio_context = portfolio_context or {}

    symbol = _safe_str(signal, "ticker") or _safe_str(signal, "symbol", "UNKNOWN")
    conviction_band = _safe_str(signal, "conviction_band", "defer")
    conviction_score = _safe_float(signal, "conviction_score", 0.0)
    signal_score = _safe_float(signal, "signal_score", 0.0)
    confidence_score = _safe_float(signal, "confidence_score", 0.0)
    effective_score = _safe_float(signal, "effective_score", signal_score)
    sizing_multiplier = _safe_float(signal, "sizing_multiplier", 0.0)
    suggested_pct = (
        _safe_float(signal, "suggested_allocation")
        or _safe_float(signal, "suggested_pct")
    )
    suggested_amount = _safe_float(signal, "suggested_amount")
    cooldown_active = _safe_bool(signal, "cooldown_active")
    data_mode = _safe_str(signal, "data_mode", "live")

    is_existing = _is_existing_holding(symbol, portfolio_context)
    band_rank = _conviction_rank(conviction_band)
    min_rank = _conviction_rank(_MIN_ACTIONABLE_BAND)

    risk_flags: list[str] = []

    # --- Base decision ---
    if band_rank < min_rank:
        # Below starter threshold: no capital action.
        decision = DECISION_AVOID
        risk_flags.append("weak_conviction")
        urgency = URGENCY_LOW

    elif confidence_score < _CONFIDENCE_FLOOR:
        decision = DECISION_HOLD if is_existing else DECISION_WAIT
        risk_flags.append("low_confidence")
        urgency = URGENCY_LOW

    elif cooldown_active:
        decision = DECISION_HOLD if is_existing else DECISION_WAIT
        risk_flags.append("cooldown_active")
        urgency = URGENCY_LOW

    elif band_rank >= _conviction_rank("high_conviction"):
        decision = DECISION_SCALE if is_existing else DECISION_BUY
        urgency = URGENCY_HIGH

    elif band_rank >= _conviction_rank("normal"):
        if signal_score >= 0.65 or effective_score >= 0.65:
            decision = DECISION_SCALE if is_existing else DECISION_BUY
            urgency = URGENCY_MEDIUM
        else:
            decision = DECISION_HOLD if is_existing else DECISION_WAIT
            urgency = URGENCY_LOW

    else:
        # starter band — conservative entry only
        decision = DECISION_WAIT
        urgency = URGENCY_LOW

    if data_mode == "fallback":
        risk_flags.append("fallback_data")

    # --- Priority ---
    raw = (
        conviction_score * 0.45
        + signal_score * 0.35
        + confidence_score * 0.20
    )
    priority = round(raw * _SOURCE_PRIORITY_CEILING[SOURCE_WATCHLIST], 4)

    # --- Reason ---
    parts = [
        f"Band={conviction_band.replace('_', ' ')}, "
        f"conviction={conviction_score:.2f}, signal={signal_score:.2f}, "
        f"confidence={confidence_score:.2f}.",
    ]
    if is_existing:
        parts.append("Existing holding.")
    if risk_flags:
        parts.append(f"Flags: {', '.join(risk_flags)}.")
    reason = " ".join(parts)

    recommended_amount = _compute_recommended_amount(
        suggested_pct * sizing_multiplier if sizing_multiplier else suggested_pct,
        suggested_amount,
        portfolio_context,
    )

    return {
        "symbol": symbol,
        "decision": decision,
        "priority": priority,
        "urgency": urgency,
        "source": SOURCE_WATCHLIST,
        "recommended_action": (
            f"{'Add to' if is_existing else 'Open'} {symbol} position."
            if decision in (DECISION_BUY, DECISION_SCALE)
            else f"Stand by on {symbol} — {decision.lower()}."
        ),
        "recommended_amount": recommended_amount,
        "recommended_allocation_pct": suggested_pct or None,
        "reason": reason,
        "risk_flags": risk_flags,
        "confidence": round(min(conviction_score, confidence_score), 4),
        "inputs_used": {
            "conviction_band": conviction_band,
            "conviction_score": conviction_score,
            "signal_score": signal_score,
            "confidence_score": confidence_score,
            "effective_score": effective_score,
            "sizing_multiplier": sizing_multiplier,
            "cooldown_active": cooldown_active,
            "data_mode": data_mode,
            "is_existing_holding": is_existing,
        },
    }


def decision_from_market_opportunity(
    opportunity: dict,
    portfolio_context: Optional[dict] = None,
) -> dict:
    """
    Convert a market opportunity (underweight target, contribution deployment,
    rebalance target) into a decision record.

    Expected fields (all optional):
      symbol           str
      opportunity_type str  — "underweight_target"|"contribution_target"|"rebalance_target"
      suggested_pct    float
      suggested_amount float
      reason           str
      urgency          str  — optional override
    """
    portfolio_context = portfolio_context or {}

    symbol = _safe_str(opportunity, "symbol", "UNKNOWN")
    opp_type = _safe_str(opportunity, "opportunity_type", "rebalance_target")
    suggested_pct = _safe_float(opportunity, "suggested_pct")
    suggested_amount = _safe_float(opportunity, "suggested_amount")
    reason_input = _safe_str(opportunity, "reason", f"{opp_type} opportunity.")
    urgency_override = _safe_str(opportunity, "urgency")

    is_existing = _is_existing_holding(symbol, portfolio_context)

    decision = DECISION_SCALE if is_existing else DECISION_BUY
    priority = _MARKET_OPP_PRIORITY.get(opp_type, 0.50)
    urgency = urgency_override or (
        URGENCY_HIGH if opp_type == "underweight_target" else URGENCY_MEDIUM
    )

    recommended_amount = _compute_recommended_amount(
        suggested_pct, suggested_amount, portfolio_context
    )

    return {
        "symbol": symbol,
        "decision": decision,
        "priority": priority,
        "urgency": urgency,
        "source": SOURCE_MARKET,
        "recommended_action": (
            f"Deploy capital into {symbol} — {opp_type.replace('_', ' ')}."
        ),
        "recommended_amount": recommended_amount,
        "recommended_allocation_pct": suggested_pct or None,
        "reason": reason_input,
        "risk_flags": [],
        "confidence": 0.80,
        "inputs_used": {
            "opportunity_type": opp_type,
            "suggested_pct": suggested_pct,
            "suggested_amount": suggested_amount,
            "is_existing_holding": is_existing,
        },
    }


def decision_from_finance_recommendation(
    recommendation: dict,
    portfolio_context: Optional[dict] = None,
) -> dict:
    """
    Convert a scored finance recommendation into a decision record.

    FinanceRecommendation fields used (all optional):
      id           str
      title        str
      action       str
      action_level str  — "ACTION_REQUIRED"|"RECOMMENDED"|"MONITOR"|"FYI"
      impact_area  str
      trigger      str
    """
    portfolio_context = portfolio_context or {}

    # Finance recs are not always symbol-linked; use id as identifier.
    rec_id = _safe_str(recommendation, "id", "FINANCE")
    symbol = _safe_str(recommendation, "symbol") or rec_id
    title = _safe_str(recommendation, "title", "Finance recommendation")
    action = _safe_str(recommendation, "action", "Review.")
    action_level = _safe_str(recommendation, "action_level", "MONITOR")
    impact_area = _safe_str(recommendation, "impact_area", "")
    trigger = _safe_str(recommendation, "trigger", "")

    urgency, priority = _FINANCE_URGENCY.get(
        action_level, (URGENCY_LOW, 0.28)
    )

    # Finance recs map to HOLD by default; ACTION_REQUIRED with specific action
    # text may imply a buy or sell — best surfaced as the action string.
    decision = (
        DECISION_SELL
        if "sell" in action.lower() or "reduce" in action.lower()
        else DECISION_BUY
        if "buy" in action.lower() or "add" in action.lower()
        else DECISION_HOLD
    )

    reason_parts = [f"{title}."]
    if trigger:
        reason_parts.append(f"Trigger: {trigger}.")
    if impact_area:
        reason_parts.append(f"Impact: {impact_area}.")

    return {
        "symbol": symbol,
        "decision": decision,
        "priority": priority,
        "urgency": urgency,
        "source": SOURCE_FINANCE,
        "recommended_action": action,
        "recommended_amount": None,
        "recommended_allocation_pct": None,
        "reason": " ".join(reason_parts),
        "risk_flags": [],
        "confidence": 0.75,
        "inputs_used": {
            "action_level": action_level,
            "impact_area": impact_area,
            "trigger": trigger,
        },
    }


# ---------------------------------------------------------------------------
# Override layer
# ---------------------------------------------------------------------------


def apply_decision_overrides(
    decision_record: dict,
    portfolio_context: Optional[dict] = None,
) -> dict:
    """
    Apply portfolio-level safety overrides to an already-built decision record.

    Rules (applied in priority order):
      1. degraded_mode or data_mode=fallback → cap BUY/SCALE at WAIT.
      2. drawdown regime "bear" or "severe" → cap BUY at HOLD for non-structural sources.
      3. Guardrail conflict (symbol in structural violations) → cap BUY/SCALE at HOLD.

    SELL decisions are never downgraded — structural authority is preserved.
    Returns a shallow copy with modified fields; does not mutate the input.
    """
    portfolio_context = portfolio_context or {}

    record = dict(decision_record)
    record["risk_flags"] = list(decision_record.get("risk_flags") or [])

    current = record["decision"]
    source = record.get("source", "")

    # SELL is authoritative — skip all downgrade rules.
    if current == DECISION_SELL:
        return record

    # Rule 1: Degraded data
    degraded = (
        _safe_bool(portfolio_context, "degraded_mode")
        or _safe_str(portfolio_context, "data_mode") == "fallback"
    )
    if degraded and current in (DECISION_BUY, DECISION_SCALE):
        record["decision"] = DECISION_WAIT
        if "degraded_data" not in record["risk_flags"]:
            record["risk_flags"].append("degraded_data")

    # Rule 2: Drawdown regime caps non-structural BUY actions.
    drawdown_regime = _safe_str(portfolio_context, "drawdown_regime")
    if drawdown_regime in ("bear", "severe") and source != SOURCE_STRUCTURAL:
        if record["decision"] in (DECISION_BUY, DECISION_SCALE):
            record["decision"] = DECISION_HOLD
            if "drawdown_regime" not in record["risk_flags"]:
                record["risk_flags"].append("drawdown_regime")

    # Rule 3: Symbol appears in active structural violations.
    active_violations = portfolio_context.get("active_structural_violations") or []
    violation_symbols = {
        _safe_str(v, "symbol") for v in active_violations if isinstance(v, dict)
    }
    if record["symbol"] in violation_symbols and record["decision"] in (
        DECISION_BUY,
        DECISION_SCALE,
    ):
        record["decision"] = DECISION_HOLD
        if "guardrail_conflict" not in record["risk_flags"]:
            record["risk_flags"].append("guardrail_conflict")

    record["risk_flags"] = _dedup_flags(record["risk_flags"])
    return record


# ---------------------------------------------------------------------------
# Violation resolution helpers
# ---------------------------------------------------------------------------


def _resolve_leverage_violations(
    violations: list,
    portfolio_adjustments: list,
) -> list:
    """
    Expand generic leverage violations (symbol='PORTFOLIO') to per-holding decisions.

    guardrails.py reports leverage as an aggregate check with symbol='PORTFOLIO'.
    When leveraged holding symbols are available from portfolio_adjustments, substitute
    them so each leveraged holding gets its own SELL decision.

    Non-leverage violations pass through unchanged. Falls back to the generic
    'PORTFOLIO' symbol when no leveraged holdings can be identified.
    """
    leveraged_symbols: list = [
        _safe_str(a, "symbol")
        for a in portfolio_adjustments
        if isinstance(a, dict)
        and _safe_bool(a, "is_leveraged")
        and _safe_str(a, "symbol") not in _GENERIC_SYMBOLS
    ]

    result: list = []
    for v in violations:
        if not isinstance(v, dict):
            continue
        if _safe_str(v, "violation_type") != "leverage":
            result.append(v)
            continue
        symbol = _safe_str(v, "symbol")
        if symbol not in _GENERIC_SYMBOLS:
            # Already has a specific ticker — use as-is.
            result.append(v)
            continue
        if not leveraged_symbols:
            # No leveraged holdings identifiable — keep generic placeholder.
            result.append(v)
            continue
        for lev_sym in leveraged_symbols:
            result.append({**v, "symbol": lev_sym})
    return result


def _suppress_structural_hold_conflicts(decisions: list) -> list:
    """
    Remove portfolio HOLD decisions that contradict an active structural SELL.

    A portfolio HOLD and a structural SELL on the same specific symbol are
    contradictory — the structural SELL is authoritative. Generic symbols
    (PORTFOLIO, UNKNOWN) are excluded to avoid over-suppression.
    """
    structural_sell_symbols: set = {
        d["symbol"]
        for d in decisions
        if d.get("source") == SOURCE_STRUCTURAL
        and d.get("decision") == DECISION_SELL
        and d.get("symbol") not in _GENERIC_SYMBOLS
    }
    if not structural_sell_symbols:
        return decisions
    return [
        d for d in decisions
        if not (
            d.get("source") == SOURCE_PORTFOLIO
            and d.get("decision") == DECISION_HOLD
            and d.get("symbol") in structural_sell_symbols
        )
    ]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def build_decision_plan(
    structural_violations: Optional[list[dict]] = None,
    portfolio_adjustments: Optional[list[dict]] = None,
    watchlist_signals: Optional[list[dict]] = None,
    market_opportunities: Optional[list[dict]] = None,
    finance_recommendations: Optional[list[dict]] = None,
    portfolio_context: Optional[dict] = None,
) -> list[dict]:
    """
    Unify all input sources into a ranked list of decision records.

    Structural violations and portfolio adjustments are treated as authoritative
    and are not subject to portfolio-level overrides (their decisions originate
    from already-evaluated guardrails). Watchlist, market, and finance decisions
    are passed through apply_decision_overrides before ranking.

    Returns a list of DecisionRecord dicts sorted by priority descending.
    """
    portfolio_context = portfolio_context or {}

    # Expand generic leverage violations (symbol='PORTFOLIO') to specific holdings.
    resolved_violations = _resolve_leverage_violations(
        structural_violations or [],
        portfolio_adjustments or [],
    )

    all_decisions: list[dict] = []

    # --- Authoritative sources (no overrides applied) ---
    for v in resolved_violations:
        if isinstance(v, dict):
            all_decisions.append(
                decision_from_structural_violation(v, portfolio_context)
            )

    for adj in portfolio_adjustments or []:
        if isinstance(adj, dict):
            all_decisions.append(
                decision_from_portfolio_adjustment(adj, portfolio_context)
            )

    # --- Opportunity sources (overrides applied) ---
    for sig in watchlist_signals or []:
        if isinstance(sig, dict):
            d = decision_from_watchlist_signal(sig, portfolio_context)
            d = apply_decision_overrides(d, portfolio_context)
            all_decisions.append(d)

    for opp in market_opportunities or []:
        if isinstance(opp, dict):
            d = decision_from_market_opportunity(opp, portfolio_context)
            d = apply_decision_overrides(d, portfolio_context)
            all_decisions.append(d)

    for rec in finance_recommendations or []:
        if isinstance(rec, dict):
            d = decision_from_finance_recommendation(rec, portfolio_context)
            d = apply_decision_overrides(d, portfolio_context)
            all_decisions.append(d)

    # Drop portfolio HOLDs that contradict an active structural SELL on the same symbol.
    all_decisions = _suppress_structural_hold_conflicts(all_decisions)

    return rank_decisions(all_decisions)


def rank_decisions(decisions: list[dict]) -> list[dict]:
    """
    Sort decisions by priority descending.

    Secondary sort key places SELL > BUY/SCALE > HOLD > WAIT > AVOID when
    priorities are tied, and AVOID decisions always trail regardless of score.
    """
    def _sort_key(r: dict) -> tuple:
        decision_order = _DECISION_RANK.get(r.get("decision", DECISION_HOLD), 2)
        is_avoid = 1 if r.get("decision") == DECISION_AVOID else 0
        return (
            -is_avoid,               # non-avoid first
            r.get("priority", 0.0),  # higher priority first (will be negated)
            decision_order,           # tiebreak by decision strength
        )

    return sorted(
        decisions,
        key=lambda r: (
            0 if r.get("decision") != DECISION_AVOID else 1,
            -(r.get("priority") or 0.0),
            -(_DECISION_RANK.get(r.get("decision", DECISION_HOLD), 2)),
        ),
    )


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def summarize_decision_plan(
    decisions: list[dict],
    portfolio_context: Optional[dict] = None,
) -> str:
    """
    Produce a concise operator-readable summary of the ranked decision plan.

    Includes:
      - Header with timestamp and portfolio context highlights.
      - Count breakdown by decision type and urgency.
      - Top-5 priority actions with symbol, decision, urgency, and reason snippet.
      - Total recommended capital deployment.
      - Active risk flags across the plan.
    """
    portfolio_context = portfolio_context or {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    if not decisions:
        return f"[{now}] DECISION PLAN: No decisions generated."

    lines: list[str] = []
    lines.append("=" * 68)
    lines.append(f"DECISION PLAN SUMMARY  —  {now}")
    lines.append("=" * 68)

    # Portfolio context header
    total_value = portfolio_context.get("total_portfolio_value")
    cash = portfolio_context.get("cash")
    if total_value:
        lines.append(f"Portfolio value : ${total_value:,.0f}")
    if cash is not None:
        lines.append(f"Available cash  : ${cash:,.0f}")
    degraded = (
        _safe_bool(portfolio_context, "degraded_mode")
        or _safe_str(portfolio_context, "data_mode") == "fallback"
    )
    if degraded:
        lines.append("Data quality    : DEGRADED — decisions downgraded.")
    lines.append("")

    # Decision type counts
    from collections import Counter
    decision_counts = Counter(d.get("decision") for d in decisions)
    urgency_counts = Counter(d.get("urgency") for d in decisions)

    lines.append("Decisions by type:")
    for label in (
        DECISION_SELL, DECISION_BUY, DECISION_SCALE,
        DECISION_HOLD, DECISION_WAIT, DECISION_AVOID
    ):
        count = decision_counts.get(label, 0)
        if count:
            lines.append(f"  {label:<8} {count}")

    lines.append("")
    lines.append("Decisions by urgency:")
    for label in (URGENCY_CRITICAL, URGENCY_HIGH, URGENCY_MEDIUM, URGENCY_LOW):
        count = urgency_counts.get(label, 0)
        if count:
            lines.append(f"  {label:<10} {count}")

    lines.append("")

    # Top actions
    actionable = [
        d for d in decisions
        if d.get("decision") in (DECISION_SELL, DECISION_BUY, DECISION_SCALE)
    ]
    top = decisions[:5]
    lines.append(f"Top {len(top)} priority actions:")
    for i, d in enumerate(top, 1):
        sym = d.get("symbol", "?")
        dec = d.get("decision", "?")
        urg = d.get("urgency", "?")
        pri = d.get("priority", 0.0)
        src = d.get("source", "?")
        reason = d.get("reason", "")[:80]
        amt = d.get("recommended_amount")
        amt_str = f"  ${amt:,.0f}" if amt else ""
        lines.append(
            f"  {i}. [{urg.upper():<8}] {sym:<8} {dec:<6} "
            f"pri={pri:.3f} src={src}{amt_str}"
        )
        lines.append(f"     {reason}")

    lines.append("")

    # Capital summary
    total_deploy = sum(
        d.get("recommended_amount") or 0.0
        for d in actionable
        if d.get("decision") in (DECISION_BUY, DECISION_SCALE)
    )
    if total_deploy:
        lines.append(f"Total capital recommended: ${total_deploy:,.0f}")

    # Risk flags summary
    all_flags: list[str] = []
    for d in decisions:
        all_flags.extend(d.get("risk_flags") or [])
    flag_counts = Counter(all_flags)
    if flag_counts:
        lines.append("")
        lines.append("Active risk flags:")
        for flag, count in flag_counts.most_common():
            lines.append(f"  {flag} ({count}x)")

    lines.append("=" * 68)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    _ctx = {
        "total_portfolio_value": 50_000,
        "cash": 5_000,
        "current_holdings": {
            "MSFT": {"value": 4_000, "pct": 0.08},
            "TQQQ": {"value": 3_000, "pct": 0.06},
        },
        "drawdown_regime": "neutral",
        "degraded_mode": False,
        "data_mode": "live",
        "active_structural_violations": [],
    }

    _violations = [
        {
            "symbol": "TQQQ",
            "violation_type": "leverage",
            "current_pct": 0.18,
            "cap_pct": 0.15,
            "required_action": "trim",
        },
        {
            "symbol": "QQQ",
            "violation_type": "concentration",
            "current_pct": 0.43,
            "cap_pct": 0.40,
            "required_action": "trim",
        },
    ]

    _watchlist = [
        {
            "ticker": "NVDA",
            "conviction_band": "high_conviction",
            "conviction_score": 0.88,
            "signal_score": 0.82,
            "confidence_score": 0.91,
            "effective_score": 0.85,
            "sizing_multiplier": 1.0,
            "suggested_allocation": 0.04,
            "suggested_amount": 2_000,
        },
        {
            "ticker": "IONQ",
            "conviction_band": "observe",
            "conviction_score": 0.22,
            "signal_score": 0.55,
            "confidence_score": 0.70,
        },
        {
            "ticker": "MSFT",
            "conviction_band": "normal",
            "conviction_score": 0.75,
            "signal_score": 0.70,
            "confidence_score": 0.85,
            "suggested_amount": 1_000,
        },
    ]

    _market = [
        {
            "symbol": "VFH",
            "opportunity_type": "underweight_target",
            "suggested_pct": 0.03,
            "suggested_amount": 1_500,
            "reason": "Financial sector underweight vs target by 3%.",
        },
    ]

    _finance = [
        {
            "id": "PORTFOLIO_DRIFT",
            "title": "Portfolio drift approaching rebalance threshold",
            "action": "Review sector weights and rebalance if drift exceeds band.",
            "action_level": "RECOMMENDED",
            "impact_area": "PORTFOLIO_RISK",
            "trigger": "Equity drift +8% above target over 30 days.",
        },
    ]

    plan = build_decision_plan(
        structural_violations=_violations,
        watchlist_signals=_watchlist,
        market_opportunities=_market,
        finance_recommendations=_finance,
        portfolio_context=_ctx,
    )

    print(summarize_decision_plan(plan, _ctx))

    print("\nFull decision records:")
    for rec in plan:
        print(
            f"  {rec['symbol']:<10} {rec['decision']:<6} "
            f"pri={rec['priority']:.3f} urg={rec['urgency']:<8} "
            f"src={rec['source']:<12} flags={rec['risk_flags']}"
        )
