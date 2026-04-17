"""
Report writer for recommendation outcome attribution results.

Writes two files:
  outputs/policy/recommendation_outcomes.json  - machine-readable metrics
  outputs/policy/recommendation_outcomes.md    - human-readable summary

Both are advisory only. Neither file modifies live scoring or recommendation
logic.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from policy_evaluator.outcome_attributor import OutcomeResult

logger = logging.getLogger("policy_evaluator.outcome_writer")

_POLICY_DIR = Path("outputs/policy")


def _pct(value) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.2f}%"


def _ret(value) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value * 100:.2f}%"


def _small_sample_note(bucket: dict) -> str:
    return " (small sample)" if bucket.get("small_sample") else ""


def _append_bucket_table(
    lines: list[str],
    title: str,
    buckets: dict,
    *,
    order: Optional[list[str]] = None,
) -> None:
    if not buckets:
        return

    lines.append(f"## {title}")
    lines.append("")
    lines.append("| Bucket | Count | Attr | Hit Rate | Avg 5d | Median 5d | Strong Win | Adverse |")
    lines.append("|--------|-------|------|----------|--------|-----------|------------|---------|")

    keys = order or list(buckets.keys())
    for key in keys:
        bucket = buckets.get(key)
        if bucket and bucket["count"] > 0:
            lines.append(
                f"| {key}{_small_sample_note(bucket)} | {bucket['count']} | {bucket['attributable_count']} "
                f"| {_pct(bucket.get('hit_rate'))} | {_ret(bucket.get('avg_forward_return_5d'))} "
                f"| {_ret(bucket.get('median_forward_return_5d'))} | {_pct(bucket.get('strong_win_rate'))} "
                f"| {_pct(bucket.get('adverse_rate'))} |"
            )
    lines.append("")


def _build_markdown(result: OutcomeResult) -> str:
    lines: list[str] = []
    lines.append("# Recommendation Outcome Attribution Report")
    lines.append("")
    lines.append(f"_Generated: {result.generated_at}_")
    lines.append("")
    lines.append(
        "> **Attribution method:** Option A - Portfolio-level proxy. "
        "Each recommendation event is linked to realized portfolio total_value at "
        "T+1, T+3, T+5, and T+10 calendar days. This is analytics-only and does not "
        "change live recommendations."
    )
    lines.append("")
    lines.append("## What Is Being Measured")
    lines.append("")
    lines.append("- This is portfolio-level attribution, not ticker-level attribution.")
    lines.append(
        f"- The exact headline hit definition is T+{result.outcome_thresholds.get('primary_horizon_days', 5)}d return > 0.00%."
    )
    lines.append(
        "- Returns use the first portfolio snapshot on or after each target horizon, with a maximum 3-day gap."
    )
    lines.append(
        "- Sparse run cadence can cause 1d and 3d, or 3d and 5d, to resolve to the same snapshot."
    )
    lines.append("")

    cov = result.coverage_rate
    dr = result.date_range
    lines.append("## Coverage")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|-------|-------|")
    lines.append(f"| Total recommendation records | {result.total_records} |")
    lines.append(f"| Attributable records | {result.attributable_records} |")
    lines.append(f"| Unevaluable records | {result.unevaluable_records} |")
    lines.append(f"| Coverage rate | {_pct(cov)} |")
    lines.append(f"| Date range | {dr.get('first', 'n/a')} -> {dr.get('last', 'n/a')} |")
    lines.append(f"| Sample quality | {result.sample_quality} |")
    lines.append("")

    if result.coverage_by_horizon:
        lines.append("### Coverage by Horizon")
        lines.append("")
        lines.append("| Horizon | Resolved count |")
        lines.append("|---------|----------------|")
        for horizon in (1, 3, 5, 10):
            lines.append(
                f"| T+{horizon}d | {result.coverage_by_horizon.get(f'count_{horizon}d', 0)} |"
            )
        lines.append("")

    if result.aliasing_notes:
        lines.append("### Aliasing Notes")
        lines.append("")
        lines.append("| Comparison | Same snapshot count | Comparison count | Rate |")
        lines.append("|------------|---------------------|------------------|------|")
        lines.append(
            f"| 1d vs 3d | {result.aliasing_notes.get('count_1d_3d_same_snapshot', 0)} "
            f"| {result.aliasing_notes.get('comparison_count_1d_3d', 0)} "
            f"| {_pct(result.aliasing_notes.get('rate_1d_3d_same_snapshot'))} |"
        )
        lines.append(
            f"| 3d vs 5d | {result.aliasing_notes.get('count_3d_5d_same_snapshot', 0)} "
            f"| {result.aliasing_notes.get('comparison_count_3d_5d', 0)} "
            f"| {_pct(result.aliasing_notes.get('rate_3d_5d_same_snapshot'))} |"
        )
        lines.append("")

    if result.outcome_data_gaps:
        lines.append("### Outcome Data Gaps")
        lines.append("")
        lines.append("| Gap | Count |")
        lines.append("|-----|-------|")
        lines.append(f"| Missing run_date | {result.outcome_data_gaps.get('missing_run_date_count', 0)} |")
        lines.append(f"| Missing base snapshot at T | {result.outcome_data_gaps.get('missing_value_at_t_count', 0)} |")
        lines.append(
            f"| Missing all forward horizons | {result.outcome_data_gaps.get('missing_all_forward_horizons_count', 0)} |"
        )
        lines.append(f"| Missing 1d horizon | {result.outcome_data_gaps.get('missing_1d_count', 0)} |")
        lines.append(f"| Missing 3d horizon | {result.outcome_data_gaps.get('missing_3d_count', 0)} |")
        lines.append(f"| Missing 5d horizon | {result.outcome_data_gaps.get('missing_5d_count', 0)} |")
        lines.append(f"| Missing 10d horizon | {result.outcome_data_gaps.get('missing_10d_count', 0)} |")
        lines.append("")

    if result.data_quality_notes:
        lines.append("## Data Quality Notes")
        lines.append("")
        for note in result.data_quality_notes:
            lines.append(f"- {note}")
        lines.append("")

    if result.attributable_records > 0:
        lines.append("## Overall Outcome Summary")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Hit rate (T+5d > 0%) | {_pct(result.hit_rate_overall)} |")
        lines.append(f"| Strong win rate (T+5d > +2%) | {_pct(result.strong_win_rate_overall)} |")
        lines.append(f"| Adverse rate (T+5d < -2%) | {_pct(result.adverse_rate_overall)} |")
        lines.append(f"| Avg return T+1d | {_ret(result.avg_forward_return_1d)} |")
        lines.append(f"| Avg return T+3d | {_ret(result.avg_forward_return_3d)} |")
        lines.append(f"| Avg return T+5d | {_ret(result.avg_forward_return_5d)} |")
        lines.append(f"| Avg return T+10d | {_ret(result.avg_forward_return_10d)} |")
        lines.append(f"| Avg MFE | {_ret(result.avg_mfe)} |")
        lines.append(f"| Avg MAE | {_ret(result.avg_mae)} |")
        lines.append("")

    if result.by_confidence_tier:
        lines.append("## Confidence Calibration")
        lines.append("")
        lines.append(
            "_Base hit-rate definition is unchanged. These rows add average return, median return, strong win rate, and adverse rate by confidence tier._"
        )
        lines.append("")
        lines.append("| Tier | Count | Attr | Hit Rate | Avg 5d | Median 5d | Strong Win | Adverse |")
        lines.append("|------|-------|------|----------|--------|-----------|------------|---------|")
        for tier in ("low", "medium", "high"):
            bucket = result.by_confidence_tier.get(tier)
            if bucket and bucket["count"] > 0:
                lines.append(
                    f"| {tier}{_small_sample_note(bucket)} | {bucket['count']} | {bucket['attributable_count']} "
                    f"| {_pct(bucket.get('hit_rate'))} | {_ret(bucket.get('avg_forward_return_5d'))} "
                    f"| {_ret(bucket.get('median_forward_return_5d'))} | {_pct(bucket.get('strong_win_rate'))} "
                    f"| {_pct(bucket.get('adverse_rate'))} |"
                )
        calibration_notes = result.confidence_calibration.get("notes", [])
        if calibration_notes:
            lines.append("")
            lines.append("Confidence calibration notes:")
            for note in calibration_notes:
                lines.append(f"- {note}")
        lines.append("")

    _append_bucket_table(
        lines,
        "Outcome by Action Level",
        result.by_action_level,
        order=["Action Required", "Recommended", "Monitor", "FYI", "unknown"],
    )
    _append_bucket_table(lines, "Outcome by Impact Area", result.by_impact_area)
    _append_bucket_table(
        lines,
        "Outcome by Degraded Mode",
        result.by_degraded_mode,
        order=["normal", "degraded"],
    )
    _append_bucket_table(lines, "Outcome by Drawdown Regime", result.by_drawdown_regime)
    _append_bucket_table(
        lines,
        "Outcome by Priority Bucket",
        result.by_priority_bucket,
        order=["0-33", "34-66", "67-100"],
    )

    if result.by_score_decile:
        lines.append("## Outcome by Score Decile")
        lines.append("")
        lines.append("| Score Range | Count | Attr | Hit Rate | Avg 5d | Median 5d |")
        lines.append("|-------------|-------|------|----------|--------|-----------|")
        for bucket in result.by_score_decile:
            if bucket["count"] > 0:
                lines.append(
                    f"| {bucket['label']}{_small_sample_note(bucket)} | {bucket['count']} | {bucket['attributable_count']} "
                    f"| {_pct(bucket.get('hit_rate'))} | {_ret(bucket.get('avg_forward_return_5d'))} "
                    f"| {_ret(bucket.get('median_forward_return_5d'))} |"
                )
        lines.append("")

    if result.notable_wins or result.notable_misses:
        lines.append("## Notable Recent Outcomes")
        lines.append("")
        if result.notable_wins:
            lines.append("### Top Wins")
            lines.append("")
            lines.append("| Run | Rec | Level | Conf | Score | Regime | Degraded | T+5d | T+10d |")
            lines.append("|-----|-----|-------|------|-------|--------|----------|------|-------|")
            for item in result.notable_wins:
                lines.append(
                    f"| {item['run_id']} | {item['rec_base_id']} | {item['action_level']} | {item['confidence']} "
                    f"| {item['score']} | {item['drawdown_regime']} | {'yes' if item['degraded_mode'] else 'no'} "
                    f"| {_ret(item['forward_return_5d'])} | {_ret(item['forward_return_10d'])} |"
                )
            lines.append("")
        if result.notable_misses:
            lines.append("### Notable Misses")
            lines.append("")
            lines.append("| Run | Rec | Level | Conf | Score | Regime | Degraded | T+5d | T+10d |")
            lines.append("|-----|-----|-------|------|-------|--------|----------|------|-------|")
            for item in result.notable_misses:
                lines.append(
                    f"| {item['run_id']} | {item['rec_base_id']} | {item['action_level']} | {item['confidence']} "
                    f"| {item['score']} | {item['drawdown_regime']} | {'yes' if item['degraded_mode'] else 'no'} "
                    f"| {_ret(item['forward_return_5d'])} | {_ret(item['forward_return_10d'])} |"
                )
            lines.append("")

    lines.append("## Outcome Thresholds")
    lines.append("")
    t = result.outcome_thresholds
    lines.append("| Threshold | Value |")
    lines.append("|-----------|-------|")
    lines.append(f"| Positive return (hit) | > {t.get('positive_return_threshold', 0.0) * 100:.0f}% |")
    lines.append(f"| Strong win | > {t.get('strong_win_threshold', 0.02) * 100:.0f}% |")
    lines.append(f"| Acceptable loss | > {t.get('acceptable_loss_threshold', -0.01) * 100:.0f}% |")
    lines.append(f"| Adverse outcome | < {t.get('adverse_threshold', -0.02) * 100:.0f}% |")
    lines.append(f"| Max gap days | {t.get('max_gap_days', 3)} days |")
    lines.append(f"| Small sample warning | < {t.get('small_sample_warning', 5)} attributable records |")
    lines.append(f"| Primary horizon | T+{t.get('primary_horizon_days', 5)}d |")
    lines.append("")
    lines.append("---")
    lines.append("_Advisory only - this report does not affect live recommendations or portfolio logic._")
    lines.append("")
    return "\n".join(lines)


def write_outcome_reports(
    result: OutcomeResult,
    policy_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> bool:
    """
    Write recommendation_outcomes.json and recommendation_outcomes.md.
    """
    out_dir = policy_dir or _POLICY_DIR

    if dry_run:
        logger.debug("outcome_writer: dry_run=True - skipping report writes")
        return True

    try:
        out_dir.mkdir(parents=True, exist_ok=True)

        json_path = out_dir / "recommendation_outcomes.json"
        json_path.write_text(
            json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logger.info("outcome_writer: wrote %s", json_path)

        md_path = out_dir / "recommendation_outcomes.md"
        md_path.write_text(_build_markdown(result), encoding="utf-8")
        logger.info("outcome_writer: wrote %s", md_path)

        return True

    except Exception as exc:  # noqa: BLE001
        logger.warning("outcome_writer: report write failed (non-fatal) - %s", exc)
        return False


def build_outcome_memo(result: OutcomeResult) -> str:
    """
    Return a short plain-text outcome summary suitable for email digests.
    """
    if result.total_records == 0 or result.attributable_records == 0:
        return (
            f"Outcome Attribution: {result.total_records} records, "
            "0 attributable (no portfolio snapshots available yet)."
        )

    lines = [
        f"Outcome Attribution ({result.attributable_records}/{result.total_records} "
        f"attributed, {result.coverage_rate * 100:.0f}% coverage):"
    ]

    if result.hit_rate_overall is not None:
        lines.append(
            f"  T+5d hit rate: {result.hit_rate_overall * 100:.0f}%  "
            f"(strong win {(result.strong_win_rate_overall or 0) * 100:.0f}%  "
            f"adverse {(result.adverse_rate_overall or 0) * 100:.0f}%)"
        )

    if result.avg_forward_return_5d is not None:
        lines.append(f"  Avg T+5d portfolio return: {_ret(result.avg_forward_return_5d)}")

    if result.avg_mfe is not None and result.avg_mae is not None:
        lines.append(
            f"  Avg MFE: {_ret(result.avg_mfe)}  Avg MAE: {_ret(result.avg_mae)}"
        )

    if result.sample_quality != "dense_daily":
        lines.append(f"  Sample quality: {result.sample_quality}")

    return "\n".join(lines)
