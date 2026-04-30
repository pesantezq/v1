from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("stockbot.portfolio_automation.confidence_calibration")

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

OUTCOMES_JSONL_RELATIVE_PATH = ("outputs", "policy", "decision_outcomes.jsonl")
CALIBRATION_JSON_RELATIVE_PATH = ("outputs", "policy", "confidence_calibration.json")
CALIBRATION_MD_RELATIVE_PATH = ("outputs", "policy", "confidence_calibration.md")
LATEST_CALIBRATION_JSON_RELATIVE_PATH = ("outputs", "latest", "confidence_calibration.json")
LATEST_CALIBRATION_MD_RELATIVE_PATH = ("outputs", "latest", "confidence_calibration.md")
DQ_REPORT_RELATIVE_PATH = ("outputs", "latest", "data_quality_report.json")

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

# Enhanced calibration thresholds
_OVERCONFIDENT_GAP = 0.15    # avg_confidence - hit_rate > 0.15 → overconfident
_UNDERCONFIDENT_GAP = -0.15  # avg_confidence - hit_rate < -0.15 → underconfident
_MIN_SIGNAL_RESOLVED = 5     # min resolved rows per signal to include in per-signal analysis

# 5-bucket system: (label, lower_inclusive, upper_exclusive)
# Upper bound of very_high is 1.01 so that confidence=1.0 is included
CONFIDENCE_BUCKETS_5 = (
    ("very_low",  0.00, 0.25),
    ("low",       0.25, 0.50),
    ("medium",    0.50, 0.70),
    ("high",      0.70, 0.85),
    ("very_high", 0.85, 1.01),
)

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
# Enhanced calibration dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CalibrationBucket:
    label: str
    lower: float
    upper: float
    count: int = 0
    correct: int = 0
    total_confidence: float = 0.0

    @property
    def hit_rate(self) -> float | None:
        return self.correct / self.count if self.count else None

    @property
    def average_confidence(self) -> float | None:
        return self.total_confidence / self.count if self.count else None

    @property
    def calibration_gap(self) -> float | None:
        ac = self.average_confidence
        hr = self.hit_rate
        if ac is None or hr is None:
            return None
        return ac - hr


@dataclass
class SignalCalibrationResult:
    signal_id: str
    known_in_registry: bool
    discovery_only: bool
    count: int
    hit_rate: float | None
    average_confidence: float | None
    calibration_gap: float | None
    overconfident: bool
    underconfident: bool
    suggested_review: bool
    note: str


@dataclass
class ConfidenceCalibrationSummary:
    generated_at: str
    observe_only: bool
    available: bool
    insufficient_data: bool
    total_resolved: int
    min_required: int
    overall_hit_rate: float | None
    overall_average_confidence: float | None
    overall_calibration_gap: float | None
    buckets_5: list[CalibrationBucket]
    signal_results: list[SignalCalibrationResult]
    dq_warnings: list[str]
    summary_line: str


# ---------------------------------------------------------------------------
# Enhanced calibration helpers
# ---------------------------------------------------------------------------


def _normalize_confidence(v: float) -> float:
    return v / 100.0 if v > 1.0 else v


def _compute_bucket_5(confidence: float | None) -> str:
    if confidence is None:
        return "unknown"
    for label, lower, upper in CONFIDENCE_BUCKETS_5:
        if lower <= confidence < upper:
            return label
    return "very_high"  # fallback for conf >= 1.01 edge case


def _compute_calibration_buckets_5(rows: list[dict[str, Any]]) -> list[CalibrationBucket]:
    buckets: dict[str, CalibrationBucket] = {
        label: CalibrationBucket(label, lower, upper)
        for label, lower, upper in CONFIDENCE_BUCKETS_5
    }
    for row in rows:
        conf_raw = _safe_float(row.get("confidence"))
        if conf_raw is None:
            continue
        conf = _normalize_confidence(conf_raw)
        label = _compute_bucket_5(conf)
        if label not in buckets:
            continue
        b = buckets[label]
        b.count += 1
        b.total_confidence += conf
        if row.get("direction_correct") is True:
            b.correct += 1
    return list(buckets.values())


def _compute_signal_result(
    signal_id: str,
    rows: list[dict[str, Any]],
    registry: Any | None,
) -> SignalCalibrationResult:
    known = registry is not None and registry.validate_signal_id(signal_id)
    discovery_only = (registry is None) or registry.is_discovery_only(signal_id)

    judgeables = [r for r in rows if r.get("direction_correct") is not None]
    correct = sum(1 for r in judgeables if r.get("direction_correct") is True)
    hit_rate = correct / len(judgeables) if judgeables else None

    confs = [
        _normalize_confidence(c)
        for r in rows
        if (c := _safe_float(r.get("confidence"))) is not None
    ]
    avg_conf = sum(confs) / len(confs) if confs else None

    gap = (avg_conf - hit_rate) if (avg_conf is not None and hit_rate is not None) else None

    overconfident = gap is not None and gap > _OVERCONFIDENT_GAP
    underconfident = gap is not None and gap < _UNDERCONFIDENT_GAP
    suggested_review = (not discovery_only) and (overconfident or underconfident)

    if discovery_only:
        note = "Discovery-only signal: observe mode only, not eligible for tuning."
    elif overconfident:
        note = f"Overconfident by {gap:.2f} — consider lowering confidence floor."
    elif underconfident:
        note = f"Underconfident by {abs(gap):.2f} — consider raising confidence floor."
    else:
        note = "Within calibration tolerance."

    return SignalCalibrationResult(
        signal_id=signal_id,
        known_in_registry=known,
        discovery_only=discovery_only,
        count=len(rows),
        hit_rate=hit_rate,
        average_confidence=avg_conf,
        calibration_gap=gap,
        overconfident=overconfident,
        underconfident=underconfident,
        suggested_review=suggested_review,
        note=note,
    )


def _extract_dq_context(dq_report: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if not dq_report:
        return warnings
    for issue in dq_report.get("issues", []):
        if isinstance(issue, dict):
            sev = issue.get("severity", "")
            msg = issue.get("message", "") or issue.get("issue_type", "")
            if sev in ("critical", "warning") and msg:
                warnings.append(f"[{sev.upper()}] {msg}")
    if dq_report.get("degraded_mode"):
        warnings.append("[WARNING] System in degraded mode during data collection.")
    return warnings[:10]


def _summary_to_dict(summary: ConfidenceCalibrationSummary) -> dict[str, Any]:
    return {
        "generated_at": summary.generated_at,
        "observe_only": summary.observe_only,
        "available": summary.available,
        "insufficient_data": summary.insufficient_data,
        "total_resolved": summary.total_resolved,
        "min_required": summary.min_required,
        "overall_hit_rate": summary.overall_hit_rate,
        "overall_average_confidence": summary.overall_average_confidence,
        "overall_calibration_gap": summary.overall_calibration_gap,
        "buckets_5": [
            {
                "label": b.label,
                "lower": b.lower,
                "upper": b.upper,
                "count": b.count,
                "hit_rate": b.hit_rate,
                "average_confidence": b.average_confidence,
                "calibration_gap": b.calibration_gap,
            }
            for b in summary.buckets_5
        ],
        "signal_results": [
            {
                "signal_id": s.signal_id,
                "known_in_registry": s.known_in_registry,
                "discovery_only": s.discovery_only,
                "count": s.count,
                "hit_rate": s.hit_rate,
                "average_confidence": s.average_confidence,
                "calibration_gap": s.calibration_gap,
                "overconfident": s.overconfident,
                "underconfident": s.underconfident,
                "suggested_review": s.suggested_review,
                "note": s.note,
            }
            for s in summary.signal_results
        ],
        "dq_warnings": summary.dq_warnings,
        "summary_line": summary.summary_line,
    }


def _build_enhanced_markdown(summary: ConfidenceCalibrationSummary) -> str:
    lines: list[str] = [
        "# System Confidence Calibration (Enhanced)",
        "",
        "> **Observe-only advisory system. No trades are executed.**",
        "> **Calibration is retrospective and for operator awareness only.**",
        "",
        f"Generated: {summary.generated_at}",
        f"Total resolved: {summary.total_resolved}",
        "",
    ]

    if summary.insufficient_data:
        lines += [
            f"> Insufficient data: {summary.total_resolved} resolved decisions "
            f"(minimum {summary.min_required} required).",
            "",
            "---",
            "*Observe-only. No trades are executed by this system.*",
        ]
        return "\n".join(lines).strip() + "\n"

    gap_str = f"{summary.overall_calibration_gap:+.3f}" if summary.overall_calibration_gap is not None else "—"
    hr_str = f"{summary.overall_hit_rate:.0%}" if summary.overall_hit_rate is not None else "—"
    conf_str = f"{summary.overall_average_confidence:.0%}" if summary.overall_average_confidence is not None else "—"

    lines += [
        "## Overall Calibration",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Overall hit rate | {hr_str} |",
        f"| Average confidence | {conf_str} |",
        f"| Calibration gap | {gap_str} |",
        "",
        "## 5-Bucket Confidence Analysis",
        "",
        "| Bucket | Count | Hit Rate | Avg Confidence | Gap |",
        "|--------|-------|----------|----------------|-----|",
    ]
    for b in summary.buckets_5:
        hr = f"{b.hit_rate:.0%}" if b.hit_rate is not None else "—"
        ac = f"{b.average_confidence:.0%}" if b.average_confidence is not None else "—"
        g = f"{b.calibration_gap:+.3f}" if b.calibration_gap is not None else "—"
        lines.append(f"| {b.label} | {b.count} | {hr} | {ac} | {g} |")
    lines.append("")

    if summary.signal_results:
        lines += [
            "## Per-Signal Calibration",
            "",
            "| Signal | Count | Hit Rate | Gap | Review? | Note |",
            "|--------|-------|----------|-----|---------|------|",
        ]
        for s in summary.signal_results:
            hr = f"{s.hit_rate:.0%}" if s.hit_rate is not None else "—"
            g = f"{s.calibration_gap:+.3f}" if s.calibration_gap is not None else "—"
            review = "Yes" if s.suggested_review else "No"
            lines.append(f"| {s.signal_id} | {s.count} | {hr} | {g} | {review} | {s.note} |")
        lines.append("")

    if summary.dq_warnings:
        lines += ["## Data Quality Context", ""]
        for w in summary.dq_warnings:
            lines.append(f"- {w}")
        lines.append("")

    lines += ["---", "*Observe-only. No trades are executed by this system.*"]
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# New public functions — enhanced calibration layer
# ---------------------------------------------------------------------------


def load_decision_outcomes(root: Path | str | None = None) -> list[dict[str, Any]]:
    root_path = Path(root) if root is not None else Path(".")
    return _load_jsonl(root_path.joinpath(*OUTCOMES_JSONL_RELATIVE_PATH))


def load_data_quality_report(root: Path | str | None = None) -> dict[str, Any]:
    root_path = Path(root) if root is not None else Path(".")
    path = root_path.joinpath(*DQ_REPORT_RELATIVE_PATH)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def evaluate_confidence_calibration(
    resolved: list[dict[str, Any]],
    dq_report: dict[str, Any] | None = None,
    registry: Any | None = None,
    min_resolved: int = MIN_RESOLVED_ROWS,
    min_signal_resolved: int = _MIN_SIGNAL_RESOLVED,
) -> ConfidenceCalibrationSummary:
    """
    Pure evaluation — no file I/O. Returns ConfidenceCalibrationSummary.
    Non-fatal: callers should wrap in try/except for pipeline safety.
    """
    total = len(resolved)
    ts = datetime.now(timezone.utc).isoformat()
    dq = dq_report or {}

    if total < min_resolved:
        return ConfidenceCalibrationSummary(
            generated_at=ts,
            observe_only=True,
            available=True,
            insufficient_data=True,
            total_resolved=total,
            min_required=min_resolved,
            overall_hit_rate=None,
            overall_average_confidence=None,
            overall_calibration_gap=None,
            buckets_5=[],
            signal_results=[],
            dq_warnings=_extract_dq_context(dq),
            summary_line=(
                f"Insufficient data: {total} resolved decisions "
                f"(minimum {min_resolved} required)."
            ),
        )

    judgeables = [r for r in resolved if r.get("direction_correct") is not None]
    correct = sum(1 for r in judgeables if r.get("direction_correct") is True)
    hit_rate = correct / len(judgeables) if judgeables else None

    confs = [
        _normalize_confidence(c)
        for r in resolved
        if (c := _safe_float(r.get("confidence"))) is not None
    ]
    avg_conf = sum(confs) / len(confs) if confs else None
    gap = (avg_conf - hit_rate) if (avg_conf is not None and hit_rate is not None) else None

    buckets_5 = _compute_calibration_buckets_5(resolved)

    signal_groups: dict[str, list[dict[str, Any]]] = {}
    for row in resolved:
        sid = _safe_str(row.get("source") or "")
        if not sid or sid == "unknown":
            continue
        signal_groups.setdefault(sid, []).append(row)

    signal_results = [
        _compute_signal_result(sid, rows, registry)
        for sid, rows in sorted(signal_groups.items())
        if len(rows) >= min_signal_resolved
    ]

    hr_str = f"{hit_rate:.0%}" if hit_rate is not None else "—"
    return ConfidenceCalibrationSummary(
        generated_at=ts,
        observe_only=True,
        available=True,
        insufficient_data=False,
        total_resolved=total,
        min_required=min_resolved,
        overall_hit_rate=hit_rate,
        overall_average_confidence=avg_conf,
        overall_calibration_gap=gap,
        buckets_5=buckets_5,
        signal_results=signal_results,
        dq_warnings=_extract_dq_context(dq),
        summary_line=f"{total} resolved decisions. Overall hit rate: {hr_str}.",
    )


def write_confidence_calibration_report(
    root: Path | str | None = None,
    *,
    min_resolved: int = MIN_RESOLVED_ROWS,
    min_signal_resolved: int = _MIN_SIGNAL_RESOLVED,
) -> ConfidenceCalibrationSummary:
    """
    Load outcomes + DQ report, evaluate, write enhanced artifacts to outputs/latest/.
    Non-fatal: callers should wrap in try/except for pipeline safety.
    """
    from portfolio_automation.data_governance import (
        OutputNamespace,
        safe_write_json,
        safe_write_text,
    )

    root_path = Path(root) if root is not None else Path(".")
    base_dir = str(root_path / "outputs")

    all_rows = load_decision_outcomes(root_path)
    resolved = [r for r in all_rows if r.get("resolved") is True]
    dq_report = load_data_quality_report(root_path)

    registry = None
    try:
        from portfolio_automation.signal_registry import load_signal_registry
        registry = load_signal_registry()
    except Exception:
        pass

    summary = evaluate_confidence_calibration(
        resolved,
        dq_report=dq_report,
        registry=registry,
        min_resolved=min_resolved,
        min_signal_resolved=min_signal_resolved,
    )

    payload = _summary_to_dict(summary)
    safe_write_json(
        OutputNamespace.LATEST,
        "confidence_calibration.json",
        payload,
        base_dir=base_dir,
    )
    markdown = _build_enhanced_markdown(summary)
    safe_write_text(
        OutputNamespace.LATEST,
        "confidence_calibration.md",
        markdown,
        base_dir=base_dir,
    )

    return summary


# ---------------------------------------------------------------------------
# Public entry point (legacy — POLICY write; also triggers LATEST write)
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
        # POLICY write — backward-compatible; GUI reads from here
        json_path = root_path.joinpath(*CALIBRATION_JSON_RELATIVE_PATH)
        md_path = root_path.joinpath(*CALIBRATION_MD_RELATIVE_PATH)
        _safe_json_write(json_path, payload)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown, encoding="utf-8")
        # LATEST write — enhanced report; non-blocking
        try:
            write_confidence_calibration_report(root_path, min_resolved=min_resolved)
        except Exception as _err:
            logger.warning("confidence_calibration: LATEST write non-fatal — %s", _err)

    return payload, markdown


if __name__ == "__main__":
    run_calibration()
