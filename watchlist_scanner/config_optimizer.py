from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from watchlist_scanner.outcome_reporting import load_resolved_alert_outcomes


def _outcome_bucket(row: dict[str, Any]) -> str:
    return str(row.get("outcome_label") or "unknown")


def _priority_bucket(score: float) -> str:
    if score >= 0.80:
        return "0.80+"
    if score >= 0.65:
        return "0.65-0.79"
    if score >= 0.50:
        return "0.50-0.64"
    return "<0.50"


def _avg(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 4)


def _rate(rows: list[dict[str, Any]], label: str) -> float:
    if not rows:
        return 0.0
    return round(sum(1 for row in rows if _outcome_bucket(row) == label) / len(rows), 4)


def _follow_through_rate(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    return round(
        sum(1 for row in rows if _outcome_bucket(row) in {"positive", "flat"}) / len(rows),
        0 if not rows else 4,
    )


def _group_rows(rows: list[dict[str, Any]], key_fn) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(key_fn(row), []).append(row)

    summary: dict[str, dict[str, Any]] = {}
    for key, bucket_rows in sorted(grouped.items()):
        returns = [float(row.get("return_pct") or 0.0) for row in bucket_rows if row.get("return_pct") is not None]
        summary[key] = {
            "count": len(bucket_rows),
            "win_rate": _rate(bucket_rows, "positive"),
            "follow_through_rate": _follow_through_rate(bucket_rows),
            "avg_return_pct": _avg(returns),
        }
    return summary


def _clamp(value: float, *, min_value: float, max_value: float) -> float:
    return round(max(min_value, min(max_value, value)), 4)


def _suggestion(field: str, current: Any, suggested: Any, reason: str, sample_size: int) -> dict[str, Any]:
    return {
        "field": field,
        "current": current,
        "suggested": suggested,
        "reason": reason,
        "sample_size": sample_size,
    }


def analyze_outcomes_and_suggest_config(
    state: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    """
    Analyze resolved alert outcomes and return explainable config tuning suggestions.

    This is intentionally conservative:
    - read-only
    - sample-size gated
    - small step suggestions only
    - no auto-apply
    """
    rows = [row for row in state if not bool(row.get("outcome_pending", 1))]
    signals_cfg = dict(config.get("signals") or {})
    ranking_cfg = dict(config.get("ranking") or {})
    runtime_cfg = dict(config.get("config_runtime") or {})
    profile = runtime_cfg.get("profile") or "base"

    by_tier = _group_rows(rows, lambda row: str(row.get("alert_tier") or "unknown"))
    by_priority_bucket = _group_rows(rows, lambda row: _priority_bucket(float(row.get("priority_score") or 0.0)))
    by_evidence_count = _group_rows(rows, lambda row: str(int(row.get("evidence_count") or row.get("evidence_breadth") or 0)))

    suggestions: list[dict[str, Any]] = []
    min_sample_size = max(8, int(signals_cfg.get("optimizer_min_sample_size", 8)))

    overall_returns = [float(row.get("return_pct") or 0.0) for row in rows if row.get("return_pct") is not None]
    overall_win_rate = _rate(rows, "positive")
    overall_follow_through = _follow_through_rate(rows)
    overall_avg_return = _avg(overall_returns)

    medium_stats = by_tier.get("medium")
    high_stats = by_tier.get("high")
    low_stats = by_tier.get("low")

    if medium_stats and medium_stats["count"] >= min_sample_size:
        if medium_stats["win_rate"] < 0.5 or medium_stats["avg_return_pct"] < 0:
            current = int(signals_cfg.get("min_evidence_count", 2))
            suggested = min(current + 1, 5)
            if suggested != current:
                suggestions.append(
                    _suggestion(
                        "signals.min_evidence_count",
                        current,
                        suggested,
                        (
                            "Medium-tier alerts are underperforming on win rate or average return; "
                            "raising the evidence requirement should reduce thin medium-confidence emissions."
                        ),
                        medium_stats["count"],
                    )
                )

    if overall_win_rate < 0.5 and len(rows) >= min_sample_size:
        current = float(signals_cfg.get("min_confidence_score", 0.50))
        suggested = _clamp(current + 0.05, min_value=0.40, max_value=0.95)
        if suggested != current:
            suggestions.append(
                _suggestion(
                    "signals.min_confidence_score",
                    current,
                    suggested,
                    (
                        "Overall resolved alerts are winning less than half the time; "
                        "a modest increase in the minimum confidence floor should improve alert quality."
                    ),
                    len(rows),
                )
            )

    if high_stats and high_stats["count"] >= min_sample_size and medium_stats and medium_stats["count"] >= min_sample_size:
        if (
            high_stats["avg_return_pct"] > medium_stats["avg_return_pct"] + 0.75
            and high_stats["win_rate"] > medium_stats["win_rate"] + 0.10
        ):
            current = float(ranking_cfg.get("confidence_weight", 0.30))
            suggested = _clamp(current + 0.05, min_value=0.0, max_value=0.60)
            if suggested != current:
                suggestions.append(
                    _suggestion(
                        "ranking.confidence_weight",
                        current,
                        suggested,
                        (
                            "High-tier alerts are materially outperforming medium-tier alerts; "
                            "slightly increasing confidence weight should reward the stronger tier separation."
                        ),
                        min(high_stats["count"], medium_stats["count"]),
                    )
                )

    low_priority = by_priority_bucket.get("<0.50")
    mid_priority = by_priority_bucket.get("0.50-0.64")
    if low_priority and low_priority["count"] >= min_sample_size and mid_priority:
        if low_priority["avg_return_pct"] < 0 and low_priority["win_rate"] < 0.45:
            current = float(signals_cfg.get("min_signal_score", 0.50))
            suggested = _clamp(current + 0.05, min_value=0.30, max_value=0.95)
            if suggested != current:
                suggestions.append(
                    _suggestion(
                        "signals.min_signal_score",
                        current,
                        suggested,
                        (
                            "Lowest-priority resolved alerts are underperforming; "
                            "raising the minimum signal score should trim weaker entries before ranking."
                        ),
                        low_priority["count"],
                    )
                )

    evidence_one = by_evidence_count.get("1")
    evidence_two = by_evidence_count.get("2")
    if evidence_one and evidence_one["count"] >= min_sample_size and evidence_two:
        if evidence_one["avg_return_pct"] + 0.75 < evidence_two["avg_return_pct"]:
            current = float(ranking_cfg.get("evidence_weight", 0.15))
            suggested = _clamp(current + 0.05, min_value=0.0, max_value=0.40)
            if suggested != current:
                suggestions.append(
                    _suggestion(
                        "ranking.evidence_weight",
                        current,
                        suggested,
                        (
                            "Single-evidence alerts are lagging stronger evidence setups; "
                            "a slightly higher evidence weight should improve ranked prioritization."
                        ),
                        min(evidence_one["count"], evidence_two["count"]),
                    )
                )

    if low_stats and low_stats["count"] >= min_sample_size and low_stats["avg_return_pct"] < 0:
        tiers = dict(signals_cfg.get("confidence_tiers") or {})
        current = float(tiers.get("low", 0.50))
        suggested = _clamp(current + 0.03, min_value=0.30, max_value=float(tiers.get("medium", 0.65)))
        if suggested != current:
            suggestions.append(
                _suggestion(
                    "signals.confidence_tiers.low",
                    current,
                    suggested,
                    (
                        "Low-tier alerts are producing negative average returns; "
                        "raising the low threshold modestly should reduce weak emissions."
                    ),
                    low_stats["count"],
                )
            )

    return {
        "profile": profile,
        "generated_at": datetime.now().isoformat(),
        "sample_size": len(rows),
        "summary": {
            "overall_win_rate": overall_win_rate,
            "overall_follow_through_rate": overall_follow_through,
            "overall_avg_return_pct": overall_avg_return,
            "by_confidence_tier": by_tier,
            "by_priority_bucket": by_priority_bucket,
            "by_evidence_count": by_evidence_count,
        },
        "suggestions": suggestions,
    }


def load_state_for_optimization(
    db_path: str | Path = "data/portfolio.db",
    *,
    limit: int = 500,
) -> list[dict[str, Any]]:
    return load_resolved_alert_outcomes(db_path=db_path, limit=limit)


def write_config_suggestions(
    suggestions: dict[str, Any],
    *,
    config_path: str | Path,
) -> str | None:
    source = Path(config_path).resolve()
    config_dir: Path | None = None
    if source.is_dir():
        config_dir = source
    elif source.name == "base.json" and source.parent.name == "config":
        config_dir = source.parent

    if config_dir is None:
        return None

    history_dir = config_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    profile = str(suggestions.get("profile") or "base").replace(" ", "_")
    out_path = history_dir / f"config_suggestions_{timestamp}_{profile}.json"
    out_path.write_text(json.dumps(suggestions, indent=2, default=str), encoding="utf-8")
    return str(out_path)
