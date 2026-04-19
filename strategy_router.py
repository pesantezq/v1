"""
Strategy router for broader-market opportunities.

Classifies promoted opportunities into longer-term compounders or
shorter-term momentum trades using explainable, additive rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from decision_support import (
    as_finite_float,
    factor_breakdown_dict,
    normalize_strategy_type,
    normalize_symbol,
    read_value,
)


@dataclass
class StrategyRoute:
    symbol: str
    strategy_type: str
    rationale: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy_type": self.strategy_type,
            "rationale": list(self.rationale),
            "signals": dict(self.signals),
        }


def route_opportunity(opportunity: Any) -> StrategyRoute:
    symbol = normalize_symbol(read_value(opportunity, "symbol", ""))
    events = {
        str(event or "").upper()
        for event in (read_value(opportunity, "events", []) or [])
        if str(event or "").strip()
    }
    factor_breakdown = factor_breakdown_dict(opportunity)
    relative_strength = as_finite_float(factor_breakdown.get("relative_strength"), default=None)
    momentum = as_finite_float(factor_breakdown.get("momentum"), default=None)
    volume_confirmation = as_finite_float(factor_breakdown.get("volume_confirmation"), default=None)
    volatility_sanity = as_finite_float(factor_breakdown.get("volatility_sanity"), default=None)
    theme_support = _theme_support(opportunity)
    pct_from_200dma = as_finite_float(
        read_value(opportunity, "pct_from_200dma", _nested_scan_value(opportunity, "pct_from_200dma")),
        default=None,
    )
    label = normalize_strategy_type(read_value(opportunity, "label", ""), default="")

    rationale: list[str] = []

    compounder_votes = 0
    momentum_votes = 0

    if label == "compounder":
        compounder_votes += 1
        rationale.append("promotion engine already tagged this as a compounder")
    elif label == "momentum":
        momentum_votes += 1
        rationale.append("promotion engine already tagged this as a momentum setup")

    if "BREAKOUT_PROXY" in events:
        compounder_votes += 1
        rationale.append("near 52-week high breakout proxy is active")
        if "STRONG_MOVE_UP" in events and (pct_from_200dma is None or pct_from_200dma < 10):
            momentum_votes += 1
            rationale.append("breakout is recent and not well-established above 200dma — tactical bias")
    if "STRONG_MOVE_UP" in events:
        momentum_votes += 1
        rationale.append("fast upward move is active")
    if "VOLUME_SPIKE" in events:
        momentum_votes += 1
        rationale.append("volume surge confirms the move")

    if relative_strength is not None and relative_strength >= 75:
        compounder_votes += 1
        rationale.append(f"relative strength is strong ({relative_strength:.1f})")
    if momentum is not None and momentum >= 70:
        momentum_votes += 1
        rationale.append(f"momentum score is elevated ({momentum:.1f})")
    if volume_confirmation is not None and volume_confirmation >= 65:
        momentum_votes += 1
        rationale.append(f"volume confirmation is strong ({volume_confirmation:.1f})")
    if theme_support is not None and theme_support >= 0.55:
        compounder_votes += 1
        rationale.append(f"theme support is durable ({theme_support:.2f})")
    if pct_from_200dma is not None and pct_from_200dma >= 5:
        compounder_votes += 1
        rationale.append(f"price is materially above the 200dma ({pct_from_200dma:+.1f}%)")
    if volatility_sanity is not None and volatility_sanity < 40 and momentum_votes > 0:
        rationale.append("setup is volatile, which fits a tactical momentum profile better than a compounder")
        momentum_votes += 1

    if compounder_votes >= momentum_votes:
        strategy_type = "compounder"
    else:
        strategy_type = "momentum"

    if not rationale:
        rationale.append(
            "classification fell back to compounder because the setup lacked enough tactical evidence to route as momentum"
        )

    return StrategyRoute(
        symbol=symbol,
        strategy_type=strategy_type,
        rationale=rationale,
        signals={
            "events": sorted(events),
            "relative_strength": relative_strength,
            "momentum": momentum,
            "volume_confirmation": volume_confirmation,
            "volatility_sanity": volatility_sanity,
            "theme_support": theme_support,
            "pct_from_200dma": pct_from_200dma,
            "compounder_votes": compounder_votes,
            "momentum_votes": momentum_votes,
        },
    )


def _nested_scan_value(obj: Any, key: str) -> Any:
    scan_result = read_value(obj, "scan_result")
    if scan_result is None:
        return None
    return read_value(scan_result, key)


def _theme_support(obj: Any) -> float | None:
    direct = as_finite_float(read_value(obj, "theme_support", None), default=None)
    if direct is not None:
        return max(0.0, min(1.0, direct))
    return None
