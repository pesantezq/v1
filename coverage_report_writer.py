"""
Coverage Report Writer
======================
Writes evaluation output from the market-coverage pipeline to disk.

Output artifacts
----------------
  outputs/policy/coverage_evaluation.json  — full CoverageEvalResult dict
  outputs/policy/coverage_evaluation.md   — human-readable Markdown report

Public functions
----------------
  write_coverage_reports(result, policy_dir, dry_run) → bool
  build_coverage_memo(result) → str   (4–6 line plain-text for email digests)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from coverage_evaluator import CoverageEvalResult

logger = logging.getLogger("portfolio_automation.coverage_report_writer")

# TODO(v2-data-governance): migrate direct output writes to data_governance safe writers.
_DEFAULT_POLICY_DIR = Path("outputs/policy")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_coverage_reports(
    result: CoverageEvalResult,
    policy_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> bool:
    """
    Write coverage_evaluation.json and coverage_evaluation.md to disk.

    Args:
        result:     Output from coverage_evaluator.evaluate_coverage().
        policy_dir: Override default output directory.
        dry_run:    If True, build content but skip writing.

    Returns:
        True on success (or dry_run), False if any write failed.
    """
    out_dir = Path(policy_dir) if policy_dir else _DEFAULT_POLICY_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    if dry_run:
        logger.debug("coverage_report_writer: dry_run — skipping file writes")
        return True

    ok = True

    # JSON
    json_path = out_dir / "coverage_evaluation.json"
    try:
        json_path.write_text(
            json.dumps(result.to_dict(), indent=2), encoding="utf-8"
        )
        logger.info("coverage_report_writer: wrote %s", json_path)
    except OSError as exc:
        logger.warning("coverage_report_writer: JSON write failed: %s", exc)
        ok = False

    # Markdown
    md_path = out_dir / "coverage_evaluation.md"
    try:
        md_path.write_text(_build_markdown(result), encoding="utf-8")
        logger.info("coverage_report_writer: wrote %s", md_path)
    except OSError as exc:
        logger.warning("coverage_report_writer: MD write failed: %s", exc)
        ok = False

    return ok


def build_coverage_memo(result: CoverageEvalResult) -> str:
    """
    Build a short (4–6 line) plain-text memo for email digests.

    Follows the same tone as build_outcome_memo() in policy_evaluator.
    """
    lines: list[str] = ["[Market Coverage Evaluation]"]

    if result.total_entries == 0:
        lines.append("  No coverage data yet — enable market_universe scanning first.")
        return "\n".join(lines)

    cov_pct = f"{result.coverage_rate * 100:.0f}%"
    lines.append(
        f"  {result.attributable_entries}/{result.total_entries} "
        f"entries attributed ({cov_pct} coverage)"
    )

    # Overall hit rate + avg return from by_label totals
    all_returns = [
        r for b in result.by_label for r in b.returns
    ]
    all_hits = sum(b.hit_count for b in result.by_label)
    n_returns = sum(len(b.returns) for b in result.by_label)

    if n_returns > 0:
        hit_pct = f"{all_hits / n_returns * 100:.0f}%"
        avg_ret = f"{sum(all_returns) / n_returns * 100:+.1f}%"
        lines.append(f"  T+5d hit rate: {hit_pct}  |  avg return: {avg_ret}")

    # Best label
    by_label = [b for b in result.by_label if b.attributable >= 3]
    if by_label:
        best = max(by_label, key=lambda b: b.avg_return or -999.0)
        worst = min(by_label, key=lambda b: b.avg_return or 999.0)
        lines.append(
            f"  Best label: {best.name} ({(best.avg_return or 0)*100:+.1f}% avg)  "
            f"Weakest: {worst.name} ({(worst.avg_return or 0)*100:+.1f}% avg)"
        )

    # Exit quality
    eq_vals = [b.avg_exit_quality for b in result.by_label if b.avg_exit_quality is not None]
    if eq_vals:
        avg_eq = sum(eq_vals) / len(eq_vals)
        lines.append(f"  Exit quality (retained/peak): {avg_eq:.0%}")

    if result.data_quality_notes:
        lines.append(f"  Note: {result.data_quality_notes[0]}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown builder
# ---------------------------------------------------------------------------

def _build_markdown(result: CoverageEvalResult) -> str:
    lines: list[str] = [
        "# Market Coverage Evaluation",
        "",
        f"*Generated: {result.generated_at}*",
        "",
        "## Coverage Summary",
        "",
        f"| Metric | Value |",
        f"| ------ | ----- |",
        f"| Total entries | {result.total_entries} |",
        f"| Attributable | {result.attributable_entries} |",
        f"| Coverage rate | {result.coverage_rate*100:.0f}% |",
        "",
    ]

    if result.data_quality_notes:
        lines.append("### Data Quality Notes")
        lines.append("")
        for note in result.data_quality_notes:
            lines.append(f"- {note}")
        lines.append("")

    # By label
    if result.by_label:
        lines += [
            "## Returns by Strategy Label",
            "",
            "| Label | Count | Attr | Hit Rate | Avg Ret 5d | Avg MFE | Avg MAE | Exit Quality | Small? |",
            "| ----- | ----- | ---- | -------- | ---------- | ------- | ------- | ------------ | ------ |",
        ]
        for b in result.by_label:
            lines.append(
                f"| {b.name} "
                f"| {b.count} "
                f"| {b.attributable} "
                f"| {_fmt_pct(b.hit_rate)} "
                f"| {_fmt_pct(b.avg_return)} "
                f"| {_fmt_pct(b.avg_mfe)} "
                f"| {_fmt_pct(b.avg_mae)} "
                f"| {_fmt_pct(b.avg_exit_quality)} "
                f"| {'⚠' if b.small_sample else ''} |"
            )
        lines.append("")

    # By score band
    if result.by_score_band:
        lines += [
            "## Returns by Score Band",
            "",
            "| Band | Count | Attr | Hit Rate | Avg Ret 5d | Small? |",
            "| ---- | ----- | ---- | -------- | ---------- | ------ |",
        ]
        for b in result.by_score_band:
            lines.append(
                f"| {b.name} "
                f"| {b.count} "
                f"| {b.attributable} "
                f"| {_fmt_pct(b.hit_rate)} "
                f"| {_fmt_pct(b.avg_return)} "
                f"| {'⚠' if b.small_sample else ''} |"
            )
        lines.append("")

    # By event type
    if result.by_event_type:
        lines += [
            "## Returns by Event Type",
            "",
            "| Event | Count | Attr | Hit Rate | Avg Ret 5d | Small? |",
            "| ----- | ----- | ---- | -------- | ---------- | ------ |",
        ]
        for b in result.by_event_type:
            lines.append(
                f"| {b.name} "
                f"| {b.count} "
                f"| {b.attributable} "
                f"| {_fmt_pct(b.hit_rate)} "
                f"| {_fmt_pct(b.avg_return)} "
                f"| {'⚠' if b.small_sample else ''} |"
            )
        lines.append("")

    # By hold duration
    if result.by_hold_duration:
        lines += [
            "## Return vs Hold Duration",
            "",
            "| Hold (days) | Count | Avg Return | Hit Rate |",
            "| ----------- | ----- | ---------- | -------- |",
        ]
        for h in result.by_hold_duration:
            lines.append(
                f"| T+{h.horizon_days}d "
                f"| {h.count} "
                f"| {_fmt_pct(h.avg_return)} "
                f"| {_fmt_pct(h.hit_rate)} |"
            )
        lines.append("")

    # By regime
    if result.by_regime:
        lines += [
            "## Regime-Aware Breakdown",
            "",
            "| Regime | Count | Attr | Hit Rate | Avg Ret 5d | Small? |",
            "| ------ | ----- | ---- | -------- | ---------- | ------ |",
        ]
        for b in result.by_regime:
            lines.append(
                f"| {b.name} "
                f"| {b.count} "
                f"| {b.attributable} "
                f"| {_fmt_pct(b.hit_rate)} "
                f"| {_fmt_pct(b.avg_return)} "
                f"| {'⚠' if b.small_sample else ''} |"
            )
        lines.append("")

    if result.by_action_bucket:
        lines += [
            "## Portfolio Action Bucket Breakdown",
            "",
            "| Action Bucket | Count | Attr | Hit Rate | Avg Ret 5d | Small? |",
            "| ------------- | ----- | ---- | -------- | ---------- | ------ |",
        ]
        for b in result.by_action_bucket:
            lines.append(
                f"| {b.name} "
                f"| {b.count} "
                f"| {b.attributable} "
                f"| {_fmt_pct(b.hit_rate)} "
                f"| {_fmt_pct(b.avg_return)} "
                f"| {'âš ' if b.small_sample else ''} |"
            )
        lines.append("")

    # Notable wins/misses
    if result.notable_wins:
        lines += ["## Notable Wins (T+5d)", ""]
        for w in result.notable_wins:
            lines.append(
                f"- **{w['symbol']}** ({w.get('label', '?')})  "
                f"return: {_fmt_pct(w.get('forward_return_5d'))}  "
                f"MFE: {_fmt_pct(w.get('mfe'))}  "
                f"score: {w.get('score', 0):.0f}  "
                f"entry: {w.get('entry_date', '?')}"
            )
        lines.append("")

    if result.notable_misses:
        lines += ["## Notable Misses (T+5d)", ""]
        for m in result.notable_misses:
            lines.append(
                f"- **{m['symbol']}** ({m.get('label', '?')})  "
                f"return: {_fmt_pct(m.get('forward_return_5d'))}  "
                f"MAE: {_fmt_pct(m.get('mae'))}  "
                f"score: {m.get('score', 0):.0f}  "
                f"entry: {m.get('entry_date', '?')}"
            )
        lines.append("")

    # Thresholds appendix
    lines += [
        "---",
        "## Thresholds",
        "",
        "| Parameter | Value |",
        "| --------- | ----- |",
        "| Hit threshold | return > 0.00 |",
        "| Strong win | return ≥ +2.00% |",
        "| Adverse | return ≤ −2.00% |",
        "| Primary horizon | T+5d |",
        "| Max track days | 30 |",
        "| Small-sample warning | < 5 entries |",
        "",
        "Attribution method: symbol-level price proxy.  "
        "Entry = first promotion.  Observations = subsequent promotions within 30 days.",
    ]

    return "\n".join(lines) + "\n"


def _fmt_pct(v) -> str:
    if v is None:
        return "—"
    return f"{float(v)*100:+.1f}%"
