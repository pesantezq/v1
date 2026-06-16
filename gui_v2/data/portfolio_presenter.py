"""Portfolio view-model presenter (display-layer only, observe-only).

Composes advisory picks + crowd overlay + summary cards + reasoning strip from
EXISTING artifact-derived data. Pure functions; no artifact generation, no scoring,
no decision/allocation logic. Crowd input is ALWAYS subordinate context — it never
changes a pick's action and never appears as the sole reason.
"""
from __future__ import annotations

from typing import Any

# Action → directional lean (for crowd-agreement classification only; does NOT
# change the action). Kept conservative.
_BULLISH = {"BUY", "SCALE", "SCALE_IN", "ACCUMULATE", "ADD"}
_BEARISH = {"SELL", "SCALE_OUT", "TRIM", "REDUCE", "AVOID", "EXIT"}


def confidence_pct(raw: Any) -> int:
    try:
        v = float(raw or 0)
    except (TypeError, ValueError):
        return 0
    if v <= 1.0:
        v *= 100
    return max(0, min(100, int(round(v))))


def conviction_band(pct: int) -> str:
    return "High" if pct >= 70 else "Medium" if pct >= 40 else "Low"


def _direction(action: str) -> str:
    a = (action or "").upper()
    if a in _BULLISH:
        return "bullish"
    if a in _BEARISH:
        return "bearish"
    return "neutral"


def _crowd_agreement(action: str, crowd: dict | None) -> str:
    """Agree / Disagree / Inconclusive — crowd label vs pick direction. Context only."""
    if not crowd or not crowd.get("present"):
        return "Inconclusive"
    label = (crowd.get("label") or "").lower()
    direction = _direction(action)
    if direction == "neutral" or label in ("neutral", "insufficient data", "high attention"):
        return "Inconclusive"
    supportive = label == "supportive"
    cautious = label == "caution"
    if direction == "bullish":
        return "Agree" if supportive else "Disagree" if cautious else "Inconclusive"
    # bearish
    return "Agree" if cautious else "Disagree" if supportive else "Inconclusive"


def build_summary_cards(*, portfolio_value: float | None, cash_summary: dict,
                        risk_delta: dict, holdings: list[dict]) -> list[dict]:
    pv = portfolio_value if portfolio_value is not None else cash_summary.get("portfolio_value")
    cash = cash_summary.get("cash_available")
    cash_pct = cash_summary.get("current_cash_pct")
    target_pct = cash_summary.get("target_cash_pct")
    below_floor = cash_summary.get("below_safety_floor")

    conc = (risk_delta.get("concentration") or {}) if risk_delta else {}
    top = conc.get("top_position") or {}
    top_sym, top_w = top.get("symbol"), top.get("weight")
    n_positions = len(conc.get("positions") or []) or len(holdings)
    sectors = sorted({h.get("sector") for h in holdings if h.get("sector")})

    cards = [
        {"key": "value", "label": "Portfolio Value", "severity": "blue",
         "value": f"${pv:,.0f}" if pv is not None else "—",
         "sub": "day P/L unavailable (no intraday feed)"},
        {"key": "cash", "label": "Cash Balance", "severity": "amber" if below_floor else "blue",
         "value": f"${cash:,.0f}" if cash is not None else "—",
         "sub": (f"{cash_pct*100:.1f}% of portfolio · target {target_pct*100:.0f}%"
                 + (" · below floor" if below_floor else "")) if cash_pct is not None else "cash % unavailable"},
        {"key": "drift", "label": "Portfolio Drift",
         "severity": "amber" if (isinstance(top_w, (int, float)) and top_w >= 0.5) else "green",
         "value": (f"{top_sym} {top_w*100:.0f}%" if top_sym and isinstance(top_w, (int, float)) else "—"),
         "sub": "top-position concentration vs structural cap"},
        {"key": "diversification", "label": "Diversification", "severity": "blue",
         "value": f"{n_positions} holdings",
         "sub": f"{len(sectors)} sector(s)" + (f": {', '.join(sectors[:3])}" if sectors else "")},
    ]
    return cards


def build_advisory_picks(decisions: list[dict], crowd_by_symbol: dict[str, dict],
                         holdings_by_symbol: dict[str, dict]) -> list[dict]:
    picks = []
    for d in decisions or []:
        sym = (d.get("ticker") or "").upper()
        action = (d.get("action") or "").upper()
        crowd = crowd_by_symbol.get(sym)
        conf = confidence_pct(d.get("confidence"))

        # Row 1 — Portfolio (drift if held, else candidate). Always the primary reason.
        held = holdings_by_symbol.get(sym)
        if held:
            w = held.get("normalized_allocation_pct")
            portfolio_row = (f"Held — target weight {w:.1f}%" if isinstance(w, (int, float))
                             else "Currently held")
        else:
            portfolio_row = "Candidate — not currently held; sized by drift/quant rules"

        # Row 2 — Crowd (subordinate context; honest fallback).
        if crowd and crowd.get("present"):
            note = (crowd.get("top_reasons") or crowd.get("lines") or [""])[0]
            crowd_row = f"{crowd.get('label')} · {note}" if note else crowd.get("label")
            crowd_agree = _crowd_agreement(action, crowd)
        else:
            crowd_row = ((crowd or {}).get("lines") or ["Crowd context unavailable — insufficient governed source coverage"])[0]
            crowd_agree = "Inconclusive"

        # Row 3 — Catalyst / Risk (from rationale/urgency + crowd risk warnings; honest).
        risk_bits = []
        if d.get("urgency"):
            risk_bits.append(f"urgency {d['urgency']}")
        if crowd and crowd.get("warnings"):
            risk_bits.append(crowd["warnings"][0])
        catalyst_row = " · ".join(risk_bits) if risk_bits else "No specific catalyst/risk flagged"

        pick = {
            "ticker": sym, "action": action, "confidence_pct": conf,
            "conviction": conviction_band(conf),
            "thesis": d.get("rationale") or "Advisory pick from decision_plan",
            "portfolio_row": portfolio_row,
            "crowd_row": crowd_row,
            "crowd_present": bool(crowd and crowd.get("present")),
            "crowd_severity": (crowd or {}).get("severity", "gray"),
            "crowd_agreement": crowd_agree,
            "catalyst_row": catalyst_row,
            "signal_strength": conf,  # 0-100 bar
            "crowd_disagrees": crowd_agree == "Disagree",
        }
        # Additive display-only: surface joined unified-crowd context when present.
        # Does NOT touch action / confidence / scoring fields.
        unified = (crowd or {}).get("unified") if isinstance(crowd, dict) else None
        if isinstance(unified, dict):
            pick["unified_crowd_state"] = unified.get("crowd_state")
            pick["unified_retail_attention"] = unified.get("retail_attention_score")
            pick["unified_fmp_context"] = unified.get("fmp_attention_score")
            pick["unified_confirmation"] = unified.get("cross_source_confirmation_score")
            pick["unified_divergence"] = unified.get("cross_source_divergence_score")
            pick["unified_explanation"] = unified.get("explanation")
        picks.append(pick)
    return picks


def build_crowd_overlay(picks: list[dict], crowd_status: dict) -> dict:
    shown = picks or []
    covered = [p for p in shown if p["crowd_present"]]
    coverage_pct = round(100 * len(covered) / len(shown), 0) if shown else 0
    agree = sum(1 for p in covered if p["crowd_agreement"] == "Agree")
    disagree = sum(1 for p in covered if p["crowd_agreement"] == "Disagree")
    inconclusive = len(shown) - agree - disagree
    enabled = crowd_status.get("enabled_categories") or []
    return {
        "available": bool(crowd_status.get("available")),
        "active_sources": len(enabled),
        "active_source_names": enabled,
        "coverage_pct": int(coverage_pct),
        "agree": agree, "disagree": disagree, "inconclusive": inconclusive,
        "social_disabled": bool(crowd_status.get("social_disabled")),
        "generated_at": crowd_status.get("generated_at"),
        "legend": [
            {"band": "High", "range": "70–100", "desc": "many sources aligned; strong conviction", "severity": "green"},
            {"band": "Medium", "range": "40–69", "desc": "mixed signals; moderate conviction", "severity": "amber"},
            {"band": "Low", "range": "0–39", "desc": "little agreement; weak conviction", "severity": "gray"},
        ],
    }


def build_why_these_picks(*, risk_delta: dict, cash_summary: dict, crowd_overlay: dict,
                          n_picks: int) -> list[dict]:
    conc = (risk_delta.get("concentration") or {}) if risk_delta else {}
    top = conc.get("top_position") or {}
    return [
        {"title": "Portfolio Drift & Gaps", "severity": "blue",
         "body": (f"Top position {top.get('symbol')} at {top.get('weight', 0)*100:.0f}% of cap; "
                  f"cash {cash_summary.get('current_cash_pct', 0)*100:.1f}% vs target "
                  f"{cash_summary.get('target_cash_pct', 0)*100:.0f}%. Under/overweights drive sizing.")},
        {"title": "Technical & Quant Confirmation", "severity": "blue",
         "body": "Momentum, trend, valuation and risk validation from the decision engine "
                 "rank and gate every pick before display."},
        {"title": "Crowd Context (Support Only)", "severity": "blue",
         "body": (f"{crowd_overlay['active_sources']} governed crowd source-categories; "
                  f"{crowd_overlay['coverage_pct']}% of picks covered. Pulse/velocity/breadth "
                  "are supporting context — never the sole reason.")},
        {"title": "Risk & Governance Checks", "severity": "blue",
         "body": "Liquidity, volatility, correlation and structural-cap/policy checks apply "
                 "to every advisory pick. Crowd input is non-binding."},
    ]


def build_view_model(*, decisions: list[dict], crowd_by_symbol: dict[str, dict],
                     crowd_status: dict, holdings: list[dict], risk_delta: dict,
                     cash_summary: dict, portfolio_value: float | None) -> dict:
    holdings_by_symbol = {(h.get("symbol") or "").upper(): h for h in (holdings or [])}
    picks = build_advisory_picks(decisions, crowd_by_symbol, holdings_by_symbol)
    overlay = build_crowd_overlay(picks, crowd_status)
    return {
        "summary_cards": build_summary_cards(
            portfolio_value=portfolio_value, cash_summary=cash_summary,
            risk_delta=risk_delta, holdings=holdings),
        "advisory_picks": picks,
        "advisory_count": len(picks),
        "crowd_overlay": overlay,
        "why_these_picks": build_why_these_picks(
            risk_delta=risk_delta, cash_summary=cash_summary,
            crowd_overlay=overlay, n_picks=len(picks)),
    }
