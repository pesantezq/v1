"""Analyst adapter — consensus distribution + recent grade direction → [-1, 1]."""
from __future__ import annotations

from portfolio_automation.crowd_intelligence import endpoint_registry as reg
from portfolio_automation.crowd_intelligence.adapters import fetch_endpoint
from portfolio_automation.crowd_intelligence.schemas import CategoryResult, NormalizedEvent
from portfolio_automation.crowd_intelligence.normalization import clamp

CATEGORY = "analyst"
ENDPOINT_IDS = ["ratings_snapshot", "ratings_historical", "stock_grades", "grades_consensus"]


def _consensus_score(row: dict) -> tuple[float | None, int]:
    if not isinstance(row, dict):
        return None, 0
    def g(*keys):
        for k in keys:
            v = row.get(k)
            if isinstance(v, (int, float)):
                return float(v)
        return 0.0
    sb = g("strongBuy", "analystRatingsStrongBuy")
    b = g("buy", "analystRatingsbuy", "analystRatingsBuy")
    h = g("hold", "analystRatingsHold")
    s = g("sell", "analystRatingsSell")
    ss = g("strongSell", "analystRatingsStrongSell")
    total = sb + b + h + s + ss
    if total <= 0:
        return None, 0
    return clamp((sb + b - s - ss) / total), int(total)


def run(symbol, *, client, usable, shared, now=None) -> CategoryResult:
    res = CategoryResult(category=CATEGORY)
    sym_u = symbol.upper()
    cons_score = None
    direction = 0.0

    for eid in ENDPOINT_IDS:
        (res.enabled_endpoints if eid in usable else res.disabled_endpoints).append(eid)

    if "grades_consensus" in usable:
        rows = fetch_endpoint(client, reg.entry("grades_consensus"), symbol=symbol)
        row = rows[0] if isinstance(rows, list) and rows else (rows if isinstance(rows, dict) else None)
        sc, total = _consensus_score(row or {})
        if sc is not None:
            cons_score = sc
            res.has_data = True
            res.reasons.append(f"analyst consensus {sc:+.2f} ({total} ratings)")
            res.events.append(NormalizedEvent("fmp", "grades_consensus", sym_u, "analyst",
                                              None, "consensus", row or {}))

    if "stock_grades" in usable:
        rows = fetch_endpoint(client, reg.entry("stock_grades"), symbol=symbol)
        recent = [r for r in (rows or []) if isinstance(r, dict)][:5]
        ups = sum(1 for r in recent if str(r.get("action", "")).lower() in ("upgrade", "up"))
        downs = sum(1 for r in recent if str(r.get("action", "")).lower() in ("downgrade", "down"))
        if recent:
            res.has_data = True
            direction = clamp((ups - downs) / max(1, ups + downs)) if (ups + downs) else 0.0
            if ups or downs:
                res.reasons.append(f"recent grade actions: {ups} up / {downs} down")
            for r in recent:
                res.events.append(NormalizedEvent("fmp", "stock_grades", sym_u, "analyst",
                                                  r.get("date"), "grade_action", r))

    if "ratings_snapshot" in usable:
        rows = fetch_endpoint(client, reg.entry("ratings_snapshot"), symbol=symbol)
        row = rows[0] if isinstance(rows, list) and rows else (rows if isinstance(rows, dict) else None)
        if isinstance(row, dict) and row.get("rating"):
            res.has_data = True
            res.reasons.append(f"rating {row.get('rating')}")

    # Combine: consensus is the backbone; recent direction nudges it.
    if cons_score is not None:
        res.score = clamp(0.8 * cons_score + 0.2 * direction)
    else:
        res.score = clamp(0.5 * direction)
    res.freshness = 1.0 if res.has_data else 0.0
    return res
