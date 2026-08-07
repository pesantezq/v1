"""Discovery-research memo sections (sandbox-only).

Split out of ``daily_memo`` on 2026-08-07. These two builders are the cleanest
seam in that module: 326 lines depending on exactly one shared helper and two
constants, both of which travel with them or live in ``_shared``.

Both are re-exported from ``watchlist_scanner.daily_memo``, so existing import
paths are unchanged.

SANDBOX ONLY: nothing here is a buy/sell recommendation, and none of it updates
the official watchlist, the portfolio, or ``decision_plan.json``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from watchlist_scanner.memo._shared import (
    _FORBIDDEN_MEMO_DECISIONS,
    _LINE,
    _flt,
    _safe_load,
)

_DISCOVERY_DISCLAIMER = (
    "Discovery candidates are sandbox research only. "
    "They are not buy/sell recommendations and do not update the official watchlist or portfolio."
)




def _build_discovery_section(data: dict[str, Any]) -> str:
    """Build a plain-text Discovery Research section for the daily memo."""
    emerging      = data.get("emerging") or {}
    rejected_data = data.get("rejected") or {}
    memory        = data.get("memory") or {}
    approvals     = data.get("approvals") or []

    candidates     = [c for c in (emerging.get("candidates") or []) if isinstance(c, dict)]
    rejected_cands = [c for c in (rejected_data.get("candidates") or []) if isinstance(c, dict)]

    watch      = [c for c in candidates if str(c.get("status", "")).lower() == "watch"]
    discovered = [c for c in candidates if str(c.get("status", "")).lower() == "discovered"]

    # Collapse the section when nothing has been emitted today.
    if not watch and not discovered and not rejected_cands and not approvals:
        memory_entries = memory.get("entries") if isinstance(memory, dict) else None
        if not memory_entries:
            return "\n".join([
                _LINE,
                "  DISCOVERY RESEARCH  [Sandbox Only]",
                _LINE,
                "  No sandbox research candidates today.",
                "",
            ])

    # Defense-in-depth: exclude forbidden decision values even if they slipped through
    valid_approvals = [
        ap for ap in approvals
        if str(ap.get("decision", "")).lower() not in _FORBIDDEN_MEMO_DECISIONS
    ]

    approved_count = sum(
        1 for ap in valid_approvals
        if str(ap.get("decision", "")).lower() == "approve_for_research_review"
    )
    needs_evidence_count = sum(
        1 for ap in valid_approvals
        if str(ap.get("decision", "")).lower() == "needs_more_evidence"
    )

    lines: list[str] = []
    a = lines.append

    a(_LINE)
    a("  DISCOVERY RESEARCH  [Sandbox Only]")
    a(_LINE)
    a(f"  {_DISCOVERY_DISCLAIMER}")
    a("")
    a(
        f"  WATCH={len(watch)}, "
        f"DISCOVERED={len(discovered)}, "
        f"REJECTED={len(rejected_cands)}"
    )
    if valid_approvals:
        a(
            f"  Approval decisions: {len(valid_approvals)} "
            f"(approved for research: {approved_count}, "
            f"needs more evidence: {needs_evidence_count})"
        )
    a("")

    # Top WATCH candidates
    if watch:
        a("  Top Research Candidates (WATCH):")
        approval_by_symbol: dict[str, dict] = {}
        for ap in reversed(valid_approvals):
            sym = str(ap.get("symbol", "")).upper().strip()
            if sym and sym not in approval_by_symbol:
                approval_by_symbol[sym] = ap

        for idx, c in enumerate(watch[:5], 1):
            ticker     = str(c.get("ticker", "-")).upper()
            score      = _flt(c.get("score"))
            corr_score = _flt(c.get("corroboration_score"))
            corr_level = str(c.get("corroboration_level", "-"))
            event      = str(c.get("event_type", "-"))
            risk_note  = " [risk flag]" if bool(c.get("risk_flag")) else ""
            a(
                f"  {idx}. {ticker} — score {score:.2f}, "
                f"corroboration: {corr_level} ({corr_score:.2f}), "
                f"event: {event}{risk_note}"
            )
            snippets = [s for s in (c.get("evidence_snippets") or []) if s]
            if snippets:
                a(f"     Evidence: {str(snippets[0])[:120]}")
            ap_rec = approval_by_symbol.get(ticker)
            if ap_rec:
                dec     = str(ap_rec.get("decision", ""))
                reason  = str(ap_rec.get("decision_reason", "")).strip()
                ts      = str(ap_rec.get("generated_at", ""))[:10]
                ap_line = f"     Research decision: {dec}"
                if reason:
                    ap_line += f" — {reason[:80]}"
                if ts:
                    ap_line += f" ({ts})"
                a(ap_line)
        a("")

    # Monitoring (DISCOVERED candidates)
    if discovered:
        tickers_str = ", ".join(str(c.get("ticker", "?")) for c in discovered[:8])
        a(f"  Monitoring ({len(discovered)} candidates): {tickers_str}")
        if len(discovered) > 8:
            a(f"  ...and {len(discovered) - 8} more.")
        a("")

    # Memory / persistence
    memory_entries = memory.get("entries") or []
    if isinstance(memory_entries, list) and memory_entries:
        persistent = [
            e["ticker"] for e in memory_entries
            if isinstance(e, dict) and e.get("ticker") and int(e.get("seen_runs", 0)) > 1
        ]
        new_this_run = [
            e["ticker"] for e in memory_entries
            if isinstance(e, dict) and e.get("ticker") and int(e.get("seen_runs", 0)) == 1
        ]
        if persistent:
            a(f"  Persistent (seen multiple runs): {', '.join(sorted(persistent)[:6])}")
        if new_this_run:
            a(f"  New this run: {', '.join(sorted(new_this_run)[:6])}")
        if persistent or new_this_run:
            a("")

    # Recent approval decisions
    if valid_approvals:
        a("  Recent Research Decisions (operator review):")
        for ap in valid_approvals[-5:]:
            sym      = str(ap.get("symbol", "?"))
            dec      = str(ap.get("decision", "-"))
            reason   = str(ap.get("decision_reason", "")).strip()
            ts       = str(ap.get("generated_at", ""))[:10]
            line_str = f"  - {sym}: {dec}"
            if reason:
                line_str += f" — {reason[:80]}"
            if ts:
                line_str += f" ({ts})"
            a(line_str)
        a("")

    # Rejected / risk summary
    risk_flagged = [c for c in candidates if bool(c.get("risk_flag"))]
    if rejected_cands or risk_flagged:
        a("  Rejected / Risk Summary:")
        a(f"  - Rejected: {len(rejected_cands)} candidates (not recommendations)")
        if risk_flagged:
            a(f"  - Risk flags: {len(risk_flagged)} research candidates")
        reasons = [
            str(c.get("rejection_reason", ""))
            for c in rejected_cands if c.get("rejection_reason")
        ]
        if reasons:
            seen_r: set[str] = set()
            unique_reasons: list[str] = []
            for r in reasons:
                if r not in seen_r:
                    seen_r.add(r)
                    unique_reasons.append(r)
            a(f"  - Top reasons: {'; '.join(unique_reasons[:3])}")
        a("")

    a("  [Research lane — sandbox only. No official action taken.]")
    a("")

    return "\n".join(lines)


def _build_discovery_section_md(data: dict[str, Any]) -> str:
    """Build a Markdown Discovery Research section for the daily memo."""
    emerging      = data.get("emerging") or {}
    rejected_data = data.get("rejected") or {}
    memory        = data.get("memory") or {}
    approvals     = data.get("approvals") or []

    candidates     = [c for c in (emerging.get("candidates") or []) if isinstance(c, dict)]
    rejected_cands = [c for c in (rejected_data.get("candidates") or []) if isinstance(c, dict)]

    watch      = [c for c in candidates if str(c.get("status", "")).lower() == "watch"]
    discovered = [c for c in candidates if str(c.get("status", "")).lower() == "discovered"]

    # When every count is zero and there are no approvals or memory entries,
    # collapse the section to a single line — the disclaimer adds no signal.
    if not watch and not discovered and not rejected_cands and not approvals:
        memory_entries = memory.get("entries") if isinstance(memory, dict) else None
        if not memory_entries:
            return "## Discovery Research — Sandbox Only\n\n_No sandbox research candidates today._\n"

    valid_approvals = [
        ap for ap in approvals
        if str(ap.get("decision", "")).lower() not in _FORBIDDEN_MEMO_DECISIONS
    ]

    approved_count = sum(
        1 for ap in valid_approvals
        if str(ap.get("decision", "")).lower() == "approve_for_research_review"
    )
    needs_evidence_count = sum(
        1 for ap in valid_approvals
        if str(ap.get("decision", "")).lower() == "needs_more_evidence"
    )

    lines: list[str] = []
    a = lines.append

    a("## Discovery Research — Sandbox Only")
    a("")
    a(f"> {_DISCOVERY_DISCLAIMER}")
    a("")
    a(
        f"**WATCH:** {len(watch)} · "
        f"**DISCOVERED:** {len(discovered)} · "
        f"**REJECTED:** {len(rejected_cands)}"
    )
    if valid_approvals:
        a(
            f"**Approval decisions:** {len(valid_approvals)} "
            f"(approved for research: {approved_count}, "
            f"needs more evidence: {needs_evidence_count})"
        )
    a("")

    if watch:
        a("### Research Candidates (WATCH)")
        a("")
        approval_by_symbol: dict[str, dict] = {}
        for ap in reversed(valid_approvals):
            sym = str(ap.get("symbol", "")).upper().strip()
            if sym and sym not in approval_by_symbol:
                approval_by_symbol[sym] = ap

        for c in watch[:5]:
            ticker     = str(c.get("ticker", "-")).upper()
            score      = _flt(c.get("score"))
            corr_score = _flt(c.get("corroboration_score"))
            corr_level = str(c.get("corroboration_level", "-"))
            event      = str(c.get("event_type", "-"))
            risk_note  = " ⚠ risk flag" if bool(c.get("risk_flag")) else ""
            a(
                f"- **{ticker}** — score `{score:.2f}`, "
                f"corroboration: {corr_level} (`{corr_score:.2f}`), "
                f"event: `{event}`{risk_note}"
            )
            snippets = [s for s in (c.get("evidence_snippets") or []) if s]
            if snippets:
                a(f"  - Evidence: {str(snippets[0])[:120]}")
            ap_rec = approval_by_symbol.get(ticker)
            if ap_rec:
                dec     = str(ap_rec.get("decision", ""))
                reason  = str(ap_rec.get("decision_reason", "")).strip()
                ts      = str(ap_rec.get("generated_at", ""))[:10]
                ap_text = f"  - Research decision: `{dec}`"
                if reason:
                    ap_text += f" — {reason[:80]}"
                if ts:
                    ap_text += f" ({ts})"
                a(ap_text)
        a("")

    if discovered:
        a("### Monitoring")
        tickers_str = ", ".join(f"`{c.get('ticker', '?')}`" for c in discovered[:8])
        a(f"Candidates in monitoring: {tickers_str}")
        if len(discovered) > 8:
            a(f"...and {len(discovered) - 8} more.")
        a("")

    memory_entries = memory.get("entries") or []
    if isinstance(memory_entries, list) and memory_entries:
        persistent = [
            e["ticker"] for e in memory_entries
            if isinstance(e, dict) and e.get("ticker") and int(e.get("seen_runs", 0)) > 1
        ]
        new_this_run = [
            e["ticker"] for e in memory_entries
            if isinstance(e, dict) and e.get("ticker") and int(e.get("seen_runs", 0)) == 1
        ]
        if persistent or new_this_run:
            a("### Persistence")
            if persistent:
                a(f"- Persistent (multiple runs): {', '.join(sorted(persistent)[:6])}")
            if new_this_run:
                a(f"- New this run: {', '.join(sorted(new_this_run)[:6])}")
            a("")

    if valid_approvals:
        a("### Operator Research Decisions")
        a("")
        for ap in valid_approvals[-5:]:
            sym      = str(ap.get("symbol", "?"))
            dec      = str(ap.get("decision", "-"))
            reason   = str(ap.get("decision_reason", "")).strip()
            ts       = str(ap.get("generated_at", ""))[:10]
            line_str = f"- **{sym}**: `{dec}`"
            if reason:
                line_str += f" — {reason[:80]}"
            if ts:
                line_str += f" ({ts})"
            a(line_str)
        a("")

    risk_flagged = [c for c in candidates if bool(c.get("risk_flag"))]
    if rejected_cands or risk_flagged:
        a("### Rejected / Risk Summary")
        a(f"- Rejected: {len(rejected_cands)} candidates (not recommendations)")
        if risk_flagged:
            a(f"- Risk flags: {len(risk_flagged)} candidates")
        reasons = [
            str(c.get("rejection_reason", ""))
            for c in rejected_cands if c.get("rejection_reason")
        ]
        if reasons:
            seen_r: set[str] = set()
            unique_reasons: list[str] = []
            for r in reasons:
                if r not in seen_r:
                    seen_r.add(r)
                    unique_reasons.append(r)
            for r in unique_reasons[:3]:
                a(f"  - {r}")
        a("")

    a("_Research lane — sandbox only. No official action taken._")

    return "\n".join(lines)
