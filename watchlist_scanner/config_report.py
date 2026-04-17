from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


CONFIDENCE_WEIGHTS = {
    "low": 0.50,
    "medium": 0.80,
    "high": 1.00,
}


def confidence_level(sample_size: int) -> str:
    if sample_size < 20:
        return "low"
    if sample_size < 50:
        return "medium"
    return "high"


def _group_metric(summary: dict[str, Any], group_key: str, bucket: str, metric: str) -> float:
    group = dict(summary.get(group_key) or {})
    metrics = dict(group.get(bucket) or {})
    return float(metrics.get(metric) or 0.0)


def _field_effect_metrics(field: str, summary: dict[str, Any]) -> tuple[float, float, str]:
    if field == "signals.min_evidence_count":
        weak_win = _group_metric(summary, "by_evidence_count", "1", "win_rate")
        strong_win = _group_metric(summary, "by_evidence_count", "2", "win_rate")
        weak_ret = _group_metric(summary, "by_evidence_count", "1", "avg_return_pct")
        strong_ret = _group_metric(summary, "by_evidence_count", "2", "avg_return_pct")
        return strong_win - weak_win, strong_ret - weak_ret, "should reduce thin medium-confidence alerts"

    if field == "signals.min_confidence_score":
        overall_win = float(summary.get("overall_win_rate") or 0.0)
        overall_ret = float(summary.get("overall_avg_return_pct") or 0.0)
        return 0.50 - overall_win, max(0.0, -overall_ret), "should improve base alert quality"

    if field == "ranking.confidence_weight":
        high_win = _group_metric(summary, "by_confidence_tier", "high", "win_rate")
        medium_win = _group_metric(summary, "by_confidence_tier", "medium", "win_rate")
        high_ret = _group_metric(summary, "by_confidence_tier", "high", "avg_return_pct")
        medium_ret = _group_metric(summary, "by_confidence_tier", "medium", "avg_return_pct")
        return high_win - medium_win, high_ret - medium_ret, "should rank stronger confidence setups earlier"

    if field == "signals.min_signal_score":
        low_win = _group_metric(summary, "by_priority_bucket", "<0.50", "win_rate")
        mid_win = _group_metric(summary, "by_priority_bucket", "0.50-0.64", "win_rate")
        low_ret = _group_metric(summary, "by_priority_bucket", "<0.50", "avg_return_pct")
        mid_ret = _group_metric(summary, "by_priority_bucket", "0.50-0.64", "avg_return_pct")
        return mid_win - low_win, mid_ret - low_ret, "should trim weak low-priority alerts before ranking"

    if field == "ranking.evidence_weight":
        one_win = _group_metric(summary, "by_evidence_count", "1", "win_rate")
        two_win = _group_metric(summary, "by_evidence_count", "2", "win_rate")
        one_ret = _group_metric(summary, "by_evidence_count", "1", "avg_return_pct")
        two_ret = _group_metric(summary, "by_evidence_count", "2", "avg_return_pct")
        return two_win - one_win, two_ret - one_ret, "should reward broader evidence in alert ranking"

    if field == "signals.confidence_tiers.low":
        low_win = _group_metric(summary, "by_confidence_tier", "low", "win_rate")
        medium_win = _group_metric(summary, "by_confidence_tier", "medium", "win_rate")
        low_ret = _group_metric(summary, "by_confidence_tier", "low", "avg_return_pct")
        medium_ret = _group_metric(summary, "by_confidence_tier", "medium", "avg_return_pct")
        return medium_win - low_win, medium_ret - low_ret, "should reduce weak low-tier emissions"

    return 0.0, 0.0, "should modestly improve calibration"


def _impact_score(win_rate_delta: float, avg_return_delta: float, sample_size: int) -> float:
    sample_factor = min(1.0, sample_size / 50.0)
    raw = max(0.0, win_rate_delta) * 60.0 + max(0.0, avg_return_delta) * 8.0
    return round(raw * sample_factor, 2)


def build_config_report(
    suggestions: list[dict[str, Any]],
    summary: dict[str, Any],
    *,
    profile: str = "base",
    generated_at: str | None = None,
) -> dict[str, Any]:
    recommendations: list[dict[str, Any]] = []

    for suggestion in suggestions:
        field = str(suggestion.get("field") or "")
        sample_size = int(suggestion.get("sample_size") or 0)
        level = confidence_level(sample_size)
        win_rate_delta, avg_return_delta, expected_effect = _field_effect_metrics(field, summary)
        impact = _impact_score(win_rate_delta, avg_return_delta, sample_size)
        priority_value = round(impact * CONFIDENCE_WEIGHTS[level], 2)

        recommendations.append(
            {
                "field": field,
                "current": suggestion.get("current"),
                "suggested": suggestion.get("suggested"),
                "impact_score": impact,
                "confidence_level": level,
                "priority_value": priority_value,
                "reason": suggestion.get("reason", ""),
                "expected_effect": expected_effect,
                "sample_size": sample_size,
                "win_rate_delta": round(win_rate_delta, 4),
                "avg_return_delta": round(avg_return_delta, 4),
            }
        )

    recommendations.sort(
        key=lambda item: (
            float(item.get("priority_value") or 0.0),
            float(item.get("impact_score") or 0.0),
            int(item.get("sample_size") or 0),
        ),
        reverse=True,
    )

    for idx, recommendation in enumerate(recommendations, start=1):
        recommendation["priority_rank"] = idx

    return {
        "profile": profile,
        "generated_at": generated_at or datetime.now().isoformat(),
        "summary": {
            "overall_win_rate": float(summary.get("overall_win_rate") or 0.0),
            "overall_follow_through_rate": float(summary.get("overall_follow_through_rate") or 0.0),
            "overall_avg_return_pct": float(summary.get("overall_avg_return_pct") or 0.0),
        },
        "recommendations": recommendations,
    }


def render_config_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Config Calibration Report",
        "",
        f"Profile: **{report.get('profile', 'base')}**  ",
        f"Generated: {report.get('generated_at', '')}  ",
        f"Overall win rate: **{float((report.get('summary') or {}).get('overall_win_rate', 0.0)):.2%}**  ",
        f"Overall follow-through rate: **{float((report.get('summary') or {}).get('overall_follow_through_rate', 0.0)):.2%}**  ",
        f"Overall average return: **{float((report.get('summary') or {}).get('overall_avg_return_pct', 0.0)):+.2f}%**  ",
        "",
    ]

    recommendations = list(report.get("recommendations") or [])
    if not recommendations:
        lines.append("No calibration recommendations were generated.")
        return "\n".join(lines)

    lines.extend(
        [
            "## Top Recommendations",
            "",
            "| Rank | Field | Current | Suggested | Impact | Confidence | Sample | Expected Effect |",
            "|------|-------|---------|-----------|--------|------------|--------|-----------------|",
        ]
    )
    for rec in recommendations:
        lines.append(
            f"| {rec.get('priority_rank')} | {rec.get('field')} | {rec.get('current')} | "
            f"{rec.get('suggested')} | {float(rec.get('impact_score') or 0.0):.2f} | "
            f"{rec.get('confidence_level')} | {rec.get('sample_size')} | {rec.get('expected_effect')} |"
        )

    lines.extend(["", "## Recommendation Details", ""])
    for rec in recommendations:
        lines.append(f"### #{rec.get('priority_rank')} {rec.get('field')}")
        lines.append(
            f"- Change: `{rec.get('current')}` -> `{rec.get('suggested')}`"
        )
        lines.append(
            f"- Impact score: **{float(rec.get('impact_score') or 0.0):.2f}**"
        )
        lines.append(
            f"- Confidence: **{rec.get('confidence_level')}** from sample size **{rec.get('sample_size')}**"
        )
        lines.append(
            f"- Observed delta: win rate {float(rec.get('win_rate_delta') or 0.0):+.2%}, "
            f"avg return {float(rec.get('avg_return_delta') or 0.0):+.2f}%"
        )
        lines.append(f"- Reason: {rec.get('reason')}")
        lines.append(f"- Expected effect: {rec.get('expected_effect')}")
        lines.append("")

    return "\n".join(lines)


def write_config_report(
    report: dict[str, Any],
    *,
    config_path: str | Path,
) -> dict[str, str] | None:
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
    profile = str(report.get("profile") or "base").replace(" ", "_")
    md_path = history_dir / f"config_report_{timestamp}_{profile}.md"
    json_path = history_dir / f"config_report_{timestamp}_{profile}.json"

    md_path.write_text(render_config_report_markdown(report), encoding="utf-8")
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    return {
        "markdown_path": str(md_path),
        "json_path": str(json_path),
    }
