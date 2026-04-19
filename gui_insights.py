"""
GUI Insights
============
Pure Python — no Streamlit. Converts existing analytics artifacts into
operator-readable insight cards that synthesize confidence, rotation,
execution, and data-trust signals.

Public API:
    generate_insights(pa, rot_events) -> list[InsightCard]

Always returns exactly four InsightCards (one per category).  Degrades
gracefully when artifacts are missing or data is thin.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class InsightCard:
    category: str   # "Confidence" | "Rotation" | "Execution" | "Data Trust"
    title: str
    status: str     # "Healthy" | "Watch" | "Investigate" | "Insufficient Data"
    trust: str      # "low" | "medium" | "high"
    guidance: str   # one-sentence operator note
    detail: str     # short supporting metric string


def generate_insights(
    pa: dict[str, Any] | None,
    rot_events: list[dict[str, Any]] | None,
) -> list[InsightCard]:
    """
    Synthesise existing analytics artifacts into four insight cards.

    Args:
        pa:         profit_attribution.json dict (or empty/None when absent).
        rot_events: list of rotation_events.jsonl records (or empty/None).

    Returns:
        Exactly four InsightCards: Confidence, Rotation, Execution, Data Trust.
    """
    _pa = pa or {}
    _rot = list(rot_events or [])
    return [
        _confidence_insight(_pa),
        _rotation_insight(_rot),
        _execution_insight(_pa),
        _data_trust_insight(_pa),
    ]


# ---------------------------------------------------------------------------
# A. Confidence insight
# ---------------------------------------------------------------------------

def _confidence_insight(pa: dict[str, Any]) -> InsightCard:
    cat = "Confidence"
    ex = pa.get("execution") or {}
    cal = ex.get("confidence_calibration") or {}

    if not cal:
        return InsightCard(
            cat, "Confidence Calibration", "Insufficient Data", "low",
            "No confidence calibration data available yet. "
            "Requires execution attribution with resolved outcomes.",
            "—",
        )

    ss = cal.get("sample_summary") or {}
    total_m = _coerce_int(ss.get("total_matched"))
    cal_status = str(cal.get("status") or "no_data")
    band_order_valid = cal.get("band_order_valid")

    if total_m < 5:
        return InsightCard(
            cat, "Confidence Calibration", "Insufficient Data", "low",
            f"Only {total_m} matched execution event(s) — "
            "calibration conclusions are not yet reliable.",
            f"matched={total_m}",
        )

    trust = "high" if total_m >= 20 else ("medium" if total_m >= 10 else "low")

    if band_order_valid is False:
        return InsightCard(
            cat, "Confidence Calibration", "Investigate", "medium",
            "High-confidence recommendations are not outperforming lower-confidence tiers. "
            "Consider reviewing confidence score calibration.",
            f"matched={total_m} · band_order=inverted",
        )

    if cal_status == "healthy" and band_order_valid is True:
        return InsightCard(
            cat, "Confidence Calibration", "Healthy", trust,
            "High-confidence recommendations are outperforming lower-confidence ones. "
            "Tiers appear well-separated.",
            f"matched={total_m} · status={cal_status}",
        )

    if cal_status == "weak_separation":
        return InsightCard(
            cat, "Confidence Calibration", "Watch", trust,
            "Confidence tiers show weak win-rate separation. "
            "Monitor as more outcomes resolve.",
            f"matched={total_m} · status={cal_status}",
        )

    if cal_status == "insufficient_data":
        return InsightCard(
            cat, "Confidence Calibration", "Insufficient Data", "low",
            "Insufficient execution events to draw calibration conclusions.",
            f"matched={total_m}",
        )

    return InsightCard(
        cat, "Confidence Calibration", "Insufficient Data", "low",
        "Calibration status could not be determined.",
        f"status={cal_status}",
    )


# ---------------------------------------------------------------------------
# B. Rotation insight
# ---------------------------------------------------------------------------

def _rotation_insight(rot_events: list[dict[str, Any]]) -> InsightCard:
    cat = "Rotation"
    total = len(rot_events)

    if total == 0:
        return InsightCard(
            cat, "Momentum Rotation Quality", "Insufficient Data", "low",
            "No rotation events logged yet. "
            "Panel populates when exit evaluation runs with a challenger opportunity.",
            "—",
        )

    triggered = sum(1 for e in rot_events if e.get("rotation_triggered"))
    resolved = sum(1 for e in rot_events if e.get("outcome_resolved"))
    detail = f"total={total} · triggered={triggered} · resolved={resolved}"

    if total < 5:
        return InsightCard(
            cat, "Momentum Rotation Quality", "Insufficient Data", "low",
            f"Only {total} rotation evaluation(s) logged — too few to assess quality.",
            detail,
        )

    if resolved == 0:
        return InsightCard(
            cat, "Momentum Rotation Quality", "Insufficient Data", "low",
            "Rotation events are accumulating but no outcomes have resolved yet. "
            "Check back after T+5d.",
            detail,
        )

    trust = "medium" if resolved >= 10 else "low"

    if triggered == 0:
        return InsightCard(
            cat, "Momentum Rotation Quality", "Insufficient Data", "low",
            "Rotation was evaluated but never triggered in this window. "
            "No trigger quality data available yet.",
            detail,
        )

    small_margin = sum(
        1 for e in rot_events
        if e.get("rotation_triggered")
        and e.get("actual_margin") is not None
        and e.get("required_margin") is not None
        and _coerce_float(e["actual_margin"]) < _coerce_float(e["required_margin"]) * 1.25
    )
    near_threshold_rate = small_margin / triggered if triggered > 0 else 0.0

    if near_threshold_rate > 0.5 and triggered >= 3:
        resolved_trig = [
            e for e in rot_events
            if e.get("rotation_triggered") and e.get("outcome_resolved")
            and e.get("forward_return_5d") is not None
        ]
        if resolved_trig:
            avg_return = sum(
                _coerce_float(e["forward_return_5d"]) for e in resolved_trig
            ) / len(resolved_trig)
            if avg_return < 0:
                return InsightCard(
                    cat, "Momentum Rotation Quality", "Investigate", trust,
                    f"{int(near_threshold_rate * 100)}% of triggered rotations are near-threshold "
                    "and resolved outcomes are negative on average. "
                    "Consider reviewing the rotation gap threshold later.",
                    detail + f" · near_thr={int(near_threshold_rate * 100)}% · avg_ret={avg_return:+.1%}",
                )
        return InsightCard(
            cat, "Momentum Rotation Quality", "Watch", trust,
            f"{int(near_threshold_rate * 100)}% of triggered rotations are near-threshold. "
            "Monitor forward outcomes before drawing conclusions.",
            detail + f" · near_thr={int(near_threshold_rate * 100)}%",
        )

    trig_resolved = [
        e for e in rot_events
        if e.get("rotation_triggered") and e.get("outcome_resolved")
        and e.get("forward_return_5d") is not None
    ]
    if trig_resolved:
        avg_ret = sum(_coerce_float(e["forward_return_5d"]) for e in trig_resolved) / len(trig_resolved)
        wins = sum(1 for e in trig_resolved if _coerce_float(e["forward_return_5d"]) > 0)
        win_rate = wins / len(trig_resolved)
        if win_rate < 0.4:
            return InsightCard(
                cat, "Momentum Rotation Quality", "Investigate", trust,
                f"Triggered rotations have a win rate of {win_rate:.0%} — below expectations. "
                "Evaluate whether rotation gap thresholds are appropriate.",
                detail + f" · win_rate={win_rate:.0%} · avg_ret={avg_ret:+.1%}",
            )

    return InsightCard(
        cat, "Momentum Rotation Quality", "Healthy", trust,
        "Momentum rotation quality appears acceptable. "
        "Continue monitoring as more outcomes resolve.",
        detail,
    )


# ---------------------------------------------------------------------------
# C. Execution insight
# ---------------------------------------------------------------------------

def _execution_insight(pa: dict[str, Any]) -> InsightCard:
    cat = "Execution"
    ex = pa.get("execution") or {}

    if not ex:
        return InsightCard(
            cat, "Execution Attribution", "Insufficient Data", "low",
            "No execution attribution data available yet.",
            "—",
        )

    total = _coerce_int(ex.get("total_events"))
    matched = _coerce_int(ex.get("matched_events"))
    match_rate = _coerce_float(ex.get("match_rate"))

    if total < 3:
        return InsightCard(
            cat, "Execution Attribution", "Insufficient Data", "low",
            f"Only {total} execution event(s) logged — too few to assess quality.",
            f"total={total}",
        )

    if match_rate < 0.30:
        return InsightCard(
            cat, "Execution Attribution", "Insufficient Data", "low",
            f"Match rate is {match_rate:.0%} — too low for action-level results to be reliable. "
            "Execution insights pending more matched outcomes.",
            f"total={total} · matched={matched} · match_rate={match_rate:.0%}",
        )

    trust = "high" if (matched >= 15 and match_rate >= 0.70) else ("medium" if matched >= 7 else "low")
    detail = f"total={total} · matched={matched} · match_rate={match_rate:.0%}"

    if match_rate < 0.50:
        return InsightCard(
            cat, "Execution Attribution", "Watch", "low",
            f"Match rate is {match_rate:.0%} — execution attribution is incomplete. "
            "Results should be treated as preliminary.",
            detail,
        )

    by_action = list(ex.get("by_action") or [])
    buy = next((a for a in by_action if a.get("action") == "BUY"), None)

    if buy and buy.get("win_rate") is not None:
        buy_wr = _coerce_float(buy.get("win_rate"))
        buy_exp = buy.get("expectancy")
        buy_n = _coerce_int(buy.get("matched_events"))

        if buy_n >= 3:
            if buy_wr >= 0.60 and (buy_exp is None or _coerce_float(buy_exp) >= 0):
                return InsightCard(
                    cat, "Execution Attribution", "Healthy", trust,
                    f"BUY actions have a {buy_wr:.0%} win rate with positive expectancy. "
                    "Execution quality looks reasonable.",
                    detail + f" · buy_wr={buy_wr:.0%}",
                )
            if buy_wr < 0.40 or (buy_exp is not None and _coerce_float(buy_exp) < 0):
                return InsightCard(
                    cat, "Execution Attribution", "Watch", trust,
                    f"BUY win rate is {buy_wr:.0%}. "
                    "Monitor whether this reflects signal quality or execution timing.",
                    detail + f" · buy_wr={buy_wr:.0%}",
                )

    return InsightCard(
        cat, "Execution Attribution", "Watch", trust,
        "Execution events matched but BUY performance data is limited. "
        "Monitor as more outcomes resolve.",
        detail,
    )


# ---------------------------------------------------------------------------
# D. Data trust insight
# ---------------------------------------------------------------------------

def _data_trust_insight(pa: dict[str, Any]) -> InsightCard:
    cat = "Data Trust"

    if not pa:
        return InsightCard(
            cat, "Attribution Data Quality", "Insufficient Data", "low",
            "Attribution artifact not available yet. "
            "Run the system after signals have resolved.",
            "—",
        )

    m = pa.get("metrics") or {}
    total = _coerce_int(m.get("total_entries"))
    coverage = _coerce_float(m.get("coverage_rate"))
    detail = f"entries={total} · coverage={coverage:.0%}"

    if total == 0:
        return InsightCard(
            cat, "Attribution Data Quality", "Insufficient Data", "low",
            "Attribution file exists but contains no entries yet.",
            detail,
        )

    if total < 5:
        return InsightCard(
            cat, "Attribution Data Quality", "Insufficient Data", "low",
            f"Only {total} attribution entries — not enough for reliable conclusions.",
            detail,
        )

    trust = "high" if (total >= 20 and coverage >= 0.70) else ("medium" if total >= 10 else "low")

    if coverage < 0.50:
        return InsightCard(
            cat, "Attribution Data Quality", "Watch", trust,
            f"Coverage rate is {coverage:.0%} — fewer than half of entries have resolved outcomes. "
            "Safe to monitor but not to tune.",
            detail,
        )

    ex = pa.get("execution") or {}
    bands = ex.get("by_confidence_band") or []
    if bands and all(b.get("small_sample", False) for b in bands):
        return InsightCard(
            cat, "Attribution Data Quality", "Watch", "low",
            "All confidence bands are flagged as small-sample. "
            "Metrics are preliminary — monitor only.",
            detail + " · all_bands_small=True",
        )

    if total >= 10 and coverage >= 0.70:
        return InsightCard(
            cat, "Attribution Data Quality", "Healthy", trust,
            f"{total} entries with {coverage:.0%} coverage. "
            "Attribution data is sufficient for monitoring.",
            detail,
        )

    return InsightCard(
        cat, "Attribution Data Quality", "Watch", trust,
        f"{total} entries logged with {coverage:.0%} coverage. "
        "Data is building — safe to monitor but conclusions are preliminary.",
        detail,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _coerce_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _coerce_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
