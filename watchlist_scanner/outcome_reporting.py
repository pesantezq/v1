from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from watchlist_scanner.state import WatchlistStateStore


def _is_resolved_outcome(row: dict[str, Any]) -> bool:
    return not bool(row.get("outcome_pending", 1))


def _portfolio_priority_bucket(priority: float) -> str:
    if priority > 0:
        return "portfolio_favored"
    if priority < 0:
        return "portfolio_penalized"
    return "portfolio_neutral"


def _round_avg(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 2)


def _group_rows(
    rows: list[dict[str, Any]],
    key_fn: Callable[[dict[str, Any]], str],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = key_fn(row)
        grouped.setdefault(key, []).append(row)

    summary: dict[str, dict[str, Any]] = {}
    for key, bucket_rows in sorted(grouped.items()):
        returns = [float(r.get("return_pct") or 0.0) for r in bucket_rows if r.get("return_pct") is not None]
        label_counts: dict[str, int] = {}
        for r in bucket_rows:
            label = str(r.get("outcome_label") or "unknown")
            label_counts[label] = label_counts.get(label, 0) + 1
        summary[key] = {
            "count": len(bucket_rows),
            "avg_return_pct": _round_avg(returns),
            "outcome_labels": label_counts,
        }
    return summary


def build_outcome_analytics_summary(
    resolved_rows: list[dict[str, Any]],
    *,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """
    Build a read-only outcome analytics summary from resolved lifecycle rows.

    Groupings are intentionally explainable and operator-facing:
    - alert quality tier
    - evidence breadth
    - confirmation count
    - portfolio priority bucket
    - watchlist source
    - outcome label
    """
    now = as_of or datetime.now()
    rows = [row for row in resolved_rows if _is_resolved_outcome(row)]
    returns = [float(r.get("return_pct") or 0.0) for r in rows if r.get("return_pct") is not None]

    outcome_label_counts: dict[str, int] = {}
    for row in rows:
        label = str(row.get("outcome_label") or "unknown")
        outcome_label_counts[label] = outcome_label_counts.get(label, 0) + 1

    return {
        "generated_at": now.isoformat(),
        "resolved_count": len(rows),
        "avg_return_pct": _round_avg(returns),
        "outcome_labels": outcome_label_counts,
        "by_alert_quality_tier": _group_rows(rows, lambda r: str(r.get("alert_quality_tier") or "none")),
        "by_evidence_breadth": _group_rows(rows, lambda r: str(int(r.get("evidence_breadth") or 0))),
        "by_confirmation_count": _group_rows(rows, lambda r: str(int(r.get("confirmation_count") or 0))),
        "by_portfolio_priority_bucket": _group_rows(
            rows,
            lambda r: _portfolio_priority_bucket(float(r.get("portfolio_priority") or 0.0)),
        ),
        "by_watchlist_source": _group_rows(rows, lambda r: str(r.get("watchlist_source") or "unknown")),
    }


def load_resolved_alert_outcomes(
    db_path: str | Path = "data/portfolio.db",
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Load recent resolved watchlist alert lifecycle rows from the shared store."""
    store = WatchlistStateStore(db_path)
    return [row for row in store.list_alert_lifecycles(limit=limit) if _is_resolved_outcome(row)]


def render_outcome_analytics_markdown(summary: dict[str, Any]) -> str:
    """Render a compact markdown report for resolved watchlist outcomes."""
    lines = [
        "# Watchlist Outcome Analytics",
        "",
        f"Generated: {summary.get('generated_at', '')}  ",
        f"Resolved outcomes: **{summary.get('resolved_count', 0)}**  ",
        f"Average return: **{summary.get('avg_return_pct', 0.0):+.2f}%**  ",
        "",
    ]

    resolved_count = int(summary.get("resolved_count") or 0)
    if resolved_count == 0:
        lines.append("No resolved watchlist alert outcomes yet.")
        return "\n".join(lines)

    def _append_group(title: str, group: dict[str, Any]) -> None:
        if not group:
            return
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| Bucket | Count | Avg Return | Labels |")
        lines.append("|--------|-------|------------|--------|")
        for key, metrics in group.items():
            labels = ", ".join(f"{label}:{count}" for label, count in sorted((metrics.get("outcome_labels") or {}).items()))
            lines.append(
                f"| {key} | {metrics.get('count', 0)} | {float(metrics.get('avg_return_pct', 0.0)):+.2f}% | {labels or 'none'} |"
            )
        lines.append("")

    label_bits = ", ".join(
        f"{label}: {count}" for label, count in sorted((summary.get("outcome_labels") or {}).items())
    )
    lines.append(f"Outcome labels: {label_bits}")
    lines.append("")

    _append_group("By Alert Quality", summary.get("by_alert_quality_tier") or {})
    _append_group("By Watchlist Source", summary.get("by_watchlist_source") or {})
    _append_group("By Evidence Breadth", summary.get("by_evidence_breadth") or {})
    _append_group("By Portfolio Priority Bucket", summary.get("by_portfolio_priority_bucket") or {})

    return "\n".join(lines)


def write_outcome_analytics_reports(
    output_dir: str | Path,
    summary: dict[str, Any],
) -> dict[str, str]:
    """Write JSON and markdown outcome analytics reports."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "watchlist_outcome_analytics.json"
    md_path = out_dir / "watchlist_outcome_analytics.md"

    json_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_outcome_analytics_markdown(summary), encoding="utf-8")

    return {
        "json_path": str(json_path),
        "markdown_path": str(md_path),
    }


def generate_outcome_analytics_reports(
    db_path: str | Path = "data/portfolio.db",
    output_dir: str | Path = "outputs/latest",
    *,
    limit: int = 200,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Load resolved outcomes, build the summary, and write JSON/markdown reports."""
    rows = load_resolved_alert_outcomes(db_path=db_path, limit=limit)
    summary = build_outcome_analytics_summary(rows, as_of=as_of)
    paths = write_outcome_analytics_reports(output_dir, summary)
    return {
        "summary": summary,
        "paths": paths,
    }
