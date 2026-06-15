"""Congress adapter — disclosed congressional trade activity. LOW WEIGHT, dampened
and clamped to ±0.5. Context only — explanations avoid any causal / privileged-insight
implication."""
from __future__ import annotations

from portfolio_automation.crowd_intelligence import endpoint_registry as reg
from portfolio_automation.crowd_intelligence.adapters import fetch_endpoint
from portfolio_automation.crowd_intelligence.schemas import CategoryResult, NormalizedEvent
from portfolio_automation.crowd_intelligence.normalization import clamp

CATEGORY = "congress"
ENDPOINT_IDS = ["senate_trading", "house_trading"]  # per-symbol; *_by_name are member-keyed (not per-symbol)
_DAMPEN = 0.5
_CAP = 0.5


def _direction(row: dict) -> int:
    t = str(row.get("type") or row.get("transaction") or row.get("transactionType") or "").lower()
    if "purchase" in t or "buy" in t:
        return 1
    if "sale" in t or "sell" in t:
        return -1
    return 0


def run(symbol, *, client, usable, shared, now=None) -> CategoryResult:
    res = CategoryResult(category=CATEGORY)
    sym_u = symbol.upper()
    buys = sells = 0
    for eid in ENDPOINT_IDS:
        if eid not in usable:
            res.disabled_endpoints.append(eid)
            continue
        res.enabled_endpoints.append(eid)
        rows = fetch_endpoint(client, reg.entry(eid), symbol=symbol)
        for r in (rows or []):
            if not isinstance(r, dict):
                continue
            d = _direction(r)
            if d > 0:
                buys += 1
            elif d < 0:
                sells += 1
            res.events.append(NormalizedEvent("fmp", eid, sym_u, "congress",
                                              r.get("transactionDate") or r.get("dateRecieved"),
                                              "congress_trade",
                                              {k: r.get(k) for k in ("type", "amount", "office")}))
    n = buys + sells
    if n:
        res.has_data = True
        # dampened + capped: low-weight, context-only.
        res.score = clamp(_DAMPEN * (buys - sells) / n, -_CAP, _CAP)
        res.reasons.append(
            f"{n} disclosed congressional trade(s) ({buys} purchase / {sells} sale) — "
            f"public disclosure, context only")
        res.freshness = 1.0
    return res
