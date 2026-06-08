"""Portfolio Manager cockpit — read view.

Composes normalized `shared.card(...)` cards from decision-core and
advisory artifacts. Reuses:
  - `gui_v2.data.portfolio.collect_portfolio_view` for holdings/watchlist/signals
  - `gui_v2.data.risk_impact.collect_risk_impact_view` for risk delta
Rather than re-reading those artifacts from scratch.

Source-of-truth rule: only decision_plan / system_decision_summary cards
carry advisory action verbs. Risk, watchlist, news, advisor cards describe
EVIDENCE / STATE, not actions.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gui_v2.data.shared import card, _read_json
from gui_v2.data.portfolio import collect_portfolio_view as _portfolio_data
from gui_v2.data.risk_impact import collect_risk_impact_view as _risk_data


# ---------------------------------------------------------------------------
# Holdings from real snapshot keys (H1 fix)
# ---------------------------------------------------------------------------

def _holdings_from_real_snapshot(root: Path) -> list[dict[str, Any]]:
    """
    Build holdings rows directly from outputs/portfolio/portfolio_snapshot.json
    using the real producer keys (ticker/suggested_allocation/conviction_score/
    conviction_band/normalized_allocation/sector).

    Each row: {symbol, suggested_allocation_pct, normalized_allocation_pct,
               conviction, band, sector}.

    Returns [] on any error or absent snapshot.
    """
    snap_path = Path(root) / "outputs" / "portfolio" / "portfolio_snapshot.json"
    snap = _read_json(snap_path)
    if not isinstance(snap, dict):
        return []
    rows_raw = snap.get("rows") or []
    if not isinstance(rows_raw, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows_raw:
        if not isinstance(row, dict):
            continue
        ticker = row.get("ticker")
        if not ticker:
            continue
        suggested = row.get("suggested_allocation")
        normalized = row.get("normalized_allocation")
        out.append({
            "symbol": ticker,
            "suggested_allocation_pct": (
                round(float(suggested) * 100, 1) if suggested is not None else None
            ),
            "normalized_allocation_pct": (
                round(float(normalized) * 100, 1) if normalized is not None else None
            ),
            "conviction": row.get("conviction_score"),
            "band": row.get("conviction_band"),
            "sector": row.get("sector"),
        })
    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _memo_first_lines(root: Path, max_lines: int = 8) -> str:
    """Return the first `max_lines` non-blank lines of daily_memo.md."""
    memo_path = Path(root) / "outputs" / "latest" / "daily_memo.md"
    try:
        if not memo_path.exists():
            return ""
        lines = memo_path.read_text(encoding="utf-8").splitlines()
        kept: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped:
                kept.append(stripped)
            if len(kept) >= max_lines:
                break
        return " / ".join(kept)
    except Exception:
        return ""


def _top_decisions(dp: dict | None, max_decisions: int = 5) -> list[dict[str, Any]]:
    """Extract top N decisions from decision_plan, normalized for the decision_card component."""
    if not isinstance(dp, dict):
        return []
    decisions = dp.get("decisions") or []
    if not isinstance(decisions, list):
        return []

    out: list[dict[str, Any]] = []
    for d in decisions:
        if not isinstance(d, dict):
            continue
        # Normalize action verb: use 'decision' field (BUY/SELL/HOLD/SCALE)
        action = (d.get("decision") or d.get("recommended_action_label") or "").upper()
        out.append({
            "ticker": d.get("symbol") or d.get("ticker") or "",
            "action": action,
            "priority": d.get("priority") or d.get("priority_score") or 0,
            "urgency": d.get("urgency") or "",
            "rationale": d.get("reason") or d.get("decision_reason") or "",
            "confidence": d.get("confidence") or 0,
            "source": d.get("source") or "decision_plan",
        })
        if len(out) >= max_decisions:
            break
    return out


# ---------------------------------------------------------------------------
# Public collector
# ---------------------------------------------------------------------------

def collect_portfolio_view(root: Path) -> dict[str, Any]:
    """
    Persona collector for /dashboard/portfolio.

    Returns::

        {
          "cards": [ <card dicts> ],
          "persona": "portfolio",
          "decisions": [ <decision row dicts> ],  # for decision_card component
          "holdings": [ ... ],                    # from portfolio data source
          "allocation": { ... },
          "watchlist": [ ... ],
          "recent_signals": [ ... ],
        }
    """
    root = Path(root)
    latest = root / "outputs" / "latest"
    cards: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 1. Top Insight  — system_decision_summary.json
    #    (decision-core sourced — may carry advisory language)
    # ------------------------------------------------------------------
    sds = _read_json(latest / "system_decision_summary.json") or {}

    if sds:
        top_opp = sds.get("top_opportunity") or {}
        top_theme = sds.get("top_theme") or {}
        opp_ticker = (top_opp.get("ticker") or "") if isinstance(top_opp, dict) else ""
        theme_name = (top_theme.get("name") or "") if isinstance(top_theme, dict) else ""
        changes_obj = sds.get("changes") or {}
        change_line = (
            (changes_obj.get("summary_line") or "") if isinstance(changes_obj, dict) else ""
        )
        policy = sds.get("policy_insight") or {}
        policy_reason = (
            (policy.get("recommendation_reason") or "") if isinstance(policy, dict) else ""
        )
        summary_parts = []
        if theme_name:
            summary_parts.append(f"Theme: {theme_name}")
        if opp_ticker:
            summary_parts.append(f"Lead opportunity: {opp_ticker}")
        if change_line:
            summary_parts.append(change_line)
        if policy_reason:
            summary_parts.append(policy_reason)
        cards.append(card(
            "Top Insight",
            status="info",
            label="decision-core",
            summary="; ".join(summary_parts) or "System decision summary available",
            source_artifacts=["system_decision_summary.json"],
            updated_at=sds.get("generated_at"),
        ))
    else:
        cards.append(card(
            "Top Insight",
            status="unknown",
            label="unavailable",
            summary="system_decision_summary.json absent — run daily pipeline",
            source_artifacts=["system_decision_summary.json"],
        ))

    # ------------------------------------------------------------------
    # 2. Decision Queue  — decision_plan.json (decision-core)
    #    Only this card may convey advisory action language.
    # ------------------------------------------------------------------
    dp = _read_json(latest / "decision_plan.json")
    decisions = _top_decisions(dp)

    if dp is not None:
        total = (dp.get("total_decisions") or len((dp.get("decisions") or [])))
        ctx = dp.get("portfolio_context") or {}
        run_mode = dp.get("run_mode") or "unknown"
        # cash may be under "cash_available" or "cash"
        _cash_val = None
        if isinstance(ctx, dict):
            _cash_val = ctx.get("cash_available") or ctx.get("cash")
        _cash_str = (
            f"Cash available: ${float(_cash_val):,.2f}" if _cash_val is not None else ""
        )
        cards.append(card(
            "Decision Queue",
            status="ok",
            label=f"{total} advisory actions",
            summary=(
                f"{total} advisory actions (mode: {run_mode}). "
                + _cash_str
            ),
            source_artifacts=["decision_plan.json"],
            updated_at=dp.get("generated_at"),
        ))
    else:
        cards.append(card(
            "Decision Queue",
            status="red",
            label="MISSING",
            summary="decision_plan.json absent — no advisory actions available",
            source_artifacts=["decision_plan.json"],
        ))

    # ------------------------------------------------------------------
    # 3. Risk Focus  — risk_delta + correlation_risk_advisor +
    #    vol_regime_advisor + earnings_gate + exit_advisor
    #    These cards describe STATE / EVIDENCE; no action verbs.
    # ------------------------------------------------------------------
    risk_view = _risk_data(root)
    rd = risk_view.get("risk_delta") or {}
    rd_status = rd.get("overall_status") or "unknown" if rd else "unknown"

    # Map risk_delta status → card status
    _risk_map = {"ok": "ok", "ok_with_warnings": "warning", "near_cap": "warning",
                 "breach": "red", "failed": "red", "partial": "warning"}
    rd_card_status = _risk_map.get(rd_status, "warning")

    # Gather sub-advisor summaries
    corr = _read_json(latest / "correlation_risk_advisor.json") or {}
    vol = _read_json(latest / "vol_regime_advisor.json") or {}
    earn = _read_json(latest / "earnings_gate.json") or {}
    exit_adv = _read_json(latest / "exit_advisor.json") or {}

    corr_status = corr.get("status") or "unknown"
    vol_status = vol.get("status") or "unknown"

    earn_counts = earn.get("counts") or {}
    earn_near = earn_counts.get("near_earnings", 0) if isinstance(earn_counts, dict) else 0
    earn_in = earn_counts.get("in_earnings_window", 0) if isinstance(earn_counts, dict) else 0
    earn_line = earn.get("summary_line") or f"{earn_near} near / {earn_in} in earnings window"

    exit_counts = exit_adv.get("counts") or {}
    exit_flagged = (
        exit_counts.get("flagged", 0) if isinstance(exit_counts, dict) else 0
    )
    exit_line = exit_adv.get("summary_line") or f"{exit_flagged} exit-flagged positions"

    risk_summary_parts = []
    if rd:
        risk_summary_parts.append(f"Risk delta: {rd_status}")
    if corr:
        risk_summary_parts.append(f"Correlation: {corr_status}")
    if vol:
        risk_summary_parts.append(f"Vol regime: {vol_status}")
    if earn:
        risk_summary_parts.append(earn_line)
    if exit_adv:
        risk_summary_parts.append(exit_line)

    cards.append(card(
        "Risk Focus",
        status=rd_card_status,
        label=rd_status,
        summary="; ".join(risk_summary_parts) or "Risk data unavailable",
        source_artifacts=[
            "risk_delta.json",
            "correlation_risk_advisor.json",
            "vol_regime_advisor.json",
            "earnings_gate.json",
            "exit_advisor.json",
        ],
        updated_at=rd.get("generated_at") if rd else None,
    ))

    # ------------------------------------------------------------------
    # 4. Capital / Allocation  — cash_deployment_plan + tax_harvest_advisor
    #    Describes STATE (cash levels, tax positions); no action verbs.
    # ------------------------------------------------------------------
    cash = _read_json(latest / "cash_deployment_plan.json") or {}
    tax = _read_json(latest / "tax_harvest_advisor.json") or {}

    cash_summary_obj = cash.get("cash_summary") or {}
    cash_avail = (
        cash_summary_obj.get("cash_available") if isinstance(cash_summary_obj, dict) else None
    )
    degraded = cash.get("degraded_mode") or False
    cash_label = "degraded" if degraded else ("available" if cash else "unavailable")
    cash_deployed = cash.get("total_deployed_amount") or 0

    tax_harvest_count = tax.get("harvestable_count") or 0
    tax_loss_dollars = tax.get("total_harvestable_loss_dollars") or 0
    tax_summary_line = tax.get("summary_line") or f"{tax_harvest_count} positions harvestable"
    is_taxable = tax.get("is_taxable_account")

    cap_parts: list[str] = []
    if cash:
        cap_parts.append(
            f"Cash available: ${float(cash_avail):,.2f}" if cash_avail is not None
            else f"Deployed: ${cash_deployed:,.0f}"
        )
    if tax:
        cap_parts.append(tax_summary_line)
    if is_taxable is False:
        cap_parts.append("Non-taxable account")

    cards.append(card(
        "Capital / Allocation",
        status="info" if (cash or tax) else "unknown",
        label=cash_label,
        summary="; ".join(cap_parts) or "Capital data unavailable",
        source_artifacts=["cash_deployment_plan.json", "tax_harvest_advisor.json"],
        updated_at=cash.get("generated_at") if cash else None,
    ))

    # ------------------------------------------------------------------
    # 5. Watchlist / Opportunities  — watchlist_signals + market_opportunities
    #    + news_evidence_layer
    #    Evidence layer: describes signals/evidence; no advisory action verbs.
    # ------------------------------------------------------------------
    ws = _read_json(latest / "watchlist_signals.json") or {}
    mo = _read_json(latest / "market_opportunities.json") or {}
    news = _read_json(latest / "news_evidence_layer.json") or {}

    ws_scan = ws.get("scan_summary") or {}
    ws_signals_count = (
        ws_scan.get("signals_count") or len(ws.get("results") or [])
        if isinstance(ws_scan, dict) else len(ws.get("results") or [])
    )
    ws_alerts = ws.get("alerts") or []
    ws_alert_count = len(ws_alerts) if isinstance(ws_alerts, list) else 0

    mo_promoted = (mo.get("promoted") or []) if isinstance(mo.get("promoted"), list) else []
    mo_event_summary = (mo.get("event_summary") or "") if isinstance(mo, dict) else ""

    news_count = 0
    if isinstance(news, dict):
        news_items = news.get("items") or news.get("results") or news.get("evidence") or []
        news_count = len(news_items) if isinstance(news_items, list) else 0

    watch_parts: list[str] = []
    if ws:
        watch_parts.append(f"{ws_signals_count} watchlist signals, {ws_alert_count} alerts")
    if mo:
        promoted_tickers = [
            (p.get("ticker") or p.get("symbol") or "?")
            for p in mo_promoted[:3]
            if isinstance(p, dict)
        ]
        if promoted_tickers:
            watch_parts.append(f"Promoted: {', '.join(promoted_tickers)}")
        elif mo_event_summary:
            watch_parts.append(mo_event_summary[:80])
    if news:
        watch_parts.append(f"{news_count} news items")

    cards.append(card(
        "Watchlist / Opportunities",
        status="info" if (ws or mo) else "unknown",
        label="evidence",
        summary="; ".join(watch_parts) or "Watchlist data unavailable",
        source_artifacts=[
            "watchlist_signals.json",
            "market_opportunities.json",
            "news_evidence_layer.json",
        ],
        updated_at=ws.get("generated_at") if ws else None,
    ))

    # ------------------------------------------------------------------
    # 6. Memo Summary  — daily_memo.md first lines
    # ------------------------------------------------------------------
    memo_text = _memo_first_lines(root)
    cards.append(card(
        "Memo Summary",
        status="info" if memo_text else "unknown",
        label="daily memo" if memo_text else "unavailable",
        summary=memo_text or "daily_memo.md absent — run daily pipeline",
        source_artifacts=["daily_memo.md"],
    ))

    # ------------------------------------------------------------------
    # Holdings: built directly from the real snapshot keys (H1 fix)
    # Watchlist / recent_signals from existing portfolio collector
    # ------------------------------------------------------------------
    portfolio_data = _portfolio_data(root)
    holdings = _holdings_from_real_snapshot(root)

    return {
        "cards": cards,
        "persona": "portfolio",
        # Decision rows for the decision_card component (decision-core only)
        "decisions": decisions,
        # Holdings from real snapshot keys (H1 fix — not from legacy portfolio.py)
        "holdings": holdings,
        "allocation": portfolio_data.get("allocation") or {},
        "watchlist": portfolio_data.get("watchlist") or [],
        "recent_signals": portfolio_data.get("recent_signals") or [],
        # Raw dp for context flags
        "observe_only": (dp.get("observe_only") if isinstance(dp, dict) else True),
    }
