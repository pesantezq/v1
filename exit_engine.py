"""
Exit engine for broader-market portfolio actions.

Produces advisory exit/trim/hold suggestions for compounders and
momentum trades based on trend breaks, thesis weakening, stronger
replacement opportunities, and profit protection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from decision_support import as_finite_float, normalize_score, normalize_strategy_type, read_value


DEFAULT_THRESHOLDS = {
    "momentum_trend_break_50dma": -3.0,
    "compounder_trend_break_200dma": -5.0,
    "compounder_hard_break_200dma": -8.0,
    "theme_support_floor": 0.30,
    "replacement_gap_momentum": 12.0,
    "replacement_gap_compounder": 25.0,
    "profit_protect_momentum": 0.12,
    "profit_protect_compounder": 0.25,
    # How much urgency compresses each strategy's profit-protection threshold.
    # urgency=1.0 + sensitivity=0.40 → threshold shrinks by 40% (exit sooner).
    # Set to 0.0 to disable urgency adjustment for that strategy.
    "urgency_sensitivity_momentum": 0.40,
    "urgency_sensitivity_compounder": 0.20,
}


@dataclass
class ExitSuggestion:
    symbol: str
    action: str
    strategy_type: str
    reasons: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    # Populated whenever a stronger_opportunity is evaluated; empty dict otherwise.
    # Keys: incumbent_score, challenger_score, actual_margin, required_margin,
    #       rotation_triggered, score_basis.
    rotation_detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "action": self.action,
            "strategy_type": self.strategy_type,
            "reasons": list(self.reasons),
            "triggers": list(self.triggers),
            "rotation_detail": dict(self.rotation_detail),
        }


def evaluate_exit(
    holding: Any,
    *,
    strategy_type: str,
    stronger_opportunity: Any | None = None,
    context: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> ExitSuggestion:
    cfg = dict(DEFAULT_THRESHOLDS)
    cfg.update(config or {})
    context = context or {}

    symbol = str(read_value(holding, "symbol", "UNKNOWN") or "UNKNOWN").upper()
    strategy_type = normalize_strategy_type(strategy_type)
    reasons: list[str] = []
    triggers: list[str] = []
    rotation_detail: dict[str, Any] = {}

    pct_from_50dma = _value(holding, "pct_from_50dma", fallback=None)
    pct_from_200dma = _value(holding, "pct_from_200dma", fallback=None)
    theme_support = _theme_support(holding)
    current_strength = _holding_strength(holding)
    unrealized_return = _infer_return(holding)
    stronger_score = _opportunity_score(stronger_opportunity)
    urgency = _urgency_score(holding)

    degraded_mode = bool(context.get("degraded_mode"))

    if strategy_type == "momentum":
        if pct_from_50dma is not None and pct_from_50dma <= _config_float(cfg, "momentum_trend_break_50dma", DEFAULT_THRESHOLDS["momentum_trend_break_50dma"]):
            triggers.append("trend_break")
            reasons.append(f"price is below the 50dma by {pct_from_50dma:.1f}%")
        if theme_support is not None and theme_support < _config_float(cfg, "theme_support_floor", DEFAULT_THRESHOLDS["theme_support_floor"], minimum=0.0, maximum=1.0):
            triggers.append("thesis_weakening")
            reasons.append("theme support has faded materially")
        sens_m = _config_float(cfg, "urgency_sensitivity_momentum", DEFAULT_THRESHOLDS["urgency_sensitivity_momentum"])
        adjusted_pp_m = _config_float(cfg, "profit_protect_momentum", DEFAULT_THRESHOLDS["profit_protect_momentum"]) * (1.0 - urgency * sens_m)
        if unrealized_return >= adjusted_pp_m and current_strength < 60:
            triggers.append("profit_protection")
            if urgency >= 0.6:
                reasons.append(f"open profit exists with elevated move urgency ({urgency:.2f}) — tightening exit threshold")
            else:
                reasons.append("open profit exists and short-term momentum is fading")
        required_margin_m = _config_float(cfg, "replacement_gap_momentum", DEFAULT_THRESHOLDS["replacement_gap_momentum"], minimum=0.0)
        if stronger_score is not None:
            actual_margin_m = round(stronger_score - current_strength, 2)
            rotation_fired_m = actual_margin_m >= required_margin_m
            rotation_detail = {
                "incumbent_score": round(current_strength, 2),
                "challenger_score": round(stronger_score, 2),
                "actual_margin": actual_margin_m,
                "required_margin": required_margin_m,
                "rotation_triggered": rotation_fired_m,
                "score_basis": "composite_0_to_100",
            }
            if rotation_fired_m:
                triggers.append("opportunity_rotation")
                reasons.append(
                    f"challenger ({stronger_score:.1f}) leads incumbent ({current_strength:.1f})"
                    f" by {actual_margin_m:.1f} pts — exceeds the {required_margin_m:.1f}-pt rotation bar"
                )
    else:
        if pct_from_200dma is not None:
            hard_break_threshold = _config_float(cfg, "compounder_hard_break_200dma", DEFAULT_THRESHOLDS["compounder_hard_break_200dma"])
            soft_break_threshold = _config_float(cfg, "compounder_trend_break_200dma", DEFAULT_THRESHOLDS["compounder_trend_break_200dma"])
            is_hard_break = pct_from_200dma <= hard_break_threshold
            is_soft_break = (
                pct_from_200dma <= soft_break_threshold
                and theme_support is not None
                and theme_support < 0.55
            )
            if is_hard_break or is_soft_break:
                triggers.append("trend_break")
                label = "—well below the hard-break floor" if is_hard_break else " with weakening support"
                reasons.append(f"price is below the 200dma by {pct_from_200dma:.1f}%{label}")
        if theme_support is not None and theme_support < _config_float(cfg, "theme_support_floor", DEFAULT_THRESHOLDS["theme_support_floor"], minimum=0.0, maximum=1.0):
            triggers.append("thesis_weakening")
            reasons.append("durable thesis support has weakened")
        sens_c = _config_float(cfg, "urgency_sensitivity_compounder", DEFAULT_THRESHOLDS["urgency_sensitivity_compounder"])
        adjusted_pp_c = _config_float(cfg, "profit_protect_compounder", DEFAULT_THRESHOLDS["profit_protect_compounder"]) * (1.0 - urgency * sens_c)
        if unrealized_return >= adjusted_pp_c and current_strength < 55:
            triggers.append("profit_protection")
            if urgency >= 0.6:
                reasons.append(f"large open gain with elevated move urgency ({urgency:.2f}) — proactive trim suggested")
            else:
                reasons.append("large open gain exists while quality signals have softened")
        required_margin_c = _config_float(cfg, "replacement_gap_compounder", DEFAULT_THRESHOLDS["replacement_gap_compounder"], minimum=0.0)
        if stronger_score is not None:
            actual_margin_c = round(stronger_score - current_strength, 2)
            rotation_fired_c = actual_margin_c >= required_margin_c
            rotation_detail = {
                "incumbent_score": round(current_strength, 2),
                "challenger_score": round(stronger_score, 2),
                "actual_margin": actual_margin_c,
                "required_margin": required_margin_c,
                "rotation_triggered": rotation_fired_c,
                "score_basis": "composite_0_to_100",
            }
            if rotation_fired_c:
                triggers.append("opportunity_rotation")
                reasons.append(
                    f"challenger ({stronger_score:.1f}) leads incumbent ({current_strength:.1f})"
                    f" by {actual_margin_c:.1f} pts — exceeds the {required_margin_c:.1f}-pt rotation bar"
                )

    if degraded_mode and triggers and all(trigger not in {"trend_break", "opportunity_rotation"} for trigger in triggers):
        reasons.append("degraded data mode argues for patience unless the trend break is clear")
        return ExitSuggestion(
            symbol=symbol,
            action="HOLD",
            strategy_type=strategy_type,
            reasons=reasons,
            triggers=triggers,
            rotation_detail=rotation_detail,
        )

    if "trend_break" in triggers or "opportunity_rotation" in triggers or "thesis_weakening" in triggers:
        action = "SELL"
    elif "profit_protection" in triggers:
        action = "TRIM"
    else:
        action = "HOLD"
        reasons.append("holding still fits its current strategy profile")

    return ExitSuggestion(
        symbol=symbol,
        action=action,
        strategy_type=strategy_type,
        reasons=reasons,
        triggers=triggers,
        rotation_detail=rotation_detail,
    )


def _value(obj: Any, key: str, fallback: float | None = 0.0) -> float | None:
    return as_finite_float(read_value(obj, key, None), default=fallback)


def _theme_support(obj: Any) -> float | None:
    direct = _value(obj, "theme_support", fallback=None)
    if direct is not None:
        return max(0.0, min(1.0, direct))
    return None


def _holding_strength(obj: Any) -> float:
    for key in ("holding_strength", "score", "total_score"):
        score = _value(obj, key, fallback=None)
        if score is not None:
            return normalize_score(score, default=50.0)
    signal = _value(obj, "signal_score", fallback=None)
    confidence = _value(obj, "confidence_score", fallback=None)
    if signal is not None and confidence is not None:
        return signal * confidence * 100.0
    return 50.0


def _infer_return(obj: Any) -> float:
    for key in ("unrealized_return", "return_since_entry", "profit_return"):
        value = _value(obj, key, fallback=None)
        if value is not None:
            return value if abs(value) <= 2.0 else value / 100.0
    return 0.0


def _urgency_score(obj: Any) -> float:
    """
    0.0 (slow/stable) to 1.0 (fast/volatile).  Missing data contributes 0.

    Two equal-weight (0.5 each) components:
      - day_range_pct: intraday high-low range as % of price.  6% saturates.
      - pct_change_1d: today's % price move (upward only).    4% saturates.
    """
    day_range = _value(obj, "day_range_pct", fallback=None)
    pct_1d = _value(obj, "pct_change_1d", fallback=None)
    score = 0.0
    if day_range is not None:
        score += min(1.0, day_range / 6.0) * 0.50
    if pct_1d is not None and pct_1d > 0:
        score += min(1.0, pct_1d / 4.0) * 0.50
    return round(min(1.0, score), 3)


def _opportunity_score(obj: Any | None) -> float | None:
    if obj is None:
        return None
    score = _value(obj, "score", fallback=None)
    if score is None:
        score = _value(obj, "total_score", fallback=None)
    if score is None:
        return None
    return normalize_score(score, default=None)


def _config_float(
    cfg: dict[str, Any],
    key: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = as_finite_float(cfg.get(key), default=default)
    if value is None:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value
