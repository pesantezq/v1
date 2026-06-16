"""
Unified Crowd Intelligence — artifact assembly + the pipeline entrypoint.

``run(root)`` is the non-blocking entrypoint wired as a daily pipeline stage (after
both crowd lanes have written their artifacts). It NEVER raises; on any failure it
writes a degraded status and returns it.

Writes (OutputNamespace.LATEST):
    outputs/latest/unified_crowd_intelligence.json
    outputs/latest/unified_crowd_intelligence_status.json
    outputs/latest/unified_crowd_intelligence.md
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from portfolio_automation.crowd_intelligence.unified_bus import build_unified_rows
from portfolio_automation.crowd_intelligence.unified_loader import (
    load_fmp_lane,
    load_social_lane,
)
from portfolio_automation.crowd_intelligence.unified_schema import (
    SCHEMA_VERSION,
    SOURCE_LABEL,
    SS_PLAN_LOCKED,
    STATE_CONFIRMED_ATTENTION,
    STATE_DIVERGENT_ATTENTION,
    STATE_INSTITUTIONAL_ONLY,
    STATE_RETAIL_ONLY,
    UnifiedCrowdRow,
)
from portfolio_automation.crowd_intelligence.unified_bus import _social_sentiment_status
from portfolio_automation.data_governance import OutputNamespace, safe_write_json, safe_write_text
from portfolio_automation.social_intelligence.base import utc_now_iso

_TOP_N = 10


def _top(rows: list[UnifiedCrowdRow], state: str, n: int = _TOP_N) -> list[dict[str, Any]]:
    picks = [r for r in rows if r.crowd_state == state]
    picks.sort(key=lambda x: x.crowd_confidence, reverse=True)
    return [
        {
            "ticker": r.ticker,
            "crowd_confidence": round(r.crowd_confidence, 4),
            "retail_attention_score": r.retail_attention_score,
            "fmp_attention_score": r.fmp_attention_score,
            "cross_source_confirmation_score": round(r.cross_source_confirmation_score, 4),
            "cross_source_divergence_score": round(r.cross_source_divergence_score, 4),
            "explanation": r.explanation,
        }
        for r in picks[:n]
    ]


def build_status(
    rows: list[UnifiedCrowdRow],
    *,
    generated_at: str,
    social_available: bool,
    fmp_available: bool,
    enabled_categories: list[str],
    disabled_categories: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    lane_a = sum(1 for r in rows if r.source_lanes_present.get("social_intelligence"))
    lane_b = sum(1 for r in rows if r.source_lanes_present.get("crowd_intelligence"))
    overlap = sum(
        1 for r in rows
        if r.source_lanes_present.get("social_intelligence")
        and r.source_lanes_present.get("crowd_intelligence")
    )
    state_counts: dict[str, int] = {}
    for r in rows:
        state_counts[r.crowd_state] = state_counts.get(r.crowd_state, 0) + 1
    confs = [r.crowd_confidence for r in rows]
    conf_avg = round(sum(confs) / len(confs), 4) if confs else 0.0
    breadth_max = max((r.source_breadth_total for r in rows), default=0)

    if not rows:
        overall = "degraded" if (social_available or fmp_available) else "failed"
    elif not (social_available and fmp_available):
        overall = "degraded"  # single-lane: honest "not fully joined"
    else:
        overall = "ok"

    return {
        "schema_version": SCHEMA_VERSION,
        "source": f"{SOURCE_LABEL}_status",
        "simulation_active": True,
        "production_gated": True,
        "human_approval_required_for_production": True,
        "feeds_decision_engine": False,
        "no_trade": True,
        "generated_at": generated_at,
        "signal_date": (generated_at or "")[:10],
        "overall_status": overall,
        "total_tickers": len(rows),
        "lane_a_tickers": lane_a,
        "lane_b_tickers": lane_b,
        "overlap_tickers": overlap,
        "source_breadth_max": breadth_max,
        "enabled_categories": list(enabled_categories or []),
        "disabled_categories": list(disabled_categories or []),
        "social_sentiment_status": _social_sentiment_status(disabled_categories, enabled_categories),
        "crowd_confidence_avg": conf_avg,
        "state_counts": state_counts,
        "top_confirmed_attention": _top(rows, STATE_CONFIRMED_ATTENTION),
        "top_retail_only_attention": _top(rows, STATE_RETAIL_ONLY),
        "top_divergent_attention": _top(rows, STATE_DIVERGENT_ATTENTION),
        "top_institutional_context_only": _top(rows, STATE_INSTITUTIONAL_ONLY),
        "warnings": list(warnings or []),
    }


def _render_md(status: dict[str, Any]) -> str:
    lines = [
        "# Unified Crowd Intelligence",
        "",
        "Simulation-active join of retail attention (ApeWisdom) + FMP market/context attention.",
        "Production-gated: never feeds the decision engine (production trade execution);",
        "the active simulation lane MAY consume it. Production changes require human approval.",
        "",
        f"- Generated: {status.get('generated_at')}",
        f"- Status: **{status.get('overall_status')}**",
        f"- Tickers: {status.get('total_tickers')} "
        f"(Lane A {status.get('lane_a_tickers')} · Lane B {status.get('lane_b_tickers')} · "
        f"overlap {status.get('overlap_tickers')})",
        f"- social_sentiment: {status.get('social_sentiment_status')}",
        f"- FMP categories enabled: {', '.join(status.get('enabled_categories') or []) or '—'}",
        f"- FMP categories disabled: {', '.join(status.get('disabled_categories') or []) or '—'}",
        "",
        "## State counts",
    ]
    for st, n in sorted((status.get("state_counts") or {}).items(), key=lambda kv: -kv[1]):
        lines.append(f"- {st}: {n}")
    return "\n".join(lines) + "\n"


def build_payload(rows: list[UnifiedCrowdRow], *, generated_at: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE_LABEL,
        "simulation_active": True,
        "production_gated": True,
        "human_approval_required_for_production": True,
        "feeds_decision_engine": False,
        "no_trade": True,
        "generated_at": generated_at,
        "record_count": len(rows),
        "records": [r.to_dict() for r in rows],
    }


def run(root: str | Path = ".") -> dict[str, Any]:
    """Non-blocking entrypoint. Reads both lanes, joins, writes artifacts. Never raises."""
    root = Path(root)
    generated_at = utc_now_iso()
    try:
        social = load_social_lane(root)
        fmp = load_fmp_lane(root)
        warnings: list[str] = []
        if not social["available"]:
            warnings.append("social_lane_unavailable")
        if not fmp["available"]:
            warnings.append("fmp_lane_unavailable")

        rows = build_unified_rows(
            social_records=social.get("records"),
            fmp_by_symbol=fmp.get("by_symbol"),
            enabled_categories=fmp.get("enabled_categories"),
            disabled_categories=fmp.get("disabled_categories"),
            generated_at=generated_at,
            social_stale=bool(social.get("stale")),
            fmp_stale=bool(fmp.get("stale")),
        )
        payload = build_payload(rows, generated_at=generated_at)
        status = build_status(
            rows,
            generated_at=generated_at,
            social_available=social["available"],
            fmp_available=fmp["available"],
            enabled_categories=fmp.get("enabled_categories") or [],
            disabled_categories=fmp.get("disabled_categories") or [],
            warnings=warnings,
        )
        safe_write_json(OutputNamespace.LATEST, "unified_crowd_intelligence.json", payload, base_dir=root / "outputs")
        safe_write_json(OutputNamespace.LATEST, "unified_crowd_intelligence_status.json", status, base_dir=root / "outputs")
        safe_write_text(OutputNamespace.LATEST, "unified_crowd_intelligence.md", _render_md(status), base_dir=root / "outputs")
        return status
    except Exception as exc:  # pragma: no cover - defensive, never break the pipeline
        degraded = {
            "schema_version": SCHEMA_VERSION,
            "source": f"{SOURCE_LABEL}_status",
            "simulation_active": True,
            "production_gated": True,
            "human_approval_required_for_production": True,
            "feeds_decision_engine": False,
            "generated_at": generated_at,
            "overall_status": "failed",
            "total_tickers": 0,
            "warnings": [f"unified_writer_error: {exc}"],
        }
        try:
            safe_write_json(OutputNamespace.LATEST, "unified_crowd_intelligence_status.json", degraded, base_dir=root / "outputs")
        except Exception:
            pass
        return degraded


if __name__ == "__main__":  # pragma: no cover
    import sys
    r = run(sys.argv[1] if len(sys.argv) > 1 else ".")
    print("unified_crowd:", r.get("overall_status"), "tickers:", r.get("total_tickers"))
