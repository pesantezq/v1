"""News adapter — velocity + attention context. Directional score is NEUTRAL
(no sentiment field on Starter; RSS-sentiment is PLAN_LOCKED). Risk-event keywords
become warnings only — never buy/sell signals."""
from __future__ import annotations

import datetime as _dt

from portfolio_automation.crowd_intelligence import endpoint_registry as reg
from portfolio_automation.crowd_intelligence.adapters import fetch_endpoint
from portfolio_automation.crowd_intelligence.schemas import CategoryResult, NormalizedEvent
from portfolio_automation.crowd_intelligence.normalization import clamp01

CATEGORY = "news"
PER_SYMBOL_IDS = ["stock_news_search"]
SHARED_IDS = ["stock_news_latest", "fmp_articles", "general_news", "crypto_news", "forex_news"]
ENDPOINT_IDS = PER_SYMBOL_IDS + SHARED_IDS

_RISK_KEYWORDS = ("bankruptcy", "chapter 11", "sec investigation", "fraud", "halted",
                  "recall", "lawsuit", "delist", "default", "subpoena", "probe", "going concern")


def _parse_dt(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(str(s)[:19], fmt)
        except Exception:
            pass
    return None


def run(symbol, *, client, usable, shared, now=None) -> CategoryResult:
    now = now or _dt.datetime.utcnow()
    res = CategoryResult(category=CATEGORY)
    sym_u = symbol.upper()
    articles: list[dict] = []

    if "stock_news_search" in usable:
        res.enabled_endpoints.append("stock_news_search")
        rows = fetch_endpoint(client, reg.entry("stock_news_search"), symbol=symbol)
        if isinstance(rows, list):
            articles += [r for r in rows if isinstance(r, dict)]
    else:
        res.disabled_endpoints.append("stock_news_search")

    for sid in SHARED_IDS:
        if sid not in usable:
            res.disabled_endpoints.append(sid)
            continue
        res.enabled_endpoints.append(sid)
        pool = shared.get(sid) or []
        if isinstance(pool, list):
            for r in pool:
                if isinstance(r, dict):
                    blob = f"{r.get('title','')} {r.get('text','')} {r.get('symbol','')}".upper()
                    if sym_u in blob:
                        articles.append(r)

    cnt_24h = cnt_7d = 0
    latest = None
    for a in articles:
        d = _parse_dt(a.get("publishedDate") or a.get("date"))
        if d:
            age = (now - d).total_seconds()
            if age <= 86400:
                cnt_24h += 1
            if age <= 7 * 86400:
                cnt_7d += 1
            latest = d if (latest is None or d > latest) else latest

    total = len(articles)
    res.has_data = total > 0
    baseline = max(1.0, cnt_7d / 7.0)
    velocity = round(cnt_24h / baseline, 2) if baseline else 0.0
    res.score = 0.0  # neutral by design — news contributes attention/velocity, not direction
    if total:
        res.reasons.append(f"{total} articles ({cnt_24h} in 24h / {cnt_7d} in 7d); velocity {velocity}")

    flags = sorted({kw for a in articles[:40] for kw in _RISK_KEYWORDS
                    if kw in str(a.get("title", "")).lower()})
    if flags:
        res.warnings.append("news risk-event keywords (context only): " + ", ".join(flags))

    if latest:
        res.freshness = clamp01(1.0 - (now - latest).total_seconds() / 86400 / 7.0)
    for a in articles[:20]:
        res.events.append(NormalizedEvent(
            "fmp", "news", sym_u, "news", a.get("publishedDate") or a.get("date"),
            "headline", {"title": a.get("title"), "site": a.get("site") or a.get("publisher")}))
    return res
