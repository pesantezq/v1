from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("stockbot.portfolio_automation.decision_performance_attribution")

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

OUTCOMES_JSONL_RELATIVE_PATH = ("outputs", "policy", "decision_outcomes.jsonl")
TRIAGE_JSON_RELATIVE_PATH = ("outputs", "latest", "decision_triage.json")
ATTRIBUTION_JSON_RELATIVE_PATH = ("outputs", "policy", "decision_performance_attribution.json")
ATTRIBUTION_MD_RELATIVE_PATH = ("outputs", "policy", "decision_performance_attribution.md")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_RESOLVED_ROWS = 20

_DECISION_KEYS = ("BUY", "SELL", "SCALE", "WAIT", "HOLD")
_STRATEGY_KEYS = ("structural", "portfolio", "market", "compounder", "momentum")
_VALIDATION_KEYS = ("aligned", "caution", "contradiction", "insufficient_context")
_TRIAGE_KEYS = ("critical_action", "action_candidate", "monitor", "ignore_for_now")

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        result = float(v)
        return result if result == result else None
    except (TypeError, ValueError):
        return None


def _safe_str(v: Any) -> str:
    return str(v or "").strip()


# ---------------------------------------------------------------------------
# JSONL loader
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except json.JSONDecodeError:
                pass
    except OSError:
        pass
    return rows


# ---------------------------------------------------------------------------
# Triage bucket enrichment
# ---------------------------------------------------------------------------


def _load_triage_bucket_map(triage_path: Path) -> dict[tuple[str, str], str]:
    """Return {(SYMBOL, DECISION): triage_bucket} from decision_triage.json."""
    if not triage_path.exists():
        return {}
    try:
        payload = json.loads(triage_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    bucket_map: dict[tuple[str, str], str] = {}
    for bucket_name, bucket_rows in (payload.get("buckets") or {}).items():
        if not isinstance(bucket_rows, list):
            continue
        for row in bucket_rows:
            if not isinstance(row, dict):
                continue
            sym = _safe_str(row.get("symbol")).upper()
            dec = _safe_str(row.get("decision")).upper()
            if sym and dec:
                bucket_map[(sym, dec)] = _safe_str(bucket_name)
    return bucket_map


def _enrich_triage(rows: list[dict[str, Any]], bucket_map: dict[tuple[str, str], str]) -> list[dict[str, Any]]:
    """Return copies of rows with triage_bucket filled from the map (or 'unknown')."""
    enriched = []
    for row in rows:
        sym = _safe_str(row.get("symbol")).upper()
        dec = _safe_str(row.get("decision")).upper()
        r = dict(row)
        r["triage_bucket"] = bucket_map.get((sym, dec), "unknown")
        enriched.append(r)
    return enriched


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def _group_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute attribution stats for a slice of outcome rows."""
    resolved = [r for r in rows if r.get("resolved") is True]
    judgeable = [r for r in resolved if r.get("direction_correct") is not None]
    correct = [r for r in judgeable if r.get("direction_correct")]
    returns = [
        v for r in resolved
        if (v := _safe_float(r.get("return_pct"))) is not None
    ]
    return {
        "total": len(rows),
        "resolved": len(resolved),
        "hit_rate": len(correct) / len(judgeable) if judgeable else None,
        "avg_return": sum(returns) / len(returns) if returns else None,
    }


def _breakdown(
    rows: list[dict[str, Any]],
    field: str,
    canonical_keys: tuple[str, ...],
) -> dict[str, Any]:
    """Group rows by field value and compute stats; always include canonical keys."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = _safe_str(row.get(field) or "unknown")
        groups.setdefault(key, []).append(row)
    for k in canonical_keys:
        groups.setdefault(k, [])
    return {k: _group_stats(v) for k, v in sorted(groups.items())}


# ---------------------------------------------------------------------------
# Best / worst decisions
# ---------------------------------------------------------------------------


def _decision_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": _safe_str(row.get("symbol")),
        "decision": _safe_str(row.get("decision")).upper(),
        "date": _safe_str(row.get("date")),
        "source": _safe_str(row.get("source")),
        "validation_status": _safe_str(row.get("validation_status")),
        "triage_bucket": _safe_str(row.get("triage_bucket")),
        "return_pct": _safe_float(row.get("return_pct")),
        "direction_correct": row.get("direction_correct"),
        "days_elapsed": row.get("days_elapsed"),
    }


def _best_worst(
    resolved: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    with_return = [r for r in resolved if _safe_float(r.get("return_pct")) is not None]
    if not with_return:
        return None, None
    best = max(with_return, key=lambda r: r["return_pct"])
    worst = min(with_return, key=lambda r: r["return_pct"])
    return _decision_summary(best), _decision_summary(worst)


# ---------------------------------------------------------------------------
# Build full payload
# ---------------------------------------------------------------------------


def build_attribution(
    all_rows: list[dict[str, Any]],
    resolved: list[dict[str, Any]],
) -> dict[str, Any]:
    overall = _group_stats(all_rows)
    best, worst = _best_worst(resolved)
    return {
        "generated_at": datetime.now().isoformat(),
        "observe_only": True,
        "available": True,
        "insufficient_data": False,
        "total_decisions": overall["total"],
        "resolved_decisions": overall["resolved"],
        "hit_rate": overall["hit_rate"],
        "avg_return": overall["avg_return"],
        "by_decision": _breakdown(all_rows, "decision", _DECISION_KEYS),
        "by_strategy": _breakdown(all_rows, "source", _STRATEGY_KEYS),
        "by_validation_status": _breakdown(all_rows, "validation_status", _VALIDATION_KEYS),
        "by_triage_bucket": _breakdown(all_rows, "triage_bucket", _TRIAGE_KEYS),
        "best_decision": best,
        "worst_decision": worst,
    }


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def _fmt_rate(v: float | None) -> str:
    return f"{v:.0%}" if v is not None else "—"


def _fmt_ret(v: float | None) -> str:
    return f"{v:+.2%}" if v is not None else "—"


def _breakdown_table(
    title: str,
    header: str,
    data: dict[str, Any],
) -> list[str]:
    rows_with_data = [(k, v) for k, v in data.items() if v.get("total", 0) > 0]
    if not rows_with_data:
        return []
    lines = [
        f"## {title}",
        "",
        f"| {header} | Total | Resolved | Hit Rate | Avg Return |",
        f"|{'-' * (len(header) + 2)}|-------|----------|----------|------------|",
    ]
    for label, stats in rows_with_data:
        lines.append(
            f"| {label.replace('_', ' ').title()} "
            f"| {stats.get('total', 0)} "
            f"| {stats.get('resolved', 0)} "
            f"| {_fmt_rate(stats.get('hit_rate'))} "
            f"| {_fmt_ret(stats.get('avg_return'))} |"
        )
    lines.append("")
    return lines


def render_attribution_md(payload: dict[str, Any]) -> str:
    total = payload.get("total_decisions", 0)
    resolved = payload.get("resolved_decisions", 0)
    hit_rate = payload.get("hit_rate")
    avg_return = payload.get("avg_return")

    lines: list[str] = [
        "# Decision Performance Attribution",
        "",
        "> **Observe-only advisory system. No trades are executed.**",
        "> **Attribution is retrospective and for operator awareness only.**",
        "",
        f"Generated: {payload.get('generated_at', '—')}",
        "",
        "## Overall",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total decisions | {total} |",
        f"| Resolved decisions | {resolved} |",
        f"| Hit rate | {_fmt_rate(hit_rate)} |",
        f"| Avg return | {_fmt_ret(avg_return)} |",
        "",
    ]

    if payload.get("insufficient_data"):
        min_req = payload.get("min_required", MIN_RESOLVED_ROWS)
        lines += [
            f"> Insufficient data: {resolved} resolved decisions "
            f"(minimum {min_req} required). "
            "Attribution will populate as history accumulates.",
            "",
            "---",
            "*Observe-only. No trades are executed by this system.*",
        ]
        return "\n".join(lines).strip() + "\n"

    lines += _breakdown_table("By Decision Type", "Decision", payload.get("by_decision") or {})
    lines += _breakdown_table("By Strategy", "Strategy", payload.get("by_strategy") or {})
    lines += _breakdown_table(
        "By Validation Status", "Validation", payload.get("by_validation_status") or {}
    )
    lines += _breakdown_table(
        "By Triage Bucket", "Triage Bucket", payload.get("by_triage_bucket") or {}
    )

    best = payload.get("best_decision")
    worst = payload.get("worst_decision")
    if best or worst:
        lines += ["## Notable Decisions", ""]
        if best:
            lines.append(
                f"Best: **{best.get('decision')} {best.get('symbol')}** "
                f"on {best.get('date')} — {_fmt_ret(best.get('return_pct'))}"
                f" ({best.get('validation_status', '—')})"
            )
        if worst:
            lines.append(
                f"Worst: **{worst.get('decision')} {worst.get('symbol')}** "
                f"on {worst.get('date')} — {_fmt_ret(worst.get('return_pct'))}"
                f" ({worst.get('validation_status', '—')})"
            )
        lines.append("")

    lines += ["---", "*Observe-only. No trades are executed by this system.*"]
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# I/O helper
# ---------------------------------------------------------------------------


def _safe_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_performance_attribution(
    root: Path | str | None = None,
    *,
    write_files: bool = True,
    min_resolved: int = MIN_RESOLVED_ROWS,
) -> tuple[dict[str, Any], str]:
    """
    Load decision_outcomes.jsonl → enrich with triage buckets → compute attribution.

    Non-fatal: callers should wrap in try/except for pipeline safety.
    Returns (payload_dict, markdown_str).
    """
    root_path = Path(root) if root is not None else Path(".")
    jsonl_path = root_path.joinpath(*OUTCOMES_JSONL_RELATIVE_PATH)
    triage_path = root_path.joinpath(*TRIAGE_JSON_RELATIVE_PATH)

    all_rows = _load_jsonl(jsonl_path)
    bucket_map = _load_triage_bucket_map(triage_path)
    all_rows = _enrich_triage(all_rows, bucket_map)

    resolved = [r for r in all_rows if r.get("resolved") is True]
    total_resolved = len(resolved)

    if total_resolved < min_resolved:
        payload: dict[str, Any] = {
            "generated_at": datetime.now().isoformat(),
            "observe_only": True,
            "available": False,
            "insufficient_data": True,
            "total_decisions": len(all_rows),
            "resolved_decisions": total_resolved,
            "min_required": min_resolved,
            "hit_rate": None,
            "avg_return": None,
            "by_decision": {},
            "by_strategy": {},
            "by_validation_status": {},
            "by_triage_bucket": {},
            "best_decision": None,
            "worst_decision": None,
            "summary_line": (
                f"Insufficient data: {total_resolved} resolved decisions "
                f"(minimum {min_resolved} required)."
            ),
        }
    else:
        payload = build_attribution(all_rows, resolved)
        payload["min_required"] = min_resolved
        hr_str = _fmt_rate(payload.get("hit_rate"))
        payload["summary_line"] = (
            f"{total_resolved} resolved decisions analyzed. "
            f"Hit rate: {hr_str}."
        )

    markdown = render_attribution_md(payload)

    if write_files:
        json_path = root_path.joinpath(*ATTRIBUTION_JSON_RELATIVE_PATH)
        md_path = root_path.joinpath(*ATTRIBUTION_MD_RELATIVE_PATH)
        _safe_json_write(json_path, payload)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown, encoding="utf-8")
        logger.debug(
            "PERFORMANCE ATTRIBUTION: written (resolved=%d, available=%s)",
            total_resolved,
            payload.get("available"),
        )

    return payload, markdown


if __name__ == "__main__":
    run_performance_attribution()
