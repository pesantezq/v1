"""Insider adapter — net buy/sell pressure → [-1, 1]. Winsorized so one filing
cannot dominate."""
from __future__ import annotations

from portfolio_automation.crowd_intelligence import endpoint_registry as reg
from portfolio_automation.crowd_intelligence.adapters import fetch_endpoint
from portfolio_automation.crowd_intelligence.schemas import CategoryResult, NormalizedEvent
from portfolio_automation.crowd_intelligence.normalization import clamp, winsorize

CATEGORY = "insider"
ENDPOINT_IDS = ["latest_insider_trading", "search_insider_trades", "insider_trade_statistics"]


def _is_buy(row: dict) -> bool | None:
    v = str(row.get("acquisitionOrDisposition") or row.get("transactionType") or "").upper()
    if v.startswith("A"):
        return True
    if v.startswith("D") or "SALE" in v or "SELL" in v:
        return False
    return None


def run(symbol, *, client, usable, shared, now=None) -> CategoryResult:
    res = CategoryResult(category=CATEGORY)
    sym_u = symbol.upper()
    for eid in ENDPOINT_IDS:
        (res.enabled_endpoints if eid in usable else res.disabled_endpoints).append(eid)

    buys: list[float] = []
    sells: list[float] = []

    if "search_insider_trades" in usable:
        rows = fetch_endpoint(client, reg.entry("search_insider_trades"), symbol=symbol)
        for r in (rows or []):
            if not isinstance(r, dict):
                continue
            b = _is_buy(r)
            notional = abs(float(r.get("securitiesTransacted") or r.get("price") or 1) or 1)
            if b is True:
                buys.append(notional)
            elif b is False:
                sells.append(notional)
            res.events.append(NormalizedEvent("fmp", "search_insider_trades", sym_u, "insider",
                                              r.get("filingDate") or r.get("transactionDate"),
                                              "insider_trade", {k: r.get(k) for k in
                                              ("acquisitionOrDisposition", "filingDate", "securitiesTransacted")}))

    if "insider_trade_statistics" in usable and not (buys or sells):
        rows = fetch_endpoint(client, reg.entry("insider_trade_statistics"), symbol=symbol)
        row = rows[0] if isinstance(rows, list) and rows else (rows if isinstance(rows, dict) else None)
        if isinstance(row, dict):
            ratio = row.get("buySellRatio") or row.get("acquiredDisposedRatio")
            if isinstance(ratio, (int, float)) and ratio >= 0:
                # ratio>1 buy-heavy, <1 sell-heavy → map to [-1,1]
                res.has_data = True
                res.score = clamp((float(ratio) - 1.0) / (float(ratio) + 1.0))
                res.reasons.append(f"insider buy/sell ratio {float(ratio):.2f}")
                res.freshness = 1.0
                return res

    if buys or sells:
        res.has_data = True
        wb = winsorize(buys) if buys else []
        ws = winsorize(sells) if sells else []
        bsum, ssum = sum(wb), sum(ws)
        denom = bsum + ssum
        res.score = clamp((bsum - ssum) / denom) if denom else 0.0
        res.reasons.append(f"{len(buys)} insider buys / {len(sells)} sells (net {res.score:+.2f})")
        res.freshness = 1.0
    return res
