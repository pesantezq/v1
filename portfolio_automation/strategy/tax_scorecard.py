"""Tax-aware scorecard (spec §24, Tax-Aware strategy + §23.11).

Computes tax fields when broker cost-basis / tax-lot data is available; otherwise
emits an explicit degraded scorecard with placeholders (never a coarse guess).
Wash-sale risk is informational only. Advisory; trades nothing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from portfolio_automation.next_stage.contracts import observe_only_envelope


def has_tax_lot_data(positions: Any) -> bool:
    """True iff broker positions carry cost-basis/tax-lot fields we can use."""
    if not isinstance(positions, dict):
        return False
    for p in positions.get("positions", []) or []:
        if any(k in p for k in ("cost_basis", "tax_lots", "average_cost", "unrealized_gain")):
            return True
    return False


def _unrealized(p: Any) -> float | None:
    try:
        q, ac, mv = float(p["quantity"]), float(p["average_cost"]), float(p["market_value"])
        return round(mv - q * ac, 2)
    except (KeyError, TypeError, ValueError):
        pass
    # backward-compat: accept a pre-supplied unrealized_gain field
    try:
        v = p.get("unrealized_gain")
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _holding_period(acquired_date: str | None, now_iso: str) -> str | None:
    if not acquired_date:
        return None
    try:
        a = datetime.fromisoformat(str(acquired_date).replace("Z", "+00:00"))
        n = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
        if a.tzinfo is None:
            a = a.replace(tzinfo=timezone.utc)
        if n.tzinfo is None:
            n = n.replace(tzinfo=timezone.utc)
        return "long" if (n - a).days > 365 else "short"
    except Exception:
        return None


def build_tax_scorecard(now_iso: str, positions: Any, account_types: list[str] | None = None,
                        tax_lots: dict[str, list[dict]] | None = None) -> dict[str, Any]:
    payload = observe_only_envelope(now_iso, source="tax_scorecard",
                                    wash_sale_note="informational only")
    rows = positions.get("positions", []) if isinstance(positions, dict) else []
    if not rows or not has_tax_lot_data(positions):
        payload["degraded_mode"] = True
        payload["degraded_reason"] = "no cost-basis / tax-lot data (broker not configured or fields absent)"
        payload["scorecards"] = []
        payload["account_types_separated"] = bool(account_types)
        payload["degraded_fields"] = ["unrealized_gain_loss", "short_term_vs_long_term", "wash_sale_window"]
        payload["portfolio_unrealized_gain"] = None
        payload["placeholders"] = {
            "unrealized_gain_loss": None, "short_term_vs_long_term": None,
            "tlh_candidates": [], "wash_sale_risk": [],
        }
        return payload

    have_lots = bool(tax_lots)
    cards, total = [], 0.0
    for p in rows:
        sym = str(p.get("symbol", "")).upper()
        ug = _unrealized(p)
        if isinstance(ug, (int, float)):
            total += ug
        card = {"symbol": sym, "unrealized_gain": ug,
                "tlh_candidate": bool(isinstance(ug, (int, float)) and ug < 0),
                "wash_sale_risk_informational": False}
        if have_lots and tax_lots.get(sym):
            periods = {_holding_period(l.get("acquired_date"), now_iso) for l in tax_lots[sym]}
            card["holding_period"] = ("long" if periods == {"long"}
                                      else "short" if periods == {"short"} else "mixed")
        cards.append(card)
    payload["degraded_mode"] = False
    payload["scorecards"] = cards
    payload["portfolio_unrealized_gain"] = round(total, 2)
    payload["account_types_separated"] = bool(account_types)
    payload["degraded_fields"] = [] if have_lots else ["short_term_vs_long_term", "wash_sale_window"]
    return payload
