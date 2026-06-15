"""GUI loader: per-advisory-pick crowd context (observe-only, artifact-only).

Reads Phase-2A artifacts via context_loader and applies the context-only enricher.
No FMP / HTTP / governor calls. Safe empty/stale states.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from portfolio_automation.crowd_intelligence.context_loader import load_crowd_context
from portfolio_automation.crowd_intelligence import advisory_context_enricher as enr


def _banner(ctx: dict) -> str | None:
    if not ctx["available"]:
        return "Crowd context unavailable — artifact not generated yet."
    if ctx["stale"]:
        return f"Crowd context may be stale — last generated at {ctx.get('generated_at')}."
    return None


def crowd_context_for(root: Path | str, symbols: list[str]) -> dict[str, Any]:
    ctx = load_crowd_context(root)
    social_disabled = bool(ctx.get("social_disabled"))
    by_symbol: dict[str, Any] = {}
    for sym in {str(s).upper() for s in (symbols or [])}:
        sig = ctx["by_symbol"].get(sym)
        if not ctx["available"]:
            by_symbol[sym] = {"present": False, "label": "Insufficient Data",
                              "severity": "gray",
                              "lines": ["Crowd context unavailable — artifact not generated yet."]}
            continue
        if sig is None:
            by_symbol[sym] = {"present": False, "label": "Insufficient Data",
                              "severity": "gray",
                              "lines": ["No crowd context available for this symbol."]}
            continue
        label = enr.context_label(sig)
        by_symbol[sym] = {
            "present": True,
            "label": label,
            "severity": enr.label_severity(label),
            "composite": sig.get("composite_crowd_score"),
            "confidence": sig.get("confidence"),
            "enabled_sources": sig.get("enabled_sources") or [],
            "disabled_sources": sig.get("disabled_sources") or [],
            "data_freshness": sig.get("data_freshness"),
            "top_reasons": (sig.get("top_reasons") or [])[:3],
            "warnings": sig.get("warnings") or [],
            "lines": enr.enrich(sig, label, social_disabled=social_disabled),
        }
    return {
        "status": {
            "available": ctx["available"], "stale": ctx["stale"],
            "generated_at": ctx.get("generated_at"),
            "social_disabled": social_disabled, "banner": _banner(ctx),
        },
        "by_symbol": by_symbol,
    }
