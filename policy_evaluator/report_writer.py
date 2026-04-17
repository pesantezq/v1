"""
Report writer for recommendation evaluation results.

Writes two files:
  outputs/policy/recommendation_evaluation.json  — machine-readable metrics
  outputs/policy/recommendation_evaluation.md    — human-readable summary

Both are safe to include in email digests as advisory attachments.
Neither file modifies live scoring or recommendation logic.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from policy_evaluator.evaluator import EvaluationResult

logger = logging.getLogger("policy_evaluator.report_writer")

_POLICY_DIR = Path("outputs/policy")
_JSON_PATH = _POLICY_DIR / "recommendation_evaluation.json"
_MD_PATH = _POLICY_DIR / "recommendation_evaluation.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _fmt_score(value) -> str:
    if value is None:
        return "n/a"
    return str(value)


def _calibration_label(score: Optional[float]) -> str:
    if score is None:
        return "insufficient data"
    if score >= 0.8:
        return "well-calibrated"
    if score >= 0.5:
        return "partially calibrated"
    return "poorly calibrated"


# ---------------------------------------------------------------------------
# Markdown builder
# ---------------------------------------------------------------------------

def _build_markdown(result: EvaluationResult) -> str:
    lines = []
    lines.append("# Recommendation Evaluation Report")
    lines.append("")
    lines.append(f"_Generated: {result.generated_at}_")
    lines.append("")

    # Summary
    lines.append("## Summary")
    lines.append("")
    lines.append(f"| Field | Value |")
    lines.append(f"|-------|-------|")
    lines.append(f"| Total records | {result.total_records} |")
    lines.append(f"| Runs analyzed | {result.total_runs} |")
    lines.append(f"| Date range | {result.date_range.get('first', 'n/a')} → {result.date_range.get('last', 'n/a')} |")
    lines.append("")

    # Action level distribution
    if result.action_level_distribution:
        lines.append("## Action Level Distribution")
        lines.append("")
        lines.append("| Level | Count |")
        lines.append("|-------|-------|")
        for level_order in ["Action Required", "Recommended", "Monitor", "FYI"]:
            cnt = result.action_level_distribution.get(level_order, 0)
            if cnt > 0:
                lines.append(f"| {level_order} | {cnt} |")
        lines.append("")

    # Impact area breakdown
    if result.impact_area_breakdown:
        lines.append("## Impact Area Breakdown")
        lines.append("")
        lines.append("| Area | Count |")
        lines.append("|------|-------|")
        for area, cnt in sorted(result.impact_area_breakdown.items(), key=lambda x: -x[1]):
            lines.append(f"| {area} | {cnt} |")
        lines.append("")

    # Hit rate by regime
    if result.hit_rate_by_regime:
        lines.append("## Hit Rate by Regime")
        lines.append("")
        lines.append(
            "_Hit rate = fraction of recommendations that did not recur in the next run "
            "(proxy for resolution or action taken)._"
        )
        lines.append("")
        lines.append("| Regime | Total | Resolved | Hit Rate |")
        lines.append("|--------|-------|----------|----------|")
        for regime, b in sorted(result.hit_rate_by_regime.items()):
            lines.append(
                f"| {regime} | {b['total']} | {b['resolved']} | {_pct(b['hit_rate'])} |"
            )
        lines.append("")
    elif result.total_runs < 2:
        lines.append("## Hit Rate by Regime")
        lines.append("")
        lines.append("_Requires ≥ 2 runs in history._")
        lines.append("")

    # Hit rate by mode
    if result.hit_rate_by_mode:
        lines.append("## Hit Rate by Data Mode")
        lines.append("")
        lines.append("| Mode | Total | Resolved | Hit Rate |")
        lines.append("|------|-------|----------|----------|")
        for mode, b in result.hit_rate_by_mode.items():
            lines.append(
                f"| {mode} | {b['total']} | {b['resolved']} | {_pct(b['hit_rate'])} |"
            )
        lines.append("")

    # Confidence calibration
    cal = result.confidence_calibration
    if cal:
        tiers = cal.get("tiers", {})
        cal_score = cal.get("calibration_score")
        lines.append("## Confidence Calibration")
        lines.append("")
        lines.append(f"**Calibration score:** {cal_score if cal_score is not None else 'n/a'} — {_calibration_label(cal_score)}")
        lines.append("")
        lines.append(
            "_Score = 1.0 if resolution rate increases with confidence tier "
            "(well-calibrated); 0.0 if reversed._"
        )
        lines.append("")
        if tiers:
            lines.append("| Tier | Confidence Range | Count | Avg Score | Avg Raw Score | Resolution Rate |")
            lines.append("|------|-----------------|-------|-----------|---------------|-----------------|")
            for tier in ("low", "medium", "high"):
                t = tiers.get(tier)
                if t:
                    lines.append(
                        f"| {t['tier']} | {t['range']} | {t['count']} | "
                        f"{t['avg_score']:.1f} | {t['avg_raw_score']:.1f} | "
                        f"{_pct(t['resolution_rate'])} |"
                    )
        lines.append("")

    # Recommendation stability
    stab = result.recommendation_stability
    if stab:
        avg_churn = stab.get("avg_churn_rate")
        avg_stab = stab.get("avg_stability")
        lines.append("## Recommendation Stability")
        lines.append("")
        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Avg churn rate | {_pct(avg_churn)} |")
        lines.append(f"| Avg stability | {_pct(avg_stab)} |")
        lines.append("")
        per_run = stab.get("per_run", [])
        if per_run:
            lines.append("| Run | Total | New | Carried Over | Churn Rate |")
            lines.append("|-----|-------|-----|--------------|------------|")
            for row in per_run:
                lines.append(
                    f"| {row['run_id']} | {row['total']} | {row['new_count']} | "
                    f"{row['carried_over']} | {_pct(row['churn_rate'])} |"
                )
        lines.append("")

    # Best-vs-recommended gap
    gap = result.best_vs_recommended_gap
    if gap:
        lines.append("## Best-vs-Recommended Gap")
        lines.append("")
        lines.append(
            "_Gap vs threshold = best_score − 75.  Positive = run had ACTION_REQUIRED items. "
            "Confidence discount = raw_score − final_score on the highest-scored item._"
        )
        lines.append("")
        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Avg gap vs ACTION_REQUIRED threshold | {gap.get('avg_gap_vs_action_required_threshold', 'n/a')} |")
        lines.append(f"| Max gap vs threshold | {gap.get('max_gap_vs_action_required_threshold', 'n/a')} |")
        lines.append(f"| Avg confidence discount | {gap.get('avg_confidence_discount', 'n/a')} |")
        lines.append(f"| Max confidence discount | {gap.get('max_confidence_discount', 'n/a')} |")
        lines.append("")
        per_run = gap.get("per_run", [])
        if per_run:
            lines.append("| Run | Best Score | Raw Score | Discount | Has ACTION_REQUIRED | Gap vs 75 |")
            lines.append("|-----|-----------|-----------|----------|---------------------|-----------|")
            for row in per_run:
                lines.append(
                    f"| {row['run_id']} | {row['best_score']} | {row['best_raw_score']} | "
                    f"{row['max_confidence_discount']} | {'yes' if row['has_action_required'] else 'no'} | "
                    f"{row['gap_best_vs_action_required_threshold']:+d} |"
                )
        lines.append("")

    lines.append("---")
    lines.append("_Advisory only — this report does not affect live recommendations._")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main writer
# ---------------------------------------------------------------------------

def write_evaluation_reports(
    result: EvaluationResult,
    policy_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> bool:
    """
    Write recommendation_evaluation.json and recommendation_evaluation.md.

    Parameters
    ----------
    result     : EvaluationResult from evaluate_history()
    policy_dir : override default outputs/policy/ (used in tests)
    dry_run    : if True, skip file writes

    Returns
    -------
    True on success, False on any write error.
    """
    out_dir = policy_dir or _POLICY_DIR

    if dry_run:
        logger.debug("policy_evaluator: dry_run=True — skipping report writes")
        return True

    try:
        out_dir.mkdir(parents=True, exist_ok=True)

        # JSON
        json_path = out_dir / "recommendation_evaluation.json"
        json_path.write_text(
            json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logger.info("policy_evaluator: wrote %s", json_path)

        # Markdown
        md_path = out_dir / "recommendation_evaluation.md"
        md_path.write_text(_build_markdown(result), encoding="utf-8")
        logger.info("policy_evaluator: wrote %s", md_path)

        return True

    except Exception as exc:  # noqa: BLE001
        logger.warning("policy_evaluator: report write failed (non-fatal) — %s", exc)
        return False


def build_memo_summary(result: EvaluationResult) -> str:
    """
    Return a short plain-text summary suitable for inclusion in email digests
    or monthly memos (advisory section only).
    """
    if result.total_records == 0:
        return "Policy Evaluation: no history yet."

    lines = [f"Policy Evaluation ({result.total_records} records, {result.total_runs} runs):"]

    stab = result.recommendation_stability
    avg_churn = stab.get("avg_churn_rate")
    if avg_churn is not None:
        lines.append(f"  Avg churn {avg_churn * 100:.0f}% | stability {(1 - avg_churn) * 100:.0f}%")

    cal = result.confidence_calibration
    cal_score = cal.get("calibration_score") if cal else None
    if cal_score is not None:
        lines.append(f"  Calibration: {cal_score:.2f} ({_calibration_label(cal_score)})")

    gap = result.best_vs_recommended_gap
    avg_gap = gap.get("avg_gap_vs_action_required_threshold") if gap else None
    if avg_gap is not None:
        urgency = "has urgent items" if avg_gap > 0 else "no urgent items on average"
        lines.append(f"  Best-vs-threshold gap: {avg_gap:+.1f} ({urgency})")

    return "\n".join(lines)
