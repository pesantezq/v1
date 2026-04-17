from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.policy_framework import (
    POLICY_REGISTRY,
    STRATEGY_PROFILES,
    PolicyDefinition,
    StrategyProfile,
    list_policy_names,
    list_profile_names,
)


DEFAULT_CONTEXT_JSON = Path("outputs/latest/watchlist_signals.json")
DEFAULT_REGIME_JSON = Path("outputs/regime/regime_performance.json")
DEFAULT_SIMULATION_JSON = Path("outputs/simulations/policy_simulation.json")
DEFAULT_OUTPUT_DIR = Path("outputs/policy")

LOW_REGIME_CONFIDENCE = 0.60
SUPPORTED_POLICY_HISTORY = 4
SUPPORTED_REGIME_HISTORY = 2
SUPPORTED_REGIME_SAMPLE = 3

AGGRESSIVE_POLICY_NAMES = {
    "combined",
    "high_conviction_only",
    "high_quality_concentrated",
    "risk_on_only",
}
AGGRESSIVE_PROFILE_NAMES = {"aggressive_growth", "momentum_focus"}
LOW_CONFIDENCE_POLICY_NAMES = {
    "quality_growth",
    "regime_aligned",
    "starter_plus",
    "conservative_size_cap",
    "degraded_safe_mode",
}
LOW_CONFIDENCE_PROFILE_NAMES = {
    "balanced_growth",
    "defensive_quality",
    "conservative_observe",
}
DEGRADED_SAFE_POLICY_NAMES = {
    "conservative_size_cap",
    "defensive_rotation",
    "degraded_safe_mode",
    "quality_growth",
}
DEGRADED_SAFE_PROFILE_NAMES = {
    "conservative_observe",
    "defensive_quality",
}

REGIME_POLICY_PRIORS: dict[str, dict[str, float]] = {
    "risk_on": {
        "quality_growth": 0.93,
        "regime_aligned": 0.90,
        "high_quality_concentrated": 0.86,
        "combined": 0.82,
        "risk_on_only": 0.80,
        "starter_plus": 0.72,
        "baseline": 0.58,
        "avoid_risk_off": 0.52,
        "conservative_size_cap": 0.40,
        "defensive_rotation": 0.30,
        "degraded_safe_mode": 0.12,
    },
    "neutral": {
        "quality_growth": 0.88,
        "regime_aligned": 0.84,
        "starter_plus": 0.76,
        "defensive_rotation": 0.74,
        "avoid_risk_off": 0.70,
        "conservative_size_cap": 0.68,
        "high_quality_concentrated": 0.62,
        "baseline": 0.60,
        "combined": 0.42,
        "degraded_safe_mode": 0.28,
        "risk_on_only": 0.25,
    },
    "risk_off": {
        "defensive_rotation": 0.94,
        "conservative_size_cap": 0.83,
        "degraded_safe_mode": 0.62,
        "baseline": 0.50,
        "starter_plus": 0.48,
        "quality_growth": 0.22,
        "avoid_risk_off": 0.20,
        "regime_aligned": 0.18,
        "high_quality_concentrated": 0.18,
        "combined": 0.12,
        "risk_on_only": 0.05,
    },
    "high_volatility": {
        "degraded_safe_mode": 0.92,
        "conservative_size_cap": 0.86,
        "defensive_rotation": 0.80,
        "baseline": 0.48,
        "starter_plus": 0.38,
        "quality_growth": 0.30,
        "avoid_risk_off": 0.26,
        "regime_aligned": 0.24,
        "high_quality_concentrated": 0.12,
        "combined": 0.08,
        "risk_on_only": 0.05,
    },
}

REGIME_PROFILE_PRIORS: dict[str, dict[str, float]] = {
    "risk_on": {
        "balanced_growth": 0.93,
        "aggressive_growth": 0.90,
        "momentum_focus": 0.88,
        "defensive_quality": 0.45,
        "conservative_observe": 0.35,
    },
    "neutral": {
        "balanced_growth": 0.90,
        "defensive_quality": 0.78,
        "conservative_observe": 0.72,
        "aggressive_growth": 0.55,
        "momentum_focus": 0.48,
    },
    "risk_off": {
        "defensive_quality": 0.94,
        "conservative_observe": 0.90,
        "balanced_growth": 0.55,
        "aggressive_growth": 0.25,
        "momentum_focus": 0.18,
    },
    "high_volatility": {
        "conservative_observe": 0.96,
        "defensive_quality": 0.86,
        "balanced_growth": 0.60,
        "aggressive_growth": 0.18,
        "momentum_focus": 0.12,
    },
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _parse_optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    return None


def _read_json_if_exists(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _extract_current_context(
    payload: dict[str, Any] | None,
    *,
    regime_label: str | None = None,
    regime_confidence: float | None = None,
    degraded_mode: bool | None = None,
    degraded_reason: str | None = None,
) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    regime_source = source.get("market_regime") if isinstance(source.get("market_regime"), dict) else source
    data_health = source.get("data_health") if isinstance(source.get("data_health"), dict) else {}

    resolved_regime = str(
        regime_label
        or regime_source.get("regime_label")
        or ""
    ).strip().lower()
    if not resolved_regime:
        raise RuntimeError(
            "Current regime is required. Provide --regime or a context JSON with market_regime.regime_label."
        )

    resolved_confidence = _safe_float(
        regime_confidence if regime_confidence is not None else regime_source.get("regime_confidence"),
        0.45,
    )
    resolved_degraded = (
        degraded_mode
        if degraded_mode is not None else bool(
            source.get("degraded_mode", data_health.get("degraded_mode", False))
        )
    )
    resolved_reason = (
        degraded_reason
        or source.get("degraded_reason")
        or data_health.get("degraded_reason")
        or (regime_source.get("regime_inputs") or {}).get("degraded_reason")
        or (("unknown" if resolved_degraded else None))
    )

    return {
        "regime_label": resolved_regime,
        "regime_confidence": round(_clamp(resolved_confidence), 3),
        "degraded_mode": bool(resolved_degraded),
        "degraded_reason": resolved_reason,
        "regime_data_quality": str(regime_source.get("regime_data_quality") or "limited"),
        "regime_reasoning": str(regime_source.get("regime_reasoning") or ""),
        "context_source": (
            str(source.get("generated_at") or source.get("scan_summary", {}).get("scan_status") or "manual_or_file")
            if isinstance(source, dict) else "manual_or_file"
        ),
    }


def _policy_alignment_score(policy: PolicyDefinition, regime_label: str) -> float:
    score = REGIME_POLICY_PRIORS.get(regime_label, {}).get(policy.name, 0.45)
    if regime_label in policy.allowed_regimes:
        score += 0.08
    if regime_label in policy.blocked_regimes:
        score -= 0.35
    if regime_label in policy.regime_preferences:
        score += 0.05
    return round(_clamp(score), 3)


def _profile_alignment_score(profile: StrategyProfile, regime_label: str) -> float:
    score = REGIME_PROFILE_PRIORS.get(regime_label, {}).get(profile.name, 0.45)
    if regime_label in profile.regime_preferences:
        score += 0.06
    return round(_clamp(score), 3)


def _policy_safety_score(policy: PolicyDefinition, context: dict[str, Any]) -> float:
    degraded_mode = bool(context.get("degraded_mode"))
    regime_confidence = _safe_float(context.get("regime_confidence"), 0.45)

    score = 0.58
    if degraded_mode:
        if policy.degraded_mode_handling == "require":
            score += 0.34
        elif policy.degraded_mode_handling == "cautious":
            score += 0.20
        elif policy.degraded_mode_handling == "avoid":
            score -= 0.45
        if policy.name in DEGRADED_SAFE_POLICY_NAMES:
            score += 0.12
    else:
        if policy.degraded_mode_handling == "require":
            score -= 0.35
        elif policy.degraded_mode_handling == "avoid":
            score += 0.08
        elif policy.degraded_mode_handling == "cautious":
            score += 0.03

    if regime_confidence < LOW_REGIME_CONFIDENCE:
        if policy.name in AGGRESSIVE_POLICY_NAMES:
            score -= 0.16
        if policy.name in LOW_CONFIDENCE_POLICY_NAMES:
            score += 0.08

    return round(_clamp(score), 3)


def _profile_safety_score(profile: StrategyProfile, context: dict[str, Any]) -> float:
    degraded_mode = bool(context.get("degraded_mode"))
    regime_confidence = _safe_float(context.get("regime_confidence"), 0.45)
    tolerance = str(profile.degraded_mode_tolerance or "")

    base = {
        "high_only": 0.92 if degraded_mode else 0.55,
        "very_low": 0.86 if degraded_mode else 0.64,
        "medium": 0.66 if degraded_mode else 0.74,
        "low": 0.36 if degraded_mode else 0.78,
    }.get(tolerance, 0.60)

    if degraded_mode and profile.name in DEGRADED_SAFE_PROFILE_NAMES:
        base += 0.10
    if regime_confidence < LOW_REGIME_CONFIDENCE:
        if profile.name in AGGRESSIVE_PROFILE_NAMES:
            base -= 0.14
        if profile.name in LOW_CONFIDENCE_PROFILE_NAMES:
            base += 0.08

    return round(_clamp(base), 3)


def _avg_return_component(avg_return_pct: float | None) -> float:
    if avg_return_pct is None:
        return 0.50
    return round(_clamp(0.5 + (avg_return_pct / 10.0)), 3)


def _drawdown_component(max_drawdown_pct: float | None) -> float:
    if max_drawdown_pct is None:
        return 0.50
    return round(_clamp(1.0 - (max_drawdown_pct / 15.0)), 3)


def _policy_performance_support(
    policy_name: str,
    simulation_summary: dict[str, Any] | None,
    *,
    regime_label: str,
) -> dict[str, Any]:
    if not isinstance(simulation_summary, dict):
        return {
            "availability": "missing",
            "score": None,
            "total_trades": 0,
            "regime_trades": 0,
            "win_rate": None,
            "avg_return_pct": None,
            "max_drawdown_pct": None,
            "regime_win_rate": None,
            "regime_avg_return_pct": None,
        }

    policy_rows = {
        str(item.get("policy") or ""): item
        for item in (simulation_summary.get("policies") or [])
        if isinstance(item, dict)
    }
    policy_row = policy_rows.get(policy_name)
    if not isinstance(policy_row, dict):
        return {
            "availability": "missing",
            "score": None,
            "total_trades": 0,
            "regime_trades": 0,
            "win_rate": None,
            "avg_return_pct": None,
            "max_drawdown_pct": None,
            "regime_win_rate": None,
            "regime_avg_return_pct": None,
        }

    total_trades = int(_safe_float(policy_row.get("total_trades"), 0))
    win_rate = _safe_float(policy_row.get("win_rate"), 0.0)
    avg_return_pct = _safe_float(policy_row.get("avg_return_pct"), 0.0)
    max_drawdown_pct = _safe_float(policy_row.get("max_drawdown_pct"), 0.0)
    regime_row = (policy_row.get("performance_by_regime") or {}).get(regime_label)
    regime_trades = int(_safe_float((regime_row or {}).get("total_trades"), 0))
    regime_win_rate = _safe_float((regime_row or {}).get("win_rate"), 0.0) if regime_row else None
    regime_avg_return_pct = _safe_float((regime_row or {}).get("avg_return_pct"), 0.0) if regime_row else None

    overall_score = (
        (0.50 * _clamp(win_rate))
        + (0.25 * _avg_return_component(avg_return_pct))
        + (0.25 * _drawdown_component(max_drawdown_pct))
    )
    regime_score = None
    if regime_row:
        regime_score = (
            (0.60 * _clamp(regime_win_rate or 0.0))
            + (0.40 * _avg_return_component(regime_avg_return_pct))
        )

    if regime_score is not None:
        score = (0.65 * overall_score) + (0.35 * regime_score)
    else:
        score = overall_score

    availability = "supported"
    if total_trades < SUPPORTED_POLICY_HISTORY or regime_trades < SUPPORTED_REGIME_HISTORY:
        availability = "sparse"

    return {
        "availability": availability,
        "score": round(_clamp(score), 3),
        "total_trades": total_trades,
        "regime_trades": regime_trades,
        "win_rate": round(_clamp(win_rate), 3),
        "avg_return_pct": round(avg_return_pct, 3),
        "max_drawdown_pct": round(max_drawdown_pct, 3),
        "regime_win_rate": round(_clamp(regime_win_rate), 3) if regime_row else None,
        "regime_avg_return_pct": round(regime_avg_return_pct or 0.0, 3) if regime_row else None,
    }


def _profile_performance_support(
    profile: StrategyProfile,
    simulation_summary: dict[str, Any] | None,
    *,
    regime_label: str,
) -> dict[str, Any]:
    policy_support = [
        _policy_performance_support(name, simulation_summary, regime_label=regime_label)
        for name in profile.policy_bundle
    ]
    supported = [item for item in policy_support if item.get("availability") == "supported" and item.get("score") is not None]
    sparse = [item for item in policy_support if item.get("availability") == "sparse" and item.get("score") is not None]

    if supported:
        score = sum(float(item["score"]) for item in supported) / len(supported)
        availability = "supported"
    elif sparse:
        score = sum(float(item["score"]) for item in sparse) / len(sparse)
        availability = "sparse"
    else:
        score = None
        availability = "missing"

    return {
        "availability": availability,
        "score": round(score, 3) if score is not None else None,
        "bundle_policies": list(profile.policy_bundle),
    }


def _policy_reasoning_lines(
    policy: PolicyDefinition,
    context: dict[str, Any],
    performance: dict[str, Any],
) -> list[str]:
    regime_label = str(context.get("regime_label") or "neutral")
    lines = [f"{policy.name} maps well to the current `{regime_label}` regime."]
    if bool(context.get("degraded_mode")) and policy.name in DEGRADED_SAFE_POLICY_NAMES:
        lines.append("Degraded mode is active, so defensive/degraded-safe handling gets extra weight.")
    if _safe_float(context.get("regime_confidence"), 0.45) < LOW_REGIME_CONFIDENCE:
        lines.append("Regime confidence is modest, so the scorer leans away from aggressive filters.")
    if performance.get("availability") == "supported":
        lines.append(
            f"Simulation support is available: {float(performance.get('win_rate') or 0.0):.1%} win rate, "
            f"{float(performance.get('avg_return_pct') or 0.0):+.2f}% average return, "
            f"{float(performance.get('max_drawdown_pct') or 0.0):.2f}% max drawdown."
        )
    elif performance.get("availability") == "sparse":
        lines.append("Simulation history exists but is sparse, so rule-based regime mapping still drives much of the recommendation.")
    else:
        lines.append("No usable policy simulation history was found, so the score relies on rule-based regime alignment.")
    return lines


def _profile_reasoning_lines(
    profile: StrategyProfile,
    context: dict[str, Any],
    performance: dict[str, Any],
) -> list[str]:
    regime_label = str(context.get("regime_label") or "neutral")
    lines = [f"{profile.name} fits the current `{regime_label}` backdrop and the registry's intended use case."]
    if bool(context.get("degraded_mode")) and profile.name in DEGRADED_SAFE_PROFILE_NAMES:
        lines.append("The profile is intentionally conservative enough for degraded-data conditions.")
    if _safe_float(context.get("regime_confidence"), 0.45) < LOW_REGIME_CONFIDENCE and profile.name in LOW_CONFIDENCE_PROFILE_NAMES:
        lines.append("Lower regime confidence favors balanced or conservative profiles over aggressive concentration.")
    if performance.get("availability") == "supported":
        lines.append("Its policy bundle also has enough simulation history to provide supporting evidence.")
    elif performance.get("availability") == "sparse":
        lines.append("Its policy bundle has only shallow simulation history, so confidence is limited.")
    return lines


def _score_policy_candidate(
    policy: PolicyDefinition,
    context: dict[str, Any],
    simulation_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    regime_label = str(context.get("regime_label") or "neutral")
    alignment_score = _policy_alignment_score(policy, regime_label)
    safety_score = _policy_safety_score(policy, context)
    performance = _policy_performance_support(policy.name, simulation_summary, regime_label=regime_label)

    if performance.get("availability") == "supported":
        recommendation_score = (
            (0.45 * alignment_score)
            + (0.35 * float(performance.get("score") or 0.0))
            + (0.20 * safety_score)
        )
    elif performance.get("availability") == "sparse":
        recommendation_score = (
            (0.60 * alignment_score)
            + (0.15 * float(performance.get("score") or 0.0))
            + (0.25 * safety_score)
        )
    else:
        recommendation_score = (
            (0.70 * alignment_score)
            + (0.30 * safety_score)
        )
    if bool(context.get("degraded_mode")):
        if policy.name in DEGRADED_SAFE_POLICY_NAMES:
            recommendation_score += 0.08
        elif policy.name in AGGRESSIVE_POLICY_NAMES:
            recommendation_score -= 0.10

    return {
        "name": policy.name,
        "kind": "policy",
        "recommendation_score": round(_clamp(recommendation_score), 3),
        "regime_alignment_score": alignment_score,
        "performance_support_score": performance.get("score"),
        "performance_support_availability": performance.get("availability"),
        "safety_score": safety_score,
        "reasoning": _policy_reasoning_lines(policy, context, performance),
        "inputs": {
            "regime_label": regime_label,
            "regime_confidence": round(_safe_float(context.get("regime_confidence"), 0.45), 3),
            "degraded_mode": bool(context.get("degraded_mode")),
            "total_trades": int(performance.get("total_trades") or 0),
            "regime_trades": int(performance.get("regime_trades") or 0),
        },
        "performance": performance,
    }


def _score_profile_candidate(
    profile: StrategyProfile,
    context: dict[str, Any],
    simulation_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    regime_label = str(context.get("regime_label") or "neutral")
    alignment_score = _profile_alignment_score(profile, regime_label)
    safety_score = _profile_safety_score(profile, context)
    performance = _profile_performance_support(profile, simulation_summary, regime_label=regime_label)

    if performance.get("availability") == "supported":
        recommendation_score = (
            (0.50 * alignment_score)
            + (0.30 * float(performance.get("score") or 0.0))
            + (0.20 * safety_score)
        )
    elif performance.get("availability") == "sparse":
        recommendation_score = (
            (0.60 * alignment_score)
            + (0.15 * float(performance.get("score") or 0.0))
            + (0.25 * safety_score)
        )
    else:
        recommendation_score = (
            (0.72 * alignment_score)
            + (0.28 * safety_score)
        )
    if bool(context.get("degraded_mode")):
        if profile.name in DEGRADED_SAFE_PROFILE_NAMES:
            recommendation_score += 0.20
        elif profile.name in AGGRESSIVE_PROFILE_NAMES:
            recommendation_score -= 0.12
        else:
            recommendation_score -= 0.08

    return {
        "name": profile.name,
        "kind": "profile",
        "recommendation_score": round(_clamp(recommendation_score), 3),
        "regime_alignment_score": alignment_score,
        "performance_support_score": performance.get("score"),
        "performance_support_availability": performance.get("availability"),
        "safety_score": safety_score,
        "reasoning": _profile_reasoning_lines(profile, context, performance),
        "inputs": {
            "regime_label": regime_label,
            "regime_confidence": round(_safe_float(context.get("regime_confidence"), 0.45), 3),
            "degraded_mode": bool(context.get("degraded_mode")),
            "bundle_policies": list(profile.policy_bundle),
        },
        "performance": performance,
    }


def _rank_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        entries,
        key=lambda item: (
            float(item.get("recommendation_score") or 0.0),
            float(item.get("regime_alignment_score") or 0.0),
            float(item.get("safety_score") or 0.0),
        ),
        reverse=True,
    )


def _recommendation_source(
    context: dict[str, Any],
    policy_entry: dict[str, Any],
    profile_entry: dict[str, Any],
) -> str:
    if bool(context.get("degraded_mode")) and (
        policy_entry.get("name") in DEGRADED_SAFE_POLICY_NAMES
        or profile_entry.get("name") in DEGRADED_SAFE_PROFILE_NAMES
    ):
        return "degraded_mode_override"
    if (
        policy_entry.get("performance_support_availability") == "supported"
        or profile_entry.get("performance_support_availability") == "supported"
    ):
        return "performance_backed_logic"
    return "rule_based_fallback"


def _recommendation_data_quality(
    source: str,
    *,
    policy_entry: dict[str, Any],
    regime_performance: dict[str, Any] | None,
    regime_label: str,
) -> tuple[str, str | None]:
    regime_row = ((regime_performance or {}).get("by_regime") or {}).get(regime_label) if isinstance(regime_performance, dict) else {}
    regime_sample = int(_safe_float((regime_row or {}).get("total_signals"), 0))
    availability = str(policy_entry.get("performance_support_availability") or "missing")

    if source == "performance_backed_logic" and availability == "supported" and regime_sample >= SUPPORTED_REGIME_SAMPLE:
        return "performance_backed", None
    if availability == "sparse":
        return "sparse_simulation_history", "Recommendation confidence is limited due to sparse policy simulation history."
    if regime_sample < SUPPORTED_REGIME_SAMPLE:
        return "limited_regime_history", "Recommendation confidence is limited because current-regime outcome history is shallow."
    return "rule_based_limited_data", "Recommendation confidence is limited because recent policy support data is unavailable."


def _recommendation_confidence(
    context: dict[str, Any],
    policy_ranking: list[dict[str, Any]],
    profile_ranking: list[dict[str, Any]],
    *,
    source: str,
    data_quality: str,
) -> float:
    regime_confidence = _safe_float(context.get("regime_confidence"), 0.45)
    policy_gap = 0.10
    if len(policy_ranking) >= 2:
        policy_gap = max(
            0.0,
            float(policy_ranking[0].get("recommendation_score") or 0.0)
            - float(policy_ranking[1].get("recommendation_score") or 0.0),
        )
    profile_gap = 0.10
    if len(profile_ranking) >= 2:
        profile_gap = max(
            0.0,
            float(profile_ranking[0].get("recommendation_score") or 0.0)
            - float(profile_ranking[1].get("recommendation_score") or 0.0),
        )

    base = 0.45 * regime_confidence
    if source == "performance_backed_logic":
        base += 0.24
    elif source == "degraded_mode_override":
        base += 0.18
    else:
        base += 0.10
    base += min(policy_gap + profile_gap, 0.20)

    if data_quality == "sparse_simulation_history":
        base -= 0.08
    elif data_quality == "limited_regime_history":
        base -= 0.06
    elif data_quality == "rule_based_limited_data":
        base -= 0.10

    if bool(context.get("degraded_mode")):
        base -= 0.03

    return round(_clamp(base), 3)


def _build_support_block(
    *,
    context: dict[str, Any],
    policy_ranking: list[dict[str, Any]],
    profile_ranking: list[dict[str, Any]],
    regime_performance: dict[str, Any] | None,
    simulation_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    regime_label = str(context.get("regime_label") or "neutral")
    simulation_comparison = dict((simulation_summary or {}).get("comparison") or {}) if isinstance(simulation_summary, dict) else {}
    regime_row = ((regime_performance or {}).get("by_regime") or {}).get(regime_label) if isinstance(regime_performance, dict) else None

    regime_supported_policies = [item["name"] for item in policy_ranking[:4]]
    degraded_safe_policies = [
        item["name"]
        for item in policy_ranking
        if item["name"] in DEGRADED_SAFE_POLICY_NAMES
    ][:4]

    return {
        "regime_supported_policies": regime_supported_policies,
        "degraded_safe_policies": degraded_safe_policies,
        "regime_supported_profiles": [item["name"] for item in profile_ranking[:3]],
        "best_recent_policy_by_win_rate": simulation_comparison.get("best_by_win_rate"),
        "best_recent_policy_by_drawdown": simulation_comparison.get("best_by_drawdown"),
        "best_recent_policy_for_current_regime": (simulation_comparison.get("best_policy_by_regime") or {}).get(regime_label),
        "current_regime_history": regime_row,
    }


def build_policy_recommendation(
    *,
    current_context: dict[str, Any],
    regime_performance: dict[str, Any] | None = None,
    policy_simulation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy_ranking = _rank_entries(
        [
            _score_policy_candidate(policy, current_context, policy_simulation)
            for policy in POLICY_REGISTRY.values()
        ]
    )
    profile_ranking = _rank_entries(
        [
            _score_profile_candidate(profile, current_context, policy_simulation)
            for profile in STRATEGY_PROFILES.values()
        ]
    )

    recommended_policy = policy_ranking[0]
    recommended_profile = profile_ranking[0]
    source = _recommendation_source(current_context, recommended_policy, recommended_profile)
    data_quality, quality_note = _recommendation_data_quality(
        source,
        policy_entry=recommended_policy,
        regime_performance=regime_performance,
        regime_label=str(current_context.get("regime_label") or "neutral"),
    )
    recommendation_confidence = _recommendation_confidence(
        current_context,
        policy_ranking,
        profile_ranking,
        source=source,
        data_quality=data_quality,
    )

    reasoning = [
        f"Current regime is `{current_context['regime_label']}` with confidence {float(current_context.get('regime_confidence') or 0.0):.2f}.",
        f"Recommended profile `{recommended_profile['name']}` and policy `{recommended_policy['name']}` lead on the transparent advisory score.",
        *recommended_policy.get("reasoning", [])[:2],
        *recommended_profile.get("reasoning", [])[:2],
    ]
    if quality_note:
        reasoning.append(quality_note)

    support = _build_support_block(
        context=current_context,
        policy_ranking=policy_ranking,
        profile_ranking=profile_ranking,
        regime_performance=regime_performance,
        simulation_summary=policy_simulation,
    )

    return {
        "generated_at": datetime.now().isoformat(),
        "formula": {
            "policy_score": (
                "Supported history: 0.45*regime_alignment + 0.35*performance_support + 0.20*safety. "
                "Sparse history: 0.60*regime_alignment + 0.15*performance_support + 0.25*safety. "
                "No usable history: 0.70*regime_alignment + 0.30*safety."
            ),
            "profile_score": (
                "Supported history: 0.50*regime_alignment + 0.30*bundle_performance + 0.20*safety. "
                "Sparse history: 0.60*regime_alignment + 0.15*bundle_performance + 0.25*safety. "
                "No usable history: 0.72*regime_alignment + 0.28*safety."
            ),
            "performance_support": (
                "Policy performance support combines win rate, average return, and max drawdown, "
                "with an extra regime-specific win-rate/return check when current-regime samples exist."
            ),
        },
        "current_context": {
            "regime_label": current_context["regime_label"],
            "regime_confidence": round(_safe_float(current_context.get("regime_confidence"), 0.0), 3),
            "degraded_mode": bool(current_context.get("degraded_mode")),
            "degraded_reason": current_context.get("degraded_reason"),
            "regime_data_quality": current_context.get("regime_data_quality", "limited"),
            "regime_reasoning": current_context.get("regime_reasoning", ""),
        },
        "recommendation": {
            "recommended_policy": recommended_policy["name"],
            "recommended_profile": recommended_profile["name"],
            "recommendation_score": round(
                (
                    float(recommended_policy.get("recommendation_score") or 0.0)
                    + float(recommended_profile.get("recommendation_score") or 0.0)
                ) / 2.0,
                3,
            ),
            "recommendation_confidence": recommendation_confidence,
            "recommendation_reasoning": reasoning,
            "recommendation_inputs": {
                "policy": recommended_policy["inputs"],
                "profile": recommended_profile["inputs"],
            },
            "recommendation_data_quality": data_quality,
            "recommendation_source": source,
            "recommendation_quality_note": quality_note,
        },
        "alternatives": {
            "policies": [
                {
                    "name": item["name"],
                    "recommendation_score": item["recommendation_score"],
                    "reasoning": item["reasoning"][:2],
                }
                for item in policy_ranking[1:4]
            ],
            "profiles": [
                {
                    "name": item["name"],
                    "recommendation_score": item["recommendation_score"],
                    "reasoning": item["reasoning"][:2],
                }
                for item in profile_ranking[1:4]
            ],
        },
        "policy_rankings": policy_ranking,
        "profile_rankings": profile_ranking,
        "support": support,
    }


def render_policy_recommendation_markdown(summary: dict[str, Any]) -> str:
    context = dict(summary.get("current_context") or {})
    recommendation = dict(summary.get("recommendation") or {})
    support = dict(summary.get("support") or {})
    policy_alternatives = list((summary.get("alternatives") or {}).get("policies") or [])
    profile_alternatives = list((summary.get("alternatives") or {}).get("profiles") or [])
    quality_note = recommendation.get("recommendation_quality_note")

    lines = [
        "# Policy Recommendation",
        "",
        f"Generated: {summary.get('generated_at', '')}  ",
        f"Regime: **{context.get('regime_label', 'unknown')}**  ",
        f"Regime confidence: **{float(context.get('regime_confidence') or 0.0):.2f}**  ",
        f"Degraded mode: **{'yes' if context.get('degraded_mode') else 'no'}**  ",
        "",
        "## Recommendation",
        "",
        f"- Recommended profile: `{recommendation.get('recommended_profile') or 'n/a'}`",
        f"- Recommended policy: `{recommendation.get('recommended_policy') or 'n/a'}`",
        f"- Recommendation score: {float(recommendation.get('recommendation_score') or 0.0):.2f}",
        f"- Recommendation confidence: {float(recommendation.get('recommendation_confidence') or 0.0):.2f}",
        f"- Source: `{recommendation.get('recommendation_source') or 'rule_based_fallback'}`",
        f"- Data quality: `{recommendation.get('recommendation_data_quality') or 'limited'}`",
    ]
    if quality_note:
        lines.append(f"- Note: {quality_note}")

    reasoning = list(recommendation.get("recommendation_reasoning") or [])
    if reasoning:
        lines += ["", "## Why", ""]
        lines.extend(f"- {line}" for line in reasoning)

    lines += ["", "## Alternatives", ""]
    if policy_alternatives:
        lines.append("Policy alternatives:")
        lines.extend(
            f"- `{item.get('name', '')}` ({float(item.get('recommendation_score') or 0.0):.2f})"
            for item in policy_alternatives
        )
    else:
        lines.append("- No policy alternatives available.")
    if profile_alternatives:
        lines.append("")
        lines.append("Profile alternatives:")
        lines.extend(
            f"- `{item.get('name', '')}` ({float(item.get('recommendation_score') or 0.0):.2f})"
            for item in profile_alternatives
        )

    lines += [
        "",
        "## Support",
        "",
        f"- Regime-supported policies: {', '.join(support.get('regime_supported_policies') or ['n/a'])}",
        f"- Degraded-safe policies: {', '.join(support.get('degraded_safe_policies') or ['n/a'])}",
        f"- Best recent policy by win rate: `{support.get('best_recent_policy_by_win_rate') or 'n/a'}`",
        f"- Best recent policy by drawdown: `{support.get('best_recent_policy_by_drawdown') or 'n/a'}`",
        f"- Best recent policy for current regime: `{support.get('best_recent_policy_for_current_regime') or 'n/a'}`",
        "",
        "## Formula",
        "",
        f"- Policy score: {summary.get('formula', {}).get('policy_score', '')}",
        f"- Profile score: {summary.get('formula', {}).get('profile_score', '')}",
        f"- Performance support: {summary.get('formula', {}).get('performance_support', '')}",
        "",
    ]
    return "\n".join(lines)


def run_policy_recommendation(
    *,
    input_context_json: Path = DEFAULT_CONTEXT_JSON,
    input_regime_json: Path = DEFAULT_REGIME_JSON,
    input_simulation_json: Path = DEFAULT_SIMULATION_JSON,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    regime_label: str | None = None,
    regime_confidence: float | None = None,
    degraded_mode: bool | None = None,
    degraded_reason: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    context_payload = _read_json_if_exists(input_context_json)
    regime_performance = _read_json_if_exists(input_regime_json)
    simulation_summary = _read_json_if_exists(input_simulation_json)

    current_context = _extract_current_context(
        context_payload,
        regime_label=regime_label,
        regime_confidence=regime_confidence,
        degraded_mode=degraded_mode,
        degraded_reason=degraded_reason,
    )
    summary = build_policy_recommendation(
        current_context=current_context,
        regime_performance=regime_performance,
        policy_simulation=simulation_summary,
    )

    if dry_run:
        return summary

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "policy_recommendation.json"
    md_path = output_dir / "policy_recommendation.md"
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    md_path.write_text(render_policy_recommendation_markdown(summary), encoding="utf-8")
    summary["paths"] = {
        "json_path": str(json_path),
        "markdown_path": str(md_path),
    }
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Advisory-only policy and strategy-profile recommender.")
    parser.add_argument("--regime", default=None, help="Current regime label override.")
    parser.add_argument("--regime-confidence", type=float, default=None, help="Current regime confidence override.")
    parser.add_argument("--degraded-mode", default=None, help="Override degraded mode (true/false).")
    parser.add_argument("--degraded-reason", default=None, help="Optional degraded-mode reason override.")
    parser.add_argument("--input-context-json", default=str(DEFAULT_CONTEXT_JSON), help="Optional context JSON path (watchlist_signals-style payload).")
    parser.add_argument("--input-regime-json", default=str(DEFAULT_REGIME_JSON), help="Regime performance JSON path.")
    parser.add_argument("--input-simulation-json", default=str(DEFAULT_SIMULATION_JSON), help="Policy simulation JSON path.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for recommendation artifacts.")
    parser.add_argument("--dry-run", action="store_true", help="Print the advisory recommendation without writing files.")
    parser.add_argument("--list-policies", action="store_true", help="Print available policy names and exit.")
    parser.add_argument("--list-profiles", action="store_true", help="Print available profile names and exit.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.list_policies:
        for name in list_policy_names():
            print(name)
        return
    if args.list_profiles:
        for name in list_profile_names():
            print(name)
        return

    summary = run_policy_recommendation(
        input_context_json=Path(args.input_context_json),
        input_regime_json=Path(args.input_regime_json),
        input_simulation_json=Path(args.input_simulation_json),
        output_dir=Path(args.output_dir),
        regime_label=args.regime,
        regime_confidence=args.regime_confidence,
        degraded_mode=_parse_optional_bool(args.degraded_mode),
        degraded_reason=args.degraded_reason,
        dry_run=bool(args.dry_run),
    )
    if args.dry_run:
        print(render_policy_recommendation_markdown(summary))
        return

    print(f"Policy recommendation written: {summary['paths']['json_path']}")
    print(f"                             {summary['paths']['markdown_path']}")


if __name__ == "__main__":
    main()
