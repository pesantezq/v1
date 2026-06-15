"""
Multi-source Crowd Radar aggregator (observe-only, sandbox-only).

Merges normalized per-ticker records from the active crowd sources into a single
view with cross-source derived metrics. Probe-only / blocked / not-entitled
sources contribute NOTHING to scores — only sources that returned usable records
(SourceStatus.OK/DEGRADED with records) participate.

Handles, by construction: zero active sources, one active source, partial source
failure, and all-degraded. Confidence is capped when source breadth is low; when
only ApeWisdom is active the conclusions are labelled ``mention_velocity_only``
and ``low_source_breadth``.

Crowd signals adjust sandbox research priority ONLY — they can never trigger a
trade or mutate any official artifact.
"""
from __future__ import annotations

from typing import Any

from portfolio_automation.social_intelligence.base import SourceStatus
from portfolio_automation.social_sources.base import SourceResult

# Sources that, alone, provide mention velocity but no sentiment.
_MENTION_ONLY_SOURCES = {"apewisdom"}

# Confidence ceilings by number of contributing sources.
_CONFIDENCE_CAP_BY_BREADTH = {0: 0.0, 1: 0.35, 2: 0.6, 3: 0.8}


def _cap_for_breadth(breadth: int) -> float:
    return _CONFIDENCE_CAP_BY_BREADTH.get(breadth, 0.9)


def _safe_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def aggregate_crowd_sources(
    normalized: list[SourceResult],
    *,
    context: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Aggregate normalized source results into per-ticker crowd records.

    ``context`` (optional) maps TICKER -> {"news_attention": float,
    "price_velocity": float} for the retail-vs-news / retail-vs-price ratios. All
    context is best-effort; missing context yields None metrics, never an error.

    Returns a dict: {active_sources, contributing_sources, source_breadth_max,
    labels, records[]}.
    """
    context = context or {}
    contributing = [r for r in normalized if r.status in (SourceStatus.OK, SourceStatus.DEGRADED) and r.records]
    contributing_names = sorted({r.source_name for r in contributing})

    # Merge records by ticker across contributing sources.
    by_ticker: dict[str, dict[str, Any]] = {}
    for res in contributing:
        for rec in res.records:
            tk = str(rec.get("ticker") or "").upper()
            if not tk:
                continue
            slot = by_ticker.setdefault(tk, {"ticker": tk, "per_source": {}})
            slot["per_source"][res.source_name] = rec

    records: list[dict[str, Any]] = []
    for tk, slot in by_ticker.items():
        per_source = slot["per_source"]
        active = sorted(per_source.keys())
        breadth = len(active)

        # mention velocity: prefer explicit ratio, else delta sign.
        velocities = [_safe_float(r.get("mention_velocity_ratio")) for r in per_source.values()]
        velocities = [v for v in velocities if v is not None]
        mention_velocity = round(max(velocities), 4) if velocities else None

        # sentiment: only from sources that actually carry it (FMP/Finnhub if entitled).
        sentiments = [_safe_float(r.get("sentiment_score")) for r in per_source.values()]
        sentiments = [s for s in sentiments if s is not None]
        sentiment_score = round(sum(sentiments) / len(sentiments), 4) if sentiments else None

        # source agreement: do the sources agree on rising vs falling mentions?
        directions = []
        for r in per_source.values():
            delta = _safe_float(r.get("mention_delta_24h"))
            if delta is not None:
                directions.append(1 if delta > 0 else (-1 if delta < 0 else 0))
        if len(directions) >= 2:
            up = sum(1 for d in directions if d > 0)
            down = sum(1 for d in directions if d < 0)
            agreement = round(max(up, down) / len(directions), 3)
            disagreement = round(1.0 - agreement, 3)
        else:
            agreement = None
            disagreement = None

        # context-relative attention ratios (best-effort).
        ctx = context.get(tk, {})
        news_attention = _safe_float(ctx.get("news_attention"))
        price_velocity = _safe_float(ctx.get("price_velocity"))
        retail_vs_news = (round(mention_velocity / news_attention, 3)
                          if (mention_velocity and news_attention) else None)
        retail_vs_price = (round(mention_velocity / price_velocity, 3)
                           if (mention_velocity and price_velocity) else None)

        # crowd_early_or_late: high crowd velocity with LOW news/price attention =
        # early (crowd ahead of news); high velocity WITH high news/price = late.
        early_or_late = None
        if mention_velocity is not None and (retail_vs_news is not None or retail_vs_price is not None):
            ref = max(x for x in (retail_vs_news, retail_vs_price) if x is not None)
            early_or_late = round(min(1.0, max(-1.0, (ref - 1.0))), 3)  # >0 early, <0 late

        # hype_risk: fast mentions + high upvote/mention concentration + low breadth.
        upm = [_safe_float(r.get("upvote_per_mention")) for r in per_source.values()]
        upm = [u for u in upm if u is not None]
        hype = 0.0
        if mention_velocity and mention_velocity > 1.5:
            hype += min(0.5, (mention_velocity - 1.5) / 4.0)
        if upm and max(upm) > 5:
            hype += 0.25
        if breadth <= 1:
            hype += 0.25
        hype_risk_score = round(min(1.0, hype), 3)

        # confidence: base on velocity strength, then HARD-cap by breadth.
        base_conf = 0.5 if mention_velocity else 0.2
        if sentiment_score is not None:
            base_conf += 0.15
        confidence = round(min(base_conf, _cap_for_breadth(breadth)), 3)

        labels = []
        if breadth <= 1:
            labels.append("low_source_breadth")
        if active and set(active).issubset(_MENTION_ONLY_SOURCES) and sentiment_score is None:
            labels.append("mention_velocity_only")

        records.append({
            "ticker": tk,
            "active_sources": active,
            "source_breadth": breadth,
            "mention_velocity": mention_velocity,
            "sentiment_score_if_available": sentiment_score,
            "sentiment_velocity_if_available": None,  # needs sentiment history (no entitled source yet)
            "source_agreement": agreement,
            "source_disagreement": disagreement,
            "retail_attention_vs_news_attention": retail_vs_news,
            "retail_attention_vs_price_velocity": retail_vs_price,
            "crowd_early_or_late_score": early_or_late,
            "hype_risk_score": hype_risk_score,
            "confidence": confidence,
            "labels": labels,
            "recommended_next_step": "monitor",  # research verb only — never a trade verb
        })

    records.sort(key=lambda x: (x["mention_velocity"] is None, -(x["mention_velocity"] or 0)))

    agg_labels = []
    if not contributing_names:
        agg_labels.append("no_active_sources")
    elif set(contributing_names).issubset(_MENTION_ONLY_SOURCES):
        agg_labels.extend(["mention_velocity_only", "low_source_breadth"])

    return {
        "contributing_sources": contributing_names,
        "source_breadth_max": max((r["source_breadth"] for r in records), default=0),
        "labels": agg_labels,
        "record_count": len(records),
        "records": records,
    }
