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


_HOLDINGS_NO_DOLLAR_DATA = "__no_per_position_dollar_data__"


def _holdings_from_snapshot(snapshot: dict | None) -> list[dict[str, Any]]:
    """Normalised holdings projection: symbol, qty, price, value, alloc_pct, drift_pct.

    When the snapshot rows lack per-position dollar/drift fields (e.g. the
    allocation-advisor snapshot that stores conviction/allocation scores but
    no live price/value/drift), returns a single sentinel row so the template
    can render an honest empty-state note instead of a table of blank cells.
    """
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

    # D-H1 honest empty-state: if every row is missing dollar/drift data,
    # signal that to the template rather than showing a blank table.
    if out and all(
        r.get("value") is None
        and r.get("shares") is None
        and r.get("price") is None
        and r.get("drift_pct") is None
        for r in out
    ):
        return [{"_no_dollar_data": True}]

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
    # D-H1: suppress misleading $0 sector totals when snapshot has no dollar data.
    if holdings and holdings[0].get("_no_dollar_data"):
        return {"total_value": None, "by_sector_value": {}, "drift_warnings": []}

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


def _profit_attribution(repo_root: Path) -> dict[str, Any]:
    """Project outputs/policy/profit_attribution.json to what Portfolio renders.

    Defensive — the upstream artifact is rich; we only surface a small
    operator-friendly subset. Returns ``available=False`` when missing.
    """
    payload = _read_json(Path(repo_root) / "outputs" / "policy" / "profit_attribution.json")
    if not isinstance(payload, dict):
        return {"available": False}

    metrics = payload.get("metrics") or {}
    metrics_view: dict[str, Any] = {}
    if isinstance(metrics, dict):
        # Pull common keys; tolerate any subset
        for k in ("total_trades", "attributed_trades", "win_rate", "hit_rate",
                  "avg_return_pct", "rr", "risk_reward", "expectancy",
                  "missed_opportunities"):
            if k in metrics:
                metrics_view[k] = metrics[k]

    def _breakdown(rows: Any, max_rows: int = 8) -> list[dict[str, Any]]:
        """Normalise a dict-or-list breakdown into a list of {key, count, value}."""
        if isinstance(rows, dict):
            items = []
            for key, val in rows.items():
                if isinstance(val, dict):
                    items.append({"key": key, **val})
                else:
                    items.append({"key": key, "value": val})
            return items[:max_rows]
        if isinstance(rows, list):
            return [r for r in rows[:max_rows] if isinstance(r, dict)]
        return []

    missed = payload.get("missed_opportunities") or []
    missed_rows: list[dict[str, Any]] = []
    if isinstance(missed, list):
        for r in missed[:5]:
            if isinstance(r, dict):
                missed_rows.append({
                    "symbol": r.get("symbol") or r.get("ticker"),
                    "missed_return_pct": r.get("missed_return_pct") or r.get("return_pct"),
                    "reason": r.get("reason") or "",
                })

    return {
        "available": True,
        "generated_at": payload.get("generated_at"),
        "metrics": metrics_view,
        "by_strategy": _breakdown(payload.get("by_strategy")),
        "by_score_band": _breakdown(payload.get("by_score_band")),
        "by_regime": _breakdown(payload.get("by_regime")),
        "missed_opportunities": missed_rows,
        "total_opportunity_cost": payload.get("total_opportunity_cost"),
        "data_quality_notes": [
            n for n in (payload.get("data_quality_notes") or []) if n
        ][:5],
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
    base["profit_attribution"] = _profit_attribution(repo_root)
    return base
