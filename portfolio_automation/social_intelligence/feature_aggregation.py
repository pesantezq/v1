"""
Aggregate raw posts + ticker detections into per-ticker feature vectors.

Pure, deterministic, no network. Consumes :class:`RawPost` objects and the
ticker detections from :mod:`ticker_extractor`, and produces the feature dict
that :mod:`crowd_state_classifier` maps into a crowd-knowledge state.

All "market context" features (price move before social spike, volume
confirmation, options/short-interest context, external news match) are passed in
by the orchestrator as an optional per-ticker context dict — this module never
calls FMP itself, keeping it cheap and unit-testable.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from portfolio_automation.social_intelligence.base import RawPost
from portfolio_automation.social_intelligence.ticker_extractor import (
    TickerDetection,
    extract_from_text,
)

# Heuristic lexicons (deterministic; no AI). Kept small + auditable.
_DD_MARKERS = (
    "dd", "due diligence", "valuation", "dcf", "balance sheet", "earnings",
    "revenue", "guidance", "margin", "cash flow", "10-k", "10-q", "filing",
    "catalyst", "thesis", "fundamentals", "p/e", "ev/ebitda",
)
_MEME_MARKERS = (
    "moon", "rocket", "🚀", "yolo", "tendies", "diamond hands", "💎", "🙌",
    "to the moon", "lambo", "fomo", "squeeze", "apes", "hodl", "stonk",
    "can't go tits up", "printing", "free money",
)
_POSITIVE_MARKERS = ("buy", "calls", "long", "bull", "undervalued", "up", "moon", "green")
_NEGATIVE_MARKERS = ("sell", "puts", "short", "bear", "overvalued", "down", "crash", "red")


@dataclass
class TickerFeatures:
    """Per-ticker aggregated features fed to the classifier."""

    ticker: str
    mention_count: int = 0
    mention_velocity_zscore: float = 0.0
    unique_author_count: int = 0
    author_concentration: float = 0.0        # 0..1 (1 = single author dominates)
    flair_counts: dict[str, int] = field(default_factory=dict)
    dd_density: float = 0.0                   # 0..1 fraction of posts with DD markers
    evidence_score: float = 0.0               # 0..1 blended DD + external support
    sentiment_score: float = 0.0              # -1..1
    sentiment_dispersion: float = 0.0         # 0..1 (1 = highly fragmented)
    meme_language_score: float = 0.0          # 0..1
    avg_detection_confidence: float = 0.0     # 0..1
    # Market-context features (supplied by orchestrator; default neutral/unknown).
    external_news_match: bool = False
    price_move_before_social_spike: float | None = None  # pct, signed
    volume_confirmation: bool | None = None
    options_or_short_interest_context: float | None = None  # z-ish or None
    historical_outcome_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "mention_count": self.mention_count,
            "mention_velocity_zscore": round(self.mention_velocity_zscore, 4),
            "unique_author_count": self.unique_author_count,
            "author_concentration": round(self.author_concentration, 4),
            "flair_counts": self.flair_counts,
            "dd_density": round(self.dd_density, 4),
            "evidence_score": round(self.evidence_score, 4),
            "sentiment_score": round(self.sentiment_score, 4),
            "sentiment_dispersion": round(self.sentiment_dispersion, 4),
            "meme_language_score": round(self.meme_language_score, 4),
            "avg_detection_confidence": round(self.avg_detection_confidence, 4),
            "external_news_match": self.external_news_match,
            "price_move_before_social_spike": self.price_move_before_social_spike,
            "volume_confirmation": self.volume_confirmation,
            "options_or_short_interest_context": self.options_or_short_interest_context,
            "historical_outcome_score": self.historical_outcome_score,
        }


def _marker_hits(text: str, markers: tuple[str, ...]) -> int:
    low = text.lower()
    return sum(1 for m in markers if m in low)


def _post_sentiment(text: str) -> float:
    pos = _marker_hits(text, _POSITIVE_MARKERS)
    neg = _marker_hits(text, _NEGATIVE_MARKERS)
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total


def mention_velocity_zscore(today_count: int, history: list[int]) -> float:
    """
    Z-score of *today_count* against a baseline *history* of prior daily counts.

    Returns 0.0 when there is insufficient history (< 2 points) or zero variance,
    so a quiet ticker never looks like a spike on thin data.
    """
    if len(history) < 2:
        return 0.0
    mean = sum(history) / len(history)
    var = sum((h - mean) ** 2 for h in history) / len(history)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (today_count - mean) / std


def aggregate_ticker_features(
    posts: list[RawPost],
    *,
    known_universe: set[str] | frozenset[str] | None = None,
    company_names: dict[str, str] | None = None,
    mention_history: dict[str, list[int]] | None = None,
    market_context: dict[str, dict[str, Any]] | None = None,
    min_detection_confidence: float = 0.5,
) -> list[TickerFeatures]:
    """
    Aggregate *posts* into per-ticker feature vectors.

    Detections below *min_detection_confidence* are ignored for state purposes
    (they are still counted toward raw mentions for transparency, but they do not
    inflate evidence). ``mention_history`` maps ticker → prior daily counts for the
    velocity z-score. ``market_context`` maps ticker → context features.
    """
    mention_history = mention_history or {}
    market_context = market_context or {}

    # ticker -> accumulator
    acc: dict[str, dict[str, Any]] = {}

    for post in posts:
        detections = extract_from_text(
            post.text(),
            known_universe=known_universe,
            company_names=company_names,
        )
        text = post.text()
        is_dd = _marker_hits(text, _DD_MARKERS) > 0
        is_meme = _marker_hits(text, _MEME_MARKERS) > 0
        sentiment = _post_sentiment(text)

        seen_this_post: set[str] = set()
        for det in detections:
            if det.confidence < min_detection_confidence:
                continue
            if det.ticker in seen_this_post:
                continue
            seen_this_post.add(det.ticker)
            a = acc.setdefault(det.ticker, {
                "mentions": 0, "authors": [], "flairs": {}, "dd_posts": 0,
                "meme_posts": 0, "sentiments": [], "confidences": [],
            })
            a["mentions"] += 1
            a["authors"].append(post.author_hash or f"anon:{post.post_id}")
            if post.flair:
                a["flairs"][post.flair] = a["flairs"].get(post.flair, 0) + 1
            if is_dd:
                a["dd_posts"] += 1
            if is_meme:
                a["meme_posts"] += 1
            a["sentiments"].append(sentiment)
            a["confidences"].append(det.confidence)

    results: list[TickerFeatures] = []
    for ticker, a in acc.items():
        mentions = a["mentions"]
        authors = a["authors"]
        unique_authors = len(set(authors))
        # Author concentration: share held by the single most prolific author.
        if authors:
            from collections import Counter
            top = Counter(authors).most_common(1)[0][1]
            concentration = top / len(authors)
        else:
            concentration = 0.0
        dd_density = a["dd_posts"] / mentions if mentions else 0.0
        meme_score = a["meme_posts"] / mentions if mentions else 0.0
        sentiments = a["sentiments"]
        sentiment_score = sum(sentiments) / len(sentiments) if sentiments else 0.0
        # Dispersion: stdev of sentiment normalized to 0..1 (max stdev of a -1..1
        # series is 1.0).
        if len(sentiments) >= 2:
            m = sentiment_score
            disp = math.sqrt(sum((s - m) ** 2 for s in sentiments) / len(sentiments))
        else:
            disp = 0.0
        avg_conf = sum(a["confidences"]) / len(a["confidences"]) if a["confidences"] else 0.0

        ctx = market_context.get(ticker, {})
        ext_news = bool(ctx.get("external_news_match", False))
        # Evidence blends DD density with external corroboration.
        evidence = min(1.0, 0.6 * dd_density + (0.4 if ext_news else 0.0))

        results.append(TickerFeatures(
            ticker=ticker,
            mention_count=mentions,
            mention_velocity_zscore=mention_velocity_zscore(
                mentions, mention_history.get(ticker, [])
            ),
            unique_author_count=unique_authors,
            author_concentration=concentration,
            flair_counts=a["flairs"],
            dd_density=dd_density,
            evidence_score=evidence,
            sentiment_score=sentiment_score,
            sentiment_dispersion=min(1.0, disp),
            meme_language_score=meme_score,
            avg_detection_confidence=avg_conf,
            external_news_match=ext_news,
            price_move_before_social_spike=ctx.get("price_move_before_social_spike"),
            volume_confirmation=ctx.get("volume_confirmation"),
            options_or_short_interest_context=ctx.get("options_or_short_interest_context"),
            historical_outcome_score=ctx.get("historical_outcome_score"),
        ))

    results.sort(key=lambda f: f.mention_count, reverse=True)
    return results
