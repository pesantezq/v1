"""
Master strategy score — rank tactics by after-cost, risk-adjusted excess vs SPY,
rewarding consistency + research support, penalizing overfit / turnover / tax /
concentration / leverage. Pure; weights configurable.

A higher score = a more trustworthy strategy, NOT just the highest ending balance.
"""
from __future__ import annotations

from typing import Any

DEFAULT_WEIGHTS = {
    "excess_return_vs_spy": 1.0,
    "probability_beat_spy_bonus": 0.5,
    "drawdown_control_bonus": 0.5,
    "consistency_bonus": 0.5,
    "research_support_bonus": 0.25,
    "turnover_penalty": 0.3,
    "tax_drag_penalty": 0.3,
    "concentration_penalty": 0.3,
    "leverage_penalty": 0.3,
    "overfit_penalty": 0.8,
}


def score(components: dict[str, float], weights: dict[str, float] | None = None) -> dict[str, Any]:
    """
    Combine normalized score components into a single number + flags.

    `components` keys (all expected in ~[-1,1] or [0,1] normalized form):
      excess_return_vs_spy, probability_beat_spy, drawdown (≤0), consistency (0..1),
      has_research (bool/0-1), turnover (0..1), tax_drag (0..1), concentration (0..1),
      leverage (0..1), overfit (0..1 IS-OOS gap; None → unknown).
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    flags: list[str] = []

    excess = float(components.get("excess_return_vs_spy", 0.0))
    pbeat = float(components.get("probability_beat_spy", 0.0))
    drawdown = float(components.get("drawdown", 0.0))            # ≤ 0
    consistency = float(components.get("consistency", 0.0))
    has_research = 1.0 if components.get("has_research") else 0.0
    turnover = float(components.get("turnover", 0.0))
    tax_drag = float(components.get("tax_drag", 0.0))
    concentration = float(components.get("concentration", 0.0))
    leverage = float(components.get("leverage", 0.0))
    overfit = components.get("overfit")
    if overfit is None:
        overfit_val = 0.0
        flags.append("overfit_unknown")
    else:
        overfit_val = max(0.0, float(overfit))

    total = (
        w["excess_return_vs_spy"] * excess
        + w["probability_beat_spy_bonus"] * (pbeat - 0.5) * 2      # center at 0.5
        + w["drawdown_control_bonus"] * (1.0 + drawdown)            # less drawdown → higher
        + w["consistency_bonus"] * consistency
        + w["research_support_bonus"] * has_research
        - w["turnover_penalty"] * turnover
        - w["tax_drag_penalty"] * tax_drag
        - w["concentration_penalty"] * concentration
        - w["leverage_penalty"] * leverage
        - w["overfit_penalty"] * overfit_val
    )
    if not has_research:
        flags.append("no_academic_basis")
    return {"strategy_score": round(total, 4), "flags": flags, "components": components}


def rank(scored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort scored tactic dicts by strategy_score descending."""
    return sorted(scored, key=lambda s: s.get("strategy_score", 0.0), reverse=True)
