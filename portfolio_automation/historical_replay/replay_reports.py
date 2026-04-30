"""
Historical replay calibration and performance attribution reports.

All reports are clearly labelled as historical replay only.
Live decision_outcomes.jsonl is never read or modified here.
Outputs go to outputs/backtest/, not outputs/policy/.

Data governance: all file I/O uses OutputNamespace.HISTORICAL via the
portfolio_automation.data_governance safe writers. A live-path guard
rejects any output_dir that points inside outputs/latest, outputs/live,
outputs/policy, outputs/portfolio, or outputs/users.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    DataGovernanceError,
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger("stockbot.portfolio_automation.historical_replay.reports")

_HISTORICAL_SOURCE = "historical_replay"

# Subdirectory names that belong to live output namespaces.
# Historical replay must never write to any of these.
_BLOCKED_LIVE_DIRS: frozenset[str] = frozenset({
    "latest", "live", "policy", "portfolio", "users", "sandbox",
})


def _assert_safe_replay_output_dir(output_dir: Path) -> None:
    """Raise DataGovernanceError if output_dir touches a live namespace directory."""
    for part in output_dir.parts:
        if part in _BLOCKED_LIVE_DIRS:
            raise DataGovernanceError(
                f"Historical replay output_dir {str(output_dir)!r} contains "
                f"live-namespace segment '{part}'. "
                "Replay must only write to outputs/backtest/."
            )


def _base_dir_from_output_dir(output_dir: Path) -> Path:
    """
    Derive the data-governance base_dir from output_dir.

    Convention: output_dir is expected to be .../backtest.
    Its parent is the base_dir that the governance layer prepends 'backtest' to.
    If output_dir.name is not 'backtest', treat output_dir itself as base_dir
    so that governance still writes to output_dir/backtest/.
    """
    if output_dir.name == "backtest":
        return output_dir.parent
    return output_dir


# ---------------------------------------------------------------------------
# Shared stats helpers
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        result = float(v)
        return result if result == result else None  # exclude NaN
    except (TypeError, ValueError):
        return None


def _group_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    judgeable = [r for r in rows if r.get("direction_correct") is not None]
    correct = [r for r in judgeable if r.get("direction_correct")]
    returns = [v for r in rows if (v := _safe_float(r.get("return_pct"))) is not None]
    return {
        "count": len(rows),
        "hit_rate": len(correct) / len(judgeable) if judgeable else None,
        "avg_return": sum(returns) / len(returns) if returns else None,
    }


def _conf_bucket(confidence: float | None) -> str:
    if confidence is None:
        return "unknown"
    if confidence < 0.4:
        return "low"
    if confidence < 0.7:
        return "medium"
    return "high"


def _fmt_rate(v: float | None) -> str:
    return f"{v:.0%}" if v is not None else "—"


def _fmt_ret(v: float | None) -> str:
    return f"{v:+.2%}" if v is not None else "—"


def _stats_row(label: str, s: dict[str, Any]) -> str:
    return (
        f"| {label} | {s.get('count', 0)} "
        f"| {_fmt_rate(s.get('hit_rate'))} "
        f"| {_fmt_ret(s.get('avg_return'))} |"
    )


# ---------------------------------------------------------------------------
# Historical calibration
# ---------------------------------------------------------------------------


def build_historical_calibration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build calibration payload from historical_replay resolved rows only."""
    resolved = [
        r for r in rows
        if r.get("source") == _HISTORICAL_SOURCE and r.get("resolved") is True
    ]

    overall = _group_stats(resolved)

    by_conf: dict[str, list] = {"low": [], "medium": [], "high": [], "unknown": []}
    for r in resolved:
        bucket = _conf_bucket(_safe_float(r.get("confidence")))
        by_conf[bucket].append(r)

    by_decision: dict[str, list] = {}
    for r in resolved:
        key = str(r.get("decision") or "UNKNOWN").upper()
        by_decision.setdefault(key, []).append(r)

    by_strategy: dict[str, list] = {}
    for r in resolved:
        key = str(r.get("strategy") or "unknown")
        by_strategy.setdefault(key, []).append(r)

    return {
        "generated_at": datetime.now().isoformat(),
        "source": _HISTORICAL_SOURCE,
        "observe_only": True,
        "total_resolved": overall["count"],
        "overall_hit_rate": overall["hit_rate"],
        "overall_avg_return": overall["avg_return"],
        "by_confidence_bucket": {
            k: _group_stats(v) for k, v in by_conf.items() if v
        },
        "by_decision": {
            k: _group_stats(v) for k, v in sorted(by_decision.items())
        },
        "by_strategy": {
            k: _group_stats(v) for k, v in sorted(by_strategy.items())
        },
    }


def render_calibration_md(payload: dict[str, Any]) -> str:
    total = payload.get("total_resolved", 0)
    overall_hr = payload.get("overall_hit_rate")
    overall_ret = payload.get("overall_avg_return")
    gen = payload.get("generated_at", "—")

    lines: list[str] = [
        "# Historical Replay — Confidence Calibration",
        "",
        "> **Historical replay only. Not live trading performance.**",
        "> **Observe-only advisory system. No trades are executed.**",
        "",
        f"Generated: {gen}",
        "",
        "## Overall",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Resolved decisions | {total} |",
        f"| Overall hit rate | {_fmt_rate(overall_hr)} |",
        f"| Overall avg return | {_fmt_ret(overall_ret)} |",
        "",
    ]

    by_conf = payload.get("by_confidence_bucket") or {}
    visible_conf = {k: v for k, v in by_conf.items() if v.get("count", 0) > 0}
    if visible_conf:
        lines += [
            "## By Confidence Bucket",
            "",
            "| Bucket | Count | Hit Rate | Avg Return |",
            "|--------|-------|----------|------------|",
        ]
        for k in ("low", "medium", "high", "unknown"):
            s = visible_conf.get(k)
            if s:
                lines.append(_stats_row(k.capitalize(), s))
        lines.append("")

    by_dec = payload.get("by_decision") or {}
    visible_dec = {k: v for k, v in by_dec.items() if v.get("count", 0) > 0}
    if visible_dec:
        lines += [
            "## By Decision Type",
            "",
            "| Decision | Count | Hit Rate | Avg Return |",
            "|----------|-------|----------|------------|",
        ]
        for k, s in sorted(visible_dec.items()):
            lines.append(_stats_row(k, s))
        lines.append("")

    by_strat = payload.get("by_strategy") or {}
    visible_strat = {k: v for k, v in by_strat.items() if v.get("count", 0) > 0}
    if visible_strat:
        lines += [
            "## By Strategy",
            "",
            "| Strategy | Count | Hit Rate | Avg Return |",
            "|----------|-------|----------|------------|",
        ]
        for k, s in sorted(visible_strat.items()):
            lines.append(_stats_row(k, s))
        lines.append("")

    lines += ["---", "*Historical replay only. Not live trading performance. Observe-only.*"]
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# Historical performance attribution
# ---------------------------------------------------------------------------


def _decision_summary(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "symbol": row.get("symbol"),
        "date": row.get("date"),
        "decision": row.get("decision"),
        "return_pct": row.get("return_pct"),
        "direction_correct": row.get("direction_correct"),
    }


def build_historical_attribution(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build performance attribution payload from historical_replay rows."""
    all_hist = [r for r in rows if r.get("source") == _HISTORICAL_SOURCE]
    resolved = [r for r in all_hist if r.get("resolved") is True]

    overall = _group_stats(resolved)

    by_decision: dict[str, list] = {}
    for r in resolved:
        key = str(r.get("decision") or "UNKNOWN").upper()
        by_decision.setdefault(key, []).append(r)

    by_strategy: dict[str, list] = {}
    for r in resolved:
        key = str(r.get("strategy") or "unknown")
        by_strategy.setdefault(key, []).append(r)

    rows_with_return = [r for r in resolved if r.get("return_pct") is not None]
    best = max(rows_with_return, key=lambda r: r["return_pct"]) if rows_with_return else None
    worst = min(rows_with_return, key=lambda r: r["return_pct"]) if rows_with_return else None

    return {
        "generated_at": datetime.now().isoformat(),
        "source": _HISTORICAL_SOURCE,
        "observe_only": True,
        "total_decisions": len(all_hist),
        "resolved_decisions": overall["count"],
        "hit_rate": overall["hit_rate"],
        "avg_return": overall["avg_return"],
        "by_decision": {k: _group_stats(v) for k, v in sorted(by_decision.items())},
        "by_strategy": {k: _group_stats(v) for k, v in sorted(by_strategy.items())},
        "best_decision": _decision_summary(best),
        "worst_decision": _decision_summary(worst),
    }


def render_attribution_md(payload: dict[str, Any]) -> str:
    total = payload.get("total_decisions", 0)
    resolved = payload.get("resolved_decisions", 0)
    hr = payload.get("hit_rate")
    avg_ret = payload.get("avg_return")
    gen = payload.get("generated_at", "—")

    lines: list[str] = [
        "# Historical Replay — Performance Attribution",
        "",
        "> **Historical replay only. Not live trading performance.**",
        "> **Observe-only advisory system. No trades are executed.**",
        "",
        f"Generated: {gen}",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total decisions | {total} |",
        f"| Resolved decisions | {resolved} |",
        f"| Hit rate | {_fmt_rate(hr)} |",
        f"| Avg return | {_fmt_ret(avg_ret)} |",
        "",
    ]

    by_dec = payload.get("by_decision") or {}
    visible_dec = {k: v for k, v in by_dec.items() if v.get("count", 0) > 0}
    if visible_dec:
        lines += [
            "## By Decision Type",
            "",
            "| Decision | Count | Hit Rate | Avg Return |",
            "|----------|-------|----------|------------|",
        ]
        for k, s in sorted(visible_dec.items()):
            lines.append(_stats_row(k, s))
        lines.append("")

    by_strat = payload.get("by_strategy") or {}
    visible_strat = {k: v for k, v in by_strat.items() if v.get("count", 0) > 0}
    if visible_strat:
        lines += [
            "## By Strategy",
            "",
            "| Strategy | Count | Hit Rate | Avg Return |",
            "|----------|-------|----------|------------|",
        ]
        for k, s in sorted(visible_strat.items()):
            lines.append(_stats_row(k, s))
        lines.append("")

    best = payload.get("best_decision")
    worst = payload.get("worst_decision")
    if best or worst:
        lines += ["## Notable Decisions", ""]
        if best:
            lines.append(
                f"- Best: {best.get('symbol')} {best.get('decision')} "
                f"on {best.get('date')} → {_fmt_ret(best.get('return_pct'))}"
            )
        if worst:
            lines.append(
                f"- Worst: {worst.get('symbol')} {worst.get('decision')} "
                f"on {worst.get('date')} → {_fmt_ret(worst.get('return_pct'))}"
            )
        lines.append("")

    lines += ["---", "*Historical replay only. Not live trading performance. Observe-only.*"]
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# File I/O — governance-aware writers
# ---------------------------------------------------------------------------


def write_calibration(
    payload: dict[str, Any],
    output_dir: Path,
) -> tuple[Path, Path]:
    """
    Write historical_calibration.json + .md under output_dir.

    Uses OutputNamespace.HISTORICAL via safe_write_json / safe_write_text.
    Raises DataGovernanceError if output_dir is inside a live namespace.
    Returns (json_path, md_path).
    """
    _assert_safe_replay_output_dir(output_dir)
    base = _base_dir_from_output_dir(output_dir)
    json_path = safe_write_json(
        OutputNamespace.HISTORICAL,
        "historical_calibration.json",
        payload,
        base_dir=str(base),
    )
    md_path = safe_write_text(
        OutputNamespace.HISTORICAL,
        "historical_calibration.md",
        render_calibration_md(payload),
        base_dir=str(base),
    )
    return json_path, md_path


def write_attribution(
    payload: dict[str, Any],
    output_dir: Path,
) -> tuple[Path, Path]:
    """
    Write historical_performance_attribution.json + .md under output_dir.

    Uses OutputNamespace.HISTORICAL via safe_write_json / safe_write_text.
    Raises DataGovernanceError if output_dir is inside a live namespace.
    Returns (json_path, md_path).
    """
    _assert_safe_replay_output_dir(output_dir)
    base = _base_dir_from_output_dir(output_dir)
    json_path = safe_write_json(
        OutputNamespace.HISTORICAL,
        "historical_performance_attribution.json",
        payload,
        base_dir=str(base),
    )
    md_path = safe_write_text(
        OutputNamespace.HISTORICAL,
        "historical_performance_attribution.md",
        render_attribution_md(payload),
        base_dir=str(base),
    )
    return json_path, md_path
