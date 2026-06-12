# portfolio_automation/brokers/schwab_tax_lots.py
"""Defensive per-lot tax-data normalizer. Observe-only; no-trade; never raises.

Schwab's read-only positions payload MAY include per-lot acquisition data
(`taxLots`/`lots`). When present we normalize it; when absent we emit an explicit
no-lots marker so downstream tax math degrades honestly (never guesses lot dates).
"""
from __future__ import annotations

from typing import Any

_LOT_KEYS = ("taxLots", "tax_lots", "lots")


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_tax_lots(positions: Any, *, now_iso: str) -> dict[str, Any]:
    by_symbol: dict[str, list[dict]] = {}
    rows = positions.get("positions", []) if isinstance(positions, dict) else []
    for p in rows or []:
        if not isinstance(p, dict):
            continue
        sym = str(p.get("symbol", "")).upper()
        raw_lots = next((p[k] for k in _LOT_KEYS if isinstance(p.get(k), list)), None)
        if not sym or not raw_lots:
            continue
        lots = []
        for lot in raw_lots:
            if not isinstance(lot, dict):
                continue
            lots.append({
                "quantity": _f(lot.get("quantity") or lot.get("longQuantity")),
                "cost_basis": _f(lot.get("costBasis") or lot.get("cost_basis")),
                "acquired_date": (lot.get("acquiredDate") or lot.get("acquired_date") or None),
            })
        if lots:
            by_symbol[sym] = lots
    has_lots = bool(by_symbol)
    return {
        "generated_at": now_iso, "observe_only": True, "no_trade": True,
        "source": "schwab", "has_lots": has_lots, "by_symbol": by_symbol,
        "reason": ("per-lot acquisition data present"
                   if has_lots else "no per-lot data in broker positions (aggregate cost basis only)"),
    }
