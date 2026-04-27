from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from watchlist_scanner.state import WatchlistStateStore

logger = logging.getLogger("watchlist_scanner.weight_tuning")

_MIN_RECOMMENDATION_SAMPLE = 20

CURRENT_WEIGHTS: dict[str, float] = {
    "augmented_signal_score": 0.40,
    "confidence_score": 0.25,
    "theme_alignment_score": 0.15,
    "portfolio_fit_score": 0.20,
}

CANDIDATE_WEIGHTS: list[dict[str, Any]] = [
    {
        "name": "current",
        "weights": {
            "augmented_signal_score": 0.40,
            "confidence_score": 0.25,
            "theme_alignment_score": 0.15,
            "portfolio_fit_score": 0.20,
        },
    },
    {
        "name": "theme_heavy",
        "weights": {
            "augmented_signal_score": 0.30,
            "confidence_score": 0.20,
            "theme_alignment_score": 0.35,
            "portfolio_fit_score": 0.15,
        },
    },
    {
        "name": "portfolio_fit_heavy",
        "weights": {
            "augmented_signal_score": 0.30,
            "confidence_score": 0.20,
            "theme_alignment_score": 0.10,
            "portfolio_fit_score": 0.40,
        },
    },
    {
        "name": "confidence_heavy",
        "weights": {
            "augmented_signal_score": 0.30,
            "confidence_score": 0.45,
            "theme_alignment_score": 0.10,
            "portfolio_fit_score": 0.15,
        },
    },
    {
        "name": "signal_heavy",
        "weights": {
            "augmented_signal_score": 0.55,
            "confidence_score": 0.20,
            "theme_alignment_score": 0.15,
            "portfolio_fit_score": 0.10,
        },
    },
    {
        "name": "balanced",
        "weights": {
            "augmented_signal_score": 0.25,
            "confidence_score": 0.25,
            "theme_alignment_score": 0.25,
            "portfolio_fit_score": 0.25,
        },
    },
]


def _compute_simulated_rank(row: dict[str, Any], weights: dict[str, float]) -> float:
    aug = float(row.get("augmented_signal_score") or row.get("signal_score") or 0.0)
    conf = float(row.get("confidence_score") or 0.0)
    theme = float(row.get("theme_alignment_score") or 0.0)
    # Match alert_ranking.py default: treat missing portfolio_fit as 0.5 (neutral)
    fit_raw = row.get("portfolio_fit_score")
    fit = float(fit_raw) if fit_raw is not None else 0.5
    return round(
        aug * weights["augmented_signal_score"]
        + conf * weights["confidence_score"]
        + theme * weights["theme_alignment_score"]
        + fit * weights["portfolio_fit_score"],
        4,
    )


def _evaluate_candidate(
    rows: list[dict[str, Any]],
    candidate: dict[str, Any],
    *,
    primary_window_days: int = 3,
) -> dict[str, Any]:
    weights = candidate["weights"]
    return_col = f"outcome_return_{primary_window_days}d"
    success_col = f"outcome_success_{primary_window_days}d"
    direction_col = f"direction_correct_{primary_window_days}d"

    sorted_rows = sorted(
        rows,
        key=lambda r: _compute_simulated_rank(r, weights),
        reverse=True,
    )
    n = len(sorted_rows)
    q_size = max(1, n // 4)
    top_quartile = sorted_rows[:q_size]
    resolved_top = [r for r in top_quartile if r.get(return_col) is not None]
    sample_size = len(resolved_top)

    if not resolved_top:
        return {
            "name": candidate["name"],
            "weights": weights,
            "top_quartile_avg_return": None,
            "top_quartile_hit_rate": None,
            "top_quartile_direction_correct_rate": None,
            "sample_size": 0,
            "low_sample_warning": True,
        }

    avg_return = round(
        sum(float(r.get(return_col) or 0.0) for r in resolved_top) / sample_size, 3
    )
    hit_rate = round(
        sum(1 for r in resolved_top if int(r.get(success_col) or 0) == 1) / sample_size, 3
    )
    dir_correct = round(
        sum(1 for r in resolved_top if int(r.get(direction_col) or 0) == 1) / sample_size, 3
    )
    return {
        "name": candidate["name"],
        "weights": weights,
        "top_quartile_avg_return": avg_return,
        "top_quartile_hit_rate": hit_rate,
        "top_quartile_direction_correct_rate": dir_correct,
        "sample_size": sample_size,
        "low_sample_warning": sample_size < _MIN_RECOMMENDATION_SAMPLE,
    }


def _select_recommendation(
    evaluated: list[dict[str, Any]],
) -> tuple[str, str]:
    if not evaluated:
        return "current", "Insufficient data — defaulting to current weights"

    sufficient = [c for c in evaluated if not c.get("low_sample_warning")]
    pool = sufficient if sufficient else evaluated

    sortable = [c for c in pool if c.get("top_quartile_hit_rate") is not None]
    if not sortable:
        return "current", "No resolved outcomes available to evaluate candidates"

    best = max(
        sortable,
        key=lambda c: (
            float(c.get("top_quartile_hit_rate") or 0.0),
            float(c.get("top_quartile_avg_return") or 0.0),
        ),
    )

    name = best["name"]
    hit = best["top_quartile_hit_rate"]
    avg_ret = best["top_quartile_avg_return"]
    n = best["sample_size"]

    if not sufficient:
        reason = (
            f"Best top-quartile hit rate ({hit:.1%}) across thin samples "
            "(all candidates below 20 resolved); treat as directional only"
        )
    else:
        reason = (
            f"Best top-quartile hit rate ({hit:.1%}, avg return {avg_ret:+.2f}%) "
            f"across {n} resolved signals"
        )
    return name, reason


def build_weight_tuning_suggestions(
    rows: list[dict[str, Any]],
    *,
    primary_window_days: int = 3,
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Evaluate candidate weight blends against historical resolved signal feedback.

    Observe-only — returns a recommendation dict without mutating any config.
    """
    candidate_list = candidates if candidates is not None else CANDIDATE_WEIGHTS
    evaluated = [
        _evaluate_candidate(rows, c, primary_window_days=primary_window_days)
        for c in candidate_list
    ]
    recommended_name, reason = _select_recommendation(evaluated)
    return_col = f"outcome_return_{primary_window_days}d"
    return {
        "generated_at": datetime.now().isoformat(),
        "observe_only": True,
        "primary_window_days": primary_window_days,
        "total_rows": len(rows),
        "resolved_rows": sum(1 for r in rows if r.get(return_col) is not None),
        "current_weights": CURRENT_WEIGHTS,
        "recommended_candidate": recommended_name,
        "recommendation_reason": reason,
        "candidates": evaluated,
    }


def generate_weight_tuning_report(
    *,
    db_path: str | Path = "data/portfolio.db",
    output_dir: str | Path = "outputs/performance",
    primary_window_days: int = 3,
) -> dict[str, Any]:
    """Load signal feedback, evaluate candidates, write weight_tuning_suggestions.json."""
    store = WatchlistStateStore(db_path)
    rows = store.list_signal_feedback(limit=10000)
    suggestions = build_weight_tuning_suggestions(rows, primary_window_days=primary_window_days)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "weight_tuning_suggestions.json"
    out_path.write_text(json.dumps(suggestions, indent=2), encoding="utf-8")
    logger.info("Weight tuning suggestions written: %s", out_path)
    return {"suggestions": suggestions, "path": str(out_path)}
