"""
Profit Attribution — Report Writer
=====================================
Writes AttributionSummary to disk as JSON + Markdown and exposes a
short memo for email digests (matching the pattern of coverage_report_writer).

Output artifacts:
  outputs/policy/profit_attribution.json  — full machine-readable result
  outputs/policy/profit_attribution.md    — human-readable Markdown report

Public functions:
  write_attribution_reports(summary, policy_dir, dry_run) → bool
  build_attribution_memo(summary) → str   (4–6 line plain-text)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from profit_attribution.models import (
    AttributionSummary,
    ConfidenceCalibrationResult,
    ExecutionAttributionSummary,
)

logger = logging.getLogger("profit_attribution.report_writer")

_DEFAULT_POLICY_DIR = Path("outputs/policy")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_attribution_reports(
    summary: AttributionSummary,
    policy_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> bool:
    """
    Write profit_attribution.json and profit_attribution.md to disk.

    Args:
        summary:    Output from profit_attribution.run_profit_attribution().
        policy_dir: Override default output directory.
        dry_run:    If True, build content but skip writing.

    Returns:
        True on success (or dry_run), False if any write failed.
    """
    out_dir = Path(policy_dir) if policy_dir else _DEFAULT_POLICY_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    if dry_run:
        logger.debug("profit_attribution.report_writer: dry_run — skipping writes")
        return True

    ok = True

    json_path = out_dir / "profit_attribution.json"
    try:
        json_path.write_text(json.dumps(summary.to_dict(), indent=2), encoding="utf-8")
        logger.info("profit_attribution.report_writer: wrote %s", json_path)
    except OSError as exc:
        logger.warning("profit_attribution.report_writer: JSON write failed: %s", exc)
        ok = False

    md_path = out_dir / "profit_attribution.md"
    try:
        md_path.write_text(_build_markdown(summary), encoding="utf-8")
        logger.info("profit_attribution.report_writer: wrote %s", md_path)
    except OSError as exc:
        logger.warning("profit_attribution.report_writer: MD write failed: %s", exc)
        ok = False

    return ok


def build_attribution_memo(summary: AttributionSummary) -> str:
    """
    Build a short (4–6 line) plain-text memo for email digests.
    Follows the same tone and format as build_coverage_memo().
    """
    lines = ["[Profit Attribution]"]
    m = summary.metrics

    if m.total_entries == 0:
        lines.append("  No coverage data yet — run market scans first.")
        return "\n".join(lines)

    cov = f"{m.coverage_rate * 100:.0f}%"
    lines.append(f"  {m.attributable_entries}/{m.total_entries} trades attributed ({cov} coverage)")

    if m.win_rate is not None:
        wr = f"{m.win_rate * 100:.0f}%"
        rr = f"{m.risk_reward:.2f}x" if m.risk_reward is not None else "—"
        lines.append(f"  Win rate: {wr}  |  Risk/Reward: {rr}")

    if m.avg_gain is not None and m.avg_loss is not None:
        lines.append(
            f"  Avg gain: {m.avg_gain * 100:+.1f}%  |  Avg loss: {m.avg_loss * 100:+.1f}%"
        )

    if m.expectancy is not None:
        lines.append(f"  Expectancy per trade: {m.expectancy * 100:+.2f}%")

    # Best strategy
    by_s = [s for s in summary.by_strategy if s.attributable >= 3]
    if by_s:
        best = max(by_s, key=lambda s: s.win_rate or -1)
        lines.append(
            f"  Best strategy: {best.name} "
            f"(win {(best.win_rate or 0) * 100:.0f}%, "
            f"RR {best.risk_reward:.2f}x)"
            if best.risk_reward else
            f"  Best strategy: {best.name} (win {(best.win_rate or 0) * 100:.0f}%)"
        )

    if summary.missed_opportunities:
        n_missed = len(summary.missed_opportunities)
        total_cost = summary.total_opportunity_cost
        cost_str = f" ({total_cost * 100:+.1f}% avg)" if total_cost else ""
        lines.append(f"  Missed opportunities: {n_missed}{cost_str}")

    # Execution summary (one line if available)
    ex = summary.execution
    if ex and ex.total_events > 0:
        buy_metrics = next((a for a in ex.by_action if a.action == "BUY"), None)
        if buy_metrics and buy_metrics.win_rate is not None:
            lines.append(
                f"  Execution BUY win rate: {buy_metrics.win_rate * 100:.0f}% "
                f"({buy_metrics.matched_events}/{buy_metrics.total_events} matched)"
            )
        else:
            lines.append(
                f"  Execution events: {ex.total_events} logged, "
                f"{ex.matched_events} matched ({ex.match_rate * 100:.0f}%)"
            )

    if summary.data_quality_notes:
        lines.append(f"  Note: {summary.data_quality_notes[0]}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown builder
# ---------------------------------------------------------------------------

def _build_markdown(s: AttributionSummary) -> str:
    lines = [
        "# Profit Attribution Report",
        "",
        f"*Generated: {s.generated_at}*",
        "",
        "## Overall Metrics",
        "",
    ]

    m = s.metrics
    lines += [
        "| Metric | Value |",
        "| ------ | ----- |",
        f"| Total entries | {m.total_entries} |",
        f"| Attributable | {m.attributable_entries} |",
        f"| With 5d return | {m.entries_with_5d} |",
        f"| Coverage rate | {m.coverage_rate * 100:.0f}% |",
        f"| Win rate | {_pct(m.win_rate)} |",
        f"| Avg gain (5d) | {_pct(m.avg_gain)} |",
        f"| Avg loss (5d) | {_pct(m.avg_loss)} |",
        f"| Risk/Reward | {_ratio(m.risk_reward)} |",
        f"| Expectancy | {_pct(m.expectancy)} |",
        f"| Capital efficiency | {_pct(m.capital_efficiency)} |",
        f"| Strong win rate (≥+2%) | {_pct(m.strong_win_rate)} |",
        f"| Adverse rate (≤−2%) | {_pct(m.adverse_rate)} |",
        f"| Avg MFE | {_pct(m.avg_mfe)} |",
        f"| Avg MAE | {_pct(m.avg_mae)} |",
        f"| Avg exit quality | {_pct(m.avg_exit_quality)} |",
        f"| Avg hold days | {_days(m.avg_hold_days)} |",
        "",
    ]

    if s.data_quality_notes:
        lines += ["### Data Quality Notes", ""]
        for note in s.data_quality_notes:
            lines.append(f"- {note}")
        lines.append("")

    # Strategy breakdown
    if s.by_strategy:
        lines += [
            "## Performance by Strategy Type",
            "",
            "| Strategy | Trades | Attr | Win Rate | Avg Gain | Avg Loss | R/R | Avg Hold | Small? |",
            "| -------- | ------ | ---- | -------- | -------- | -------- | --- | -------- | ------ |",
        ]
        for b in s.by_strategy:
            lines.append(
                f"| {b.name} "
                f"| {b.total_entries} "
                f"| {b.attributable} "
                f"| {_pct(b.win_rate)} "
                f"| {_pct(b.avg_gain)} "
                f"| {_pct(b.avg_loss)} "
                f"| {_ratio(b.risk_reward)} "
                f"| {_days(b.avg_hold_days)} "
                f"| {'⚠' if b.small_sample else ''} |"
            )
        lines.append("")

    # Score band breakdown
    if s.by_score_band:
        lines += [
            "## Performance by Score Band",
            "",
            "| Band | Trades | Attr | Win Rate | Avg Gain | Avg Loss | R/R | Small? |",
            "| ---- | ------ | ---- | -------- | -------- | -------- | --- | ------ |",
        ]
        for b in s.by_score_band:
            lines.append(
                f"| {b.name} "
                f"| {b.total_entries} "
                f"| {b.attributable} "
                f"| {_pct(b.win_rate)} "
                f"| {_pct(b.avg_gain)} "
                f"| {_pct(b.avg_loss)} "
                f"| {_ratio(b.risk_reward)} "
                f"| {'⚠' if b.small_sample else ''} |"
            )
        lines.append("")

    # Regime breakdown
    if s.by_regime:
        lines += [
            "## Performance by Market Regime",
            "",
            "| Regime | Trades | Attr | Win Rate | Avg Gain | Small? |",
            "| ------ | ------ | ---- | -------- | -------- | ------ |",
        ]
        for b in s.by_regime:
            lines.append(
                f"| {b.name} "
                f"| {b.total_entries} "
                f"| {b.attributable} "
                f"| {_pct(b.win_rate)} "
                f"| {_pct(b.avg_gain)} "
                f"| {'⚠' if b.small_sample else ''} |"
            )
        lines.append("")

    # Exit quality summary
    if s.exit_summary:
        lines += [
            "## Exit Quality Summary",
            "",
            "| Label | Count |",
            "| ----- | ----- |",
        ]
        for label, count in sorted(s.exit_summary.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {label} | {count} |")
        lines.append("")
        lines += [
            "_Labels_: **protected** (≥70% of peak retained) · "
            "**partial** (30–70%) · **gave\\_back** (<30%) · "
            "**reversed** (gain → loss) · **no\\_gain** (never rose) · "
            "**unresolved** (no data)",
            "",
        ]

    # Missed opportunities
    if s.missed_opportunities:
        lines += [
            "## Missed Opportunities",
            f"*(High-scored candidates with score ≥ 70 that were not acted on)*",
            "",
        ]
        if s.total_opportunity_cost is not None:
            lines.append(
                f"**Total opportunity cost (positive returns foregone): "
                f"{s.total_opportunity_cost * 100:+.2f}%**"
            )
            lines.append("")
        lines += [
            "| Symbol | Date | Strategy | Score | 5d Return | MFE | Outcome |",
            "| ------ | ---- | -------- | ----- | --------- | --- | ------- |",
        ]
        for o in s.missed_opportunities[:20]:
            lines.append(
                f"| {o.symbol} "
                f"| {o.entry_date} "
                f"| {o.strategy_type} "
                f"| {o.score:.0f} "
                f"| {_pct(o.forward_return_5d)} "
                f"| {_pct(o.mfe)} "
                f"| {o.outcome} |"
            )
        if len(s.missed_opportunities) > 20:
            lines.append(f"*… and {len(s.missed_opportunities) - 20} more*")
        lines.append("")

    # Best trades
    if s.best_trades:
        lines += ["## Best Trades (T+5d)", ""]
        for t in s.best_trades:
            lines.append(
                f"- **{t['symbol']}** ({t.get('strategy_type', '?')})  "
                f"return: {_pct(t.get('return_5d'))}  "
                f"MFE: {_pct(t.get('mfe'))}  "
                f"score: {t.get('entry_score', 0):.0f}  "
                f"regime: {t.get('entry_regime', '?')}  "
                f"entry: {t.get('entry_date', '?')}"
            )
        lines.append("")

    # Worst trades
    if s.worst_trades:
        lines += ["## Worst Trades (T+5d)", ""]
        for t in s.worst_trades:
            lines.append(
                f"- **{t['symbol']}** ({t.get('strategy_type', '?')})  "
                f"return: {_pct(t.get('return_5d'))}  "
                f"MAE: {_pct(t.get('mae'))}  "
                f"score: {t.get('entry_score', 0):.0f}  "
                f"entry: {t.get('entry_date', '?')}"
            )
        lines.append("")

    # Execution attribution sections (additive — only if data present)
    if s.execution:
        lines += _build_execution_sections(s.execution)

    lines += [
        "---",
        "## Methodology",
        "",
        "| Parameter | Value |",
        "| --------- | ----- |",
        "| Data source | coverage_history.jsonl (scanner promotion events) |",
        "| Attribution | Symbol-level price proxy (entry price → forward observations) |",
        "| Primary horizon | T+5d |",
        "| Hit threshold | return > 0.00 |",
        "| Strong win | return ≥ +2.00% |",
        "| Adverse | return ≤ −2.00% |",
        "| Exit protected | exit_quality ≥ 0.70 |",
        "| Exit partial | exit_quality 0.30 – 0.70 |",
        "| Missed opp threshold | score ≥ 70 AND action_bucket inactive |",
        "| Small-sample warning | < 5 attributable entries |",
        "",
        "Read-only evaluation layer — no live decision logic is modified.",
    ]

    return "\n".join(lines) + "\n"


def _build_execution_sections(ex: ExecutionAttributionSummary) -> list:
    """Build Markdown sections for execution-level attribution."""
    lines = [
        "---",
        "## Execution Attribution",
        "",
        "> *Advisory execution events from `trade_events.jsonl` — "
        "system-recommended actions, not broker fills.*",
        "",
        f"**Events logged:** {ex.total_events}  "
        f"| **Matched to outcomes:** {ex.matched_events}  "
        f"| **Match rate:** {ex.match_rate * 100:.0f}%",
        "",
    ]

    if ex.data_quality_notes:
        for note in ex.data_quality_notes:
            lines.append(f"> ⚠ {note}")
        lines.append("")

    if not ex.by_action:
        lines.append("*No execution events to report.*")
        lines.append("")
        return lines

    # Action breakdown
    lines += [
        "### Action Performance Summary",
        "",
        "| Action | Events | Matched | Win Rate | Avg Gain | Avg Loss | R/R | Expectancy | Avg Exit Quality |",
        "| ------ | ------ | ------- | -------- | -------- | -------- | --- | ---------- | ---------------- |",
    ]
    for a in ex.by_action:
        lines.append(
            f"| **{a.action}** "
            f"| {a.total_events} "
            f"| {a.matched_events} "
            f"| {_pct(a.win_rate)} "
            f"| {_pct(a.avg_gain)} "
            f"| {_pct(a.avg_loss)} "
            f"| {_ratio(a.risk_reward)} "
            f"| {_pct(a.expectancy)} "
            f"| {_pct(a.avg_exit_quality)} |"
        )
    lines.append("")
    lines += [
        "_Win rate / gain / loss / R/R / expectancy apply to BUY and PROMOTE events (T+5d return)._",
        "_Avg exit quality (latest return ÷ peak gain) is most meaningful for SELL and TRIM events._",
        "",
    ]

    # Strategy breakdown (execution)
    if any(b.total_entries > 0 for b in ex.by_strategy):
        lines += [
            "### Execution by Strategy",
            "",
            "| Strategy | Events | Matched | Win Rate | Avg Gain | Avg Loss | R/R |",
            "| -------- | ------ | ------- | -------- | -------- | -------- | --- |",
        ]
        for b in ex.by_strategy:
            if b.total_entries == 0:
                continue
            lines.append(
                f"| {b.name} "
                f"| {b.total_entries} "
                f"| {b.attributable} "
                f"| {_pct(b.win_rate)} "
                f"| {_pct(b.avg_gain)} "
                f"| {_pct(b.avg_loss)} "
                f"| {_ratio(b.risk_reward)} "
                f"| {'⚠' if b.small_sample else ''} |"
            )
        lines.append("")

    # Score band breakdown (execution)
    if any(b.total_entries > 0 for b in ex.by_score_band):
        lines += [
            "### Execution by Score Band",
            "",
            "| Band | Events | Matched | Win Rate | Avg Gain | Small? |",
            "| ---- | ------ | ------- | -------- | -------- | ------ |",
        ]
        for b in ex.by_score_band:
            if b.total_entries == 0:
                continue
            lines.append(
                f"| {b.name} "
                f"| {b.total_entries} "
                f"| {b.attributable} "
                f"| {_pct(b.win_rate)} "
                f"| {_pct(b.avg_gain)} "
                f"| {'⚠' if b.small_sample else ''} |"
            )
        lines.append("")

    # Confidence band breakdown (execution)
    if any(b.total_entries > 0 for b in ex.by_confidence_band):
        lines += [
            "### Execution by Confidence Band",
            "",
            "> *Tiers: low < 0.65 · medium 0.65–0.80 · high > 0.80. "
            "Events with no confidence value fall into low.*",
            "",
            "| Band | Events | Matched | Win Rate | Avg Gain | Avg Loss | R/R | Small? |",
            "| ---- | ------ | ------- | -------- | -------- | -------- | --- | ------ |",
        ]
        for b in ex.by_confidence_band:
            if b.total_entries == 0:
                continue
            lines.append(
                f"| {b.name} "
                f"| {b.total_entries} "
                f"| {b.attributable} "
                f"| {_pct(b.win_rate)} "
                f"| {_pct(b.avg_gain)} "
                f"| {_pct(b.avg_loss)} "
                f"| {_ratio(b.risk_reward)} "
                f"| {'⚠' if b.small_sample else ''} |"
            )
        lines.append("")

    # Confidence calibration (observe-only advisory)
    lines += _build_calibration_section(ex.confidence_calibration)

    # Regime breakdown (execution)
    if any(b.total_entries > 0 for b in ex.by_regime):
        lines += [
            "### Execution by Market Regime",
            "",
            "| Regime | Events | Matched | Win Rate | Avg Gain | Small? |",
            "| ------ | ------ | ------- | -------- | -------- | ------ |",
        ]
        for b in ex.by_regime:
            if b.total_entries == 0:
                continue
            lines.append(
                f"| {b.name} "
                f"| {b.total_entries} "
                f"| {b.attributable} "
                f"| {_pct(b.win_rate)} "
                f"| {_pct(b.avg_gain)} "
                f"| {'⚠' if b.small_sample else ''} |"
            )
        lines.append("")

    return lines


def _build_calibration_section(cal: ConfidenceCalibrationResult) -> list:
    """Build Markdown section for observe-only confidence calibration."""
    _status_icon = {
        "healthy": "✓",
        "weak_separation": "⚠",
        "insufficient_data": "—",
        "no_data": "—",
    }
    icon = _status_icon.get(cal.status, "")

    lines = [
        "### Confidence Calibration",
        "",
        "> *Observe-only advisory. This output does not modify any live thresholds or decision behavior.*",
        "",
        f"**Calibration status:** {cal.status} {icon}",
        "",
    ]

    # Summary metrics table (only when there's enough data to show)
    total = cal.low_matched + cal.medium_matched + cal.high_matched
    if total > 0:
        lines += [
            "| Band | Matched | Win Rate | Expectancy |",
            "| ---- | ------- | -------- | ---------- |",
            f"| low    | {cal.low_matched}    | {_pct(cal.low_win_rate)}    | {_pct(cal.low_expectancy)} |",
            f"| medium | {cal.medium_matched} | {_pct(cal.medium_win_rate)} | {_pct(cal.medium_expectancy)} |",
            f"| high   | {cal.high_matched}   | {_pct(cal.high_win_rate)}   | {_pct(cal.high_expectancy)} |",
            "",
        ]

    if cal.band_order_valid is not None:
        order_str = "Yes (high ≥ medium ≥ low)" if cal.band_order_valid else "No — band order inverted"
        lines += [
            f"- **Band order valid:** {order_str}",
        ]
    if cal.strongest_band:
        lines.append(f"- **Strongest band:** {cal.strongest_band}")
    if cal.weakest_band:
        lines.append(f"- **Weakest band:** {cal.weakest_band}")

    lines += [
        "",
        f"**Recommendation:** {cal.recommendation}",
    ]
    if cal.recommendation_reason:
        lines.append(f"*Reason: {cal.recommendation_reason}*")
    lines.append("")

    return lines


def _pct(v) -> str:
    if v is None:
        return "—"
    return f"{float(v) * 100:+.1f}%"


def _ratio(v) -> str:
    if v is None:
        return "—"
    return f"{float(v):.2f}x"


def _days(v) -> str:
    if v is None:
        return "—"
    return f"{float(v):.1f}d"
