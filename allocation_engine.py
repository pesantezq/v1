"""
Allocation engine for broader-market portfolio actions.

Produces advisory sizing suggestions with caps, reserve checks, and
smaller tactical sizing for momentum or lower-confidence trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from decision_support import as_finite_float, normalize_confidence, normalize_strategy_type, read_value
from watchlist_scanner.allocation_preview import _rank_multiplier as _policy_rank_multiplier


# Profit-maximization tactical retune (operator-approved 2026-05-18):
# base sizes ~2×, max_position_cap nearly 2×, sector_cap 1.75×,
# low_confidence_multiplier eased. Reverts cleanly by restoring prior values.
#
# Targeted partial revert (operator-approved 2026-06-26): the 2026-05-18 gauge
# (fp e2b5ecab) underperformed the prior gauge d95e by -5.9pp hit-rate / -0.34pp
# mean-return at 1d. Attribution showed the drag was CONCENTRATED, not broad:
# the loosened sector_cap (0.20→0.35) let the engine overweight Energy into a
# downturn (Energy mean_return flipped +0.33%→-1.54%, position count 18→39), and
# Financials per-win return compressed (+1.00%→+0.29%) from diluted concentration.
# Pull the sizing caps back toward the d95e direction WITHOUT a full rollback —
# sector_cap 0.35→0.25, max_position_cap 0.15→0.12 — to cap sector overload and
# restore concentration discipline in winning sectors. Base sizes,
# low_confidence_multiplier, and ml_advisor are left untouched (Tech was stable
# and 7d metrics improved). Mints a new gauge era on the next cron; the
# attribution tracker re-scores it independently. Reverts cleanly to 0.35/0.15.
DEFAULT_CONFIG = {
    "compounder_base_pct": 0.10,
    "momentum_base_pct": 0.06,
    "high_confidence_threshold": 0.75,
    "medium_confidence_threshold": 0.60,
    "high_confidence_multiplier": 1.00,
    "medium_confidence_multiplier": 0.75,
    "low_confidence_multiplier": 0.65,
    "degraded_penalty": 0.65,
    "risk_off_compounder_multiplier": 0.85,
    "risk_off_momentum_multiplier": 0.55,
    "max_position_cap": 0.12,
    "sector_cap": 0.25,
    "cash_reserve_pct": 0.05,
    "min_position_pct": 0.01,
    # Fundamentals-based sizing guard — applied before sector/cash caps.
    # Positions where fundamentals_score (0-100) falls below the threshold
    # are capped at low_fundamentals_cap regardless of confidence.
    "low_fundamentals_threshold": 30.0,
    "low_fundamentals_cap": 0.02,
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
    # Advisory allocation policy metadata — never changes suggested_pct
    allocation_policy_source: str = "default"
    allocation_policy_candidate: str = "rank_aware"
    rank_multiplier: float = 1.0
    baseline_suggested_pct: float = 0.0
    rank_aware_suggested_pct: float = 0.0
    allocation_policy_reason: str = ""
    # P4.4 — Vol regime advisor feedback
    vol_regime_source: str = "default"  # "advisor" when plan was consulted
    vol_regime_multiplier: float = 1.0
    vol_regime_label: str = ""

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
            "allocation_policy_source": self.allocation_policy_source,
            "allocation_policy_candidate": self.allocation_policy_candidate,
            "rank_multiplier": round(self.rank_multiplier, 4),
            "baseline_suggested_pct": round(self.baseline_suggested_pct, 4),
            "rank_aware_suggested_pct": round(self.rank_aware_suggested_pct, 4),
            "allocation_policy_reason": self.allocation_policy_reason,
            "vol_regime_source": self.vol_regime_source,
            "vol_regime_multiplier": round(self.vol_regime_multiplier, 4),
            "vol_regime_label": self.vol_regime_label,
        }


def _vol_regime_multiplier_from_plan(plan: dict[str, Any] | None) -> tuple[float, str, str]:
    """
    Extract (multiplier, source, label) from a vol_regime_advisor plan.

    Returns (1.0, "default", "") when the plan is missing, malformed,
    insufficient, or carries an invalid multiplier. When the advisor is
    consulted and reports status=="ok", returns the suggested multiplier
    (clamped to (0, ∞)) and source="advisor". A multiplier of exactly 1.0
    still records source="advisor" so observability captures the consult.
    """
    if not isinstance(plan, dict):
        return 1.0, "default", ""
    if plan.get("status") != "ok":
        return 1.0, "default", ""
    raw = plan.get("sizing_multiplier_suggested")
    value = as_finite_float(raw, default=None)
    if value is None or value <= 0.0:
        return 1.0, "default", ""
    label = str(plan.get("regime") or "")
    return float(value), "advisor", label


def suggest_allocation(
    *,
    opportunity: Any,
    strategy_type: str,
    portfolio_value: float,
    cash_available: float,
    current_sector_exposure: float = 0.0,
    context: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    approved_policy: dict[str, Any] | None = None,
    vol_regime_plan: dict[str, Any] | None = None,
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

    # P4.4 — Vol regime feedback. Applied after risk-off + degraded so
    # both regimes can compound; applied before caps so caps still bind
    # on the regime-adjusted size. Multiplier=1.0 is recorded for
    # observability but does not alter suggested_pct or rationale.
    vol_mult, vol_source, vol_label = _vol_regime_multiplier_from_plan(vol_regime_plan)
    if vol_source == "advisor" and vol_mult != 1.0:
        suggested_pct *= vol_mult
        rationale.append(
            f"vol regime '{vol_label}' applies aggregate sizing multiplier ×{vol_mult:.2f}"
        )

    # Enforce a minimum viable position size.  When compounding penalties
    # (risk-off + degraded + low-confidence) would produce a sub-threshold
    # position, zeroing it prevents economically pointless micro-trades.
    min_pos_pct = _config_float(cfg, "min_position_pct", DEFAULT_CONFIG["min_position_pct"], minimum=0.0)
    if min_pos_pct > 0 and 0 < suggested_pct < min_pos_pct:
        suggested_pct = 0.0
        rationale.append("position would be too small to execute meaningfully after penalty adjustments")

    portfolio_value = max(0.0, as_finite_float(portfolio_value, default=0.0) or 0.0)
    cash_available = max(0.0, as_finite_float(cash_available, default=0.0) or 0.0)
    reserve_pct = _config_float(cfg, "cash_reserve_pct", DEFAULT_CONFIG["cash_reserve_pct"], minimum=0.0, maximum=1.0)
    reserve_target = max(0.0, portfolio_value * reserve_pct)
    deployable_cash = max(0.0, cash_available - reserve_target)

    capped_by: list[str] = []

    # Fundamentals-based guard: cap weak-fundamental positions before other caps.
    fund_score = _infer_fundamentals_score(opportunity)
    low_fund_threshold = _config_float(
        cfg, "low_fundamentals_threshold", DEFAULT_CONFIG["low_fundamentals_threshold"], minimum=0.0
    )
    low_fund_cap = _config_float(
        cfg, "low_fundamentals_cap", DEFAULT_CONFIG["low_fundamentals_cap"], minimum=0.0
    )
    if (
        fund_score is not None
        and low_fund_threshold is not None
        and low_fund_cap is not None
        and fund_score < low_fund_threshold
        and suggested_pct > low_fund_cap
    ):
        suggested_pct = low_fund_cap
        capped_by.append("low_fundamentals_cap")
        rationale.append(
            f"fundamentals score {fund_score:.0f}/100 is below threshold "
            f"({low_fund_threshold:.0f}) — position capped at {low_fund_cap:.1%}"
        )

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

    # Advisory allocation policy metadata — does not change suggested_pct
    baseline_suggested_pct = max(0.0, round(suggested_pct, 4))
    rank_mult = 1.0
    rank_aware_pct = baseline_suggested_pct
    policy_source = "default"
    policy_reason = "rank-aware allocation policy not active"

    if approved_policy is not None and approved_policy.get("_valid") is True:
        rank_score_raw = read_value(opportunity, "final_rank_score", None)
        if rank_score_raw is not None:
            rank_score = as_finite_float(rank_score_raw, default=None)
            if rank_score is not None:
                rank_mult, rank_label = _policy_rank_multiplier(rank_score)
                raw_rank_aware = baseline_suggested_pct * rank_mult
                rank_aware_pct = round(
                    min(raw_rank_aware, max_position_cap), 4
                )
                policy_source = "approved_rank_aware"
                policy_reason = (
                    f"approved rank-aware policy active; {rank_label} score "
                    f"({rank_score:.3f}) → ×{rank_mult:.2f} multiplier; "
                    f"baseline {baseline_suggested_pct:.1%} → "
                    f"rank-aware {rank_aware_pct:.1%} (advisory only)"
                )
            else:
                policy_reason = (
                    "approved policy active but final_rank_score is non-numeric"
                )
        else:
            policy_reason = (
                "approved policy active but opportunity has no final_rank_score"
            )

    return AllocationSuggestion(
        symbol=symbol,
        strategy_type=strategy_type,
        confidence=confidence,
        suggested_pct=max(0.0, round(suggested_pct, 4)),
        suggested_amount=max(0.0, round(suggested_amount, 2)),
        deployable_cash=round(deployable_cash, 2),
        capped_by=_dedupe(capped_by),
        rationale=rationale,
        allocation_policy_source=policy_source,
        allocation_policy_candidate="rank_aware",
        rank_multiplier=rank_mult,
        baseline_suggested_pct=baseline_suggested_pct,
        rank_aware_suggested_pct=rank_aware_pct,
        allocation_policy_reason=policy_reason,
        vol_regime_source=vol_source,
        vol_regime_multiplier=vol_mult,
        vol_regime_label=vol_label,
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


def _infer_fundamentals_score(opportunity: Any) -> float | None:
    """Read fundamentals_score from an opportunity dict; normalise to 0-100 scale."""
    raw = read_value(opportunity, "fundamentals_score", None)
    if raw is None:
        return None
    val = as_finite_float(raw, default=None)
    if val is None:
        return None
    # Accept both 0-1 and 0-100 representations
    if 0.0 <= val <= 1.0:
        return val * 100.0
    return val
