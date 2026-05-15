"""Portfolio page — holdings, allocation, watchlist (read-only).

The stub (`collect_portfolio_stub`) is preserved for backward compatibility.

The full view (`collect_portfolio_view`) ports the read-only sections of
the Streamlit Watchlist Manager — symbol list + tags + enabled state +
recent watchlist signals — and pairs them with holdings detail and
allocation breakdown drawn from portfolio_snapshot.json. Write actions
(Add/Remove/Import) remain in Streamlit Watchlist Manager because
gui_v2 is strict read-only by design.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> Any | None:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def collect_portfolio_stub(repo_root: Path) -> dict[str, Any]:
    path = Path(repo_root) / "outputs" / "portfolio" / "portfolio_snapshot.json"
    if not path.exists():
        return {
            "advisory_only": True,
            "no_trade": True,
            "available": False,
            "total_value": None,
            "cash_available": None,
            "generated_at": None,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "advisory_only": True,
            "no_trade": True,
            "available": False,
            "error": f"parse_failed: {exc}",
        }
    return {
        "advisory_only": True,
        "no_trade": True,
        "available": True,
        "total_value": payload.get("total_value"),
        "cash_available": payload.get("cash_available"),
        "generated_at": payload.get("generated_at"),
    }


def _holdings_from_snapshot(snapshot: dict | None) -> list[dict[str, Any]]:
    """Normalised holdings projection: symbol, qty, price, value, alloc_pct, drift_pct."""
    if not isinstance(snapshot, dict):
        return []
    rows = snapshot.get("rows") or snapshot.get("holdings") or []
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append({
            "symbol": row.get("symbol") or row.get("ticker"),
            "shares": row.get("shares") or row.get("qty"),
            "price": row.get("price") or row.get("current_price"),
            "value": row.get("value") or row.get("current_value"),
            "alloc_pct": row.get("allocation_pct") or row.get("alloc_pct"),
            "target_alloc_pct": row.get("target_alloc_pct"),
            "drift_pct": row.get("drift_pct") or row.get("drift"),
            "sector": row.get("sector"),
        })
    return out


def _watchlist_with_tags(repo_root: Path) -> list[dict[str, Any]]:
    """Symbols from config.json + tag/enabled metadata from data/watchlist_tags.json."""
    cfg = _read_json(Path(repo_root) / "config.json") or {}
    ws_cfg = cfg.get("watchlist_scanner") or {}
    symbols = ws_cfg.get("watchlist")
    if not isinstance(symbols, list):
        return []
    tags_db = _read_json(Path(repo_root) / "data" / "watchlist_tags.json") or {}
    if not isinstance(tags_db, dict):
        tags_db = {}
    out: list[dict[str, Any]] = []
    for sym in symbols:
        if not isinstance(sym, str):
            continue
        meta = tags_db.get(sym) or {}
        if not isinstance(meta, dict):
            meta = {}
        out.append({
            "symbol": sym,
            "enabled": bool(meta.get("enabled", True)),
            "tags": [t for t in (meta.get("tags") or []) if isinstance(t, str)],
            "note": meta.get("note") or "",
        })
    return out


def _recent_signals(repo_root: Path, max_rows: int = 10) -> list[dict[str, Any]]:
    """Top N rows from outputs/latest/watchlist_signals.json by signal_score."""
    data = _read_json(Path(repo_root) / "outputs" / "latest" / "watchlist_signals.json")
    if not isinstance(data, dict):
        return []
    rows = data.get("results") or data.get("signals") or []
    if not isinstance(rows, list):
        return []
    def _key(r: dict) -> float:
        try:
            return float(r.get("signal_score") or r.get("final_rank_score") or 0)
        except Exception:
            return 0.0
    ranked = sorted((r for r in rows if isinstance(r, dict)), key=_key, reverse=True)
    out: list[dict[str, Any]] = []
    for r in ranked[:max_rows]:
        out.append({
            "symbol": r.get("symbol") or r.get("ticker"),
            "signal_score": r.get("signal_score"),
            "confidence": r.get("confidence_score") or r.get("confidence"),
            "alert": r.get("alert_level") or r.get("alert"),
            "sector": r.get("sector"),
        })
    return out


def _allocation_summary(holdings: list[dict[str, Any]]) -> dict[str, Any]:
    total_value = sum(float(h.get("value") or 0) for h in holdings)
    by_sector: dict[str, float] = {}
    over_target: list[dict[str, Any]] = []
    for h in holdings:
        sector = (h.get("sector") or "Unknown")
        by_sector[sector] = by_sector.get(sector, 0.0) + float(h.get("value") or 0)
        drift = h.get("drift_pct")
        if isinstance(drift, (int, float)) and abs(float(drift)) >= 0.10:
            over_target.append({
                "symbol": h.get("symbol"),
                "drift_pct": float(drift),
            })
    return {
        "total_value": total_value,
        "by_sector_value": by_sector,
        "drift_warnings": sorted(over_target, key=lambda r: abs(r["drift_pct"]), reverse=True),
    }


def collect_portfolio_view(repo_root: Path) -> dict[str, Any]:
    """Full Portfolio page data: stub + holdings + watchlist (read-only) + signals."""
    base = collect_portfolio_stub(repo_root)
    snapshot = _read_json(Path(repo_root) / "outputs" / "portfolio" / "portfolio_snapshot.json")
    holdings = _holdings_from_snapshot(snapshot)
    base["holdings"] = holdings
    base["allocation"] = _allocation_summary(holdings)
    base["watchlist"] = _watchlist_with_tags(repo_root)
    base["recent_signals"] = _recent_signals(repo_root)
    return base
