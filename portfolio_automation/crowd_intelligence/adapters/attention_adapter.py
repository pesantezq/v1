"""Attention adapter — market-attention context from gainers/losers/most-active +
sector/industry performance. Directional from gainer/loser membership; most-active
presence is attention (raises confidence), NOT direction. Explicitly attention,
not a recommendation. All inputs are SHARED (non per-symbol) lists fetched once."""
from __future__ import annotations

from portfolio_automation.crowd_intelligence.schemas import CategoryResult, NormalizedEvent
from portfolio_automation.crowd_intelligence.normalization import clamp

CATEGORY = "attention"
SHARED_IDS = ["biggest_gainers", "biggest_losers", "most_active",
              "sector_performance_snapshot", "industry_performance_snapshot"]
ENDPOINT_IDS = SHARED_IDS


def _index_by_symbol(rows) -> dict[str, dict]:
    out = {}
    for r in (rows or []):
        if isinstance(r, dict) and r.get("symbol"):
            out[str(r["symbol"]).upper()] = r
    return out


def run(symbol, *, client, usable, shared, now=None) -> CategoryResult:
    res = CategoryResult(category=CATEGORY)
    sym_u = symbol.upper()
    for eid in SHARED_IDS:
        (res.enabled_endpoints if eid in usable else res.disabled_endpoints).append(eid)

    gainers = _index_by_symbol(shared.get("biggest_gainers")) if "biggest_gainers" in usable else {}
    losers = _index_by_symbol(shared.get("biggest_losers")) if "biggest_losers" in usable else {}
    actives = _index_by_symbol(shared.get("most_active")) if "most_active" in usable else {}

    score = 0.0
    if sym_u in gainers:
        res.has_data = True
        chg = gainers[sym_u].get("changesPercentage")
        score += 0.6
        res.reasons.append(f"in biggest gainers ({chg}%)" if chg is not None else "in biggest gainers")
        res.events.append(NormalizedEvent("fmp", "biggest_gainers", sym_u, "attention", None, "gainer", gainers[sym_u]))
    if sym_u in losers:
        res.has_data = True
        chg = losers[sym_u].get("changesPercentage")
        score -= 0.6
        res.reasons.append(f"in biggest losers ({chg}%)" if chg is not None else "in biggest losers")
        res.events.append(NormalizedEvent("fmp", "biggest_losers", sym_u, "attention", None, "loser", losers[sym_u]))
    if sym_u in actives:
        res.has_data = True
        res.reasons.append("in most-active (elevated attention)")
        res.events.append(NormalizedEvent("fmp", "most_active", sym_u, "attention", None, "most_active", actives[sym_u]))

    # Sector/industry context (non-directional flavor; mild nudge by sector sign).
    sectors = shared.get("sector_performance_snapshot") if "sector_performance_snapshot" in usable else None
    if isinstance(sectors, list) and sectors:
        res.has_data = res.has_data or False  # context only; doesn't itself set has_data
        # not symbol-mappable here (needs profile sector); surfaced as market context.
        res.reasons.append("sector-performance snapshot available (market context)")

    res.score = clamp(score)
    res.freshness = 1.0 if res.has_data else 0.0
    return res
