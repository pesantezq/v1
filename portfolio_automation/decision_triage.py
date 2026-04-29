from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("stockbot.portfolio_automation.decision_triage")

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

DECISION_PLAN_RELATIVE_PATH = ("outputs", "latest", "decision_plan.json")
AI_VALIDATION_RELATIVE_PATH = ("outputs", "latest", "ai_decision_validation.json")
OUTCOME_SUMMARY_RELATIVE_PATH = ("outputs", "policy", "decision_outcome_summary.json")
TRIAGE_JSON_RELATIVE_PATH = ("outputs", "latest", "decision_triage.json")
TRIAGE_MD_RELATIVE_PATH = ("outputs", "latest", "decision_triage.md")

# ---------------------------------------------------------------------------
# Bucket constants
# ---------------------------------------------------------------------------

BUCKET_CRITICAL = "critical_action"
BUCKET_ACTION = "action_candidate"
BUCKET_MONITOR = "monitor"
BUCKET_IGNORE = "ignore_for_now"

ALL_BUCKETS = (BUCKET_CRITICAL, BUCKET_ACTION, BUCKET_MONITOR, BUCKET_IGNORE)

# ---------------------------------------------------------------------------
# Thresholds / flag sets
# ---------------------------------------------------------------------------

_GUARDRAIL_FLAGS = frozenset({"leverage_breach", "concentration_breach"})
_DEGRADED_FLAGS = frozenset({"degraded_data", "degraded_mode", "cache_only", "fallback"})

_PRIORITY_LOW = 0.30
_PRIORITY_HIGH = 0.70
_PRIORITY_MID = 0.50

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _safe_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _safe_list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def _safe_str(v: Any) -> str:
    return str(v or "").strip()


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        result = float(v)
        return result if result == result else None  # exclude NaN
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Validation / watch_next lookup from ai_validation artifact
# ---------------------------------------------------------------------------


def _get_validation_status(
    symbol: str, decision: str, ai_validation: dict[str, Any]
) -> str:
    sym_up = symbol.upper()
    dec_up = decision.upper()
    for v in _safe_list(ai_validation.get("validations")):
        if (
            _safe_str(v.get("symbol")).upper() == sym_up
            and _safe_str(v.get("decision")).upper() == dec_up
        ):
            return _safe_str(v.get("validation_status")) or "unknown"
    return "unknown"


def _get_watch_next(
    symbol: str, decision: str, ai_validation: dict[str, Any]
) -> list[str]:
    sym_up = symbol.upper()
    dec_up = decision.upper()
    for v in _safe_list(ai_validation.get("validations")):
        if (
            _safe_str(v.get("symbol")).upper() == sym_up
            and _safe_str(v.get("decision")).upper() == dec_up
        ):
            return _safe_list(v.get("watch_next"))
    return []


# ---------------------------------------------------------------------------
# Row-level feature helpers
# ---------------------------------------------------------------------------


def _risk_flag_set(row: dict[str, Any]) -> frozenset[str]:
    return frozenset(
        str(f).strip().lower()
        for f in _safe_list(row.get("risk_flags"))
        if str(f).strip()
    )


def _has_degraded_signal(row: dict[str, Any]) -> bool:
    return bool(_risk_flag_set(row) & _DEGRADED_FLAGS)


def _is_portfolio_rebalance(row: dict[str, Any]) -> bool:
    source = _safe_str(row.get("source")).lower()
    if "rebalance" in source or "portfolio" in source:
        return True
    structured = _safe_dict(row.get("decision_reason_structured"))
    strategy = _safe_str(structured.get("strategy")).lower()
    return "rebalance" in strategy


# ---------------------------------------------------------------------------
# Core classification logic
# ---------------------------------------------------------------------------


def _classify_row(
    row: dict[str, Any],
    validation_status: str,
) -> tuple[str, str, str, str]:
    """Return (bucket, severity, reason, source_rule).

    Rules are checked top-to-bottom; first match wins.
    """
    decision = _safe_str(row.get("decision")).upper()
    urgency = _safe_str(row.get("urgency")).lower()
    risk_flags = _risk_flag_set(row)
    priority = _safe_float(row.get("priority")) or 0.0

    # Rule 1: SELL + guardrail flag → critical_action
    guardrail_hits = risk_flags & _GUARDRAIL_FLAGS
    if decision == "SELL" and guardrail_hits:
        violated = ", ".join(sorted(guardrail_hits))
        return (
            BUCKET_CRITICAL,
            "critical",
            f"SELL with {violated} — structural guardrail violation requires immediate review.",
            "sell_guardrail_breach",
        )

    # Rule 2: validation_status == contradiction → critical_action
    if validation_status == "contradiction":
        return (
            BUCKET_CRITICAL,
            "critical",
            f"{decision} has a detected contradiction between decision type and capital action.",
            "validation_contradiction",
        )

    # Rule 3: urgency == critical → critical_action
    if urgency == "critical":
        return (
            BUCKET_CRITICAL,
            "critical",
            f"{decision} flagged with critical urgency by the decision engine.",
            "urgency_critical",
        )

    # Rule 4: caution + structural risk flag → critical_action (structural elevation)
    if validation_status == "caution" and guardrail_hits:
        violated = ", ".join(sorted(guardrail_hits))
        return (
            BUCKET_CRITICAL,
            "high",
            f"{decision} is in caution state with structural risk flags ({violated}).",
            "caution_structural_risk",
        )

    # Rule 5: AVOID → ignore_for_now (checked before low-priority to make intent explicit)
    if decision == "AVOID":
        return (
            BUCKET_IGNORE,
            "low",
            "AVOID decision — signal suppressed or low relevance.",
            "avoid_decision",
        )

    # Rule 6: insufficient_context → ignore_for_now
    if validation_status == "insufficient_context":
        return (
            BUCKET_IGNORE,
            "low",
            f"{decision} has insufficient validation context — deprioritized.",
            "insufficient_context",
        )

    # Rule 7: low priority score → ignore_for_now
    if priority < _PRIORITY_LOW:
        return (
            BUCKET_IGNORE,
            "low",
            f"{decision} has low priority score ({priority:.2f}) — below action threshold.",
            "low_priority",
        )

    # Rule 8: BUY/SCALE + aligned + priority >= 0.70 → action_candidate
    if decision in {"BUY", "SCALE"} and validation_status == "aligned" and priority >= _PRIORITY_HIGH:
        return (
            BUCKET_ACTION,
            "high",
            f"{decision} is aligned and high-priority ({priority:.2f}) — candidate for action.",
            "aligned_high_priority",
        )

    # Rule 9: SCALE + portfolio rebalance + priority >= 0.50 → action_candidate
    if decision == "SCALE" and _is_portfolio_rebalance(row) and priority >= _PRIORITY_MID:
        return (
            BUCKET_ACTION,
            "medium",
            f"SCALE for portfolio rebalance with priority {priority:.2f} — eligible action candidate.",
            "scale_rebalance",
        )

    # Rule 10: WAIT/HOLD + degraded_data → monitor
    if decision in {"WAIT", "HOLD"} and _has_degraded_signal(row):
        return (
            BUCKET_MONITOR,
            "medium",
            f"{decision} with degraded data signal — monitor until data quality improves.",
            "wait_hold_degraded",
        )

    # Rule 11: caution → monitor
    if validation_status == "caution":
        return (
            BUCKET_MONITOR,
            "low",
            f"{decision} is in advisory caution state — watch but no immediate action required.",
            "caution_monitor",
        )

    # Default: monitor (conservative)
    return (
        BUCKET_MONITOR,
        "low",
        f"{decision} does not meet action thresholds — monitor.",
        "default_monitor",
    )


def _next_action_text(bucket: str, reason: str) -> str:
    if bucket == BUCKET_CRITICAL:
        return f"Review immediately — {reason}"
    if bucket == BUCKET_ACTION:
        return f"Evaluate — {reason}"
    if bucket == BUCKET_MONITOR:
        return "Watch — no immediate action required."
    return "No action needed."


# ---------------------------------------------------------------------------
# Single-row triage
# ---------------------------------------------------------------------------


def triage_single_decision(
    row: dict[str, Any],
    ai_validation: dict[str, Any],
) -> dict[str, Any]:
    """Classify one decision row. Never raises."""
    symbol = _safe_str(row.get("symbol") or "UNKNOWN")
    decision = _safe_str(row.get("decision") or "UNKNOWN").upper()
    priority = _safe_float(row.get("priority"))
    risk_flags = [_safe_str(f) for f in _safe_list(row.get("risk_flags")) if _safe_str(f)]
    validation_status = _get_validation_status(symbol, decision, ai_validation)
    watch_next = _get_watch_next(symbol, decision, ai_validation)

    bucket, severity, reason, source_rule = _classify_row(row, validation_status)

    return {
        "symbol": symbol,
        "decision": decision,
        "triage_bucket": bucket,
        "triage_rank": 0,  # filled by _rank_within_bucket
        "severity": severity,
        "reason": reason,
        "next_action": _next_action_text(bucket, reason),
        "source": source_rule,
        "priority": priority,
        "priority_score": priority,
        "validation_status": validation_status,
        "risk_flags": risk_flags,
        "watch_next": watch_next,
    }


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def _rank_within_bucket(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by severity then priority descending; assign 1-based triage_rank."""
    sorted_rows = sorted(
        rows,
        key=lambda r: (
            _SEVERITY_ORDER.get(r.get("severity", "low"), 3),
            -(r.get("priority") or 0.0),
        ),
    )
    for i, row in enumerate(sorted_rows, 1):
        row["triage_rank"] = i
    return sorted_rows


# ---------------------------------------------------------------------------
# Build full triage payload
# ---------------------------------------------------------------------------


def build_triage(
    decision_plan: dict[str, Any],
    ai_validation: dict[str, Any],
    outcome_summary: dict[str, Any] | None = None,  # reserved for future enrichment
) -> dict[str, Any]:
    decisions = _safe_list(decision_plan.get("decisions"))

    triage_rows: list[dict[str, Any]] = []
    for row in decisions:
        triage_rows.append(triage_single_decision(row, ai_validation))

    # Group into buckets
    buckets: dict[str, list[dict[str, Any]]] = {b: [] for b in ALL_BUCKETS}
    for row in triage_rows:
        bucket = row.get("triage_bucket", BUCKET_MONITOR)
        buckets.setdefault(bucket, []).append(row)

    # Sort and rank within each bucket
    for bucket in ALL_BUCKETS:
        buckets[bucket] = _rank_within_bucket(buckets[bucket])

    # Top actions: critical first, then action candidates (up to 5 total)
    top_actions = (buckets[BUCKET_CRITICAL] + buckets[BUCKET_ACTION])[:5]

    bucket_counts = {b: len(buckets[b]) for b in ALL_BUCKETS}

    return {
        "generated_at": datetime.now().isoformat(),
        "observe_only": True,
        "total_decisions": len(triage_rows),
        "bucket_counts": bucket_counts,
        "top_actions": top_actions,
        "buckets": buckets,
    }


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def render_triage_md(payload: dict[str, Any]) -> str:
    counts = payload.get("bucket_counts") or {}
    critical_n = counts.get(BUCKET_CRITICAL, 0)
    action_n = counts.get(BUCKET_ACTION, 0)
    monitor_n = counts.get(BUCKET_MONITOR, 0)
    ignore_n = counts.get(BUCKET_IGNORE, 0)
    total = payload.get("total_decisions", 0)

    lines: list[str] = [
        "# Decision Triage",
        "",
        "> **Observe-only advisory system. No trades are executed.**",
        "> **Triage is for operator attention guidance only.**",
        "",
        f"Generated: {payload.get('generated_at', '-')}",
        "",
        "## Summary",
        "",
        "| Bucket | Count |",
        "|--------|-------|",
        f"| Critical Action | {critical_n} |",
        f"| Action Candidate | {action_n} |",
        f"| Monitor | {monitor_n} |",
        f"| Ignore For Now | {ignore_n} |",
        f"| **Total** | **{total}** |",
        "",
    ]

    top_actions = payload.get("top_actions") or []
    if top_actions:
        lines += ["## Top Actions Today", ""]
        for i, row in enumerate(top_actions[:5], 1):
            lines.append(
                f"{i}. **{row.get('decision')} {row.get('symbol')}** "
                f"[{row.get('triage_bucket')} / {row.get('severity')}] — "
                f"{row.get('reason', '-')}"
            )
        lines.append("")

    buckets = payload.get("buckets") or {}

    critical_rows = buckets.get(BUCKET_CRITICAL, [])
    if critical_rows:
        lines += ["## Critical Action", ""]
        for row in critical_rows:
            lines += [
                f"### {row.get('decision')} {row.get('symbol')} (rank {row.get('triage_rank')})",
                f"- Severity: {row.get('severity')}",
                f"- Reason: {row.get('reason')}",
                f"- Next: {row.get('next_action')}",
                f"- Validation: {row.get('validation_status')}",
                f"- Priority: {row.get('priority')}",
                "",
            ]

    action_rows = buckets.get(BUCKET_ACTION, [])
    if action_rows:
        lines += ["## Action Candidates", ""]
        for row in action_rows:
            lines += [
                f"### {row.get('decision')} {row.get('symbol')} (rank {row.get('triage_rank')})",
                f"- Severity: {row.get('severity')}",
                f"- Reason: {row.get('reason')}",
                f"- Next: {row.get('next_action')}",
                f"- Validation: {row.get('validation_status')}",
                f"- Priority: {row.get('priority')}",
                "",
            ]

    monitor_rows = buckets.get(BUCKET_MONITOR, [])
    if monitor_rows:
        lines += ["## Monitor", ""]
        for row in monitor_rows:
            lines.append(
                f"- **{row.get('decision')} {row.get('symbol')}**: "
                f"{row.get('reason')} (priority={row.get('priority')})"
            )
        lines.append("")

    ignore_rows = buckets.get(BUCKET_IGNORE, [])
    if ignore_rows:
        lines += ["## Ignore For Now", ""]
        for row in ignore_rows:
            lines.append(
                f"- {row.get('decision')} {row.get('symbol')} — {row.get('reason')}"
            )
        lines.append("")

    lines += [
        "---",
        "*Observe-only. No trades are executed by this system.*",
    ]

    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _safe_json_load(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}, "malformed"
    if not isinstance(payload, dict):
        return {}, "malformed"
    return payload, "ok"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_triage(
    root: Path | str | None = None,
    *,
    write_files: bool = True,
) -> tuple[dict[str, Any], str]:
    """
    Orchestrate: load artifacts → classify → rank → write.

    Non-fatal: callers should wrap in try/except for pipeline safety.
    Returns (payload_dict, markdown_str).
    """
    root_path = Path(root) if root is not None else Path(".")

    decision_plan, plan_status = _safe_json_load(
        root_path.joinpath(*DECISION_PLAN_RELATIVE_PATH)
    )
    ai_validation, _ = _safe_json_load(
        root_path.joinpath(*AI_VALIDATION_RELATIVE_PATH)
    )
    outcome_summary, _ = _safe_json_load(
        root_path.joinpath(*OUTCOME_SUMMARY_RELATIVE_PATH)
    )

    if plan_status != "ok":
        payload: dict[str, Any] = {
            "generated_at": datetime.now().isoformat(),
            "observe_only": True,
            "available": False,
            "total_decisions": 0,
            "bucket_counts": {b: 0 for b in ALL_BUCKETS},
            "top_actions": [],
            "buckets": {b: [] for b in ALL_BUCKETS},
            "summary_line": (
                "Decision plan unavailable."
                if plan_status == "missing"
                else "Decision plan malformed."
            ),
        }
    else:
        payload = build_triage(decision_plan, ai_validation, outcome_summary or {})
        payload["available"] = True
        payload["summary_line"] = (
            f"{payload['total_decisions']} decisions triaged. "
            f"{payload['bucket_counts'].get(BUCKET_CRITICAL, 0)} critical, "
            f"{payload['bucket_counts'].get(BUCKET_ACTION, 0)} action candidate(s)."
        )

    markdown = render_triage_md(payload)

    if write_files:
        json_path = root_path.joinpath(*TRIAGE_JSON_RELATIVE_PATH)
        md_path = root_path.joinpath(*TRIAGE_MD_RELATIVE_PATH)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        md_path.write_text(markdown, encoding="utf-8")

    return payload, markdown


if __name__ == "__main__":
    run_triage()
