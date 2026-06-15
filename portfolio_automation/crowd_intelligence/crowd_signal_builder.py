"""Crowd signal builder — runs the category adapters per symbol, composes the
crowd score + confidence, and persists raw events + daily signals. Observe-only:
produces context artifacts; never reads or mutates decision_plan / allocations.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from portfolio_automation.crowd_intelligence import endpoint_registry as reg
from portfolio_automation.crowd_intelligence.adapters import (
    fetch_endpoint, usable_endpoint_ids,
    news_adapter, analyst_adapter, insider_adapter, congress_adapter, attention_adapter,
)
from portfolio_automation.crowd_intelligence.schemas import CategoryResult, CrowdSignal
from portfolio_automation.crowd_intelligence import normalization as norm

_ADAPTERS = {
    "news": news_adapter, "analyst": analyst_adapter, "insider": insider_adapter,
    "congress": congress_adapter, "attention": attention_adapter,
}
# social_sentiment endpoints — always disabled (PLAN_LOCKED on Starter).
_SOCIAL_ENDPOINTS = ["historical_social_sentiment", "social_sentiment_legacy", "stock_news_sentiment_rss"]


def _is_per_symbol(eid: str) -> bool:
    e = reg.entry(eid) or {}
    return any(isinstance(v, str) and "{symbol}" in v for v in (e.get("params_template") or {}).values())


def build_signals(symbols: list[str], *, client: Any, capabilities: dict | None = None,
                  signal_date: str | None = None, now_iso: str | None = None
                  ) -> tuple[list[CrowdSignal], list[dict], dict]:
    capabilities = capabilities or {}
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    signal_date = signal_date or now_iso[:10]
    now_dt = datetime.now(timezone.utc).replace(tzinfo=None)

    # Per-category usable/disabled split (Phase-1 capability map).
    usable: dict[str, set] = {}
    disabled_by_cat: dict[str, list] = {}
    for cat, mod in _ADAPTERS.items():
        u, d = usable_endpoint_ids(capabilities, list(getattr(mod, "ENDPOINT_IDS", [])))
        usable[cat] = set(u)
        disabled_by_cat[cat] = d

    # Pre-fetch shared (non per-symbol) usable endpoints once.
    shared: dict[str, Any] = {}
    fmp_calls = 0
    for cat, mod in _ADAPTERS.items():
        for eid in usable[cat]:
            if not _is_per_symbol(eid) and eid not in shared:
                shared[eid] = fetch_endpoint(client, reg.entry(eid))
                fmp_calls += 1

    signals: list[CrowdSignal] = []
    all_events: list[dict] = []
    for symbol in symbols:
        results: dict[str, CategoryResult] = {}
        for cat, mod in _ADAPTERS.items():
            try:
                results[cat] = mod.run(symbol, client=client, usable=usable[cat],
                                       shared=shared, now=now_dt)
            except Exception as exc:  # one adapter must never sink the symbol
                r = CategoryResult(category=cat)
                r.warnings.append(f"{cat} adapter error: {type(exc).__name__}")
                results[cat] = r
        # social is always neutral/disabled (PLAN_LOCKED).
        social = CategoryResult(category="social_sentiment", disabled_endpoints=list(_SOCIAL_ENDPOINTS))
        results["social_sentiment"] = social

        cat_scores = {c: round(r.score, 4) for c, r in results.items()}
        composite = round(norm.composite(cat_scores), 4)

        with_data = [r for c, r in results.items() if c != "social_sentiment" and r.has_data]
        coverage = len(with_data) / 5.0
        completeness = len([r for r in with_data if r.events]) / 5.0
        fresh_vals = [r.freshness for r in with_data] or [0.0]
        freshness = mean(fresh_vals)
        agree = norm.agreement([r.score for r in with_data])
        conf = norm.confidence(coverage=coverage, freshness=freshness,
                               agree=agree, completeness=completeness)

        enabled_sources = sorted({e for r in results.values() for e in r.enabled_endpoints})
        disabled_sources = sorted({e for r in results.values() for e in r.disabled_endpoints})
        reasons = [f"{c}: {rs}" for c in ("attention", "analyst", "insider", "news", "congress")
                   for rs in results[c].reasons][:5]
        warnings = sorted({w for r in results.values() for w in r.warnings})
        records = [e for r in results.values() for e in r.events]

        signals.append(CrowdSignal(
            symbol=symbol.upper(), composite_crowd_score=composite, confidence=conf,
            category_scores=cat_scores, enabled_sources=enabled_sources,
            disabled_sources=disabled_sources, top_reasons=reasons, warnings=warnings,
            data_freshness=round(freshness, 4), source_records_count=len(records)))

        for ev in records:
            d = asdict(ev)
            d["fetched_at"] = now_iso
            all_events.append(d)

    enabled_categories = sorted({c for c in _ADAPTERS if usable.get(c)})
    disabled_categories = sorted(set(_ADAPTERS) - set(enabled_categories)) + ["social_sentiment"]
    status = {
        "observe_only": True, "source": "crowd_signal_builder",
        "generated_at": now_iso, "signal_date": signal_date,
        "overall_status": "ok" if signals and any(s.source_records_count for s in signals) else "degraded",
        "symbols_count": len(signals),
        "enabled_categories": enabled_categories,
        "disabled_categories": disabled_categories,
        "fmp_calls_estimate": fmp_calls + sum(1 for _ in symbols) * sum(
            1 for cat in _ADAPTERS for eid in usable[cat] if _is_per_symbol(eid)),
        "weights": norm.WEIGHTS,
        "warnings": (["capability map absent — endpoints assumed usable optimistically"]
                     if not capabilities.get("records") else []),
    }
    return signals, all_events, status
