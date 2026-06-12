"""Read-only Schwab holdings rows for the dashboard. Never raises; observe-only."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read(p: Path) -> dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def schwab_holdings(root: str | Path = ".") -> dict[str, Any]:
    pos = _read(Path(root) / "outputs" / "latest" / "schwab_positions.json")
    rows = []
    total_mv = 0.0
    total_ug = 0.0
    for p in (pos.get("positions") or []):
        sym = str(p.get("symbol", "")).upper()
        if not sym:
            continue
        q, ac, mv = _num(p.get("quantity")), _num(p.get("average_cost")), _num(p.get("market_value"))
        cb = round(q * ac, 2) if (q is not None and ac is not None) else None
        ug = round(mv - cb, 2) if (mv is not None and cb is not None) else None
        if mv is not None:
            total_mv += mv
        if ug is not None:
            total_ug += ug
        rows.append({"symbol": sym, "quantity": q, "market_value": mv,
                     "average_cost": ac, "cost_basis": cb, "unrealized_gain": ug})
    return {"available": bool(rows), "observe_only": True, "rows": rows,
            "totals": {"market_value": round(total_mv, 2), "unrealized_gain": round(total_ug, 2)},
            "source_timestamp": pos.get("generated_at")}
