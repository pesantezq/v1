"""
Allocation engine for broader-market portfolio actions.

Produces advisory sizing suggestions with caps, reserve checks, and
smaller tactical sizing for momentum or lower-confidence trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from decision_support import as_finite_float, normalize_confidence, normalize_strategy_type, read_value


DEFAULT_CONFIG = {
    "compounder_base_pct": 0.05,
    "momentum_base_pct": 0.03,
    "high_confidence_threshold": 0.75,
    "medium_confidence_threshold": 0.60,
    "high_confidence_multiplier": 1.00,
    "medium_confidence_multiplier": 0.75,
    "low_confidence_multiplier": 0.50,
    "degraded_penalty": 0.65,
    "risk_off_compounder_multiplier": 0.85,
    "risk_off_momentum_multiplier": 0.55,
    "max_position_cap": 0.08,
    "sector_cap": None,
    "cash_reserve_pct": 0.05,
}


@dataclass
class AllocationSuggestion:
    symbol: str
    strategy_type: str
    confidence: float
    suggested_pct: float
    suggested_amount: float
    deployable_cash: float
    capped_by: list[str] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy_type": self.strategy_type,
            "confidence": round(self.confidence, 3),
            "suggested_pct": round(self.suggested_pct, 4),
            "suggested_amount": round(self.suggested_amount, 2),
            "deployable_cash": round(self.deployable_cash, 2),
            "capped_by": list(self.capped_by),
            "rationale": list(self.rationale),
        }


def suggest_allocation(
    *,
    opportunity: Any,
    strategy_type: str,
    portfolio_value: float,
    cash_available: float,
    current_sector_exposure: float = 0.0,
    context: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> AllocationSuggestion:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(config or {})
    context = context or {}

    symbol = str(read_value(opportunity, "symbol", "UNKNOWN") or "UNKNOWN").upper()
    confidence = _infer_confidence(opportunity)
    strategy_type = normalize_strategy_type(strategy_type)
    base_pct = _config_float(
        cfg,
        "compounder_base_pct" if strategy_type == "compounder" else "momentum_base_pct",
        DEFAULT_CONFIG["compounder_base_pct" if strategy_type == "compounder" else "momentum_base_pct"],
        minimum=0.0,
    )
    rationale = [f"base sizing for {strategy_type} starts at {base_pct:.1%}"]

    threshold_high = _config_float(cfg, "high_confidence_threshold", DEFAULT_CONFIG["high_confidence_threshold"], minimum=0.0, maximum=1.0)
    threshold_medium = _config_float(cfg, "medium_confidence_threshold", DEFAULT_CONFIG["medium_confidence_threshold"], minimum=0.0, maximum=1.0)
    if confidence >= threshold_high:
        confidence_multiplier = _config_float(cfg, "high_confidence_multiplier", DEFAULT_CONFIG["high_confidence_multiplier"], minimum=0.0)
        rationale.append("high-confidence setup keeps full base size")
    elif confidence >= threshold_medium:
        confidence_multiplier = _config_float(cfg, "medium_confidence_multiplier", DEFAULT_CONFIG["medium_confidence_multiplier"], minimum=0.0)
        rationale.append("medium-confidence setup is sized below the base")
    else:
        confidence_multiplier = _config_float(cfg, "low_confidence_multiplier", DEFAULT_CONFIG["low_confidence_multiplier"], minimum=0.0)
        rationale.append("lower-confidence setup is sized conservatively")

    suggested_pct = base_pct * confidence_multiplier

    regime_label = str(
        context.get("regime_label")
        or context.get("drawdown_regime")
        or context.get("market_regime")
        or "neutral"
    ).lower()
    if regime_label in {"risk_off", "significant_dip", "severe_dip"}:
        if strategy_type == "momentum":
            suggested_pct *= _config_float(cfg, "risk_off_momentum_multiplier", DEFAULT_CONFIG["risk_off_momentum_multiplier"], minimum=0.0)
            rationale.append("risk-off regime cuts tactical momentum size further")
        else:
            suggested_pct *= _config_float(cfg, "risk_off_compounder_multiplier", DEFAULT_CONFIG["risk_off_compounder_multiplier"], minimum=0.0)
            rationale.append("risk-off regime trims compounder entry size modestly")

    if bool(context.get("degraded_mode")):
        suggested_pct *= _config_float(cfg, "degraded_penalty", DEFAULT_CONFIG["degraded_penalty"], minimum=0.0)
        rationale.append("degraded data mode reduces position size")

    portfolio_value = max(0.0, as_finite_float(portfolio_value, default=0.0) or 0.0)
    cash_available = max(0.0, as_finite_float(cash_available, default=0.0) or 0.0)
    reserve_pct = _config_float(cfg, "cash_reserve_pct", DEFAULT_CONFIG["cash_reserve_pct"], minimum=0.0, maximum=1.0)
    reserve_target = max(0.0, portfolio_value * reserve_pct)
    deployable_cash = max(0.0, cash_available - reserve_target)

    capped_by: list[str] = []
    max_position_cap = _config_float(cfg, "max_position_cap", DEFAULT_CONFIG["max_position_cap"], minimum=0.0)
    if suggested_pct > max_position_cap:
        suggested_pct = max_position_cap
        capped_by.append("max_position_cap")

    sector_cap = _config_float(cfg, "sector_cap", None, minimum=0.0, allow_none=True)
    if sector_cap is not None:
        sector_headroom = max(
            0.0,
            float(sector_cap) - max(0.0, as_finite_float(current_sector_exposure, default=0.0) or 0.0),
        )
        if suggested_pct > sector_headroom:
            suggested_pct = max(0.0, sector_headroom)
            capped_by.append("sector_cap")
            rationale.append("sector cap headroom reduced the suggested position")

    target_amount = suggested_pct * max(0.0, portfolio_value)
    suggested_amount = min(target_amount, deployable_cash)
    if suggested_amount < target_amount:
        capped_by.append("cash_reserve")
        rationale.append("cash reserve left less deployable capital than the raw target size")

    if deployable_cash <= 0:
        suggested_pct = 0.0
        suggested_amount = 0.0
        rationale.append("no deployable cash is available after respecting the reserve")

    return AllocationSuggestion(
        symbol=symbol,
        strategy_type=strategy_type,
        confidence=confidence,
        suggested_pct=max(0.0, round(suggested_pct, 4)),
        suggested_amount=max(0.0, round(suggested_amount, 2)),
        deployable_cash=round(deployable_cash, 2),
        capped_by=_dedupe(capped_by),
        rationale=rationale,
    )


def _infer_confidence(opportunity: Any) -> float:
    direct = as_finite_float(read_value(opportunity, "confidence", None), default=None)
    if direct is not None:
        return normalize_confidence(direct)
    direct = as_finite_float(read_value(opportunity, "recommendation_confidence", None), default=None)
    if direct is not None:
        return normalize_confidence(direct)
    score = as_finite_float(read_value(opportunity, "score", None), default=None)
    if score is None:
        score = as_finite_float(read_value(opportunity, "total_score", 50.0), default=50.0)
    if score is None:
        return 0.5
    if 0.0 <= score <= 1.0:
        score *= 100.0
    return normalize_confidence(score)


def _config_float(
    cfg: dict[str, Any],
    key: str,
    default: float | None,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    allow_none: bool = False,
) -> float | None:
    raw = cfg.get(key, default)
    if raw is None and allow_none:
        return None
    value = as_finite_float(raw, default=default)
    if value is None:
        return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _dedupe(items: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            seen.add(item)
            output.append(item)
    return output
