from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


CONVICTION_RANK = {
    "defer": 0,
    "observe": 1,
    "starter": 2,
    "normal": 3,
    "high_conviction": 4,
}


@dataclass(frozen=True)
class PolicyDefinition:
    name: str
    description: str
    category: str
    rationale: str
    intended_use_case: str
    min_conviction_rank: int | None = None
    max_conviction_rank: int | None = None
    min_confidence_score: float | None = None
    allowed_regimes: tuple[str, ...] = ()
    blocked_regimes: tuple[str, ...] = ()
    required_reliability: tuple[str, ...] = ()
    blocked_reliability: tuple[str, ...] = ()
    degraded_mode_handling: str = "allow"  # allow | avoid | require | cautious
    minimum_data_quality: tuple[str, ...] = ("full", "partial", "limited", "degraded")
    min_allocation: float | None = None
    max_allocation: float | None = None
    allocation_multiplier: float = 1.0
    degraded_min_confidence_score: float | None = None
    degraded_required_reliability: tuple[str, ...] = ()
    degraded_allocation_multiplier: float | None = None
    regime_preferences: tuple[str, ...] = ()
    profile_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class StrategyProfile:
    name: str
    description: str
    policy_bundle: tuple[str, ...]
    preferred_conviction_bands: tuple[str, ...]
    regime_preferences: tuple[str, ...]
    degraded_mode_tolerance: str
    max_suggested_size_style: str
    avoid_risk_off: bool
    allow_starter_ideas: bool


POLICY_REGISTRY: dict[str, PolicyDefinition] = {
    "baseline": PolicyDefinition(
        name="baseline",
        description="All historical signals without extra policy filters.",
        category="baseline",
        rationale="Acts as the comparison baseline for all offline policy experiments.",
        intended_use_case="Reference point for simulator comparisons.",
        regime_preferences=("risk_on", "neutral", "risk_off", "high_volatility"),
        profile_tags=("aggressive_growth", "balanced_growth", "momentum_focus", "conservative_observe"),
    ),
    "high_conviction_only": PolicyDefinition(
        name="high_conviction_only",
        description="Only high-conviction signals.",
        category="conviction_filter",
        rationale="Focuses on the strongest derived conviction signals only.",
        intended_use_case="Test whether strict conviction improves win rate enough to justify lower trade count.",
        min_conviction_rank=CONVICTION_RANK["high_conviction"],
        regime_preferences=("risk_on", "neutral"),
        profile_tags=("aggressive_growth", "momentum_focus"),
    ),
    "starter_plus": PolicyDefinition(
        name="starter_plus",
        description="Only starter, normal, or high-conviction signals.",
        category="conviction_filter",
        rationale="Removes passive observe/defer ideas while keeping actionable setups.",
        intended_use_case="Balanced simulations that still include starter-sized ideas.",
        min_conviction_rank=CONVICTION_RANK["starter"],
        profile_tags=("balanced_growth",),
    ),
    "risk_on_only": PolicyDefinition(
        name="risk_on_only",
        description="Only signals recorded in risk_on regime.",
        category="regime_filter",
        rationale="Checks whether constructive regimes carry better follow-through than mixed environments.",
        intended_use_case="Momentum-oriented testing in constructive market conditions.",
        allowed_regimes=("risk_on",),
        regime_preferences=("risk_on",),
        profile_tags=("momentum_focus",),
    ),
    "avoid_risk_off": PolicyDefinition(
        name="avoid_risk_off",
        description="Skip risk_off regime signals.",
        category="regime_filter",
        rationale="Avoids the weakest market backdrop without filtering out neutral conditions.",
        intended_use_case="Conservative comparisons that want to reduce drawdown exposure.",
        blocked_regimes=("risk_off",),
        regime_preferences=("risk_on", "neutral"),
        profile_tags=("defensive_quality",),
    ),
    "conservative_size_cap": PolicyDefinition(
        name="conservative_size_cap",
        description="Cap simulated allocation at 1.0% and skip zero-sized ideas.",
        category="sizing_overlay",
        rationale="Preserves idea selection while testing whether tighter size control improves outcomes.",
        intended_use_case="Drawdown-aware experiments that reduce exposure without changing trade selection much.",
        max_allocation=0.01,
        min_allocation=0.0001,
        profile_tags=("conservative_observe", "defensive_quality"),
    ),
    "combined": PolicyDefinition(
        name="combined",
        description="High-conviction signals in risk_on regime only.",
        category="combined_filter",
        rationale="Combines the strongest conviction threshold with the most constructive regime filter.",
        intended_use_case="Momentum-heavy research profile with intentionally low trade count.",
        min_conviction_rank=CONVICTION_RANK["high_conviction"],
        allowed_regimes=("risk_on",),
        regime_preferences=("risk_on",),
        profile_tags=("aggressive_growth", "momentum_focus"),
    ),
    "quality_growth": PolicyDefinition(
        name="quality_growth",
        description="Normal/high conviction, constructive regimes, and no weak reliability. Degraded data gets smaller simulated sizing.",
        category="strategy_quality",
        rationale="Matches the system's confidence and reliability concepts while staying constructive on regime.",
        intended_use_case="Quality-focused growth research with moderate selectivity.",
        min_conviction_rank=CONVICTION_RANK["normal"],
        allowed_regimes=("risk_on", "neutral"),
        blocked_reliability=("weak",),
        degraded_mode_handling="cautious",
        degraded_min_confidence_score=0.85,
        degraded_required_reliability=("strong",),
        degraded_allocation_multiplier=0.5,
        max_allocation=0.015,
        regime_preferences=("risk_on", "neutral"),
        profile_tags=("balanced_growth", "defensive_quality"),
    ),
    "regime_aligned": PolicyDefinition(
        name="regime_aligned",
        description="Only constructive regimes, with starter-plus conviction and cautious degraded-data handling.",
        category="strategy_regime",
        rationale="Tests whether regime alignment is more important than broad participation.",
        intended_use_case="Regime-aware comparisons that de-emphasize risk_off and high_volatility periods.",
        min_conviction_rank=CONVICTION_RANK["starter"],
        allowed_regimes=("risk_on", "neutral"),
        degraded_mode_handling="cautious",
        degraded_min_confidence_score=0.85,
        degraded_allocation_multiplier=0.5,
        regime_preferences=("risk_on", "neutral"),
        profile_tags=("balanced_growth", "momentum_focus"),
    ),
    "defensive_rotation": PolicyDefinition(
        name="defensive_rotation",
        description="Observe/starter/normal ideas only, with tight simulated sizing and broad regime caution.",
        category="strategy_defensive",
        rationale="Prioritizes lower drawdown behavior by avoiding oversized or ultra-aggressive setups.",
        intended_use_case="Defensive comparisons meant to reduce concentration and drawdown.",
        max_conviction_rank=CONVICTION_RANK["normal"],
        max_allocation=0.0075,
        allocation_multiplier=0.75,
        blocked_regimes=("high_volatility",),
        regime_preferences=("neutral", "risk_off"),
        profile_tags=("defensive_quality", "conservative_observe"),
    ),
    "high_quality_concentrated": PolicyDefinition(
        name="high_quality_concentrated",
        description="Only strongest conviction, strong reliability, and larger simulated sizing for the top ideas.",
        category="strategy_concentrated",
        rationale="Tests whether a lower-count, higher-quality basket outperforms broader participation.",
        intended_use_case="Aggressive research profile that concentrates only on the strongest setups.",
        min_conviction_rank=CONVICTION_RANK["high_conviction"],
        min_confidence_score=0.85,
        required_reliability=("strong",),
        allowed_regimes=("risk_on", "neutral"),
        max_allocation=0.03,
        allocation_multiplier=1.5,
        regime_preferences=("risk_on", "neutral"),
        profile_tags=("aggressive_growth",),
    ),
    "degraded_safe_mode": PolicyDefinition(
        name="degraded_safe_mode",
        description="Designed specifically for degraded data: require high confidence and strong history or skip.",
        category="strategy_degraded",
        rationale="Creates a survival-mode policy for fallback or degraded data conditions.",
        intended_use_case="Offline testing of the most conservative degraded-data posture.",
        min_conviction_rank=CONVICTION_RANK["high_conviction"],
        min_confidence_score=0.85,
        required_reliability=("strong",),
        degraded_mode_handling="require",
        degraded_allocation_multiplier=0.5,
        max_allocation=0.005,
        minimum_data_quality=("degraded", "limited"),
        regime_preferences=("neutral", "risk_off"),
        profile_tags=("conservative_observe", "defensive_quality"),
    ),
}


STRATEGY_PROFILES: dict[str, StrategyProfile] = {
    "aggressive_growth": StrategyProfile(
        name="aggressive_growth",
        description="Concentrated, conviction-heavy research profile for constructive regimes.",
        policy_bundle=("baseline", "combined", "high_quality_concentrated"),
        preferred_conviction_bands=("normal", "high_conviction"),
        regime_preferences=("risk_on", "neutral"),
        degraded_mode_tolerance="low",
        max_suggested_size_style="assertive",
        avoid_risk_off=True,
        allow_starter_ideas=False,
    ),
    "balanced_growth": StrategyProfile(
        name="balanced_growth",
        description="Balanced research profile that still allows starter ideas when evidence is constructive.",
        policy_bundle=("baseline", "starter_plus", "quality_growth", "regime_aligned"),
        preferred_conviction_bands=("starter", "normal", "high_conviction"),
        regime_preferences=("risk_on", "neutral"),
        degraded_mode_tolerance="medium",
        max_suggested_size_style="moderate",
        avoid_risk_off=False,
        allow_starter_ideas=True,
    ),
    "defensive_quality": StrategyProfile(
        name="defensive_quality",
        description="Risk-aware quality profile emphasizing drawdown control and degraded-data caution.",
        policy_bundle=("avoid_risk_off", "quality_growth", "defensive_rotation", "degraded_safe_mode"),
        preferred_conviction_bands=("observe", "starter", "normal"),
        regime_preferences=("neutral", "risk_off"),
        degraded_mode_tolerance="very_low",
        max_suggested_size_style="tight",
        avoid_risk_off=True,
        allow_starter_ideas=True,
    ),
    "momentum_focus": StrategyProfile(
        name="momentum_focus",
        description="Constructive-regime profile built around strong conviction and market alignment.",
        policy_bundle=("risk_on_only", "combined", "regime_aligned"),
        preferred_conviction_bands=("normal", "high_conviction"),
        regime_preferences=("risk_on",),
        degraded_mode_tolerance="low",
        max_suggested_size_style="moderate",
        avoid_risk_off=True,
        allow_starter_ideas=False,
    ),
    "conservative_observe": StrategyProfile(
        name="conservative_observe",
        description="Observe-heavy profile for cautious experimentation, especially when data quality is weak.",
        policy_bundle=("baseline", "conservative_size_cap", "defensive_rotation", "degraded_safe_mode"),
        preferred_conviction_bands=("observe", "starter", "normal"),
        regime_preferences=("neutral", "risk_off", "high_volatility"),
        degraded_mode_tolerance="high_only",
        max_suggested_size_style="minimal",
        avoid_risk_off=False,
        allow_starter_ideas=True,
    ),
}


def list_policy_names() -> list[str]:
    return sorted(POLICY_REGISTRY.keys())


def list_profile_names() -> list[str]:
    return sorted(STRATEGY_PROFILES.keys())


def get_policy(name: str) -> PolicyDefinition:
    if name not in POLICY_REGISTRY:
        raise RuntimeError(f"Unsupported policy '{name}'")
    return POLICY_REGISTRY[name]


def get_profile(name: str) -> StrategyProfile:
    if name not in STRATEGY_PROFILES:
        raise RuntimeError(f"Unsupported profile '{name}'")
    return STRATEGY_PROFILES[name]


def resolve_requested_policies(
    *,
    policy_names: list[str] | None = None,
    profile_names: list[str] | None = None,
) -> tuple[list[PolicyDefinition], list[StrategyProfile]]:
    resolved_profiles = [get_profile(name) for name in (profile_names or [])]
    ordered_names: list[str] = []
    for name in policy_names or []:
        if name not in ordered_names:
            ordered_names.append(name)
    for profile in resolved_profiles:
        for name in profile.policy_bundle:
            if name not in ordered_names:
                ordered_names.append(name)
    resolved_policies = [get_policy(name) for name in ordered_names]
    return resolved_policies, resolved_profiles


def policy_filters_summary(policy: PolicyDefinition) -> list[str]:
    filters: list[str] = []
    if policy.min_conviction_rank is not None:
        filters.append(f"min_conviction_rank={policy.min_conviction_rank}")
    if policy.max_conviction_rank is not None:
        filters.append(f"max_conviction_rank={policy.max_conviction_rank}")
    if policy.min_confidence_score is not None:
        filters.append(f"min_confidence={policy.min_confidence_score:.2f}")
    if policy.allowed_regimes:
        filters.append("allow_regimes=" + ",".join(policy.allowed_regimes))
    if policy.blocked_regimes:
        filters.append("block_regimes=" + ",".join(policy.blocked_regimes))
    if policy.required_reliability:
        filters.append("require_reliability=" + ",".join(policy.required_reliability))
    if policy.blocked_reliability:
        filters.append("block_reliability=" + ",".join(policy.blocked_reliability))
    filters.append(f"degraded_mode={policy.degraded_mode_handling}")
    return filters


def policy_regime_preference_summary(policy: PolicyDefinition) -> str:
    if policy.regime_preferences:
        return ", ".join(policy.regime_preferences)
    if policy.allowed_regimes:
        return ", ".join(policy.allowed_regimes)
    if policy.blocked_regimes:
        return "all except " + ", ".join(policy.blocked_regimes)
    return "all"


def policy_degraded_compatibility(policy: PolicyDefinition) -> str:
    if policy.degraded_mode_handling == "require":
        return "degraded_only"
    if policy.degraded_mode_handling == "avoid":
        return "avoid_degraded"
    if policy.degraded_mode_handling == "cautious":
        return "cautious_in_degraded"
    return "compatible"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _passes_policy_filters(row: dict[str, Any], policy: PolicyDefinition) -> bool:
    conviction_rank = CONVICTION_RANK.get(str(row.get("conviction_band") or "observe"), 0)
    regime_label = str(row.get("regime_label") or "neutral")
    reliability = str(row.get("signal_reliability") or "unproven")
    degraded_mode = bool(row.get("degraded_mode"))
    confidence_score = _safe_float(row.get("confidence_score"))
    regime_data_quality = str(row.get("regime_data_quality") or "limited")
    allocation = _safe_float(row.get("normalized_allocation"))

    if policy.min_conviction_rank is not None and conviction_rank < policy.min_conviction_rank:
        return False
    if policy.max_conviction_rank is not None and conviction_rank > policy.max_conviction_rank:
        return False
    if policy.min_confidence_score is not None and confidence_score < policy.min_confidence_score:
        return False
    if policy.allowed_regimes and regime_label not in policy.allowed_regimes:
        return False
    if policy.blocked_regimes and regime_label in policy.blocked_regimes:
        return False
    if policy.required_reliability and reliability not in policy.required_reliability:
        return False
    if policy.blocked_reliability and reliability in policy.blocked_reliability:
        return False
    if regime_data_quality not in policy.minimum_data_quality:
        return False
    if policy.min_allocation is not None and allocation < policy.min_allocation:
        return False

    if policy.degraded_mode_handling == "avoid" and degraded_mode:
        return False
    if policy.degraded_mode_handling == "require" and not degraded_mode:
        return False
    if degraded_mode and policy.degraded_min_confidence_score is not None and confidence_score < policy.degraded_min_confidence_score:
        return False
    if degraded_mode and policy.degraded_required_reliability and reliability not in policy.degraded_required_reliability:
        return False
    return True


def apply_policy_definition(rows: list[dict[str, Any]], policy: PolicyDefinition) -> list[dict[str, Any]]:
    simulated: list[dict[str, Any]] = []
    for row in deepcopy(rows):
        if not _passes_policy_filters(row, policy):
            continue
        allocation = _safe_float(row.get("normalized_allocation"))
        allocation *= policy.allocation_multiplier
        if bool(row.get("degraded_mode")) and policy.degraded_allocation_multiplier is not None:
            allocation *= policy.degraded_allocation_multiplier
        if policy.max_allocation is not None:
            allocation = min(allocation, policy.max_allocation)
        row["simulated_allocation"] = round(allocation, 4)
        simulated.append(row)
    return simulated
