"""
Daily Investment Memo.

Loads outputs/latest/system_decision_summary.json and produces a clean,
human-readable memo suitable for email delivery and mobile viewing.

Writes:
  outputs/latest/daily_memo.txt  — plain text
  outputs/latest/daily_memo.md   — Markdown

CLI:
  python -m watchlist_scanner.daily_memo           # generate only
  python -m watchlist_scanner.daily_memo --send    # generate + email
  python -m watchlist_scanner.daily_memo --dry-run # print, no files written

Email env vars (all required for --send):
  SMTP_SERVER, SMTP_PORT (default 587), EMAIL_USER, EMAIL_PASS, EMAIL_TO
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
_MEMO_TXT_REL     = ("outputs", "latest", "daily_memo.txt")
_MEMO_MD_REL      = ("outputs", "latest", "daily_memo.md")

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
    if not summary:
        logger.warning(
            "daily_memo: system_decision_summary.json not found at %s — "
            "generating empty memo. Run `python -m watchlist_scanner.system_summary` first.",
            root_path.joinpath(*_SUMMARY_JSON_REL),
        )

    memo_txt = build_daily_memo(summary)
    memo_md  = build_daily_memo_md(summary)

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

def send_email(memo_text: str, *, subject: str | None = None) -> bool:
    """
    Send memo_text via SMTP using environment variables.

    Required env vars: SMTP_SERVER, EMAIL_USER, EMAIL_PASS, EMAIL_TO
    Optional env var:  SMTP_PORT (default 587)

    Returns True on success, False on any failure (never raises).
    """
    server   = os.environ.get("SMTP_SERVER", "").strip()
    port_str = os.environ.get("SMTP_PORT", "587").strip()
    user     = os.environ.get("EMAIL_USER", "").strip()
    password = os.environ.get("EMAIL_PASS", "").strip()
    to_addr  = os.environ.get("EMAIL_TO", "").strip()

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

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(server, port) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.login(user, password)
            smtp.sendmail(user, to_addr, msg.as_string())
        logger.info("daily_memo: email sent to %s via %s:%s", to_addr, server, port)
        return True
    except Exception as exc:
        logger.warning("daily_memo: send_email failed — %s", exc)
        return False


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
            "Requires SMTP_SERVER, EMAIL_USER, EMAIL_PASS, EMAIL_TO env vars."
        ),
    )
    args = parser.parse_args()

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
