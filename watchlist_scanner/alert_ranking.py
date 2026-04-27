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
    *,
    approved_weights_config: dict[str, Any] | None = None,
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

    # augmented_priority_score: same formula but uses augmented_signal_score
    # (signal_score + soft theme contribution) as the signal component.
    # Kept separate so existing consumers of priority_score are unaffected.
    augmented_signal_score = float(
        signal.get("augmented_signal_score") or signal_score
    )
    augmented_priority_score = round(
        augmented_signal_score * float(cfg["signal_weight"])
        + confidence_score * float(cfg["confidence_weight"])
        + evidence_factor * float(cfg["evidence_weight"])
        + freshness_factor * float(cfg["freshness_weight"]),
        4,
    )

    # final_rank_score: holistic ordering score that blends signal quality,
    # confidence, theme alignment, and portfolio fit.
    # Used as a secondary sort tiebreaker — does NOT replace priority_score.
    # Uses approved weights from approved_ranking_config.json when valid;
    # falls back to hardcoded defaults otherwise.
    _using_approved = bool(
        approved_weights_config and approved_weights_config.get("_valid") is True
    )
    if _using_approved:
        _w = approved_weights_config["weights"]  # type: ignore[index]
    else:
        _w = {
            "augmented_signal_score": 0.40,
            "confidence_score": 0.25,
            "theme_alignment_score": 0.15,
            "portfolio_fit_score": 0.20,
        }
    portfolio_fit_score = float(signal.get("portfolio_fit_score") or 0.5)
    theme_alignment_score = float(signal.get("theme_alignment_score") or 0.0)
    final_rank_score = round(
        augmented_signal_score * _w["augmented_signal_score"]
        + confidence_score * _w["confidence_score"]
        + theme_alignment_score * _w["theme_alignment_score"]
        + portfolio_fit_score * _w["portfolio_fit_score"],
        4,
    )

    signal["priority_score"] = priority_score
    signal["augmented_priority_score"] = augmented_priority_score
    signal["final_rank_score"] = final_rank_score
    signal["priority_explanation"] = build_priority_explanation(signal)
    signal["final_rank_weights_source"] = "approved" if _using_approved else "default"
    signal["final_rank_weights_candidate"] = (
        approved_weights_config.get("recommended_candidate")  # type: ignore[union-attr]
        if _using_approved
        else "default"
    )
    signal["final_rank_weights_approved_at"] = (
        approved_weights_config.get("approved_at")  # type: ignore[union-attr]
        if _using_approved
        else None
    )
    signal["final_rank_weight_config_valid"] = _using_approved
    return signal
