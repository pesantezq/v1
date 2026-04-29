from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("stockbot.portfolio_automation.confidence_calibration")

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

OUTCOMES_JSONL_RELATIVE_PATH = ("outputs", "policy", "decision_outcomes.jsonl")
CALIBRATION_JSON_RELATIVE_PATH = ("outputs", "policy", "confidence_calibration.json")
CALIBRATION_MD_RELATIVE_PATH = ("outputs", "policy", "confidence_calibration.md")

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

MIN_RESOLVED_ROWS = 20       # skip analysis if fewer resolved rows exist
CONF_LOW_MAX = 0.4           # [0.0, 0.4)  → low
CONF_MED_MAX = 0.7           # [0.4, 0.7)  → medium; [0.7, 1.0] → high

# Insight thresholds
_CALIBRATED_DELTA = 0.05     # high_hr - low_hr must exceed this to call calibrated
_PREDICTIVE_DELTA = 0.05     # aligned_hr - caution_hr must exceed this
_POOR_RATE = 0.40            # below this = poor performance

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        result = float(v)
        return result if result == result else None  # exclude NaN
    except (TypeError, ValueError):
        return None


def _safe_str(v: Any) -> str:
    return str(v or "").strip()


# ---------------------------------------------------------------------------
# JSONL loader (independent — no cross-module import coupling)
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
# Bucket assignment
# ---------------------------------------------------------------------------


def _confidence_bucket(confidence: float | None) -> str:
    if confidence is None:
        return "unknown"
    if confidence < CONF_LOW_MAX:
        return "low"
    if confidence < CONF_MED_MAX:
        return "medium"
    return "high"


# ---------------------------------------------------------------------------
# Statistics helper
# ---------------------------------------------------------------------------


def _group_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute hit_rate and avg_return for a slice of resolved rows."""
    judgeable = [r for r in rows if r.get("direction_correct") is not None]
    correct = [r for r in judgeable if r.get("direction_correct")]
    returns = [
        v for r in rows
        if (v := _safe_float(r.get("return_pct"))) is not None
    ]
    return {
        "count": len(rows),
        "hit_rate": len(correct) / len(judgeable) if judgeable else None,
        "avg_return": sum(returns) / len(returns) if returns else None,
    }


# ---------------------------------------------------------------------------
# Step 2 — Confidence bucket analysis
# ---------------------------------------------------------------------------

_CONFIDENCE_BUCKET_KEYS = ("low", "medium", "high", "unknown")


def compute_confidence_buckets(resolved: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {k: [] for k in _CONFIDENCE_BUCKET_KEYS}
    for row in resolved:
        conf = _safe_float(row.get("confidence"))
        bucket = _confidence_bucket(conf)
        groups[bucket].append(row)
    return {k: _group_stats(v) for k, v in groups.items()}


# ---------------------------------------------------------------------------
# Step 3 — Validation status analysis
# ---------------------------------------------------------------------------

_VALIDATION_KEYS = ("aligned", "caution", "contradiction", "insufficient_context")


def compute_validation_analysis(resolved: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in resolved:
        key = _safe_str(row.get("validation_status") or "unknown")
        groups.setdefault(key, []).append(row)
    # Ensure all canonical keys are present even when count=0
    for k in _VALIDATION_KEYS:
        groups.setdefault(k, [])
    return {k: _group_stats(v) for k, v in sorted(groups.items())}


# ---------------------------------------------------------------------------
# Step 4 — Decision type analysis
# ---------------------------------------------------------------------------

_DECISION_KEYS = ("BUY", "SELL", "SCALE", "WAIT", "HOLD", "AVOID")


def compute_decision_analysis(resolved: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in resolved:
        key = _safe_str(row.get("decision") or "UNKNOWN").upper()
        groups.setdefault(key, []).append(row)
    return {k: _group_stats(v) for k, v in sorted(groups.items())}


# ---------------------------------------------------------------------------
# Step 5 — Overall metrics
# ---------------------------------------------------------------------------


def compute_overall(resolved: list[dict[str, Any]]) -> dict[str, Any]:
    stats = _group_stats(resolved)
    return {
        "total_resolved": stats["count"],
        "overall_hit_rate": stats["hit_rate"],
        "overall_avg_return": stats["avg_return"],
    }


# ---------------------------------------------------------------------------
# Step 7 — Deterministic insight generation
# ---------------------------------------------------------------------------


def _fmt_pp(delta: float) -> str:
    return f"{delta * 100:.0f} pp"


def _fmt_pct(v: float) -> str:
    return f"{v:.0%}"


def generate_insights(
    confidence_buckets: dict[str, Any],
    validation_analysis: dict[str, Any],
    decision_analysis: dict[str, Any],
    overall: dict[str, Any],
) -> list[str]:
    insights: list[str] = []

    high = confidence_buckets.get("high") or {}
    medium = confidence_buckets.get("medium") or {}
    low = confidence_buckets.get("low") or {}

    high_hr = high.get("hit_rate")
    medium_hr = medium.get("hit_rate")
    low_hr = low.get("hit_rate")

    aligned = validation_analysis.get("aligned") or {}
    caution = validation_analysis.get("caution") or {}

    aligned_hr = aligned.get("hit_rate")
    caution_hr = caution.get("hit_rate")

    overall_hr = overall.get("overall_hit_rate")

    # Insight 1: Confidence calibration
    if high_hr is not None and low_hr is not None:
        delta = high_hr - low_hr
        if delta > _CALIBRATED_DELTA:
            insights.append(
                f"System confidence is well calibrated — high-confidence decisions "
                f"outperform low-confidence by {_fmt_pp(delta)} hit rate "
                f"({_fmt_pct(high_hr)} vs {_fmt_pct(low_hr)})."
            )
        elif delta < -_CALIBRATED_DELTA:
            insights.append(
                f"Confidence signal may be inverted — low-confidence decisions "
                f"outperform high-confidence by {_fmt_pp(-delta)} hit rate "
                f"({_fmt_pct(low_hr)} vs {_fmt_pct(high_hr)}). Review scoring."
            )
        else:
            insights.append(
                f"Confidence buckets show similar hit rates (high {_fmt_pct(high_hr)}, "
                f"low {_fmt_pct(low_hr)}) — confidence is not yet a strong differentiator."
            )

    # Insight 2: Validation layer predictiveness
    if aligned_hr is not None and caution_hr is not None:
        delta = aligned_hr - caution_hr
        if delta > _PREDICTIVE_DELTA:
            insights.append(
                f"Validation layer is predictive — aligned decisions achieve "
                f"{_fmt_pct(aligned_hr)} hit rate vs {_fmt_pct(caution_hr)} for caution "
                f"(+{_fmt_pp(delta)})."
            )
        elif caution_hr is not None and caution_hr < _POOR_RATE:
            insights.append(
                f"Caution signals show low accuracy ({_fmt_pct(caution_hr)}) — "
                "these decisions should be deprioritized or reviewed for quality."
            )
        else:
            insights.append(
                f"Aligned ({_fmt_pct(aligned_hr)}) and caution ({_fmt_pct(caution_hr)}) "
                "decisions perform similarly — validation status has limited predictive value so far."
            )

    # Insight 3: High confidence return advantage
    high_ret = high.get("avg_return")
    low_ret = low.get("avg_return")
    if high_ret is not None and low_ret is not None:
        ret_delta = high_ret - low_ret
        if ret_delta > 0.005:
            insights.append(
                f"High-confidence decisions deliver better average returns "
                f"({high_ret:+.1%} vs {low_ret:+.1%})."
            )

    # Insight 4: Overall commentary
    if overall_hr is not None:
        total = overall.get("total_resolved", 0)
        if total < 50:
            insights.append(
                f"Dataset is small ({total} resolved decisions) — "
                "calibration metrics will improve as history accumulates."
            )
        elif overall_hr >= 0.65:
            insights.append(
                f"Overall hit rate of {_fmt_pct(overall_hr)} is above the 65% advisory threshold."
            )
        elif overall_hr < 0.50:
            insights.append(
                f"Overall hit rate of {_fmt_pct(overall_hr)} is below 50% — "
                "review decision scoring and thresholds."
            )

    return insights[:5]  # cap to 5 actionable insights


# ---------------------------------------------------------------------------
# Step 6 — Build full payload
# ---------------------------------------------------------------------------


def build_calibration(resolved: list[dict[str, Any]]) -> dict[str, Any]:
    overall = compute_overall(resolved)
    confidence_buckets = compute_confidence_buckets(resolved)
    validation_analysis = compute_validation_analysis(resolved)
    decision_analysis = compute_decision_analysis(resolved)
    insights = generate_insights(
        confidence_buckets, validation_analysis, decision_analysis, overall
    )
    return {
        "generated_at": datetime.now().isoformat(),
        "observe_only": True,
        "total_resolved": overall["total_resolved"],
        "overall_hit_rate": overall["overall_hit_rate"],
        "overall_avg_return": overall["overall_avg_return"],
        "confidence_buckets": confidence_buckets,
        "validation_analysis": validation_analysis,
        "decision_analysis": decision_analysis,
        "insights": insights,
    }


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def _fmt_rate(v: float | None) -> str:
    return f"{v:.0%}" if v is not None else "—"


def _fmt_ret(v: float | None) -> str:
    return f"{v:+.2%}" if v is not None else "—"


def _stats_table_row(label: str, stats: dict[str, Any]) -> str:
    return (
        f"| {label} | {stats.get('count', 0)} "
        f"| {_fmt_rate(stats.get('hit_rate'))} "
        f"| {_fmt_ret(stats.get('avg_return'))} |"
    )


def render_calibration_md(payload: dict[str, Any]) -> str:
    total = payload.get("total_resolved", 0)
    overall_hr = payload.get("overall_hit_rate")
    overall_ret = payload.get("overall_avg_return")
    generated_at = payload.get("generated_at", "—")

    lines: list[str] = [
        "# System Confidence Calibration",
        "",
        "> **Observe-only advisory system. No trades are executed.**",
        "> **Calibration is retrospective and for operator awareness only.**",
        "",
        f"Generated: {generated_at}",
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

    if payload.get("insufficient_data"):
        lines += [
            f"> Insufficient data: {total} resolved decisions "
            f"(minimum {payload.get('min_required', MIN_RESOLVED_ROWS)} required). "
            "Calibration will populate as history accumulates.",
            "",
        ]
        lines += ["---", "*Observe-only. No trades are executed by this system.*"]
        return "\n".join(lines).strip() + "\n"

    # Confidence buckets table
    conf = payload.get("confidence_buckets") or {}
    lines += [
        "## Confidence Bucket Analysis",
        "",
        "| Bucket | Count | Hit Rate | Avg Return |",
        "|--------|-------|----------|------------|",
    ]
    for key in ("low", "medium", "high", "unknown"):
        if key in conf and conf[key].get("count", 0) > 0:
            lines.append(_stats_table_row(key.capitalize(), conf[key]))
    lines.append("")

    # Validation status table
    val = payload.get("validation_analysis") or {}
    lines += [
        "## Validation Status Analysis",
        "",
        "| Status | Count | Hit Rate | Avg Return |",
        "|--------|-------|----------|------------|",
    ]
    for key in sorted(val):
        if val[key].get("count", 0) > 0:
            lines.append(_stats_table_row(key.replace("_", " ").title(), val[key]))
    lines.append("")

    # Decision type table
    dec = payload.get("decision_analysis") or {}
    lines += [
        "## Decision Type Analysis",
        "",
        "| Decision | Count | Hit Rate | Avg Return |",
        "|----------|-------|----------|------------|",
    ]
    for key in sorted(dec):
        if dec[key].get("count", 0) > 0:
            lines.append(_stats_table_row(key, dec[key]))
    lines.append("")

    # Insights
    insights = payload.get("insights") or []
    if insights:
        lines += ["## Key Insights", ""]
        for insight in insights:
            lines.append(f"- {insight}")
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


def run_calibration(
    root: Path | str | None = None,
    *,
    write_files: bool = True,
    min_resolved: int = MIN_RESOLVED_ROWS,
) -> tuple[dict[str, Any], str]:
    """
    Load decision_outcomes.jsonl → compute calibration metrics → write artifacts.

    Non-fatal: callers should wrap in try/except for pipeline safety.
    Returns (payload_dict, markdown_str).
    """
    root_path = Path(root) if root is not None else Path(".")
    jsonl_path = root_path.joinpath(*OUTCOMES_JSONL_RELATIVE_PATH)

    all_rows = _load_jsonl(jsonl_path)
    resolved = [r for r in all_rows if r.get("resolved") is True]
    total_resolved = len(resolved)

    if total_resolved < min_resolved:
        payload: dict[str, Any] = {
            "generated_at": datetime.now().isoformat(),
            "observe_only": True,
            "available": False,
            "insufficient_data": True,
            "total_resolved": total_resolved,
            "min_required": min_resolved,
            "overall_hit_rate": None,
            "overall_avg_return": None,
            "confidence_buckets": {},
            "validation_analysis": {},
            "decision_analysis": {},
            "insights": [],
            "summary_line": (
                f"Insufficient data: {total_resolved} resolved decisions "
                f"(minimum {min_resolved} required)."
            ),
        }
    else:
        payload = build_calibration(resolved)
        payload["available"] = True
        payload["insufficient_data"] = False
        payload["min_required"] = min_resolved
        hr_str = _fmt_rate(payload.get("overall_hit_rate"))
        payload["summary_line"] = (
            f"{total_resolved} resolved decisions analyzed. "
            f"Overall hit rate: {hr_str}."
        )

    markdown = render_calibration_md(payload)

    if write_files:
        json_path = root_path.joinpath(*CALIBRATION_JSON_RELATIVE_PATH)
        md_path = root_path.joinpath(*CALIBRATION_MD_RELATIVE_PATH)
        _safe_json_write(json_path, payload)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown, encoding="utf-8")

    return payload, markdown


if __name__ == "__main__":
    run_calibration()
