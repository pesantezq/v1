"""Per-strategy objective scoring (spec §24.3). Deterministic, pure, explainable.

Computes the 18 comparison metrics for one :class:`StrategyProfile` against a
context (current portfolio weights, radar, regime). Heuristic but stable — the
comparator prefers sandbox/backtest evidence over these estimates where available
(§24.8). Nothing here trades or mutates anything.
"""
from __future__ import annotations

from typing import Any

from portfolio_automation.next_stage.contracts import StrategyProfile, StrategyId

_DD_TOL = {"high": 0.8, "medium": 0.5, "normal": 0.45, "low": 0.2}
_HORIZON_TURNOVER = {"short_term": 0.8, "medium_term": 0.5, "long_term": 0.25,
                     "very_long_term": 0.1}
_REVIEW = {"QUALIFIED", "APPROVED_WATCHLIST_REVIEW"}


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, round(float(x), 4)))


def compute_strategy_metrics(profile: StrategyProfile, context: dict[str, Any]) -> dict[str, Any]:
    weights: dict[str, float] = context.get("weights", {}) or {}
    cash_drag = context.get("cash_drag")
    radar = context.get("radar_opportunities", []) or []
    has_tax_lot = bool(context.get("has_tax_lot_data", False))
    data_quality = float(context.get("data_quality_score", 0.6) or 0.6)
    leveraged_exposure = float(context.get("leveraged_exposure", 0.0) or 0.0)

    concentration_risk = _clamp(max(weights.values())) if weights else 0.0
    diversification_score = _clamp(1.0 - concentration_risk)

    risk_level = _clamp(_DD_TOL.get(profile.drawdown_tolerance, 0.45)
                        + 0.3 * leveraged_exposure)
    expected_volatility = _clamp(risk_level * 0.9)
    max_drawdown_estimate = _clamp(risk_level * 0.5)

    turnover = _clamp(_HORIZON_TURNOVER.get(profile.horizon, 0.3))
    is_tax_aware = profile.strategy_id in (StrategyId.TAX_AWARE.value,
                                           StrategyId.LONG_TERM_COMPOUNDING.value)
    tax_efficiency = _clamp((1.0 - turnover) + (0.2 if is_tax_aware else 0.0))

    # opportunity capture: share of review-ready radar names eligible for this profile
    elig = set(profile.eligible_candidate_types)
    review_names = [o for o in radar if o.get("final_status") in _REVIEW
                    and o.get("candidate_type") in elig]
    opp_capture = _clamp(len(review_names) / 10.0) if review_names else 0.0

    liquidity_score = _clamp(0.9 if "etf" in elig else 0.6)
    implementation_complexity = _clamp(0.3 + 0.5 * turnover)
    behavioral_difficulty = _clamp(risk_level if profile.strategy_id in (
        StrategyId.AGGRESSIVE_GROWTH.value, StrategyId.BOOM_BUCKET.value,
        StrategyId.SHORT_TERM_TACTICAL.value) else 0.3)

    # objective fit: alignment of the profile's intent with current portfolio shape
    objective_fit = _clamp(0.5 + 0.2 * diversification_score
                           + 0.2 * (1.0 - abs(risk_level - 0.5) * 2)
                           + 0.1 * opp_capture)

    confidence_score = _clamp(0.5 + 0.3 * data_quality - (0.2 if not has_tax_lot and is_tax_aware else 0.0))

    # after-tax estimate degrades without tax-lot/cost-basis data (§23.11)
    after_tax_return_estimate = None if not has_tax_lot else _clamp(0.06 * tax_efficiency)

    final_rank = _clamp(0.35 * objective_fit + 0.2 * opp_capture
                        + 0.15 * diversification_score + 0.15 * tax_efficiency
                        + 0.1 * confidence_score - 0.15 * risk_level
                        - 0.1 * implementation_complexity + 0.3)

    return {
        "strategy_id": profile.strategy_id, "name": profile.name,
        "expected_objective_fit": objective_fit,
        "expected_risk_level": risk_level,
        "expected_volatility": expected_volatility,
        "max_drawdown_estimate": max_drawdown_estimate,
        "concentration_risk": concentration_risk,
        "leverage_exposure": _clamp(leveraged_exposure),
        "cash_drag": cash_drag,
        "turnover": turnover,
        "tax_efficiency": tax_efficiency,
        "after_tax_return_estimate": after_tax_return_estimate,
        "after_tax_degraded": after_tax_return_estimate is None,
        "opportunity_capture_score": opp_capture,
        "diversification_score": diversification_score,
        "liquidity_score": liquidity_score,
        "implementation_complexity": implementation_complexity,
        "behavioral_difficulty": behavioral_difficulty,
        "confidence_score": confidence_score,
        "data_quality_score": _clamp(data_quality),
        "final_strategy_rank": final_rank,
        "observe_only": True,
    }
