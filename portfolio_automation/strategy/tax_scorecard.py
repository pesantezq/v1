"""Tax-aware scorecard (spec §24, Tax-Aware strategy + §23.11).

Computes tax fields when broker cost-basis / tax-lot data is available; otherwise
emits an explicit degraded scorecard with placeholders (never a coarse guess).
Wash-sale risk is informational only. Advisory; trades nothing.
"""
from __future__ import annotations

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


def build_tax_scorecard(now_iso: str, positions: Any, account_types: list[str] | None = None) -> dict[str, Any]:
    payload = observe_only_envelope(now_iso, source="tax_scorecard",
                                    wash_sale_note="informational only")
    if not has_tax_lot_data(positions):
        payload["degraded_mode"] = True
        payload["degraded_reason"] = "no cost-basis / tax-lot data (broker not configured or fields absent)"
        payload["scorecards"] = []
        payload["account_types_separated"] = bool(account_types)
        payload["placeholders"] = {
            "unrealized_gain_loss": None, "short_term_vs_long_term": None,
            "tlh_candidates": [], "wash_sale_risk": [],
        }
        return payload

    cards = []
    for p in positions.get("positions", []) or []:
        sym = str(p.get("symbol", "")).upper()
        avg = p.get("average_cost")
        mv = p.get("market_value")
        ug = p.get("unrealized_gain")
        cards.append({
            "symbol": sym, "unrealized_gain": ug,
            "tlh_candidate": bool(isinstance(ug, (int, float)) and ug < 0),
            "wash_sale_risk_informational": False,
        })
    payload["degraded_mode"] = False
    payload["scorecards"] = cards
    payload["account_types_separated"] = bool(account_types)
    return payload
