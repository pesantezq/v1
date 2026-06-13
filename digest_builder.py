"""
Digest Builder Module

Pure-function helpers that assemble the enhanced email digest sections.
All functions accept a DigestContext and degrade gracefully when data
is missing.  No external API calls — completely deterministic.

Sections provided:
  A. Top 3 Actions              (build_top3_actions)
  C. What Changed Since Last Run (build_what_changed)
  D. Trajectory / Long-Term     (build_trajectory)
  E. Opportunity Cost           (build_opportunity_cost)
  F+G. Behavior & Hold Signal   (build_behavior_section, compute_do_nothing_score)
  H. Position Rationale         (build_holding_rationale)
  I. System Status              (build_system_status)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from projections import project_future_value
from utils import format_currency

logger = logging.getLogger("portfolio_automation.digest_builder")


# ---------------------------------------------------------------------------
# Default position rationale for common tickers
# ---------------------------------------------------------------------------
_DEFAULT_RATIONALE: Dict[str, str] = {
    "QQQ":  "long-term US growth engine — top 100 Nasdaq companies",
    "VFH":  "US financial sector exposure — cyclical diversification",
    "VXUS": "international diversification — reduces single-country concentration",
    "GLD":  "inflation hedge and portfolio ballast during stress periods",
    "QLD":  "2× leveraged Nasdaq — tactical amplifier, hard-capped at 5% of portfolio",
    "SPY":  "broad US market core via S&P 500",
    "IWM":  "US small-cap exposure — higher volatility, higher long-run expected premium",
    "VTI":  "total US equity market in one fund",
    "BND":  "investment-grade bonds — dampens portfolio volatility",
    "SCHD": "dividend-quality US equity — income + quality factor tilt",
    "XLE":  "energy sector ETF — real-asset and inflation hedge component",
    "XLF":  "financial sector ETF — cyclical beta exposure",
    "XLK":  "technology sector ETF — concentrated tech growth tilt",
}


# ---------------------------------------------------------------------------
# DigestContext
# ---------------------------------------------------------------------------

@dataclass
class DigestContext:
    """
    Snapshot of runtime data needed to build enhanced digest sections.

    Constructed once in main.py just before the email step and threaded
    through the digest builder functions.  All fields have safe defaults;
    only populate what is available — missing data causes graceful skips.
    """

    # ── Current portfolio state ──────────────────────────────────────────
    total_value: float = 0.0
    cash_available: float = 0.0
    max_drift: float = 0.0           # max absolute drift across holdings
    drawdown_pct: float = 0.0        # from 12-month rolling high
    drawdown_regime: str = "normal"
    monthly_contribution: float = 0.0
    expected_cagr: float = 0.09

    # ── Prior-run data (for "What Changed") ─────────────────────────────
    # Dict keys: total_value, cash, max_drift, run_date (str)
    prior_snapshot: Optional[Dict[str, Any]] = None
    prior_drawdown_regime: Optional[str] = None

    # ── Projections ──────────────────────────────────────────────────────
    # CompoundingDashboard object (has .to_dict()); None when growth mode off
    dashboard: Any = None

    # ── Recommendations ──────────────────────────────────────────────────
    portfolio_adjustments: List[Any] = field(default_factory=list)   # PortfolioAdjustment
    scored_recommendations: List[Any] = field(default_factory=list)  # FinanceRecommendation
    contribution_plan: List[Any] = field(default_factory=list)       # ContributionAllocation

    # ── Holdings ─────────────────────────────────────────────────────────
    holdings: List[Any] = field(default_factory=list)                # Holding
    holding_rationale: Dict[str, str] = field(default_factory=dict)  # symbol → rationale

    # ── Guardrails violations ────────────────────────────────────────────
    # Each item is a dict with keys: symbol, violation_type, current_pct, cap_pct
    guardrail_violations: List[Dict[str, Any]] = field(default_factory=list)

    # ── System status ─────────────────────────────────────────────────────
    fmp_circuit_breaker_open: bool = False
    fmp_disabled_until: Optional[str] = None
    scanner_enabled: bool = False
    watchlist_enabled: bool = False
    last_successful_weekly_days_ago: Optional[int] = None
    last_successful_monthly_days_ago: Optional[int] = None

    # ── Opportunity cost config ───────────────────────────────────────────
    idle_cash_threshold: float = 2000.0
    idle_cash_projection_years: int = 10

    # ── Theme engine highlights ───────────────────────────────────────────
    # Populated by main.py after theme engine + extended watchlist run.
    # Keys: themes_today, promoted, reinforced, expired, skipped,
    #        new_candidates, outcome_updates
    theme_highlights: Optional[Dict[str, Any]] = None


# ===========================================================================
# A.  Top 3 Actions
# ===========================================================================

def build_top3_actions(ctx: DigestContext) -> List[str]:
    """
    Return up to 3 concise, prioritised action strings.

    Priority:
      1. Structural guardrail violations (concentration / leverage cap)
      2. ACTION_REQUIRED portfolio adjustments
      3. Top contribution deployment (Growth Mode)
      4. RECOMMENDED adjustments
      5. Hold / do-nothing fallback
    """
    actions: List[str] = []

    # 1. Structural violations — always first
    for v in ctx.guardrail_violations:
        vtype = v.get("violation_type", v.get("type", ""))
        sym = v.get("symbol", "?")
        cap = v.get("cap_pct", 0)
        if vtype == "concentration":
            actions.append(f"TRIM {sym}: concentration cap breached — reduce to ≤{cap:.0%}")
        elif vtype == "leverage":
            actions.append(f"TRIM {sym}: leveraged exposure above {cap:.0%} cap — reduce first")
        if len(actions) >= 3:
            return actions

    # 2. ACTION_REQUIRED portfolio adjustments
    urgent = sorted(
        [a for a in ctx.portfolio_adjustments if a.action_level.value == "ACTION_REQUIRED"],
        key=lambda a: a.final_score,
        reverse=True,
    )
    for adj in urgent:
        mode = adj.adjustment_mode.value if adj.adjustment_mode else ""
        if mode == "USE_CASH_EXCESS":
            actions.append(
                f"BUY {adj.symbol} with cash: {_truncate(adj.do, 70)}"
            )
        elif mode == "CONTRIBUTE_ONLY":
            actions.append(f"Direct next contribution → {adj.symbol}: {adj.title}")
        else:
            actions.append(f"{adj.symbol}: {_truncate(adj.do, 80)}")
        if len(actions) >= 3:
            return actions

    # 3. Top contribution deployment
    if ctx.contribution_plan:
        top = max(ctx.contribution_plan, key=lambda c: c.recommended_dollars)
        if top.recommended_dollars > 0:
            actions.append(
                f"Deploy {format_currency(top.recommended_dollars)} → {top.symbol} "
                f"(monthly contribution — {top.reason})"
            )
        if len(actions) >= 3:
            return actions

    # 4. RECOMMENDED adjustments
    recommended = sorted(
        [a for a in ctx.portfolio_adjustments if a.action_level.value == "RECOMMENDED"],
        key=lambda a: a.final_score,
        reverse=True,
    )
    for adj in recommended[:2]:
        actions.append(f"Monitor {adj.symbol}: {adj.title}")
        if len(actions) >= 3:
            return actions

    # 5. Fallback — hold or generic
    if not actions:
        score = compute_do_nothing_score(ctx)
        if score >= 60:
            actions.append("No action needed this cycle — portfolio on track")
            actions.append("Staying invested is the preferred action")
        else:
            actions.append("No urgent actions — monitor drift at next cycle")

    return actions[:3]


# ===========================================================================
# C.  What Changed Since Last Run
# ===========================================================================

def build_what_changed(ctx: DigestContext) -> List[str]:
    """
    Return 3–6 bullets describing changes since the prior comparable run.
    Returns an empty list if no prior snapshot is available (degrades cleanly).
    """
    prior = ctx.prior_snapshot
    if not prior:
        return []

    bullets: List[str] = []
    run_date_str = prior.get("run_date", "last run")

    # Portfolio value delta
    prior_value = float(prior.get("total_value") or 0)
    if prior_value > 0 and ctx.total_value > 0:
        delta = ctx.total_value - prior_value
        pct = delta / prior_value * 100
        direction = "▲" if delta >= 0 else "▼"
        bullets.append(
            f"{direction} Portfolio: {format_currency(ctx.total_value)} "
            f"({delta:+,.0f}, {pct:+.1f}% vs {run_date_str})"
        )

    # Drift change
    prior_drift = float(prior.get("max_drift") or 0)
    if ctx.max_drift is not None and abs(ctx.max_drift - prior_drift) >= 0.01:
        improved = ctx.max_drift < prior_drift
        icon = "✓" if improved else "⚠"
        word = "improved" if improved else "worsened"
        bullets.append(
            f"{icon} Drift {word}: {prior_drift:.1%} → {ctx.max_drift:.1%}"
        )

    # Drawdown regime change
    if ctx.prior_drawdown_regime and ctx.prior_drawdown_regime != ctx.drawdown_regime:
        _labels = {
            "normal":           "Normal",
            "modest_dip":       "Modest Dip (>10%)",
            "significant_dip":  "Significant Dip (>20%)",
            "severe_dip":       "Severe Dip (>30%)",
        }
        old = _labels.get(ctx.prior_drawdown_regime, ctx.prior_drawdown_regime)
        new = _labels.get(ctx.drawdown_regime, ctx.drawdown_regime)
        icon = "📉" if "dip" in ctx.drawdown_regime else "📈"
        bullets.append(f"{icon} Drawdown regime: {old} → {new}")

    # Guardrail violations (new ones vs prior run)
    cur_vtypes = {
        f"{v.get('violation_type', v.get('type', '?'))}|{v.get('symbol', '')}"
        for v in ctx.guardrail_violations
    }
    if cur_vtypes:
        bullets.append(
            f"🚨 Active structural violations: {', '.join(sorted(cur_vtypes))}"
        )

    if not bullets:
        bullets.append(f"No significant changes since {run_date_str}")

    return bullets[:6]


# ===========================================================================
# D.  Trajectory / Long-Term Outlook
# ===========================================================================

def build_trajectory(ctx: DigestContext) -> Dict[str, str]:
    """
    Return a dict of projection labels → human-readable strings.

    Keys present when computable:
      cagr, value_5yr, value_10yr, value_10yr_no_contrib,
      extra_200_impact, milestone_100k, milestone_250k,
      milestone_500k, milestone_1m, assumption_note
    """
    result: Dict[str, str] = {}
    if ctx.total_value <= 0:
        return result

    cagr = ctx.expected_cagr or 0.09
    current = ctx.total_value
    monthly = ctx.monthly_contribution

    result["cagr"] = f"{cagr:.1%} (weighted by asset class, config-driven)"

    proj_5yr = project_future_value(current, monthly, cagr, 5)
    proj_10yr = project_future_value(current, monthly, cagr, 10)
    proj_10yr_no_c = project_future_value(current, 0, cagr, 10)
    proj_extra = project_future_value(current, monthly + 200, cagr, 10)
    extra_impact = proj_extra - proj_10yr

    result["value_5yr"] = format_currency(proj_5yr)
    result["value_10yr"] = format_currency(proj_10yr)
    result["value_10yr_no_contrib"] = format_currency(proj_10yr_no_c)
    result["extra_200_impact"] = format_currency(extra_impact)

    # Pull milestones from pre-computed dashboard if available
    if ctx.dashboard is not None:
        d = ctx.dashboard.to_dict()
        result["milestone_100k"] = d.get("YearsTo100k", "N/A")
        result["milestone_250k"] = d.get("YearsTo250k", "N/A")
        result["milestone_500k"] = d.get("YearsTo500k", "N/A")
        result["milestone_1m"] = d.get("YearsTo1m", "N/A")

    result["assumption_note"] = (
        f"Illustrative only. Assumes {cagr:.1%} CAGR and "
        f"{format_currency(monthly)}/mo fixed contribution. Not a guarantee."
    )
    return result


# ===========================================================================
# E.  Opportunity Cost of Idle Cash
# ===========================================================================

def build_opportunity_cost(ctx: DigestContext) -> Optional[str]:
    """
    Return a short insight string when idle cash materially exceeds threshold.
    Compares investing the excess vs leaving it at ~4% (HYSA rate).
    Returns None if cash is within normal operating range.
    """
    # Cash above 5% portfolio reserve is considered "idle"
    reserve_pct = 0.05
    idle = ctx.cash_available - (ctx.total_value * reserve_pct)
    if idle < ctx.idle_cash_threshold:
        return None

    cagr = ctx.expected_cagr or 0.09
    years = ctx.idle_cash_projection_years
    invested_value = project_future_value(idle, 0, cagr, years)
    idle_value = project_future_value(idle, 0, 0.04, years)
    opportunity = invested_value - idle_value

    return (
        f"Estimated {years}-year opportunity cost of {format_currency(idle)} idle cash: "
        f"~{format_currency(opportunity)} "
        f"(illustrative — {cagr:.0%} portfolio CAGR vs 4% idle rate; not guaranteed). "
        "Consider planned deployment via the contribution plan."
    )


# ===========================================================================
# F + G.  Behavior Guardrails & Do-Nothing Signal
# ===========================================================================

def compute_do_nothing_score(ctx: DigestContext) -> int:
    """
    Return a 0–100 score.  Higher = stronger signal to hold / do nothing.

    Scoring:
      No structural violations          → +30
      No ACTION_REQUIRED adjustments    → +20
      Drift < 8%                        → +20
      Regime is 'normal'                → +15
      Contribution plan exists          → +15
    """
    score = 0

    if not ctx.guardrail_violations:
        score += 30

    urgent = [a for a in ctx.portfolio_adjustments if a.action_level.value == "ACTION_REQUIRED"]
    if not urgent:
        score += 20

    if abs(ctx.max_drift) < 0.08:
        score += 20

    if ctx.drawdown_regime == "normal":
        score += 15

    if ctx.contribution_plan:
        score += 15

    return min(100, score)


def build_behavior_section(ctx: DigestContext) -> Dict[str, Any]:
    """
    Return a dict with the behavioral assessment and hold-discipline signal.

    Keys:
      do_nothing_score   int 0–100
      hold_signal        bool (True → no changes needed)
      messages           List[str] — 1–4 behavioural observations
    """
    score = compute_do_nothing_score(ctx)
    messages: List[str] = []

    # Top-level signal
    if score >= 80:
        messages.append("✓ No overtrading risk — no position changes are warranted this cycle")
        messages.append("✓ Staying invested is the optimal action at this stage")
    elif score >= 60:
        messages.append("✓ No urgent actions — minor drift is within normal operating range")
    else:
        messages.append("⚠ One or more items require attention — see recommendations above")

    # Anti-panic reminder during drawdowns
    if ctx.drawdown_regime in ("significant_dip", "severe_dip"):
        messages.append(
            "⚠ Drawdown in progress. Anti-panic mode: sell recommendations are suppressed. "
            "Contributions are being tilted toward equity — this is the plan working correctly."
        )

    # Contribution consistency
    if ctx.monthly_contribution > 0 and ctx.contribution_plan:
        messages.append(
            f"✓ Monthly contribution ({format_currency(ctx.monthly_contribution)}) active "
            "— consistent investing is the primary wealth lever at this stage"
        )

    # Underweight core positions building slowly
    underweight_cores = [
        a for a in ctx.portfolio_adjustments
        if getattr(a.adjustment_mode, "value", "") == "CONTRIBUTE_ONLY"
        and not getattr(a, "is_leveraged", False)
    ]
    if len(underweight_cores) >= 2:
        syms = ", ".join(a.symbol for a in underweight_cores[:3])
        messages.append(
            f"ℹ Core positions still building: {syms} — "
            "monthly contributions are the correct (and only needed) tool here"
        )

    return {
        "do_nothing_score": score,
        "hold_signal": score >= 60,
        "messages": messages,
    }


# ===========================================================================
# H.  Portfolio Logic / Position Rationale
# ===========================================================================

def build_holding_rationale(ctx: DigestContext) -> Dict[str, str]:
    """
    Return a symbol → rationale mapping for all current holdings.

    Lookup priority:
      1. ctx.holding_rationale (from config holding_rationale section)
      2. Built-in _DEFAULT_RATIONALE for common tickers
      3. Generic asset-class description
    """
    result: Dict[str, str] = {}
    for holding in ctx.holdings:
        sym = holding.symbol
        if sym in ctx.holding_rationale:
            result[sym] = ctx.holding_rationale[sym]
        elif sym in _DEFAULT_RATIONALE:
            result[sym] = _DEFAULT_RATIONALE[sym]
        else:
            asset_class = getattr(holding, "asset_class", "investment")
            result[sym] = f"{asset_class.replace('_', ' ')} holding"
    return result


# ===========================================================================
# I.  System Status
# ===========================================================================

def build_system_status(ctx: DigestContext) -> List[str]:
    """
    Return a list of short status strings for system health.
    Returns an empty list when all subsystems are healthy.
    """
    issues: List[str] = []

    # FMP circuit breaker
    if ctx.fmp_circuit_breaker_open:
        until = ctx.fmp_disabled_until or "unknown date"
        issues.append(
            f"⚠ S&P 500 scanner disabled (FMP circuit breaker open until {until})"
        )

    # Subsystems disabled via config
    if not ctx.scanner_enabled:
        issues.append("ℹ S&P 500 scanner off (scanner.enabled=false) — no candidate data this run")
    if not ctx.watchlist_enabled:
        issues.append(
            "ℹ Watchlist scanner off (watchlist_scanner.enabled=false) — no AV signals this run"
        )

    # Overdue scheduled runs
    if (
        ctx.last_successful_weekly_days_ago is not None
        and ctx.last_successful_weekly_days_ago > 8
    ):
        issues.append(
            f"⚠ Weekly digest overdue: last sent {ctx.last_successful_weekly_days_ago}d ago "
            "(check Task Scheduler)"
        )
    if (
        ctx.last_successful_monthly_days_ago is not None
        and ctx.last_successful_monthly_days_ago > 35
    ):
        issues.append(
            f"⚠ Monthly memo overdue: last sent {ctx.last_successful_monthly_days_ago}d ago"
        )

    return issues


# ===========================================================================
# J.  Theme Engine Highlights
# ===========================================================================

def build_theme_highlights(ctx: DigestContext) -> Optional[str]:
    """
    Build a concise Theme Engine Highlights block for the email digest.

    Returns a plain-text string (or None when no theme data is available).

    Sections (each tightly capped):
      1. Theme changes  — top themes with confidence delta vs yesterday (max 3)
      2. Candidates     — one line per ticker, priority order, no duplicates (max 4)
      3. Learning       — prior promoted symbols and how they turned out (max 2)
    """
    th = ctx.theme_highlights
    if not th:
        return None

    lines: List[str] = []

    # ── 1. Theme changes ─────────────────────────────────────────────────
    themes_today: List[Dict[str, Any]] = th.get("themes_today") or []
    themes_prior: List[Dict[str, Any]] = th.get("themes_prior") or []

    if themes_today:
        prior_conf: Dict[str, float] = {
            t.get("name", ""): float(t.get("confidence", 0))
            for t in themes_prior
        }
        theme_bullets: List[str] = []
        for t in sorted(themes_today, key=lambda x: x.get("confidence", 0), reverse=True)[:3]:
            name = t.get("name", "?")
            conf = float(t.get("confidence", 0))
            persist = int(t.get("persistence_7d", 0))

            prior = prior_conf.get(name)
            if prior is None:
                change_tag = " [new today]"
            elif conf >= prior + 0.08:
                change_tag = f" [↑ {prior:.0%}→{conf:.0%}]"
            elif conf <= prior - 0.08:
                change_tag = f" [↓ {prior:.0%}→{conf:.0%}]"
            else:
                change_tag = ""  # stable — omit noise

            persist_note = f", {persist}d streak" if persist >= 2 else ""
            theme_bullets.append(
                f"  • {name}: {conf:.0%} confidence{persist_note}{change_tag}"
            )

        if theme_bullets:
            lines.append("Theme changes:")
            lines.extend(theme_bullets)

    # ── 2. Candidate updates ─────────────────────────────────────────────
    # Each ticker appears exactly once. Priority: promoted → budget-deferred →
    # surfaced-needs-evidence → cap-blocked → reinforced → expired.
    promoted_syms: List[str] = th.get("promoted") or []
    reinforced_syms: List[str] = th.get("reinforced") or []
    expired_syms: List[str] = th.get("expired") or []
    skipped: List[Dict[str, Any]] = th.get("skipped") or []
    new_candidates: List[Dict[str, Any]] = th.get("new_candidates") or []
    budget_scanner_skipped: List[str] = th.get("budget_scanner_skipped") or []

    # Lookup: ticker → candidate metadata (suppress conf < 0.60)
    cand_info: Dict[str, Dict[str, Any]] = {
        c.get("ticker", "").upper(): c
        for c in new_candidates
        if c.get("ticker") and float(c.get("confidence", 0)) >= 0.60
    }

    # Lookup: ticker → skip reason (suppress in_static_watchlist — not user-facing)
    skip_data: Dict[str, Dict[str, Any]] = {
        s["symbol"]: s
        for s in skipped
        if s.get("symbol") and s.get("reason") != "in_static_watchlist"
    }

    cand_bullets: List[str] = []
    shown: set = set()

    # Priority 1 — Promoted to extended watchlist
    for sym in promoted_syms[:2]:
        meta = cand_info.get(sym, {})
        conf = float(meta.get("confidence", 0))
        conf_part = f" ({conf:.0%})" if conf else ""
        evidence = _format_evidence(meta.get("sources") or [], meta.get("themes") or [])
        cand_bullets.append(f"  • {sym} → extended watchlist{conf_part} — {evidence}")
        shown.add(sym)

    # Priority 2 — Budget-deferred (intentional system decision, not a failure)
    for sym in budget_scanner_skipped[:2]:
        if sym in shown:
            continue
        meta = cand_info.get(sym, {})
        conf = float(meta.get("confidence", 0))
        conf_part = f" ({conf:.0%})" if conf else ""
        cand_bullets.append(
            f"  • {sym}{conf_part} deferred — budget reserved for core watchlist"
        )
        shown.add(sym)

    # Priority 3 — Surfaced but not promoted (needs evidence or cap blocked)
    # Sorted by confidence descending; each ticker appears once.
    not_promoted = sorted(
        [
            (sym, data)
            for sym, data in skip_data.items()
            if sym not in shown and float(data.get("confidence", 0)) >= 0.60
        ],
        key=lambda x: -float(x[1].get("confidence", 0)),
    )
    for sym, sd in not_promoted:
        if len(cand_bullets) >= 4:
            break
        reason = sd.get("reason", "")
        conf = float(sd.get("confidence", 0))
        meta = cand_info.get(sym, {})
        evidence = _format_evidence(meta.get("sources") or [], meta.get("themes") or [])

        if reason == "insufficient_reinforcement":
            cand_bullets.append(
                f"  • {sym} surfaced ({conf:.0%}) — needs more evidence before promotion ({evidence})"
            )
        elif reason == "extended_watchlist_full":
            cand_bullets.append(
                f"  • {sym} surfaced ({conf:.0%}) — deferred, extended watchlist at capacity"
            )
        # below_confidence_threshold: suppress from user-facing output
        shown.add(sym)

    # Priority 4 — Reinforced (group into one line, only if room)
    if reinforced_syms and len(cand_bullets) < 4:
        cand_bullets.append(
            f"  • Watching: {', '.join(reinforced_syms[:3])} (extended — TTL refreshed)"
        )

    # Priority 5 — Expired (only if room)
    if expired_syms and len(cand_bullets) < 4:
        cand_bullets.append(
            f"  • Dropped (7d TTL, no reinforcement): {', '.join(expired_syms[:3])}"
        )

    if cand_bullets:
        if lines:
            lines.append("")
        lines.append("Candidates:")
        lines.extend(cand_bullets[:4])

    # ── 3. Learning / outcomes ────────────────────────────────────────────
    outcome_updates: List[Dict[str, Any]] = th.get("outcome_updates") or []
    outcome_bullets: List[str] = []

    for entry in outcome_updates[:3]:
        sym = entry.get("symbol", "?")
        outcome = entry.get("outcome", "none")
        days_promoted = int(entry.get("days_since_promoted", 0))
        days_label = f"{days_promoted}d ago" if days_promoted > 0 else "today"

        if outcome == "alerted":
            scan_count = entry.get("scan_count", 0)
            outcome_bullets.append(
                f"  • {sym} (promoted {days_label}): triggering scanner alerts"
                + (f" — {scan_count} scans" if scan_count > 1 else "")
            )
        elif outcome == "scanned":
            outcome_bullets.append(
                f"  • {sym} (promoted {days_label}): tracked — no alert threshold yet"
            )
        elif outcome == "expired":
            outcome_bullets.append(
                f"  • {sym} (promoted {days_label}): expired with no follow-through"
            )

    if outcome_bullets:
        if lines:
            lines.append("")
        lines.append("Learning:")
        lines.extend(outcome_bullets[:2])

    # ── Quiet fallback ────────────────────────────────────────────────────
    if not lines:
        if themes_today:
            n = len(themes_today)
            lines.append(
                f"  No material changes — {n} active theme{'s' if n != 1 else ''}, "
                "no candidates met promotion criteria"
            )
        else:
            return None  # Theme engine produced nothing useful

    return "\n".join(lines)


# ===========================================================================
# Internal helpers
# ===========================================================================

def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len characters, appending '…' if needed."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _format_evidence(sources: List[str], themes: List[str]) -> str:
    """Return a consistent evidence description for candidate promotion lines."""
    if "direct" in sources:
        return "evidence: direct article mention"
    if len(themes) >= 2:
        return f"evidence: {len(themes)} reinforcing themes"
    if len(themes) == 1:
        return f"evidence: {themes[0]} theme"
    return "evidence: theme signal"
