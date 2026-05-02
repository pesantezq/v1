"""
Daily Investment Memo.

Loads outputs/latest/system_decision_summary.json and produces a clean,
human-readable memo suitable for email delivery and mobile viewing.

Writes:
  outputs/latest/daily_memo.txt  — plain text
  outputs/latest/daily_memo.md   — Markdown

CLI:
  python -m watchlist_scanner.daily_memo              # generate only
  python -m watchlist_scanner.daily_memo --send       # generate + email
  python -m watchlist_scanner.daily_memo --dry-run    # print, no files written
  python -m watchlist_scanner.daily_memo --test-email # verify SMTP config only

Email env vars (all required for --send / --test-email):
  Preferred: SMTP_SERVER, SMTP_PORT (default 587), EMAIL_USER, EMAIL_PASS, EMAIL_TO
  Also accepted for backward compatibility: SMTP_HOST, EMAIL_SENDER,
  EMAIL_PASSWORD, EMAIL_RECIPIENT
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

logger = logging.getLogger("watchlist_scanner.daily_memo")

_SUMMARY_JSON_REL = ("outputs", "latest", "system_decision_summary.json")
_DECISION_PLAN_JSON_REL = ("outputs", "latest", "decision_plan.json")
_MEMO_TXT_REL     = ("outputs", "latest", "daily_memo.txt")
_MEMO_MD_REL      = ("outputs", "latest", "daily_memo.md")

# Discovery sandbox artifact paths (read-only; never written by this module)
_DISCOVERY_EMERGING_REL = ("outputs", "sandbox", "discovery", "emerging_candidates.json")
_DISCOVERY_REJECTED_REL = ("outputs", "sandbox", "discovery", "rejected_candidates.json")
_DISCOVERY_MEMORY_REL   = ("outputs", "sandbox", "discovery", "discovery_memory.json")
_DISCOVERY_APPROVAL_REL = ("outputs", "sandbox", "discovery", "approval_decisions.jsonl")

_DISCOVERY_DISCLAIMER = (
    "Discovery candidates are sandbox research only. "
    "They are not buy/sell recommendations and do not update the official watchlist or portfolio."
)

# Defense-in-depth: forbidden decision strings must never appear in memo output
_FORBIDDEN_MEMO_DECISIONS: frozenset[str] = frozenset(
    {"buy", "sell", "actionable", "promoted", "validated"}
)

_SEP  = "=" * 48
_LINE = "-" * 48


# ---------------------------------------------------------------------------
# Safe helpers
# ---------------------------------------------------------------------------

def _safe_load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("daily_memo: could not load %s — %s", path, exc)
        return {}


def _load_discovery_approval_decisions(path: Path) -> list[dict[str, Any]]:
    """Load and validate approval decisions from JSONL; silently skips invalid/tampered records."""
    if not path.exists():
        return []
    try:
        from portfolio_automation.discovery.approval_workflow import (
            is_valid_loaded_approval_record,
        )
    except Exception:
        return []
    decisions: list[dict[str, Any]] = []
    try:
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
                if isinstance(obj, dict) and is_valid_loaded_approval_record(obj):
                    decisions.append(obj)
            except json.JSONDecodeError:
                pass
    except Exception as exc:
        logger.warning("daily_memo: could not load discovery approvals — %s", exc)
    return decisions


def _load_discovery_sandbox_data(root_path: Path) -> "dict[str, Any] | None":
    """
    Load discovery sandbox artifacts for the daily memo section.

    Returns None when no discovery data is available.
    Tolerates missing files, malformed JSON, and import errors.
    Never writes files — read-only consumer of sandbox artifacts.
    """
    try:
        emerging  = _safe_load(root_path.joinpath(*_DISCOVERY_EMERGING_REL))
        rejected  = _safe_load(root_path.joinpath(*_DISCOVERY_REJECTED_REL))
        memory    = _safe_load(root_path.joinpath(*_DISCOVERY_MEMORY_REL))
        approvals = _load_discovery_approval_decisions(
            root_path.joinpath(*_DISCOVERY_APPROVAL_REL)
        )
        if not emerging and not rejected and not memory and not approvals:
            return None
        return {
            "emerging":  emerging,
            "rejected":  rejected,
            "memory":    memory,
            "approvals": approvals,
        }
    except Exception as exc:
        logger.warning("daily_memo: failed to load discovery sandbox data — %s", exc)
        return None


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


def _flt(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pct(val: Any, places: int = 1) -> str:
    try:
        return f"{float(val) * 100:.{places}f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_delta(val: Any, places: int = 4) -> str:
    try:
        return f"{float(val):+.{places}f}"
    except (TypeError, ValueError):
        return "—"


def _label(val: Any) -> str:
    """Normalise a snake_case label to Title Case."""
    return str(val or "—").replace("_", " ").title()


# ---------------------------------------------------------------------------
# Decision plan helpers
# ---------------------------------------------------------------------------

def _decision_payload(summary: dict[str, Any]) -> dict[str, Any]:
    """Return the attached decision-plan payload if present."""
    raw = summary.get("_decision_plan") or summary.get("decision_plan") or {}
    return raw if isinstance(raw, dict) else {}


def _decision_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Return decision rows from an attached decision-plan payload."""
    payload = _decision_payload(summary)
    rows = payload.get("decisions") or []
    return [r for r in rows if isinstance(r, dict)]


def _fmt_money(val: Any) -> str:
    try:
        return f"${float(val):,.2f}"
    except (TypeError, ValueError):
        return "â€”"


def _decision_reason(row: dict[str, Any]) -> str:
    reason = str(row.get("reason") or "").strip()
    return reason if reason else "No decision rationale provided."


def _top_structural_decisions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if str(r.get("source") or "") == "structural"]


def _top_decision_rows(summary: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    rows = []
    for row in _decision_rows(summary):
        if bool(row.get("suppressed")):
            continue
        rows.append(row)
    rows.sort(key=lambda r: _flt(r.get("priority")), reverse=True)
    return rows[:limit]


def _capital_action_summary(rows: list[dict[str, Any]]) -> tuple[dict[str, int], float | None]:
    counts = {"SELL": 0, "SCALE": 0, "BUY": 0}
    total_amount = 0.0
    amount_count = 0

    for row in rows:
        decision = str(row.get("decision") or "")
        if decision not in counts:
            continue
        counts[decision] += 1
        try:
            total_amount += float(row.get("recommended_amount"))
            amount_count += 1
        except (TypeError, ValueError):
            pass

    return counts, (total_amount if amount_count > 0 else None)


def _risk_focus_items(rows: list[dict[str, Any]]) -> list[str]:
    items: list[str] = []
    structural_rows = _top_structural_decisions(rows)

    if structural_rows:
        lead = ", ".join(
            f"{str(r.get('symbol') or '-')} ({str(r.get('decision') or '-')})"
            for r in structural_rows[:3]
        )
        items.append(f"Structural decisions lead the plan: {lead}.")

        violation_types: list[str] = []
        for row in structural_rows:
            inputs_used = row.get("inputs_used") or {}
            vtype = str(inputs_used.get("violation_type") or "").strip().lower()
            if vtype and vtype not in violation_types:
                violation_types.append(vtype)

        if "concentration" in violation_types:
            items.append("Concentration risk is active and should be reviewed first.")
        if "leverage" in violation_types:
            items.append("Leverage risk is active and should be reduced first.")
    else:
        risk_symbols = [
            str(r.get("symbol") or "-")
            for r in rows
            if r.get("risk_flags")
        ]
        if risk_symbols:
            lead = ", ".join(risk_symbols[:3])
            items.append(f"Risk flags remain active in the top decisions: {lead}.")
        else:
            items.append("No structural risk actions lead the current decision set.")

    return items[:3]


def _change_items(changes: dict[str, Any]) -> list[str]:
    raw_items = [str(c).strip() for c in (changes.get("changes") or []) if str(c).strip()]
    if raw_items:
        return raw_items[:3]

    summary_line = str(changes.get("summary_line") or "").strip()
    if summary_line:
        return [summary_line]

    if not changes.get("previous_available", True):
        return ["No previous summary is available for comparison."]

    return ["No material changes recorded."]


def _health_items(data_health: dict[str, Any]) -> list[str]:
    degraded = bool(data_health.get("degraded_mode", False))
    data_mode = str(data_health.get("data_mode") or "").strip()
    missing_count = int(data_health.get("missing_artifact_count") or 0)
    missing_details = data_health.get("missing_artifact_details") or []
    defaulting_details = data_health.get("defaulting_artifact_details") or []
    optional_details = data_health.get("optional_artifact_details") or []
    fallback_used = bool(data_health.get("fallback_alerts_used", False))

    if (
        not degraded
        and data_mode in ("", "live")
        and missing_count <= 0
        and not defaulting_details
        and not optional_details
        and not fallback_used
    ):
        return []

    items: list[str] = []
    if degraded:
        items.append("Degraded mode is active; treat all memo actions as lower-certainty.")
    if data_mode and data_mode not in ("live",):
        items.append(f"Data mode is {data_mode}.")
    if missing_count > 0:
        if missing_details:
            rendered = "; ".join(
                f"{str(item.get('path') or 'unknown path')} ({str(item.get('producer_step') or 'unknown step')})"
                for item in missing_details
            )
            items.append(f"Required artifacts missing: {rendered}.")
        else:
            items.append(f"{missing_count} required artifacts were missing during summary generation.")
    if defaulting_details:
        rendered = "; ".join(
            f"{str(item.get('path') or 'unknown path')} ({str(item.get('producer_step') or 'unknown step')})"
            for item in defaulting_details
        )
        items.append(f"Defaulting because artifacts are not present: {rendered}.")
    if optional_details:
        rendered = "; ".join(
            f"{str(item.get('path') or 'unknown path')} ({str(item.get('producer_step') or 'unknown step')})"
            for item in optional_details
        )
        items.append(f"Optional artifacts not present: {rendered}.")
    if fallback_used and len(items) < 3:
        items.append("Fallback alerts were used because stronger live signals were unavailable.")
    return items[:3]


def _build_memo_top_insight(
    top_theme: dict[str, Any],
    top_opportunity: dict[str, Any],
    decision_rows: list[dict[str, Any]],
) -> str:
    structural_rows = _top_structural_decisions(decision_rows)
    if structural_rows:
        symbols = ", ".join(str(r.get("symbol") or "-") for r in structural_rows[:2])
        first = f"Structural risk is the top priority today, led by {symbols}."
    else:
        first = _build_top_insight(top_theme, top_opportunity)

    theme_name = str(top_theme.get("name") or "").strip()
    ticker = str(top_opportunity.get("ticker") or "").strip()
    if theme_name and ticker:
        return f"{first} {ticker} remains the lead opportunity inside the {theme_name} theme."
    if theme_name:
        return f"{first} {theme_name} remains the dominant theme."
    if ticker and not structural_rows:
        return f"{first} {ticker} remains the lead opportunity."
    return first


# ---------------------------------------------------------------------------
# Subject line
# ---------------------------------------------------------------------------

def get_subject(summary: dict[str, Any]) -> str:
    """Return the email/memo subject line for a given summary dict."""
    gen_at = str(summary.get("generated_at") or "")
    date_str = gen_at[:10] if gen_at else datetime.now().strftime("%Y-%m-%d")
    return f"Daily Investment Memo — {date_str}"


# ---------------------------------------------------------------------------
# Top Insight synthesiser
# ---------------------------------------------------------------------------

def _build_top_insight(tt: dict[str, Any], to: dict[str, Any]) -> str:
    """One-sentence summary of the most important signal today."""
    theme_name  = str(tt.get("name") or "")
    ticker      = str(to.get("ticker") or "")
    conviction  = _label(to.get("conviction_band") or "")
    fit_label   = str(to.get("portfolio_fit_label") or "").replace("_", " ")
    persistence = _flt(tt.get("persistence"))

    if theme_name and ticker:
        strength = "strong" if persistence >= 0.5 else "moderate"
        fit_note = f" and {fit_label} portfolio fit" if fit_label and fit_label not in ("—", "neutral") else ""
        return (
            f"{theme_name} is the dominant theme with {strength} persistence; "
            f"{ticker} leads opportunities with {conviction.lower() or 'notable'} conviction{fit_note}."
        )
    if theme_name:
        return f"{theme_name} is the dominant market theme this session."
    if ticker:
        return f"{ticker} is the top-ranked signal; no dominant theme detected."
    return "No significant signals detected; system operating in steady state."


# ---------------------------------------------------------------------------
# Plain-text memo builder
# ---------------------------------------------------------------------------

def build_daily_memo(summary: dict[str, Any]) -> str:
    """
    Build a plain-text daily investment memo from a system_decision_summary dict.

    Safe against empty or partially missing input — every section degrades
    gracefully to a descriptive placeholder.
    """
    gen_at      = str(summary.get("generated_at") or "")
    gen_display = gen_at[:19].replace("T", " ") if gen_at else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_str    = gen_at[:10] if gen_at else datetime.now().strftime("%Y-%m-%d")

    tt = dict(summary.get("top_theme") or {})
    to = dict(summary.get("top_opportunity") or {})
    bf = dict(summary.get("best_portfolio_fit") or {})
    cp = dict(summary.get("capital_preview") or {})
    ss = dict(summary.get("system_state") or {})
    dh = dict(summary.get("data_health") or {})
    ch = dict(summary.get("changes") or {})
    dp_rows = _decision_rows(summary)

    lines: list[str] = []
    a = lines.append

    # Subject header (useful when viewing .txt as a raw email draft)
    a(f"Subject: {get_subject(summary)}")
    a("")

    # ── HEADER ──────────────────────────────────────────────────────────────
    a(_SEP)
    a("  DAILY INVESTMENT MEMO")
    a(f"  {date_str}")
    a(_SEP)
    a("")

    degraded    = bool(dh.get("degraded_mode", False))
    data_mode   = str(dh.get("data_mode") or "unknown")
    health_str  = "DEGRADED — reduced data quality" if degraded else "Normal"

    a(f"  Date:         {date_str}")
    a(f"  Data Health:  {health_str}")
    if data_mode not in ("unknown", "live", ""):
        a(f"  Data Mode:    {data_mode}")
    a(f"  Generated:    {gen_display}")
    a("")

    # ── TOP INSIGHT ──────────────────────────────────────────────────────────
    a(_LINE)
    a("  TOP INSIGHT")
    a(_LINE)
    a(f"  {_build_top_insight(tt, to)}")
    a("")

    # ── TOP THEME ────────────────────────────────────────────────────────────
    a(_LINE)
    a("  TOP THEME")
    a(_LINE)
    if tt:
        score   = _flt(tt.get("score"))
        persist = _flt(tt.get("persistence"))
        accel   = _flt(tt.get("acceleration"))
        tickers = tt.get("tickers") or []
        a(f"  {tt.get('name', '—')}  ({_label(tt.get('type') or 'classified')})")
        a(f"  Score: {score:.3f}  |  Persistence: {persist:.3f}  |  Acceleration: {accel:+.3f}")
        if tickers:
            a(f"  Key tickers: {', '.join(str(t) for t in tickers[:8])}")
    else:
        a("  No theme data available.")
    a("")

    # ── TOP OPPORTUNITY ──────────────────────────────────────────────────────
    a(_LINE)
    a("  TOP OPPORTUNITY")
    a(_LINE)
    if dh.get("fallback_alerts_used"):
        a("  Note: No strong alerts this run — showing top-ranked fallback opportunities.")
        a("")
    if to:
        rank  = _flt(to.get("final_rank_score"))
        conf  = _flt(to.get("confidence"))
        mult  = _flt(to.get("rank_multiplier"), 1.0)
        a(f"  {to.get('ticker', '—')}")
        a(f"  Rank Score: {rank:.3f}  |  Confidence: {conf:.3f}")
        a(f"  Conviction: {_label(to.get('conviction_band'))}")
        a(f"  Theme: {_label(to.get('theme_alignment_label'))}  |  Portfolio Fit: {_label(to.get('portfolio_fit_label'))}")
        if mult != 1.0:
            a(f"  Rank Multiplier: x{mult:.2f}")
    else:
        a("  No eligible signals found.")
    a("")

    # ── PORTFOLIO INSIGHT ────────────────────────────────────────────────────
    a(_LINE)
    a("  PORTFOLIO INSIGHT")
    a(_LINE)
    if bf:
        b_score  = _flt(bf.get("portfolio_fit_score"))
        b_reason = str(bf.get("portfolio_fit_reason") or "")
        a(f"  Best fit:  {bf.get('ticker', '—')}  (fit score {b_score:.3f}, {_label(bf.get('portfolio_fit_label'))})")
        if b_reason:
            a(f"  Reason:    {b_reason}")
    else:
        a("  No portfolio fit data available.")

    total_sigs   = int(dh.get("total_signals") or 0)
    eligible_sigs = int(dh.get("eligible_signals") or 0)
    if total_sigs > 0:
        theme_note = f" aligned to {tt.get('name')} theme" if tt.get("name") else ""
        a(f"  Signals:   {total_sigs} total, {eligible_sigs} alert-eligible{theme_note}")
    a("")

    # ── CAPITAL PREVIEW ──────────────────────────────────────────────────────
    a(_LINE)
    a("  CAPITAL PREVIEW")
    a(_LINE)
    if cp:
        cand       = int(cp.get("candidate_count") or 0)
        base_pct   = cp.get("total_baseline_pct")
        prev_pct   = cp.get("total_preview_pct")
        delta      = cp.get("preview_vs_baseline_delta")
        sim_sample = int(cp.get("simulation_sample_size") or 0)
        eff_delta  = cp.get("simulation_efficiency_delta")
        ret_delta  = cp.get("simulation_return_delta")

        a(f"  Candidates: {cand}")
        if base_pct is not None and prev_pct is not None:
            a(f"  Baseline:   {_pct(base_pct)}  ->  Rank-Aware: {_pct(prev_pct)}  (delta {_fmt_delta(delta)})")
        if sim_sample > 0:
            a(f"  Simulation ({sim_sample} signals):  Efficiency {_fmt_delta(eff_delta)}  |  Return {_fmt_delta(ret_delta)}")
    else:
        a("  No capital preview data available.")
    a("")

    # ── POLICY STATUS ────────────────────────────────────────────────────────
    # Decision Engine: Top Decisions
    a(_LINE)
    a("  TOP DECISIONS")
    a(_LINE)
    if dp_rows:
        for idx, row in enumerate(dp_rows[:5], 1):
            decision = str(row.get("decision") or "â€”")
            symbol = str(row.get("symbol") or "â€”")
            priority = _flt(row.get("priority"))
            source = str(row.get("source") or "â€”")
            urgency = str(row.get("urgency") or "â€”")
            a(
                f"  {idx}. {decision:<6} {symbol:<8} "
                f"pri={priority:.3f}  src={source}  urgency={urgency}"
            )
            a(f"     Reason: {_decision_reason(row)}")
            flags = row.get("risk_flags") or []
            if flags:
                a(f"     Risk Flags: {', '.join(str(f) for f in flags)}")
    else:
        a("  Decision plan unavailable.")
    a("")

    # Decision Engine: Capital Actions
    a(_LINE)
    a("  CAPITAL ACTIONS")
    a(_LINE)
    capital_rows = [
        r for r in dp_rows
        if str(r.get("decision") or "") in {"SELL", "SCALE", "BUY"}
    ]
    if capital_rows:
        action_counts = {"SELL": 0, "SCALE": 0, "BUY": 0}
        total_amount = 0.0
        amount_count = 0
        for row in capital_rows:
            action = str(row.get("decision") or "")
            action_counts[action] = action_counts.get(action, 0) + 1
            amount = row.get("recommended_amount")
            try:
                total_amount += float(amount)
                amount_count += 1
            except (TypeError, ValueError):
                pass
        a(
            "  Actions: "
            f"SELL={action_counts.get('SELL', 0)}, "
            f"SCALE={action_counts.get('SCALE', 0)}, "
            f"BUY={action_counts.get('BUY', 0)}"
        )
        if amount_count > 0:
            a(f"  Total recommended capital amount: {_fmt_money(total_amount)}")
        top_actions = [
            f"{r.get('decision', 'â€”')} {r.get('symbol', 'â€”')}"
            for r in capital_rows[:5]
        ]
        a(f"  Top actions: {', '.join(top_actions)}")
    else:
        a("  No capital actions in the current decision plan.")
    a("")

    # Decision Engine: Risk Focus
    a(_LINE)
    a("  RISK FOCUS")
    a(_LINE)
    structural_rows = _top_structural_decisions(dp_rows)
    if structural_rows:
        top_structural = ", ".join(
            f"{r.get('symbol', 'â€”')} ({r.get('decision', 'â€”')})"
            for r in structural_rows[:3]
        )
        a(f"  Structural decisions lead the plan: {top_structural}")
        violation_types: list[str] = []
        for row in structural_rows:
            vtype = str((row.get('inputs_used') or {}).get('violation_type') or '').strip()
            if vtype and vtype not in violation_types:
                violation_types.append(vtype)
        if "concentration" in violation_types:
            a("  Concentration risk is active and should be reviewed first.")
        if "leverage" in violation_types:
            a("  Leverage risk is active and should be reduced first.")
    else:
        a("  No structural decisions at the top of the current plan.")
    a("")

    a(_LINE)
    a("  POLICY STATUS")
    a(_LINE)
    ws  = _label(ss.get("ranking_weights_source") or "default")
    wc  = str(ss.get("ranking_weights_candidate") or "current")
    ap  = _label(ss.get("allocation_policy_status") or "not_approved")
    atl = bool(ss.get("applied_to_live", False))

    a(f"  Ranking Weights:    {ws}  (candidate: {wc})")
    a(f"  Allocation Policy:  {ap}")
    a(f"  Applied to Live:    {'Yes' if atl else 'No — advisory only'}")
    if bool(ss.get("simulation_observe_only", True)):
        a("  Simulation:         Observe-only, not applied to live")
    if ss.get("policy_low_sample_warning"):
        a(f"  WARNING:            Low sample ({ss.get('policy_sample_size', 0)} records)")
    a("")

    # ── CHANGES SINCE LAST RUN ───────────────────────────────────────────────
    a(_LINE)
    a("  CHANGES SINCE LAST RUN")
    a(_LINE)
    summary_line = str(ch.get("summary_line") or "No change data available.")
    a(f"  {summary_line}")
    for c in (ch.get("changes") or []):
        a(f"  - {c}")
    if not ch.get("previous_available"):
        a("  (No previous summary available for comparison.)")
    prev_gen = str(ch.get("previous_generated_at") or "")
    if prev_gen:
        a(f"  Previous run: {prev_gen[:19].replace('T', ' ')}")
    a("")

    # ── FOOTER ───────────────────────────────────────────────────────────────
    a(_LINE)
    a("  Advisory only — no trades executed.")
    a(f"  Generated: {gen_display}")
    a(_SEP)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown memo builder
# ---------------------------------------------------------------------------

def build_daily_memo_md(summary: dict[str, Any]) -> str:
    """
    Build a Markdown daily investment memo from a system_decision_summary dict.
    Mirrors the section structure of build_daily_memo.
    """
    gen_at      = str(summary.get("generated_at") or "")
    gen_display = gen_at[:19].replace("T", " ") if gen_at else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_str    = gen_at[:10] if gen_at else datetime.now().strftime("%Y-%m-%d")

    tt = dict(summary.get("top_theme") or {})
    to = dict(summary.get("top_opportunity") or {})
    bf = dict(summary.get("best_portfolio_fit") or {})
    cp = dict(summary.get("capital_preview") or {})
    ss = dict(summary.get("system_state") or {})
    dh = dict(summary.get("data_health") or {})
    ch = dict(summary.get("changes") or {})
    dp_rows = _decision_rows(summary)

    lines: list[str] = []
    a = lines.append

    subject = get_subject(summary)
    a(f"# {subject}")
    a("")

    # Header metadata
    degraded   = bool(dh.get("degraded_mode", False))
    data_mode  = str(dh.get("data_mode") or "unknown")
    health_str = "**DEGRADED** — reduced data quality" if degraded else "Normal"

    a(f"**Date:** {date_str}  ")
    a(f"**Data Health:** {health_str}  ")
    if data_mode not in ("unknown", "live", ""):
        a(f"**Data Mode:** {data_mode}  ")
    a(f"**Generated:** {gen_display}")
    a("")

    # Top Insight
    a("## Top Insight")
    a("")
    a(f"> {_build_top_insight(tt, to)}")
    a("")

    # Top Theme
    a("## Top Theme")
    if tt:
        score   = _flt(tt.get("score"))
        persist = _flt(tt.get("persistence"))
        accel   = _flt(tt.get("acceleration"))
        tickers = tt.get("tickers") or []
        a(f"**{tt.get('name', '—')}** ({_label(tt.get('type') or 'classified')})")
        a(f"Score: `{score:.3f}` · Persistence: `{persist:.3f}` · Acceleration: `{accel:+.3f}`")
        if tickers:
            a(f"Key tickers: {', '.join(f'`{t}`' for t in tickers[:8])}")
    else:
        a("_No theme data available._")
    a("")

    # Top Opportunity
    a("## Top Opportunity")
    if dh.get("fallback_alerts_used"):
        a("> **Note:** No strong alerts this run — showing top-ranked fallback opportunities.")
        a("")
    if to:
        rank = _flt(to.get("final_rank_score"))
        conf = _flt(to.get("confidence"))
        mult = _flt(to.get("rank_multiplier"), 1.0)
        a(f"**{to.get('ticker', '—')}**")
        a(f"- Rank Score: `{rank:.3f}` · Confidence: `{conf:.3f}`")
        a(f"- Conviction: {_label(to.get('conviction_band'))}")
        a(f"- Theme: {_label(to.get('theme_alignment_label'))} · Portfolio Fit: {_label(to.get('portfolio_fit_label'))}")
        if mult != 1.0:
            a(f"- Rank Multiplier: ×{mult:.2f}")
    else:
        a("_No eligible signals found._")
    a("")

    # Portfolio Insight
    a("## Portfolio Insight")
    if bf:
        b_score  = _flt(bf.get("portfolio_fit_score"))
        b_reason = str(bf.get("portfolio_fit_reason") or "")
        a(f"**Best fit:** {bf.get('ticker', '—')} (fit score `{b_score:.3f}`, {_label(bf.get('portfolio_fit_label'))})")
        if b_reason:
            a(f"Reason: {b_reason}")
    else:
        a("_No portfolio fit data available._")

    total_sigs    = int(dh.get("total_signals") or 0)
    eligible_sigs = int(dh.get("eligible_signals") or 0)
    if total_sigs > 0:
        theme_note = f" aligned to {tt.get('name')} theme" if tt.get("name") else ""
        a(f"Signals: {total_sigs} total, {eligible_sigs} alert-eligible{theme_note}")
    a("")

    # Capital Preview
    a("## Capital Preview")
    if cp:
        cand       = int(cp.get("candidate_count") or 0)
        base_pct   = cp.get("total_baseline_pct")
        prev_pct   = cp.get("total_preview_pct")
        delta      = cp.get("preview_vs_baseline_delta")
        sim_sample = int(cp.get("simulation_sample_size") or 0)
        eff_delta  = cp.get("simulation_efficiency_delta")
        ret_delta  = cp.get("simulation_return_delta")

        a(f"- Candidates: {cand}")
        if base_pct is not None and prev_pct is not None:
            a(f"- Baseline: {_pct(base_pct)} → Rank-Aware: {_pct(prev_pct)} (Δ {_fmt_delta(delta)})")
        if sim_sample > 0:
            a(f"- Simulation ({sim_sample} signals): Efficiency {_fmt_delta(eff_delta)} · Return {_fmt_delta(ret_delta)}")
    else:
        a("_No capital preview data available._")
    a("")

    # Top Decisions
    a("## Top Decisions")
    if dp_rows:
        for row in dp_rows[:5]:
            decision = str(row.get("decision") or "â€”")
            symbol = str(row.get("symbol") or "â€”")
            priority = _flt(row.get("priority"))
            source = str(row.get("source") or "â€”")
            urgency = str(row.get("urgency") or "â€”")
            a(
                f"- **{decision}** `{symbol}` Â· priority `{priority:.3f}` "
                f"Â· source `{source}` Â· urgency `{urgency}`"
            )
            a(f"  - Reason: {_decision_reason(row)}")
            flags = row.get("risk_flags") or []
            if flags:
                a(f"  - Risk flags: {', '.join(str(f) for f in flags)}")
    else:
        a("_Decision plan unavailable._")
    a("")

    # Capital Actions
    a("## Capital Actions")
    capital_rows = [
        r for r in dp_rows
        if str(r.get("decision") or "") in {"SELL", "SCALE", "BUY"}
    ]
    if capital_rows:
        action_counts = {"SELL": 0, "SCALE": 0, "BUY": 0}
        total_amount = 0.0
        amount_count = 0
        for row in capital_rows:
            action = str(row.get("decision") or "")
            action_counts[action] = action_counts.get(action, 0) + 1
            amount = row.get("recommended_amount")
            try:
                total_amount += float(amount)
                amount_count += 1
            except (TypeError, ValueError):
                pass
        a(
            f"- SELL: {action_counts.get('SELL', 0)}  "
            f"Â· SCALE: {action_counts.get('SCALE', 0)}  "
            f"Â· BUY: {action_counts.get('BUY', 0)}"
        )
        if amount_count > 0:
            a(f"- Total recommended capital amount: {_fmt_money(total_amount)}")
    else:
        a("_No capital actions in the current decision plan._")
    a("")

    # Risk Focus
    a("## Risk Focus")
    structural_rows = _top_structural_decisions(dp_rows)
    if structural_rows:
        top_structural = ", ".join(
            f"`{r.get('symbol', 'â€”')}` ({r.get('decision', 'â€”')})"
            for r in structural_rows[:3]
        )
        a(f"- Structural decisions lead the plan: {top_structural}")
        violation_types: list[str] = []
        for row in structural_rows:
            vtype = str((row.get("inputs_used") or {}).get("violation_type") or "").strip()
            if vtype and vtype not in violation_types:
                violation_types.append(vtype)
        if "concentration" in violation_types:
            a("- Concentration risk is active and should be reviewed first.")
        if "leverage" in violation_types:
            a("- Leverage risk is active and should be reduced first.")
    else:
        a("_No structural decisions at the top of the current plan._")
    a("")

    # Policy Status
    a("## Policy Status")
    ws  = _label(ss.get("ranking_weights_source") or "default")
    wc  = str(ss.get("ranking_weights_candidate") or "current")
    ap  = _label(ss.get("allocation_policy_status") or "not_approved")
    atl = bool(ss.get("applied_to_live", False))

    a(f"- Ranking weights: **{ws}** (candidate: `{wc}`)")
    a(f"- Allocation policy: **{ap}** · Applied to live: {'Yes' if atl else 'No'}")
    if bool(ss.get("simulation_observe_only", True)):
        a("- Simulation: observe-only, not applied to live")
    if ss.get("policy_low_sample_warning"):
        a(f"- ⚠ Low sample warning: {ss.get('policy_sample_size', 0)} records")
    a("")

    # Changes
    a("## Changes Since Last Run")
    summary_line = str(ch.get("summary_line") or "No change data available.")
    a(summary_line)
    for c in (ch.get("changes") or []):
        a(f"- {c}")
    if not ch.get("previous_available"):
        a("_No previous summary available for comparison._")
    prev_gen = str(ch.get("previous_generated_at") or "")
    if prev_gen:
        a(f"_Previous run: {prev_gen[:19].replace('T', ' ')}_")
    a("")

    # Footer
    a("---")
    a(f"_Advisory only — no trades executed. Generated: {gen_display}_")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Compact memo builders
# ---------------------------------------------------------------------------

def build_daily_memo(
    summary: dict[str, Any],
    *,
    discovery_data: "dict[str, Any] | None" = None,
) -> str:
    """
    Build a compact, decision-focused plain-text memo.

    The memo is intentionally brief. Full detail remains in JSON artifacts and
    GUI surfaces.
    """
    gen_at      = str(summary.get("generated_at") or "")
    gen_display = gen_at[:19].replace("T", " ") if gen_at else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_str    = gen_at[:10] if gen_at else datetime.now().strftime("%Y-%m-%d")

    tt = dict(summary.get("top_theme") or {})
    to = dict(summary.get("top_opportunity") or {})
    dh = dict(summary.get("data_health") or {})
    ch = dict(summary.get("changes") or {})
    top_rows = _top_decision_rows(summary, limit=5)
    capital_counts, capital_total = _capital_action_summary(top_rows)
    risk_items = _risk_focus_items(top_rows)
    change_items = _change_items(ch)
    health_items = _health_items(dh)

    lines: list[str] = []
    a = lines.append

    a(f"Subject: {get_subject(summary)}")
    a("")
    a(_SEP)
    a("  DAILY INVESTMENT MEMO")
    a(f"  {date_str}")
    a(_SEP)
    a("")

    a(_LINE)
    a("  TOP INSIGHT")
    a(_LINE)
    a(f"  {_build_memo_top_insight(tt, to, top_rows)}")
    a("")

    a(_LINE)
    a("  TOP DECISIONS")
    a(_LINE)
    if top_rows:
        for idx, row in enumerate(top_rows, 1):
            decision = str(row.get("decision") or "-")
            symbol = str(row.get("symbol") or "-")
            priority = _flt(row.get("priority"))
            source = str(row.get("source") or "-")
            urgency = str(row.get("urgency") or "-")
            a(f"  {idx}. {decision} {symbol} | pri {priority:.3f} | {source} | {urgency}")
            reason = _decision_reason(row)
            flags = [str(flag) for flag in (row.get("risk_flags") or []) if str(flag)]
            if flags:
                a(f"     {reason} Risk: {', '.join(flags)}.")
            else:
                a(f"     {reason}")
    else:
        a("  Decision plan unavailable.")
    a("")

    a(_LINE)
    a("  CAPITAL ACTIONS")
    a(_LINE)
    a(
        "  "
        f"SELL={capital_counts.get('SELL', 0)}, "
        f"SCALE={capital_counts.get('SCALE', 0)}, "
        f"BUY={capital_counts.get('BUY', 0)}"
    )
    if capital_total is not None:
        a(f"  Total recommended capital: {_fmt_money(capital_total)}")
    a("")

    a(_LINE)
    a("  RISK FOCUS")
    a(_LINE)
    for item in risk_items[:3]:
        a(f"  - {item}")
    a("")

    a(_LINE)
    a("  WHAT CHANGED")
    a(_LINE)
    for item in change_items[:3]:
        a(f"  - {item}")
    a("")

    if health_items:
        a(_LINE)
        a("  SYSTEM / DATA HEALTH")
        a(_LINE)
        for item in health_items[:3]:
            a(f"  - {item}")
        a("")

    if discovery_data is not None:
        try:
            a(_build_discovery_section(discovery_data))
        except Exception as exc:
            logger.warning("daily_memo: discovery section failed — %s", exc)
            a(_LINE)
            a("  DISCOVERY RESEARCH  [Sandbox Only]")
            a(_LINE)
            a("  Discovery data unavailable (loading error).")
            a("")

    a(_LINE)
    a("  Advisory only — no trades executed.")
    a(f"  Generated: {gen_display}")
    a(_SEP)

    return "\n".join(lines)


def build_daily_memo_md(
    summary: dict[str, Any],
    *,
    discovery_data: "dict[str, Any] | None" = None,
) -> str:
    """
    Build a compact, decision-focused Markdown memo.

    The memo is intentionally brief. Full detail remains in JSON artifacts and
    GUI surfaces.
    """
    gen_at      = str(summary.get("generated_at") or "")
    gen_display = gen_at[:19].replace("T", " ") if gen_at else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_str    = gen_at[:10] if gen_at else datetime.now().strftime("%Y-%m-%d")

    tt = dict(summary.get("top_theme") or {})
    to = dict(summary.get("top_opportunity") or {})
    dh = dict(summary.get("data_health") or {})
    ch = dict(summary.get("changes") or {})
    top_rows = _top_decision_rows(summary, limit=5)
    capital_counts, capital_total = _capital_action_summary(top_rows)
    risk_items = _risk_focus_items(top_rows)
    change_items = _change_items(ch)
    health_items = _health_items(dh)

    lines: list[str] = []
    a = lines.append

    a(f"# {get_subject(summary)}")
    a("")
    a(f"**Date:** {date_str}  ")
    a(f"**Generated:** {gen_display}")
    a("")

    a("## Top Insight")
    a("")
    a(f"> {_build_memo_top_insight(tt, to, top_rows)}")
    a("")

    a("## Top Decisions")
    if top_rows:
        for row in top_rows:
            decision = str(row.get("decision") or "-")
            symbol = str(row.get("symbol") or "-")
            priority = _flt(row.get("priority"))
            source = str(row.get("source") or "-")
            urgency = str(row.get("urgency") or "-")
            a(f"- **{decision}** `{symbol}` | priority `{priority:.3f}` | source `{source}` | urgency `{urgency}`")
            reason = _decision_reason(row)
            flags = [str(flag) for flag in (row.get("risk_flags") or []) if str(flag)]
            if flags:
                a(f"  - {reason} Risk: {', '.join(flags)}.")
            else:
                a(f"  - {reason}")
    else:
        a("_Decision plan unavailable._")
    a("")

    a("## Capital Actions")
    a(
        f"- SELL: {capital_counts.get('SELL', 0)} | "
        f"SCALE: {capital_counts.get('SCALE', 0)} | "
        f"BUY: {capital_counts.get('BUY', 0)}"
    )
    if capital_total is not None:
        a(f"- Total recommended capital: {_fmt_money(capital_total)}")
    a("")

    a("## Risk Focus")
    for item in risk_items[:3]:
        a(f"- {item}")
    a("")

    a("## What Changed")
    for item in change_items[:3]:
        a(f"- {item}")
    a("")

    if health_items:
        a("## System / Data Health")
        for item in health_items[:3]:
            a(f"- {item}")
        a("")

    if discovery_data is not None:
        try:
            a(_build_discovery_section_md(discovery_data))
        except Exception as exc:
            logger.warning("daily_memo: discovery section (md) failed — %s", exc)
            a("## Discovery Research — Sandbox Only")
            a("")
            a("_Discovery data unavailable (loading error)._")
            a("")

    a("---")
    a(f"_Advisory only — no trades executed. Generated: {gen_display}_")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def generate_daily_memo(
    *,
    root: "Path | str | None" = None,
    write_files: bool = True,
) -> tuple[str, str]:
    """
    Load system_decision_summary.json, build plain-text and Markdown memos,
    optionally write both to outputs/latest/, and return (txt, md).

    Degrades gracefully when the summary file is missing — returns a minimal
    memo noting that no data is available.
    """
    root_path = Path(root) if root is not None else Path(__file__).resolve().parents[2]

    summary = _safe_load(root_path.joinpath(*_SUMMARY_JSON_REL))
    decision_plan = _safe_load(root_path.joinpath(*_DECISION_PLAN_JSON_REL))
    if not summary:
        logger.warning(
            "daily_memo: system_decision_summary.json not found at %s — "
            "generating empty memo. Run `python -m watchlist_scanner.system_summary` first.",
            root_path.joinpath(*_SUMMARY_JSON_REL),
        )

    if decision_plan:
        summary = dict(summary)
        summary["_decision_plan"] = decision_plan

    discovery_data: "dict[str, Any] | None" = None
    try:
        discovery_data = _load_discovery_sandbox_data(root_path)
    except Exception as exc:
        logger.warning("daily_memo: discovery sandbox load failed (non-fatal) — %s", exc)

    memo_txt = build_daily_memo(summary, discovery_data=discovery_data)
    memo_md  = build_daily_memo_md(summary, discovery_data=discovery_data)

    if write_files:
        txt_path = root_path.joinpath(*_MEMO_TXT_REL)
        md_path  = root_path.joinpath(*_MEMO_MD_REL)
        txt_path.parent.mkdir(parents=True, exist_ok=True)
        txt_path.write_text(memo_txt, encoding="utf-8")
        md_path.write_text(memo_md, encoding="utf-8")
        logger.info("daily_memo: wrote %s and %s", txt_path, md_path)

    return memo_txt, memo_md


# ---------------------------------------------------------------------------
# Email sender
# ---------------------------------------------------------------------------

_SMTP_TIMEOUT: int = 15  # seconds per connection attempt


def _load_email_env() -> None:
    """Best-effort load of a local .env without overriding real env vars."""
    try:
        from dotenv import find_dotenv, load_dotenv
    except Exception:
        return

    candidates: list[Path] = []

    found = find_dotenv(usecwd=True)
    if found:
        candidates.append(Path(found))

    repo_env = Path(__file__).resolve().parents[1] / ".env"
    if repo_env.exists():
        candidates.append(repo_env)

    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        load_dotenv(resolved, override=False)


def _get_env_value(*names: str) -> str:
    """Return the first non-empty environment variable value from names."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def send_email(
    memo_text: str,
    *,
    subject: str | None = None,
    max_attempts: int = 3,
) -> bool:
    """
    Send memo_text via SMTP using environment variables.

    Required env vars: SMTP_SERVER, EMAIL_USER, EMAIL_PASS, EMAIL_TO
    Optional env var:  SMTP_PORT (default 587)
    Backward-compatible aliases: SMTP_HOST, EMAIL_SENDER, EMAIL_PASSWORD,
    EMAIL_RECIPIENT

    Retries up to max_attempts times on transient failures.
    Returns True on success, False on any failure (never raises).
    Credentials are never written to logs.
    """
    _load_email_env()

    server   = _get_env_value("SMTP_SERVER", "SMTP_HOST")
    port_str = _get_env_value("SMTP_PORT") or "587"
    user     = _get_env_value("EMAIL_USER", "EMAIL_SENDER")
    password = _get_env_value("EMAIL_PASS", "EMAIL_PASSWORD")
    to_addr  = _get_env_value("EMAIL_TO", "EMAIL_RECIPIENT")

    missing = [
        name for name, val in [
            ("SMTP_SERVER", server), ("EMAIL_USER", user),
            ("EMAIL_PASS", password), ("EMAIL_TO", to_addr),
        ]
        if not val
    ]
    if missing:
        logger.warning(
            "daily_memo: send_email skipped — missing env vars: %s",
            ", ".join(missing),
        )
        return False

    try:
        port = int(port_str)
    except ValueError:
        port = 587

    if subject is None:
        first_line = memo_text.split("\n", 1)[0]
        subject = first_line[9:] if first_line.startswith("Subject: ") else (
            f"Daily Investment Memo — {datetime.now().strftime('%Y-%m-%d')}"
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = user
    msg["To"]      = to_addr
    msg.attach(MIMEText(memo_text, "plain", "utf-8"))

    for attempt in range(1, max_attempts + 1):
        try:
            context = ssl.create_default_context()
            with smtplib.SMTP(server, port, timeout=_SMTP_TIMEOUT) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.login(user, password)
                smtp.sendmail(user, to_addr, msg.as_string())
            logger.info("daily_memo: email sent to %s via %s:%s", to_addr, server, port)
            return True
        except Exception as exc:
            exc_type = type(exc).__name__
            if attempt < max_attempts:
                logger.warning(
                    "daily_memo: send attempt %d/%d failed (%s) — retrying",
                    attempt, max_attempts, exc_type,
                )
            else:
                logger.warning(
                    "daily_memo: send_email failed after %d attempt(s) — %s",
                    max_attempts, exc_type,
                )
    return False


def send_test_email() -> bool:
    """
    Send a simple test message to verify SMTP configuration.
    Does not require a pipeline run or generated memo.
    Returns True on success, False on any failure (never raises).
    """
    subject = "Test Email — Investment System"
    body    = "Email system is working correctly."
    return send_email(body, subject=subject)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m watchlist_scanner.daily_memo",
        description=(
            "Generate a Daily Investment Memo from the latest system decision summary. "
            "Writes outputs/latest/daily_memo.txt and daily_memo.md. "
            "Advisory only — no live behavior changes."
        ),
    )
    parser.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="Project root (default: two levels above this module)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print memo to stdout without writing output files",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help=(
            "Send memo via email after generating. "
            "Requires SMTP_SERVER, EMAIL_USER, EMAIL_PASS, EMAIL_TO env vars "
            "(legacy aliases also accepted)."
        ),
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help=(
            "Send a simple test email to verify SMTP configuration. "
            "Does not generate the memo or require a pipeline run."
        ),
    )
    args = parser.parse_args()

    if args.test_email:
        ok = send_test_email()
        if ok:
            print("Test email sent successfully.")
        else:
            print("Test email failed — check SMTP env vars and logs.")
        return

    memo_txt, _ = generate_daily_memo(
        root=args.root,
        write_files=not args.dry_run,
    )

    print(memo_txt)

    if args.dry_run:
        print("\n[DRY-RUN] No files written.")
    else:
        print("\nFiles written:")
        print("  outputs/latest/daily_memo.txt")
        print("  outputs/latest/daily_memo.md")

    if args.send:
        ok = send_email(memo_txt)
        if ok:
            print("Email sent successfully.")
        else:
            print("Email send failed or skipped (check env vars / logs).")


if __name__ == "__main__":
    _main()
