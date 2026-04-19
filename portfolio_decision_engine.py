"""
Portfolio-focused decision layer for broader-market opportunities.

Consumes current holdings plus promoted broader-market opportunities and
emits advisory actions that help improve portfolio profit potential.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from allocation_engine import suggest_allocation
from decision_support import (
    as_finite_float,
    normalize_confidence,
    normalize_score,
    normalize_strategy_type,
    normalize_symbol,
    read_value,
)
from exit_engine import evaluate_exit
from strategy_router import route_opportunity


@dataclass
class PortfolioAction:
    action: str
    symbol: str
    strategy_type: str | None = None
    score: float | None = None
    confidence: float | None = None
    rationale: list[str] = field(default_factory=list)
    related_symbol: str | None = None
    suggested_allocation_pct: float | None = None
    suggested_allocation_amount: float | None = None
    exit_plan: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "symbol": self.symbol,
            "strategy_type": self.strategy_type,
            "score": self.score,
            "confidence": self.confidence,
            "rationale": list(self.rationale),
            "related_symbol": self.related_symbol,
            "suggested_allocation_pct": self.suggested_allocation_pct,
            "suggested_allocation_amount": self.suggested_allocation_amount,
            "exit_plan": dict(self.exit_plan or {}),
        }


def generate_portfolio_actions(
    *,
    current_holdings: list[Any],
    opportunities: list[Any],
    portfolio_value: float,
    cash_available: float,
    context: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = context or {}
    cfg = {
        "min_buy_confidence": 0.65,
        "min_degraded_buy_confidence": 0.75,
        "promote_score_threshold": 72.0,
        "watchlist_score_threshold": 55.0,
        "buy_starter_multiplier": 0.70,
    }
    cfg.update(config or {})
    allocation_cfg = cfg.get("allocation_engine", {}) if isinstance(cfg.get("allocation_engine", {}), dict) else {}
    exit_cfg = cfg.get("exit_engine", {}) if isinstance(cfg.get("exit_engine", {}), dict) else {}

    if not opportunities:
        return {
            "available": False,
            "summary_line": "No broader-market opportunities are available for portfolio action.",
            "actions": [],
        }

    holdings_by_symbol = {_holding_symbol(holding): holding for holding in current_holdings if _holding_symbol(holding)}
    sector_exposures = _sector_exposures(current_holdings, portfolio_value)
    strongest_external = None
    strongest_external_score = None

    routed = []
    for opportunity in opportunities:
        route = route_opportunity(opportunity)
        score = _opportunity_score(opportunity)
        confidence = _opportunity_confidence(opportunity)
        item = {
            "opportunity": opportunity,
            "route": route,
            "score": score,
            "confidence": confidence,
        }
        routed.append(item)
        symbol = route.symbol
        if symbol not in holdings_by_symbol:
            if strongest_external_score is None or score > strongest_external_score:
                strongest_external = item
                strongest_external_score = score

    actions: list[PortfolioAction] = []

    for item in routed:
        opportunity = item["opportunity"]
        route = item["route"]
        symbol = route.symbol
        score = item["score"]
        confidence = item["confidence"]
        existing_holding = holdings_by_symbol.get(symbol)

        if existing_holding is not None:
            exit_suggestion = evaluate_exit(
                existing_holding,
                strategy_type=route.strategy_type,
                stronger_opportunity=strongest_external["opportunity"] if strongest_external else None,
                context=context,
                config=exit_cfg,
            )
            if exit_suggestion.action in {"SELL", "TRIM"}:
                actions.append(
                    PortfolioAction(
                        action=exit_suggestion.action,
                        symbol=symbol,
                        strategy_type=route.strategy_type,
                        score=round(score, 1),
                        confidence=round(confidence, 3),
                        rationale=list(exit_suggestion.reasons),
                        related_symbol=_read(strongest_external["opportunity"], "symbol") if strongest_external else None,
                        exit_plan=exit_suggestion.to_dict(),
                    )
                )
            else:
                allocation = suggest_allocation(
                    opportunity=opportunity,
                    strategy_type=route.strategy_type,
                    portfolio_value=portfolio_value,
                    cash_available=cash_available,
                    current_sector_exposure=sector_exposures.get(_holding_sector(existing_holding), 0.0),
                    context=context,
                    config=allocation_cfg,
                )
                action = (
                    "BUY"
                    if allocation.suggested_amount > 0
                    and confidence >= _config_float(cfg, "min_buy_confidence", 0.65)
                    else "HOLD"
                )
                rationale = list(route.rationale)
                rationale.extend(allocation.rationale)
                if action == "HOLD":
                    rationale.append("existing holding remains acceptable versus the current opportunity set")
                actions.append(
                    PortfolioAction(
                        action=action,
                        symbol=symbol,
                        strategy_type=route.strategy_type,
                        score=round(score, 1),
                        confidence=round(confidence, 3),
                        rationale=rationale,
                        suggested_allocation_pct=allocation.suggested_pct if action == "BUY" else None,
                        suggested_allocation_amount=allocation.suggested_amount if action == "BUY" else None,
                        exit_plan=exit_suggestion.to_dict(),
                    )
                )
            continue

        allocation = suggest_allocation(
            opportunity=opportunity,
            strategy_type=route.strategy_type,
            portfolio_value=portfolio_value,
            cash_available=cash_available,
            current_sector_exposure=sector_exposures.get(_holding_sector(opportunity), 0.0),
            context=context,
            config=allocation_cfg,
        )
        rationale = list(route.rationale)
        rationale.extend(allocation.rationale)

        min_buy_confidence = (
            _config_float(cfg, "min_degraded_buy_confidence", 0.75)
            if bool(context.get("degraded_mode"))
            else _config_float(cfg, "min_buy_confidence", 0.65)
        )

        if allocation.deployable_cash <= 0:
            action = "ADD_TO_WATCHLIST"
            rationale.append("no capital is available after keeping the cash reserve intact")
            final_alloc_pct = None
            final_alloc_amt = None
        elif allocation.suggested_amount <= 0:
            action = "ADD_TO_WATCHLIST"
            rationale.append("sizing constraints left no practical allocation to deploy")
            final_alloc_pct = None
            final_alloc_amt = None
        elif score >= _config_float(cfg, "promote_score_threshold", 72.0) and confidence >= min_buy_confidence:
            action = "PROMOTE_TO_PORTFOLIO"
            rationale.append("setup clears the score and confidence bar for a portfolio slot")
            final_alloc_pct = allocation.suggested_pct
            final_alloc_amt = allocation.suggested_amount
        elif score >= _config_float(cfg, "watchlist_score_threshold", 55.0) and confidence >= max(0.0, min_buy_confidence - 0.05):
            action = "BUY"
            rationale.append("setup is good enough for a starter position but not a full portfolio promotion")
            # BUY is a starter position — apply the multiplier so it is always
            # smaller than a full PROMOTE_TO_PORTFOLIO allocation.
            buy_mul = max(0.0, min(1.0, _config_float(cfg, "buy_starter_multiplier", 0.70)))
            final_alloc_pct = round(allocation.suggested_pct * buy_mul, 4)
            final_alloc_amt = round(allocation.suggested_amount * buy_mul, 2)
        else:
            action = "ADD_TO_WATCHLIST"
            rationale.append("setup is interesting, but not strong enough yet to improve portfolio profit potential")
            final_alloc_pct = None
            final_alloc_amt = None

        actions.append(
            PortfolioAction(
                action=action,
                symbol=symbol,
                strategy_type=route.strategy_type,
                score=round(score, 1),
                confidence=round(confidence, 3),
                rationale=rationale,
                suggested_allocation_pct=final_alloc_pct,
                suggested_allocation_amount=final_alloc_amt,
            )
        )

    routed_symbols = {item["route"].symbol for item in routed}
    if strongest_external is not None:
        for symbol, holding in holdings_by_symbol.items():
            if symbol in routed_symbols:
                continue
            holding_strategy = normalize_strategy_type(read_value(holding, "strategy_type", "compounder"))
            exit_suggestion = evaluate_exit(
                holding,
                strategy_type=holding_strategy,
                stronger_opportunity=strongest_external["opportunity"],
                context=context,
                config=exit_cfg,
            )
            if exit_suggestion.action in {"SELL", "TRIM"}:
                actions.append(
                    PortfolioAction(
                        action=exit_suggestion.action,
                        symbol=symbol,
                        strategy_type=holding_strategy,
                        score=round(_holding_score(holding), 1),
                        confidence=round(_holding_confidence(holding), 3),
                        rationale=list(exit_suggestion.reasons),
                        related_symbol=read_value(strongest_external["opportunity"], "symbol"),
                        exit_plan=exit_suggestion.to_dict(),
                    )
                )

    summary_bits = []
    action_counts = {}
    for action in actions:
        action_counts[action.action] = action_counts.get(action.action, 0) + 1
    for key in ("PROMOTE_TO_PORTFOLIO", "BUY", "SELL", "HOLD", "ADD_TO_WATCHLIST"):
        if action_counts.get(key):
            summary_bits.append(f"{action_counts[key]} {key.lower()}")

    if action_counts.get("TRIM"):
        summary_bits.append(f"{action_counts['TRIM']} trim")

    return {
        "available": True,
        "summary_line": (
            "Portfolio decision layer: " + ", ".join(summary_bits) + "."
            if summary_bits
            else "Portfolio decision layer: no action changes were warranted."
        ),
        "actions": [action.to_dict() for action in actions],
    }


def _opportunity_score(opportunity: Any) -> float:
    score = read_value(opportunity, "score", None)
    if score is None:
        score = read_value(opportunity, "total_score", 0.0)
    return normalize_score(score, default=0.0)


def _opportunity_confidence(opportunity: Any) -> float:
    direct = read_value(opportunity, "confidence", None)
    if direct is None:
        direct = read_value(opportunity, "recommendation_confidence", None)
    if direct is not None:
        return normalize_confidence(direct)
    # No explicit confidence — infer conservatively from score, capped at 0.60.
    # This ensures PROMOTE_TO_PORTFOLIO (requires >= 0.65) always needs an
    # explicit confidence signal rather than being reachable from score alone.
    score = _opportunity_score(opportunity)
    return min(0.60, normalize_confidence(score, default=0.5))


def _holding_symbol(holding: Any) -> str:
    return normalize_symbol(read_value(holding, "symbol", ""), default="")


def _holding_sector(item: Any) -> str:
    sector = read_value(item, "sector", None)
    if sector:
        return str(sector)
    fundamentals = read_value(item, "fundamentals", {}) or {}
    if isinstance(fundamentals, dict):
        return str(fundamentals.get("sector", "Unknown"))
    return "Unknown"


def _holding_value(holding: Any) -> float:
    for key in ("market_value", "position_value"):
        value = read_value(holding, key, None)
        if value is not None:
            numeric = as_finite_float(value, default=None)
            if numeric is not None:
                return numeric
    shares = as_finite_float(read_value(holding, "shares", 0.0), default=0.0) or 0.0
    price = as_finite_float(read_value(holding, "current_price", 0.0), default=0.0) or 0.0
    return shares * price


def _sector_exposures(holdings: list[Any], portfolio_value: float) -> dict[str, float]:
    exposures: dict[str, float] = {}
    portfolio_value = as_finite_float(portfolio_value, default=0.0) or 0.0
    if portfolio_value <= 0:
        return exposures
    for holding in holdings:
        sector = _holding_sector(holding)
        exposures[sector] = exposures.get(sector, 0.0) + (_holding_value(holding) / portfolio_value)
    return exposures


def _holding_score(holding: Any) -> float:
    for key in ("holding_strength", "score", "total_score"):
        value = read_value(holding, key, None)
        if value is None:
            continue
        numeric = as_finite_float(value, default=None)
        if numeric is not None:
            return normalize_score(numeric, default=50.0)
    signal = as_finite_float(read_value(holding, "signal_score", None), default=None)
    confidence = as_finite_float(read_value(holding, "confidence_score", None), default=None)
    if signal is not None and confidence is not None:
        return signal * normalize_confidence(confidence) * 100.0
    return 50.0


def _holding_confidence(holding: Any) -> float:
    value = read_value(holding, "confidence_score", None)
    if value is not None:
        return normalize_confidence(value)
    return 0.5


def _config_float(cfg: dict[str, Any], key: str, default: float) -> float:
    value = as_finite_float(cfg.get(key), default=default)
    return default if value is None else value
