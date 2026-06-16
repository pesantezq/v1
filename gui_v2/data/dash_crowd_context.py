"""GUI loader: per-advisory-pick crowd context (observe-only, artifact-only).

Reads Phase-2A artifacts via context_loader and applies the context-only enricher.
No FMP / HTTP / governor calls. Safe empty/stale states.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from portfolio_automation.crowd_intelligence.context_loader import load_crowd_context
from portfolio_automation.crowd_intelligence import advisory_context_enricher as enr
from portfolio_automation.crowd_intelligence.unified_loader import read_unified_crowd


def _banner(ctx: dict) -> str | None:
    if not ctx["available"]:
        return "Crowd context unavailable — artifact not generated yet."
    if ctx["stale"]:
        return f"Crowd context may be stale — last generated at {ctx.get('generated_at')}."
    return None


def crowd_context_for(root: Path | str, symbols: list[str]) -> dict[str, Any]:
    ctx = load_crowd_context(root)
    social_disabled = bool(ctx.get("social_disabled"))
    # Additive: read the joined unified-crowd lane (display-only here, never raises).
    try:
        unified = read_unified_crowd(root)
        unified_by_ticker = unified.get("by_ticker") or {}
    except Exception:
        unified_by_ticker = {}
    by_symbol: dict[str, Any] = {}
    def _unified_for(sym: str) -> dict[str, Any] | None:
        row = unified_by_ticker.get(sym)
        if not isinstance(row, dict):
            return None
        return {
            "crowd_state": row.get("crowd_state"),
            "retail_attention_score": row.get("retail_attention_score"),
            "fmp_attention_score": row.get("fmp_attention_score"),
            "cross_source_confirmation_score": row.get("cross_source_confirmation_score"),
            "cross_source_divergence_score": row.get("cross_source_divergence_score"),
            "explanation": row.get("explanation"),
        }

    for sym in {str(s).upper() for s in (symbols or [])}:
        sig = ctx["by_symbol"].get(sym)
        unified_sub = _unified_for(sym)
        if not ctx["available"]:
            by_symbol[sym] = {"present": False, "label": "Insufficient Data",
                              "severity": "gray",
                              "lines": ["Crowd context unavailable — artifact not generated yet."]}
            if unified_sub is not None:
                by_symbol[sym]["unified"] = unified_sub
            continue
        if sig is None:
            by_symbol[sym] = {"present": False, "label": "Insufficient Data",
                              "severity": "gray",
                              "lines": ["No crowd context available for this symbol."]}
            if unified_sub is not None:
                by_symbol[sym]["unified"] = unified_sub
            continue
        label = enr.context_label(sig)
        by_symbol[sym] = {
            "present": True,
            "label": label,
            "severity": enr.label_severity(label),
            "composite": sig.get("composite_crowd_score"),
            "composite_trend": sig.get("composite_trend"),
            "trend": sig.get("trend_label") or "building",
            "confidence": sig.get("confidence"),
            "enabled_sources": sig.get("enabled_sources") or [],
            "disabled_sources": sig.get("disabled_sources") or [],
            "data_freshness": sig.get("data_freshness"),
            "top_reasons": (sig.get("top_reasons") or [])[:3],
            "warnings": sig.get("warnings") or [],
            "lines": enr.enrich(sig, label, social_disabled=social_disabled),
        }
        if unified_sub is not None:
            by_symbol[sym]["unified"] = unified_sub
    return {
        "status": {
            "available": ctx["available"], "stale": ctx["stale"],
            "generated_at": ctx.get("generated_at"),
            "social_disabled": social_disabled, "banner": _banner(ctx),
        },
        "by_symbol": by_symbol,
    }
