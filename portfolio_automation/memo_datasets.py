"""Observe-only memo datasets: reassemble existing memo-producer artifacts into
domain-keyed datasets (portfolio / crowd_watchlist / institutional / risk /
system). Pure reassembly — no recompute; feeds_decision_engine=false; never
writes decision_plan.json. Source of truth for per-domain briefs + GUI sub-tabs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "1"
DOMAINS = ["portfolio", "crowd_watchlist", "institutional", "risk", "system"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _section(title: str, lines: list[str], severity: str = "info") -> dict:
    return {"title": title, "lines": [l for l in lines if l], "severity": severity}


def _domain(headline: str, sections: list[dict], source_artifacts: list[str],
            warnings: list[str] | None = None) -> dict:
    present = [s for s in sections if s["lines"]]
    status = "ok" if present else "unavailable"
    return {"headline": headline, "status": status, "sections": present,
            "source_artifacts": source_artifacts, "warnings": warnings or []}


def _fmt_money(field) -> str:
    if isinstance(field, dict):
        amt, state = field.get("amount"), field.get("state")
        if state == "confirmed" and amt is not None:
            return f"${amt:,.0f}"
        return state or "unavailable"
    v = _num(field)
    return f"${v:,.0f}" if v is not None else "—"


def _build_portfolio(s: dict) -> dict:
    cp = s.get("daily_capital_plan") or {}
    sd = s.get("system_decision_summary") or {}
    dp = s.get("decision_plan") or {}
    cs = cp.get("capital_summary") or {}
    sections = []
    to = (sd.get("top_opportunity") or {}).get("ticker")
    tt = (sd.get("top_theme") or {}).get("label")
    if to or tt:
        sections.append(_section("Verdict", [
            f"Lead opportunity: {to or '—'} · dominant theme: {tt or '—'}"]))
    if cs:
        sections.append(_section("Today's Capital Plan", [
            f"Funded today: {_fmt_money(cs.get('funded_capital'))} "
            f"({cs.get('funded_count', 0)} funded / {cs.get('deferred_count', 0)} deferred)"]))
    if cp.get("bottom_line"):
        sections.append(_section("Bottom Line", [cp["bottom_line"]]))
    decs = dp.get("decisions") or []
    if decs:
        from collections import Counter
        c = Counter(str(x.get("decision")) for x in decs)
        sections.append(_section("Action counts", [
            " · ".join(f"{k}: {v}" for k, v in sorted(c.items()))]))
    return _domain("Portfolio & Capital", sections,
                   ["daily_capital_plan.json", "system_decision_summary.json", "decision_plan.json"])


def _build_crowd(s: dict) -> dict:
    sd = s.get("system_decision_summary") or {}
    uc = s.get("unified_crowd_status") or {}
    wc = s.get("watch_candidates") or {}
    sections = []
    tt = (sd.get("top_theme") or {}).get("label")
    if tt:
        sections.append(_section("Top Insight", [f"Dominant theme: {tt}"]))
    if uc:
        sc = uc.get("state_counts") or {}
        conf = uc.get("top_confirmed_attention") or []
        sections.append(_section("Unified crowd", [
            f"Status {uc.get('overall_status', '—')} · "
            f"market-context-only {sc.get('market_context_only', 0)} · "
            f"confirmed {', '.join(t.get('ticker', '') for t in conf[:5]) or 'none'}"]))
    cand = wc.get("candidates") or wc.get("watch_candidates") or []
    if cand:
        sections.append(_section("Watchlist candidates", [
            f"{len(cand)} candidate(s): "
            + ", ".join(c.get('symbol', '') for c in cand[:8])]))
    return _domain("Crowd & Watchlist", sections,
                   ["system_decision_summary.json", "unified_crowd_intelligence_status.json",
                    "watch_candidates.json"])


def _build_institutional(s: dict) -> dict:
    ii = s.get("institutional_intelligence") or {}
    recs = ii.get("records") or []
    sections = []
    for r in recs[:5]:
        sections.append(_section(r.get("symbol", "?"), [
            f"{r.get('consensus_state', '—')} · "
            f"filing {r.get('filing_age_days', '—')}d old · "
            f"eff mgrs {r.get('effective_independent_managers', '—')}"]))
    dom = _domain("Institutional (13F)", sections, ["institutional_intelligence.json"])
    if not sections:
        dom["warnings"] = ["inert / no material institutional signal"]
    return dom


def _build_risk(s: dict) -> dict:
    rd = s.get("risk_delta") or {}
    corr = s.get("correlation_risk_advisor") or {}
    sections = []
    conc = (rd.get("concentration") or {}).get("top_position") or {}
    if conc:
        w = _num(conc.get("weight"))
        cap = _num(conc.get("cap"))
        sections.append(_section("Concentration", [
            f"Top: {conc.get('symbol', '—')} "
            f"{w * 100:.1f}% (cap {cap * 100:.0f}%)" if w is not None and cap is not None
            else f"Top: {conc.get('symbol', '—')}"]))
    lev = _num((rd.get("leverage") or {}).get("total_exposure"))
    if lev is not None:
        sections.append(_section("Leverage", [f"{lev * 100:.1f}% total exposure"]))
    eib = _num(corr.get("effective_independent_bets"))
    if eib is not None:
        sections.append(_section("Correlation", [f"~{eib:.2f} effective independent bets"]))
    return _domain("Risk", sections, ["risk_delta.json", "correlation_risk_advisor.json"])


def _build_system(s: dict) -> dict:
    rs = s.get("daily_run_status") or {}
    sections = []
    if rs:
        sections.append(_section("System / Data Health", [
            f"Run status {rs.get('overall_status', '—')} · "
            f"content warnings {rs.get('content_warn_count', 0)}"]))
    return _domain("System & Ops", sections, ["daily_run_status.json"])


_BUILDERS = {
    "portfolio": _build_portfolio, "crowd_watchlist": _build_crowd,
    "institutional": _build_institutional, "risk": _build_risk, "system": _build_system,
}


def build_memo_datasets(sources: dict[str, Any], *, domains: list[str] | None = None,
                        generated_at: str | None = None) -> dict:
    domains = domains or DOMAINS
    out_domains = {}
    for d in domains:
        builder = _BUILDERS.get(d)
        if builder is None:
            continue
        try:
            out_domains[d] = builder(sources)
        except Exception as exc:  # noqa: BLE001 - one domain never breaks others
            out_domains[d] = {"headline": d, "status": "unavailable", "sections": [],
                              "source_artifacts": [], "warnings": [f"build_error:{exc}"]}
    return {
        "schema_version": SCHEMA_VERSION, "source": "memo_datasets",
        "observe_only": True, "no_trade": True, "feeds_decision_engine": False,
        "generated_at": generated_at or _now_iso(), "domains": out_domains,
    }


def render_domain_brief(dataset: dict, domain: str, *, markdown: bool = True) -> list[str]:
    dom = (dataset.get("domains") or {}).get(domain)
    if not dom or dom.get("status") == "unavailable":
        return []
    out: list[str] = []
    head = dom.get("headline", domain)
    out.append(f"## {head}" if markdown else head.upper())
    for sec in dom.get("sections", []):
        out.append(f"### {sec['title']}" if markdown else f"  {sec['title']}")
        for line in sec.get("lines", []):
            out.append(f"- {line}" if markdown else f"    {line}")
    for w in dom.get("warnings", []):
        out.append(f"> {w}" if markdown else f"  note: {w}")
    out.append("_Observe-only — reassembled from source artifacts; no funded-action override._"
               if markdown else "  Observe-only — no funded-action override.")
    out.append("")
    return out
