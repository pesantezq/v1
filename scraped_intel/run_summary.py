"""
Scraped Intel Run Summary - per-run observability artifact.

Produces a concise JSON + Markdown snapshot of what happened during a
scraped-intel + scanner run so that manual inspection, email workflows,
and debug sessions can tell at a glance whether the data pipeline is
healthy and what data sources contributed.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from degraded_mode import build_data_health_context

logger = logging.getLogger("scraped_intel.run_summary")


def build_run_summary(
    *,
    run_mode: str,
    timestamp: Optional[str] = None,
    fmp_attempted: bool = False,
    fmp_succeeded: bool = False,
    fmp_error: Optional[str] = None,
    fallback_used: bool = False,
    watchlist_source: str = "none",
    symbols_processed: Optional[List[str]] = None,
    scraped_intel_stats: Optional[Dict[str, Any]] = None,
    market_regime: Optional[Dict[str, Any]] = None,
    market_coverage: Optional[Dict[str, Any]] = None,
    output_dir: str = "outputs/latest",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Build a structured run-summary dict and write JSON + Markdown artifacts.

    All parameters are keyword-only to prevent positional argument mistakes.
    Returns the summary dict with artifact paths populated when written.
    """
    ts = timestamp or datetime.now().isoformat()
    fmp_syms = symbols_processed or []
    si = scraped_intel_stats or {}
    regime = market_regime or {}
    coverage = market_coverage or {}
    portfolio_review = coverage.get("portfolio_review") or {}
    decision_layer = coverage.get("decision_layer") or {}
    data_health = build_data_health_context(
        fmp_attempted=fmp_attempted,
        fmp_succeeded=fmp_succeeded,
        fmp_error=fmp_error,
        fallback_used=fallback_used,
        watchlist_source=watchlist_source,
        scan_status=(si.get("scan_status") if isinstance(si, dict) else None),
        data_latency_ms=(si.get("data_latency_ms") if isinstance(si, dict) else None),
    )

    summary: Dict[str, Any] = {
        "timestamp": ts,
        "run_mode": run_mode,
        "dry_run": dry_run,
        "degraded_mode": data_health["degraded_mode"],
        "degraded_reason": data_health["degraded_reason"],
        "data_sources_used": data_health["data_sources_used"],
        "data_mode": data_health["data_mode"],
        "scanner": {
            "fmp_attempted": fmp_attempted,
            "fmp_succeeded": fmp_succeeded,
            "fmp_error": fmp_error,
            "fallback_used": fallback_used,
            "watchlist_source": watchlist_source,
            "symbols_processed": fmp_syms,
            "symbol_count": len(fmp_syms),
            "data_fallback_triggered": data_health["data_fallback_triggered"],
            "data_latency_ms": data_health["data_latency_ms"],
            "fallback_depth": data_health["fallback_depth"],
            "degraded_confidence_penalty": data_health["degraded_confidence_penalty"],
        },
        "scraped_intel": {
            "symbol_count": int(si.get("symbols_processed", 0)),
            "total_evidence": int(si.get("total_evidence", 0)),
            "evidence_by_source": dict(si.get("evidence_by_source") or {}),
            "symbols_with_features": int(si.get("symbols_with_features", 0)),
            "symbols_with_signal_lift": int(si.get("symbols_with_signal_lift", 0)),
            "symbols_with_confidence_lift": int(si.get("symbols_with_confidence_lift", 0)),
            "adapter_failures": dict(si.get("adapter_failures") or {}),
        },
        "market_regime": {
            "regime_label": regime.get("regime_label", "neutral"),
            "regime_confidence": regime.get("regime_confidence", 0.0),
            "regime_reasoning": regime.get("regime_reasoning", "Regime inputs unavailable."),
            "regime_summary_line": regime.get("regime_summary_line", "Market regime unavailable."),
            "regime_inputs": dict(regime.get("regime_inputs") or {}),
            "regime_data_quality": regime.get("regime_data_quality", "limited"),
            "regime_portfolio_fit": regime.get("regime_portfolio_fit"),
            "regime_portfolio_commentary": regime.get("regime_portfolio_commentary"),
        },
        "market_coverage": {
            "enabled": bool(coverage.get("enabled", False)),
            "symbols_scanned": int(coverage.get("symbols_scanned", 0)),
            "symbols_with_price": int(coverage.get("symbols_with_price", 0)),
            "promoted_count": len(coverage.get("promoted") or []),
            "top_symbols": [
                str(row.get("symbol") or "")
                for row in (coverage.get("promoted") or [])[:5]
                if row.get("symbol")
            ],
            "portfolio_summary_line": portfolio_review.get("summary_line"),
            "rotation_candidate_count": int(portfolio_review.get("new_rotation_candidates", 0) or 0),
            "decision_summary_line": decision_layer.get("summary_line"),
            "decision_action_count": len(decision_layer.get("actions") or []),
        },
        "artifacts": {},
    }

    if dry_run:
        logger.info("RUN SUMMARY: dry-run - artifact writes skipped")
        return summary

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "scraped_intel_run_summary.json"
    md_path = out_dir / "scraped_intel_run_summary.md"

    json_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    summary["artifacts"]["json"] = str(json_path)

    md_path.write_text(_render_markdown(summary), encoding="utf-8")
    summary["artifacts"]["markdown"] = str(md_path)

    logger.info(
        "RUN SUMMARY: written -> %s, %s (FMP:%s fallback:%s src:%s)",
        json_path.name,
        md_path.name,
        "ok" if fmp_succeeded else ("failed" if fmp_attempted else "skipped"),
        fallback_used,
        watchlist_source,
    )
    return summary


def _render_markdown(summary: Dict[str, Any]) -> str:
    """Render the summary dict into a concise Markdown artifact."""
    sc = summary.get("scanner", {})
    si = summary.get("scraped_intel", {})
    ts = summary.get("timestamp", "")[:19].replace("T", " ")
    mode = summary.get("run_mode", "?")

    lines: List[str] = [
        "# Scraped Intel Run Summary",
        "",
        f"**Timestamp:** {ts}  ",
        f"**Mode:** `{mode}`  ",
        f"**Dry-run:** {summary.get('dry_run', False)}",
        "",
        "## Scanner (FMP)",
        "",
    ]

    fmp_attempted = sc.get("fmp_attempted", False)
    fmp_ok = sc.get("fmp_succeeded", False)
    fallback = sc.get("fallback_used", False)
    watchlist_src = sc.get("watchlist_source", "none")
    sym_count = sc.get("symbol_count", 0)
    data_mode = summary.get("data_mode", "live")
    degraded_mode = summary.get("degraded_mode", False)
    degraded_reason = summary.get("degraded_reason") or "none"

    if not fmp_attempted:
        lines.append("- FMP: not attempted (scanner disabled, circuit-breaker pre-empted, or skipped)")
    elif fmp_ok:
        lines.append("- FMP: **succeeded**")
    else:
        err = sc.get("fmp_error") or "unknown error"
        lines.append(f"- FMP: **FAILED** - `{err}`")

    if fallback:
        lines.append("- Fallback watchlist: **ACTIVE**")
    else:
        lines.append("- Fallback watchlist: not used")

    lines.append(f"- Watchlist source: `{watchlist_src}`")
    lines.append(f"- Symbols in watchlist: **{sym_count}**")
    lines.append(f"- Data mode: `{data_mode}`")
    lines.append(f"- Degraded mode: **{'yes' if degraded_mode else 'no'}** (`{degraded_reason}`)")
    lines.extend(
        [
            "",
            "## Market Regime",
            "",
            f"- {summary.get('market_regime', {}).get('regime_summary_line', 'Market regime unavailable.')}",
            f"- Data quality: {summary.get('market_regime', {}).get('regime_data_quality', 'limited')}",
            "",
            "## Market Coverage",
            "",
            f"- Enabled: {summary.get('market_coverage', {}).get('enabled', False)}",
            f"- Symbols scanned: {summary.get('market_coverage', {}).get('symbols_scanned', 0)}",
            f"- Promoted candidates: {summary.get('market_coverage', {}).get('promoted_count', 0)}",
            "",
            "## Scraped Intel",
            "",
            f"- Symbols processed: **{si.get('symbol_count', 0)}**",
            f"- Total evidence items: **{si.get('total_evidence', 0)}**",
        ]
    )
    market_coverage = summary.get("market_coverage", {})
    if market_coverage.get("portfolio_summary_line"):
        lines.append(f"- Portfolio review: {market_coverage['portfolio_summary_line']}")
    if market_coverage.get("decision_summary_line"):
        lines.append(f"- Decision layer: {market_coverage['decision_summary_line']}")
    top_symbols = market_coverage.get("top_symbols") or []
    if top_symbols:
        lines.append(f"- Top symbols: {', '.join(top_symbols)}")

    by_src = si.get("evidence_by_source", {})
    if by_src:
        lines.append("- Evidence by source:")
        for src_name, count in sorted(by_src.items()):
            lines.append(f"  - `{src_name}`: {count}")
    else:
        lines.append("- Evidence by source: none")

    lines.extend(
        [
            f"- Symbols with soft features: {si.get('symbols_with_features', 0)}",
            f"- Symbols with signal lift: {si.get('symbols_with_signal_lift', 0)}"
            " *(comparison mode only)*",
            f"- Symbols with confidence lift: {si.get('symbols_with_confidence_lift', 0)}"
            " *(comparison mode only)*",
        ]
    )

    failures = si.get("adapter_failures", {})
    if failures:
        lines.append("- **Adapter failures:**")
        for name, err in failures.items():
            lines.append(f"  - `{name}`: {err}")

    lines.append("")
    return "\n".join(lines)
