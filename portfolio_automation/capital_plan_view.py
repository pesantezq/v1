"""
portfolio_automation/capital_plan_view.py

Read-only "Today's Capital Plan" view model + daily-memo renderer.

Purpose
-------
The daily memo historically rendered two decision-plan-derived sections —
"Top Decisions" and "Capital Actions" — that were technically correct but not
decision-ready: they showed an undifferentiated priority number, an unexplained
``SELL: 1`` count with no detail, and a "Total recommended capital" figure that
summed *every* recommendation's intended sizing as though the operator should
spend it all today.

This module normalizes the funding split already computed by
``memo_coherence.compute_funding`` (plus the ``cash_deployment_plan`` monthly
envelope for cash/reserve/incoming, and ``decision_plan`` for sell detail and
the raw unconstrained recommendation total) into a single, decision-ready view
that both the plain-text and Markdown memo sections render from. One normalized
view model → one source of truth for the memo and its tests.

Invariants (enforced by construction + tests)
---------------------------------------------
OBSERVE-ONLY / READ-ONLY. This module NEVER mutates decisions, scores, action
enums, target allocations, approved capital, production state, simulation state,
or human-approval requirements. The machine-readable action enum
(``decision``) is left untouched; only the *memo-facing* label changes.

Honesty rules
-------------
* Money never silently becomes ``$0``. Every monetary field carries an explicit
  state: ``confirmed`` | ``missing`` | ``not_calculated`` | ``not_applicable`` |
  ``blocked`` — distinguishing a confirmed zero from missing/blocked/uncomputed.
* Gross recommended capital is never presented as a spend-today instruction.
* Estimated sale proceeds are never counted as deployable (the funding split
  already excludes them; this layer preserves that).
* If funded / deferred / gross totals do not reconcile, a visible warning is
  emitted and the raw values are preserved for audit.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config (conservative defaults; overridable via config/base.json:capital_plan)
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    # Max individually-listed deferred actions before the rest are summarized
    # by reason. Conservative default keeps the memo scannable.
    "max_deferred_displayed": 5,
    # Expand the raw "Technical details" line under each funded market opp.
    "expand_technical_details": True,
    # When True, the main "What To Do Today" section shows only funded actions
    # (deferred/watch items live in their own section).
    "main_section_funded_only": True,
}

# Reconciliation tolerance in dollars (share-price rounding slack).
_RECON_TOLERANCE = 1.0

# ---------------------------------------------------------------------------
# Monetary value helper — explicit state, never a silent zero
# ---------------------------------------------------------------------------

_STATE_CONFIRMED = "confirmed"
_STATE_MISSING = "missing"
_STATE_NOT_CALCULATED = "not_calculated"
_STATE_NOT_APPLICABLE = "not_applicable"
_STATE_BLOCKED = "blocked"


def _money(amount: Any, state: str = _STATE_CONFIRMED, note: str | None = None) -> dict[str, Any]:
    """A monetary value with an explicit availability state.

    ``amount`` may be ``None`` when the value is not a confirmed number; the
    ``state`` then explains why (missing / not_calculated / blocked / n/a).
    """
    val: float | None
    try:
        val = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        val = None
    if val is None and state == _STATE_CONFIRMED:
        state = _STATE_MISSING
    return {"amount": val, "state": state, "note": note}


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_money(value: Any, cents: bool = False) -> str:
    """Format a raw number as currency; ``—`` when not a number."""
    v = _num(value)
    if v is None:
        return "—"
    return f"${v:,.2f}" if cents else f"${v:,.0f}"


def _fmt_money_field(field: dict[str, Any], cents: bool = False) -> str:
    """Render a ``_money`` field, substituting an honest phrase for non-numbers."""
    state = field.get("state")
    amt = field.get("amount")
    if state == _STATE_CONFIRMED and amt is not None:
        return _fmt_money(amt, cents=cents)
    # Non-confirmed → explicit human phrase, never $0.
    phrases = {
        _STATE_MISSING: "unavailable",
        _STATE_NOT_CALCULATED: "not calculated",
        _STATE_NOT_APPLICABLE: "n/a",
        _STATE_BLOCKED: "blocked",
    }
    label = phrases.get(state, "unavailable")
    note = field.get("note")
    return f"{label} ({note})" if note else label


# ---------------------------------------------------------------------------
# Investor-facing action labels (memo-only; the machine enum is unchanged)
# ---------------------------------------------------------------------------

def investor_label(decision: str, *, funded: bool, tranche_type: str | None,
                   is_existing_holding: bool, partial: bool = False) -> str:
    """Translate a protected ``decision`` enum into investor-facing language.

    * SCALE → INCREASE (increase an existing position)
    * BUY   → FULL BUY when fully funded, STARTER BUY when only a starter tranche
              is funded, DEFER/WATCH when unfunded
    * SELL/TRIM → REDUCE (or SELL)
    * unfunded capital action → DEFER; informational-only → WATCH
    """
    d = (decision or "").upper()
    if d in ("SELL", "TRIM", "SELL_TO_REBALANCE", "TRIM_LEVERAGE_FIRST"):
        return "REDUCE"
    if not funded:
        # Unfunded rows are either deferred (had capital intent) or watch-only.
        return "WATCH" if d in ("WAIT", "AVOID", "WATCH", "HOLD") else "DEFER"
    if d == "SCALE":
        return "INCREASE"
    if d == "BUY":
        starter = partial or (tranche_type or "").startswith("starter")
        return "STARTER BUY" if starter else "FULL BUY"
    # Fallback keeps the raw enum visible rather than inventing a label.
    return d or "ACTION"


# ---------------------------------------------------------------------------
# Deterministic ranking category + human explanation (does NOT change scores)
# ---------------------------------------------------------------------------

# Lower rank_order sorts first. Categories mirror the operator-priority ladder:
# risk reduction → drift rebalance → cash-reserve → funded scale →
# high-confidence starter → lower-confidence/extended → informational watch.
_CATEGORY_ORDER: dict[str, int] = {
    "risk_reduction": 0,
    "portfolio_rebalance": 1,
    "cash_reserve": 2,
    "funded_increase": 3,
    "high_conf_starter": 4,
    "lower_conf_or_extended": 5,
    "watch": 6,
}

_CATEGORY_LABEL: dict[str, str] = {
    "risk_reduction": "Risk reduction",
    "portfolio_rebalance": "Portfolio rebalance",
    "cash_reserve": "Cash-reserve restoration",
    "funded_increase": "Increase existing position",
    "high_conf_starter": "New market opportunity",
    "lower_conf_or_extended": "New market opportunity (lower conviction)",
    "watch": "Informational watch",
}


def _classify_category(row: dict[str, Any]) -> str:
    """Assign an operator-priority category from existing fields only."""
    decision = (row.get("decision") or "").upper()
    source = (row.get("source") or "").lower()
    confidence = _num(row.get("confidence")) or 0.0
    extended = bool(row.get("entry_extended"))
    if decision in ("SELL", "TRIM", "SELL_TO_REBALANCE", "TRIM_LEVERAGE_FIRST"):
        # Rebalance-driven sells (drift/finance source) vs risk-reduction sells.
        if source in ("finance", "portfolio"):
            return "portfolio_rebalance"
        return "risk_reduction"
    if decision == "SCALE":
        return "funded_increase"
    if decision in ("WAIT", "AVOID", "WATCH", "HOLD"):
        return "watch"
    # BUY-class market/watchlist opportunities.
    if extended or confidence < 0.7:
        return "lower_conf_or_extended"
    return "high_conf_starter"


def _why_prioritized(category: str, row: dict[str, Any]) -> str:
    """Plain-language reason this action ranks where it does."""
    drift = _extract_drift_pct(row)
    if category == "portfolio_rebalance":
        base = ("Restoring the portfolio toward its target allocation takes "
                "priority over adding new positions.")
        if drift is not None:
            return (f"Allocation drift is {abs(drift):.0f}% beyond the rebalance "
                    f"threshold. {base}")
        return base
    if category == "risk_reduction":
        return ("A risk or concentration constraint requires reducing this "
                "position before new capital is deployed.")
    if category == "cash_reserve":
        return "Rebuilding the required cash reserve comes before new deployment."
    if category == "funded_increase":
        return ("Adding to an existing high-conviction holding ranks above new "
                "starter positions.")
    if category == "high_conf_starter":
        return ("A high-confidence new opportunity funded within today's "
                "deployment budget.")
    if category == "lower_conf_or_extended":
        return ("Lower conviction or an extended entry — funded only after "
                "higher-ranked actions.")
    return "Informational only; no capital is allocated today."


# ---------------------------------------------------------------------------
# Entry-setup translation (raw momentum / RS → plain language)
# ---------------------------------------------------------------------------

_RS_RE = re.compile(
    r"RS:\s*(near 52wk high|moderate|weak|strong)\s*\(?\s*([+-]?\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)


def _extract_distance_from_high(row: dict[str, Any]) -> tuple[str | None, float | None]:
    """Return (rs_bucket, distance_from_52wk_high_pct) parsed from the thesis.

    Distance is negative when below the high (e.g. -3.2). Returns (None, None)
    when the thesis carries no relative-strength phrase.
    """
    thesis = str(row.get("primary_thesis") or "")
    m = _RS_RE.search(thesis)
    if not m:
        return None, None
    bucket = m.group(1).lower()
    try:
        dist = float(m.group(2))
    except (TypeError, ValueError):
        dist = None
    return bucket, dist


def _extract_drift_pct(row: dict[str, Any]) -> float | None:
    """Pull a drift percentage from inputs_used or the reason/thesis text."""
    inputs = row.get("inputs_used") or {}
    d = _num(inputs.get("drift"))
    if d is not None:
        # inputs_used.drift may be a decimal fraction (0.15) or percent (15).
        return d * 100.0 if abs(d) <= 1.5 else d
    text = f"{row.get('reason') or ''} {row.get('primary_thesis') or ''}"
    m = re.search(r"[Dd]rift\s*([+-]?\d+(?:\.\d+)?)\s*%", text)
    if m:
        try:
            return float(m.group(1))
        except (TypeError, ValueError):
            return None
    return None


def entry_setup(row: dict[str, Any]) -> dict[str, Any]:
    """Convert raw momentum/RS into plain-language entry guidance.

    Returns ``{"guidance": str, "details": str|None, "available": bool}``.
    Never invents an interpretation unsupported by the data: when there is no
    relative-strength phrase the guidance reports the data is unavailable.
    """
    today_move = _num(row.get("entry_move_pct"))
    bucket, dist = _extract_distance_from_high(row)
    if bucket is None and today_move is None:
        return {"guidance": "Entry data unavailable.", "details": None, "available": False}

    near_high = bucket == "near 52wk high" or (dist is not None and dist >= -5.0)
    well_below = (dist is not None and dist <= -15.0) or bucket == "weak"
    up_today = today_move is not None and today_move > 0.25
    down_today = today_move is not None and today_move < -0.25

    if near_high and up_today:
        guidance = ("Strong trend, but entry risk is elevated. Use only a "
                    "starter position or wait for a pullback.")
    elif near_high and down_today:
        guidance = ("Long-term trend remains strong and today's weakness may "
                    "provide a better entry, but the stock is still close to "
                    "recent highs.")
    elif bucket == "moderate" and up_today:
        guidance = ("Trend is less established and today's move raises chase "
                    "risk. Defer unless supported by stronger fundamentals.")
    elif well_below:
        guidance = ("Potential recovery candidate, but relative strength is "
                    "weaker and requires fundamental confirmation.")
    elif near_high:
        guidance = "Close to its 52-week high; trend is intact."
    else:
        guidance = "Mixed setup; size conservatively."

    details = None
    parts: list[str] = []
    if today_move is not None:
        parts.append(f"Today: {today_move:+.2f}%")
    if dist is not None:
        parts.append(f"Distance from 52-week high: {dist:+.1f}%")
    if parts:
        details = " · ".join(parts)
    return {"guidance": guidance, "details": details, "available": True}


# ---------------------------------------------------------------------------
# Deferral reason → plain language
# ---------------------------------------------------------------------------

_DEFERRAL_PLAIN: dict[str, str] = {
    "DEFERRED_BY_WEEKLY_PACING": "held back by this week's deployment pace",
    "DEFERRED_BY_MONTHLY_BUDGET": "beyond this month's deployment budget",
    "BLOCKED_BY_CASH": "insufficient deployable capital",
    "BLOCKED_BY_CONCENTRATION": "an existing position/concentration constraint",
    "BLOCKED_BY_RISK": "a risk constraint",
    "beyond_deployment_budget": "beyond this month's deployment budget",
    "RESEARCH_ONLY": "informational only (no capital allocated)",
    "INSUFFICIENT_DATA": "missing price or sizing data",
}

# Coarse reason buckets for the summarized remainder.
_DEFERRAL_BUCKET: dict[str, str] = {
    "DEFERRED_BY_WEEKLY_PACING": "weekly deployment pace",
    "DEFERRED_BY_MONTHLY_BUDGET": "monthly deployment budget",
    "beyond_deployment_budget": "monthly deployment budget",
    "BLOCKED_BY_CASH": "insufficient deployable capital",
    "BLOCKED_BY_CONCENTRATION": "position/concentration constraint",
    "BLOCKED_BY_RISK": "risk constraint",
    "RESEARCH_ONLY": "informational only",
    "INSUFFICIENT_DATA": "missing execution details",
}


def _deferral_reason_plain(blocking_reason: str | None, symbol: str) -> str:
    key = (blocking_reason or "").strip()
    phrase = _DEFERRAL_PLAIN.get(key, "not funded within today's plan")
    return f"Deferred because it was {phrase}."


# ---------------------------------------------------------------------------
# Build the view model
# ---------------------------------------------------------------------------

def build_capital_plan_view(
    coherence: dict[str, Any] | None,
    cash_plan: dict[str, Any] | None,
    decision_plan: dict[str, Any] | None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the normalized, read-only Today's Capital Plan view model.

    All inputs are optional; missing inputs degrade to explicit ``missing`` /
    ``not_calculated`` states rather than fabricated zeros.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    coherence = coherence or {}
    cash_plan = cash_plan or {}
    decision_plan = decision_plan or {}

    funding = coherence.get("funding") or {}
    funding_available = bool(funding.get("available"))
    envelope = (cash_plan.get("monthly_capital_envelope")
                or funding.get("monthly_envelope") or {})
    cash_summary = cash_plan.get("cash_summary") or {}

    # Index every enriched action by symbol for thesis/entry joins.
    action_index: dict[str, dict[str, Any]] = {}
    for a in (coherence.get("actions") or []):
        sym = a.get("symbol")
        if sym and sym not in action_index:
            action_index[sym] = a

    # Raw decision-plan sizing (the unconstrained intended total) + sell rows.
    dp_rows = [r for r in (decision_plan.get("decisions") or []) if isinstance(r, dict)]
    raw_total = 0.0
    raw_count = 0
    for r in dp_rows:
        if (r.get("decision") or "").upper() in ("BUY", "SCALE"):
            amt = _num(r.get("recommended_amount"))
            if amt is not None:
                raw_total += amt
                raw_count += 1

    # ---- Capital summary (explicit states) --------------------------------
    if funding_available:
        cash_on_hand = _money(
            envelope.get("cash_on_hand")
            if envelope.get("cash_on_hand") is not None
            else (cash_summary.get("cash_available")
                  if cash_summary.get("cash_available") is not None
                  else funding.get("available_cash"))
        )
        required_reserve = _money(
            funding.get("cash_reserve_amount")
            if funding.get("cash_reserve_amount") is not None
            else envelope.get("cash_reserve_target_amount")
        )
        deployable_from_cash = _money(funding.get("deployable_from_cash"))
        deployable_from_incoming = _money(funding.get("deployable_from_incoming"))
        funded_capital = _money(funding.get("funded_capital"))
    else:
        cash_on_hand = _money(None, _STATE_MISSING, "funding data unavailable")
        required_reserve = _money(None, _STATE_MISSING, "funding data unavailable")
        deployable_from_cash = _money(None, _STATE_MISSING, "funding data unavailable")
        deployable_from_incoming = _money(None, _STATE_MISSING, "funding data unavailable")
        funded_capital = _money(None, _STATE_MISSING, "funding data unavailable")

    # Incoming contributions available for deployment.
    incoming_raw = (envelope.get("monthly_contribution_net_investable")
                    if envelope.get("monthly_contribution_net_investable") is not None
                    else envelope.get("monthly_contribution_gross"))
    if incoming_raw is None:
        if funding_available:
            incoming_contributions = _money(funding.get("deployable_from_incoming"))
        else:
            incoming_contributions = _money(None, _STATE_MISSING, "no contribution data")
    else:
        inc_val = _num(incoming_raw) or 0.0
        incoming_contributions = _money(
            inc_val,
            _STATE_CONFIRMED,
            None if inc_val > 0 else "no incoming contributions scheduled",
        )

    # Total deployable above reserve (cash + incoming), honestly summed.
    dfc = deployable_from_cash.get("amount")
    dfi = deployable_from_incoming.get("amount")
    if dfc is not None or dfi is not None:
        deployable_capital = _money((dfc or 0.0) + (dfi or 0.0))
        if (deployable_capital["amount"] or 0.0) <= 0.0:
            deployable_capital["note"] = "no deployable cash above reserve"
    else:
        deployable_capital = _money(None, _STATE_MISSING, "funding data unavailable")

    # ---- Funded actions ---------------------------------------------------
    funded_actions: list[dict[str, Any]] = []
    for fa in (funding.get("funded_actions") or []):
        sym = fa.get("symbol")
        enriched = action_index.get(sym, {})
        decision = enriched.get("decision") or fa.get("decision") or "BUY"
        tranche = fa.get("tranche_type")
        is_holding = bool(enriched.get("is_existing_holding"))
        held_back = _num(fa.get("held_for_pullback")) or 0.0
        partial = held_back > 0.0
        category = _classify_category({**enriched, "decision": decision})
        funded_actions.append({
            "symbol": sym,
            "decision": decision,
            "label": investor_label(decision, funded=True, tranche_type=tranche,
                                    is_existing_holding=is_holding, partial=partial),
            "category": category,
            "category_label": _CATEGORY_LABEL.get(category, category),
            "rank_order": _CATEGORY_ORDER.get(category, 9),
            "funded_capital": _money(fa.get("funded_capital")),
            "funding_source": _funding_source_label(fa.get("funding_source")),
            "funding_source_raw": fa.get("funding_source"),
            "priority": _num(fa.get("priority")),
            "why": _why_prioritized(category, {**enriched, "decision": decision}),
            "entry": entry_setup(enriched),
            "deployment_guidance": _deployment_guidance(decision, held_back),
            "risk": enriched.get("primary_risk"),
            "held_for_pullback": held_back,
            "pct_of_net_investable": _num(fa.get("pct_of_net_investable")),
        })
    # Deterministic order: category ladder, then priority desc, then symbol.
    funded_actions.sort(key=lambda a: (a["rank_order"], -(a["priority"] or 0.0),
                                       a["symbol"] or ""))
    for i, a in enumerate(funded_actions, 1):
        a["rank"] = i

    # ---- Deferred actions -------------------------------------------------
    deferred_raw = coherence.get("deferred_actions") or []
    deferred_actions: list[dict[str, Any]] = []
    deferred_sized_total = 0.0
    deferred_all_unsized = True
    for da in deferred_raw:
        sym = da.get("symbol")
        req = _num(da.get("requested_capital"))
        if req is not None and req > 0:
            deferred_all_unsized = False
            deferred_sized_total += req
        blocking = da.get("blocking_reason") or da.get("presentation_state")
        decision = da.get("decision") or "BUY"
        informational = (blocking in ("RESEARCH_ONLY",)) or decision in ("WAIT", "AVOID")
        deferred_actions.append({
            "symbol": sym,
            "decision": decision,
            "label": "WATCH" if informational else "DEFER",
            "intended_capital": (
                _money(req) if (req is not None and req > 0)
                else _money(None, _STATE_NOT_CALCULATED,
                            "deferred before sizing" if not informational
                            else "informational only")
            ),
            "blocking_reason": blocking,
            "reason_plain": _deferral_reason_plain(blocking, sym),
            "reason_bucket": _DEFERRAL_BUCKET.get((blocking or "").strip(),
                                                  "other"),
            "priority": _num(da.get("priority")),
            "entry": entry_setup(da),
            "would_fund_when": _would_fund_when(blocking),
        })

    # ---- Sell actions -----------------------------------------------------
    sell_actions: list[dict[str, Any]] = []
    for r in dp_rows:
        if (r.get("decision") or "").upper() not in ("SELL", "TRIM"):
            continue
        shares = _num((r.get("inputs_used") or {}).get("shares"))
        proceeds = _num(r.get("recommended_amount"))
        sell_actions.append({
            "symbol": _clean_sell_symbol(r.get("symbol")),
            "raw_symbol": r.get("symbol"),
            "decision": r.get("decision"),
            "label": "REDUCE",
            "shares": shares,
            "estimated_proceeds": _money(proceeds) if proceeds is not None
            else _money(None, _STATE_MISSING, "execution details unavailable"),
            "reason": _decision_reason_text(r.get("reason")),
            "proceeds_available": False,  # never counted until execution confirmed
            "dependent_funded_symbols": [],  # funding never depends on sale proceeds
            "detail_available": shares is not None and proceeds is not None,
        })

    # ---- Gross / deferred totals + reconciliation -------------------------
    # The system defers by weekly/monthly pace BEFORE sizing, so deferred
    # actions are typically unsized (requested_capital 0/None). We therefore
    # never fabricate a per-action deferred amount; the aggregate "capital
    # required for all recommendations" comes from the decision plan's raw
    # (unconstrained) sizing, clearly flagged as NOT a spend-today budget.
    funded_amt = funded_capital.get("amount")
    # "Unsized" only applies when deferred actions actually exist; an empty
    # deferred list is fully reconciled, not partial.
    deferred_unsized = deferred_all_unsized and bool(deferred_actions)
    unconstrained_total = (
        _money(raw_total, note="full recommendation list if fully sized — NOT a "
                               "spend-today budget")
        if raw_count > 0
        else _money(None, _STATE_MISSING, "no raw sizing in decision plan")
    )
    if deferred_unsized:
        deferred_capital = _money(
            None, _STATE_NOT_CALCULATED,
            "deferred by pacing/budget before sizing",
        )
        # Prefer the raw unconstrained total (matches the operator's mental
        # model of "capital required for all recommendations"); fall back to
        # the sized/funded portion only when no raw sizing exists.
        if raw_count > 0:
            gross_recommended_capital = _money(
                raw_total,
                note="all recommendations if fully sized — NOT a spend-today budget",
            )
        else:
            gross_recommended_capital = _money(
                funding.get("gross_recommended_sized"),
                note="sized/funded portion; deferred recommendations were not sized",
            )
    elif deferred_actions:
        deferred_capital = _money(deferred_sized_total)
        gross_recommended_capital = _money((funded_amt or 0.0) + deferred_sized_total)
    else:
        deferred_capital = _money(0.0, _STATE_CONFIRMED, "no deferred actions")
        gross_recommended_capital = _money(funded_amt if funded_amt is not None
                                           else funding.get("gross_recommended_sized"))

    reconciliation = _reconcile(funded_capital, deferred_capital,
                                gross_recommended_capital, funded_actions,
                                deferred_sized_total, deferred_unsized,
                                funding_available)

    # ---- Bottom line ------------------------------------------------------
    bottom_line = _bottom_line(funded_actions, funded_capital, deferred_actions,
                               unconstrained_total, deferred_all_unsized)

    return {
        "schema_version": "1",
        "source": "capital_plan_view",
        "observe_only": True,
        "no_trade": True,
        "generated_at": _now_iso(),
        "available": funding_available,
        "config": cfg,
        "capital_summary": {
            "cash_on_hand": cash_on_hand,
            "incoming_contributions": incoming_contributions,
            "required_reserve": required_reserve,
            "deployable_from_cash": deployable_from_cash,
            "deployable_from_incoming": deployable_from_incoming,
            "deployable_capital": deployable_capital,
            "gross_recommended_capital": gross_recommended_capital,
            "unconstrained_recommendation_total": unconstrained_total,
            "funded_capital": funded_capital,
            "deferred_capital": deferred_capital,
            "funded_count": len(funded_actions),
            "deferred_count": len(deferred_actions),
        },
        "funded_actions": funded_actions,
        "deferred_actions": deferred_actions,
        "sell_actions": sell_actions,
        "funding_warnings": reconciliation["warnings"],
        "reconciliation_status": reconciliation["status"],
        "reconciliation_detail": reconciliation["detail"],
        "weekly_pacing": envelope.get("weekly_pacing") or {},
        "bottom_line": bottom_line,
        "tie_break_rule": (coherence.get("ranking") or {}).get(
            "tie_break_rule",
            "category ladder → priority desc → symbol asc",
        ),
    }


def _deployment_guidance(decision: str, held_for_pullback: float) -> str:
    """Deployment instruction for the 'What To Do Today' section (how to fund),
    as distinct from the trend/entry-setup interpretation."""
    d = (decision or "").upper()
    if held_for_pullback and held_for_pullback > 0:
        return (f"Fund the starter amount now; {_fmt_money(held_for_pullback)} is "
                f"held back for a pullback.")
    if d == "SCALE":
        return "Add to the existing position now, or split into two tranches."
    return "Deploy the funded amount now, or split into two tranches."


def _funding_source_label(raw: str | None) -> str:
    return {
        "cash_on_hand": "Cash on hand",
        "incoming_contributions": "Incoming contributions",
    }.get((raw or "").strip(), raw or "—")


def _would_fund_when(blocking: str | None) -> str:
    key = (blocking or "").strip()
    return {
        "DEFERRED_BY_WEEKLY_PACING": "Becomes fundable next weekly tranche.",
        "DEFERRED_BY_MONTHLY_BUDGET": "Becomes fundable next month's budget cycle.",
        "beyond_deployment_budget": "Becomes fundable next month's budget cycle.",
        "BLOCKED_BY_CASH": "Becomes fundable when deployable capital increases.",
        "BLOCKED_BY_CONCENTRATION": "Becomes fundable when the concentration constraint clears.",
        "BLOCKED_BY_RISK": "Becomes fundable when the risk constraint clears.",
        "INSUFFICIENT_DATA": "Becomes fundable once price/sizing data is available.",
    }.get(key, "Re-evaluated on the next daily run.")


def _clean_sell_symbol(symbol: Any) -> str:
    """Turn a synthetic drift pseudo-symbol (drift_VFH_2026-07-20) into VFH."""
    s = str(symbol or "").strip()
    m = re.match(r"^drift_([A-Z]{1,6})_", s)
    if m:
        return m.group(1)
    return s


def _decision_reason_text(reason: Any) -> str:
    raw = str(reason or "").strip()
    if not raw:
        return "No reason provided."
    return raw.split("|", 1)[0].strip()


def _reconcile(funded, deferred, gross, funded_actions, deferred_sized_total,
               deferred_all_unsized, funding_available) -> dict[str, Any]:
    warnings: list[str] = []
    detail: dict[str, Any] = {}
    if not funding_available:
        return {"status": "degraded", "warnings": [
            "Funding data unavailable — capital plan totals could not be computed."
        ], "detail": {"reason": "funding_unavailable"}}

    f = funded.get("amount")
    d = deferred.get("amount")
    g = gross.get("amount")

    # Check 1: funded + deferred ≈ gross (only when deferred is a confirmed number).
    if f is not None and d is not None and g is not None:
        lhs = f + d
        detail["funded_plus_deferred"] = round(lhs, 2)
        detail["gross"] = round(g, 2)
        if abs(lhs - g) > _RECON_TOLERANCE:
            warnings.append(
                f"Totals do not reconcile: funded (${f:,.2f}) + deferred "
                f"(${d:,.2f}) = ${lhs:,.2f} ≠ gross ${g:,.2f}."
            )

    # Check 2: sum of funded action amounts ≈ funded_capital.
    fa_sum = sum((a["funded_capital"].get("amount") or 0.0) for a in funded_actions)
    detail["funded_actions_sum"] = round(fa_sum, 2)
    if f is not None and abs(fa_sum - f) > _RECON_TOLERANCE:
        warnings.append(
            f"Funded action amounts (${fa_sum:,.2f}) do not sum to funded "
            f"capital (${f:,.2f})."
        )

    # Check 3: sum of deferred amounts ≈ deferred_capital (when sized).
    if not deferred_all_unsized and d is not None:
        detail["deferred_actions_sum"] = round(deferred_sized_total, 2)
        if abs(deferred_sized_total - d) > _RECON_TOLERANCE:
            warnings.append(
                f"Deferred action amounts (${deferred_sized_total:,.2f}) do not "
                f"sum to deferred capital (${d:,.2f})."
            )

    if warnings:
        return {"status": "mismatch", "warnings": warnings, "detail": detail}
    if deferred_all_unsized:
        return {"status": "partial", "warnings": [], "detail": {
            **detail,
            "note": "Deferred capital not calculated (pacing/budget deferral "
                    "occurs before sizing).",
        }}
    return {"status": "ok", "warnings": [], "detail": detail}


def _bottom_line(funded_actions, funded_capital, deferred_actions,
                 unconstrained_total, deferred_all_unsized) -> str:
    f = funded_capital.get("amount")
    if f is None:
        return ("Capital plan is unavailable today — funding data could not be "
                "loaded. Review the raw artifacts before acting.")
    n_funded = len(funded_actions)
    n_deferred = len(deferred_actions)
    if n_funded == 0:
        base = (f"No capital is funded for deployment today "
                f"(${f:,.0f} available after pacing).")
    else:
        lead = funded_actions[0]
        lead_amt = lead["funded_capital"].get("amount") or 0.0
        base = (f"You have ${f:,.0f} funded to deploy today across {n_funded} "
                f"action{'s' if n_funded != 1 else ''}, led by "
                f"{lead['label']} {lead['symbol']} (${lead_amt:,.0f}).")
    if n_deferred:
        u = unconstrained_total.get("amount")
        if u is not None:
            base += (f" Defer the other {n_deferred} recommendation"
                     f"{'s' if n_deferred != 1 else ''}; the ${u:,.0f} "
                     f"unconstrained total is not an instruction to invest that "
                     f"amount today.")
        else:
            base += (f" The other {n_deferred} recommendation"
                     f"{'s' if n_deferred != 1 else ''} are deferred by this "
                     f"period's deployment pace.")
    return base


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Renderer — one function, both plain-text and Markdown
# ---------------------------------------------------------------------------

def render_capital_plan_md(view: dict[str, Any], *, markdown: bool = True,
                           rule: str | None = None) -> list[str]:
    """Render the six decision-ready sections as a list of memo lines.

    ``markdown=True`` emits Markdown headers/bullets. ``markdown=False`` emits
    the plain-text memo style; pass ``rule`` (e.g. ``"-" * 48``) to box the
    section headers the way the surrounding plain-text memo does.
    """
    cfg = {**DEFAULT_CONFIG, **(view.get("config") or {})}
    out: list[str] = []

    def h(title: str) -> None:
        if markdown:
            out.append(f"## {title}")
        elif rule:
            out.append(rule)
            out.append(f"  {title.upper()}")
            out.append(rule)
        else:
            out.append(title.upper())

    def line(text: str = "") -> None:
        out.append(text)

    def bullet(text: str) -> None:
        out.append(f"- {text}" if markdown else f"  {text}")

    if not view.get("available"):
        h("Today's Capital Plan")
        bullet("Capital plan unavailable — funding data could not be loaded.")
        line("")
        return out

    cs = view["capital_summary"]

    # 1. Today's Capital Plan
    h("Today's Capital Plan")
    bullet(f"Cash on hand: {_fmt_money_field(cs['cash_on_hand'])}")
    bullet(f"Incoming contributions: {_fmt_money_field(cs['incoming_contributions'])}")
    bullet(f"Required cash reserve: {_fmt_money_field(cs['required_reserve'])}")
    bullet(f"Deployable above reserve: {_fmt_money_field(cs['deployable_capital'])}")
    gross = cs["gross_recommended_capital"]
    gross_note = gross.get("note")
    gross_suffix = f" ({gross_note})" if (gross_note and gross.get("amount") is not None) else ""
    bullet(f"Capital required for all recommendations: "
           f"{_fmt_money_field(gross)}{gross_suffix}")
    bullet(f"Funded today: {_fmt_money_field(cs['funded_capital'])}")
    bullet(f"Deferred: {_fmt_money_field(cs['deferred_capital'])}")
    line("")
    bullet(f"{cs['funded_count']} action(s) funded · {cs['deferred_count']} action(s) deferred")
    # Show the raw unconstrained total only when it is not already the gross
    # figure (i.e. when deferred was sized and gross = funded + deferred).
    urt = cs["unconstrained_recommendation_total"]
    if (urt.get("amount") is not None
            and abs((urt["amount"] or 0.0) - (gross.get("amount") or -1.0)) > _RECON_TOLERANCE):
        bullet(f"Unconstrained recommendation total (not a spend-today budget): "
               f"{_fmt_money(urt['amount'])}")
    # Reconciliation warning surfaces here, visibly.
    for w in view.get("funding_warnings", []):
        bullet(f"⚠ Reconciliation warning: {w}")
    if view.get("reconciliation_status") == "partial":
        bullet("Note: deferred capital is not calculated — the system defers by "
               "weekly/monthly pace before sizing those recommendations.")
    line("")

    # 2. What To Do Today
    h("What To Do Today")
    funded = view.get("funded_actions", [])
    if not funded:
        bullet("No actions are funded for deployment today.")
        line("")
    else:
        for a in funded:
            amt = _fmt_money(a["funded_capital"].get("amount"))
            if markdown:
                line(f"**{a['rank']}. {a['label']} {a['symbol']} — {amt}**")
                bullet(f"Category: {a['category_label']}")
                bullet(f"Funding source: {a['funding_source']}")
                bullet(f"Why: {a['why']}")
                bullet(f"Entry guidance: {a['deployment_guidance']}")
                if a.get("risk"):
                    bullet(f"Risk: {a['risk']}")
                line("")
            else:
                line(f"  {a['rank']}. {a['label']} {a['symbol']} — {amt}")
                line(f"     Category: {a['category_label']} · Funding: {a['funding_source']}")
                line(f"     Why: {a['why']}")
                line(f"     Entry guidance: {a['deployment_guidance']}")
                if a.get("risk"):
                    line(f"     Risk: {a['risk']}")
                line("")

    # 3. Funded Market Opportunities (entry setups)
    market_funded = [a for a in funded
                     if a["category"] in ("high_conf_starter", "lower_conf_or_extended")
                     and a["entry"].get("available")]
    if market_funded:
        h("Funded Market Opportunities")
        for a in market_funded:
            if markdown:
                line(f"**{a['symbol']}**")
                bullet(f"Entry setup: {a['entry']['guidance']}")
                if cfg.get("expand_technical_details") and a["entry"].get("details"):
                    bullet(f"Technical details: {a['entry']['details']}")
                line("")
            else:
                line(f"  {a['symbol']}: {a['entry']['guidance']}")
                if cfg.get("expand_technical_details") and a["entry"].get("details"):
                    line(f"     Technical details: {a['entry']['details']}")
        line("")

    # 4. Deferred Recommendations (top N individually, rest summarized)
    deferred = view.get("deferred_actions", [])
    if deferred:
        h("Deferred Recommendations")
        limit = int(cfg.get("max_deferred_displayed", 5))
        show = deferred[:limit]
        rest = deferred[limit:]
        for a in show:
            cap = _fmt_money_field(a["intended_capital"])
            bullet(f"{a['symbol']} — {cap}: {a['reason_plain']} {a['would_fund_when']}")
        if rest:
            line("")
            buckets: dict[str, int] = {}
            for a in rest:
                buckets[a["reason_bucket"]] = buckets.get(a["reason_bucket"], 0) + 1
            bullet(f"{len(rest)} additional action(s) deferred:")
            for reason, n in sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0])):
                bullet(f"  • {n} due to {reason}")
        line("")

    # 5. Sell and Funding Dependencies
    sells = view.get("sell_actions", [])
    if sells:
        h("Sell and Funding Dependencies")
        for s in sells:
            if s["detail_available"]:
                proceeds = _fmt_money_field(s["estimated_proceeds"])
                shares = f"{s['shares']:g}" if s["shares"] is not None else "—"
                bullet(f"{s['label']} {s['symbol']} — {shares} shares")
                bullet(f"  Estimated proceeds: {proceeds}")
                bullet(f"  Reason: {s['reason']}")
                bullet("  Funding status: proceeds are NOT counted as deployable "
                       "until execution is confirmed.")
            else:
                bullet(f"{s['label']} {s['symbol']}: 1 sell action exists, but "
                       "execution details (shares, proceeds) were unavailable. "
                       "No projected proceeds were included in today's funded "
                       "capital.")
        line("")

    # 6. Bottom Line
    h("Bottom Line")
    bullet(view.get("bottom_line", ""))
    line("")
    return out


# ---------------------------------------------------------------------------
# Loader / runner (persists an audit artifact)
# ---------------------------------------------------------------------------

_LATEST_REL = ("outputs", "latest")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        import json
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def run_capital_plan_view(root: str = ".", *, write: bool = True,
                          coherence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load artifacts, build the view, optionally persist an audit JSON.

    Wrapped so a failure degrades to a ``{"available": False, ...}`` dict and
    never breaks the memo pipeline.
    """
    try:
        root_path = Path(root)
        latest = root_path.joinpath(*_LATEST_REL)
        if coherence is None:
            coherence = _load_json(latest / "memo_coherence.json")
        cash_plan = _load_json(latest / "cash_deployment_plan.json")
        decision_plan = _load_json(latest / "decision_plan.json")
        config = _load_json(root_path / "config" / "base.json").get("capital_plan")

        view = build_capital_plan_view(coherence, cash_plan, decision_plan, config)

        if write:
            try:
                from portfolio_automation.data_governance import (
                    OutputNamespace, safe_write_json,
                )
                safe_write_json(
                    OutputNamespace.LATEST, "daily_capital_plan.json", view,
                    base_dir=root_path / "outputs",
                )
            except Exception:
                # Auditing is best-effort; never block the memo on the write.
                pass
        return view
    except Exception as exc:  # pragma: no cover - defensive
        return {"available": False, "status": "error", "error": str(exc),
                "observe_only": True, "schema_version": "1",
                "source": "capital_plan_view"}
