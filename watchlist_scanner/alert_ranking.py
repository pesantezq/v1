from __future__ import annotations

from typing import Any


DEFAULT_RANKING = {
    "signal_weight": 0.45,
    "confidence_weight": 0.30,
    "evidence_weight": 0.15,
    "freshness_weight": 0.10,
}

FRESHNESS_FACTORS = {
    "fresh": 1.00,
    "partial": 0.80,
    "budget_skipped": 0.60,
    "cached": 0.45,
}


def _ranking_cfg(ranking_config: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(DEFAULT_RANKING)
    cfg.update(ranking_config or {})
    return cfg


def _evidence_factor(signal: dict[str, Any]) -> float:
    count = int(signal.get("evidence_count") or signal.get("evidence_breadth") or 0)
    return min(1.0, count / 3.0)


def _freshness_factor(signal: dict[str, Any]) -> float:
    return float(FRESHNESS_FACTORS.get(str(signal.get("data_quality") or "fresh"), 0.45))


def build_priority_explanation(signal: dict[str, Any]) -> str:
    tier = str(signal.get("alert_tier") or "unknown")
    evidence = int(signal.get("evidence_count") or signal.get("evidence_breadth") or 0)
    confidence_score = float(signal.get("confidence_score") or 0.0)
    if tier == "high" and evidence >= 3:
        return f"High confidence + {evidence} reinforcing categories"
    if tier == "high":
        return f"High confidence with evidence count {evidence}"
    if tier == "medium":
        return f"Medium confidence, evidence threshold met ({evidence})"
    return f"Tier {tier}, confidence {confidence_score:.2f}, evidence {evidence}"


def apply_priority_score(
    signal: dict[str, Any],
    ranking_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = _ranking_cfg(ranking_config)
    signal_score = float(signal.get("signal_score") or 0.0)
    confidence_score = float(signal.get("confidence_score") or 0.0)
    evidence_factor = _evidence_factor(signal)
    freshness_factor = _freshness_factor(signal)

    priority_score = round(
        signal_score * float(cfg["signal_weight"])
        + confidence_score * float(cfg["confidence_weight"])
        + evidence_factor * float(cfg["evidence_weight"])
        + freshness_factor * float(cfg["freshness_weight"]),
        4,
    )

    signal["priority_score"] = priority_score
    signal["priority_explanation"] = build_priority_explanation(signal)
    return signal
