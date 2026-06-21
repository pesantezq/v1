"""
Phase 8: Per-source and cross-source aggregate social sentiment.

Pipeline:
  1. Records arrive per source per ticker (after Phase 7 quality gates).
  2. Per-source aggregate: weighted mean sentiment weighted by engagement_score.
  3. Cross-source aggregate: each source contributes up to MAX_SOURCE_CONTRIBUTION
     (default 0.40) of total weight — prevents one dominant source from
     controlling the aggregate.
  4. If only one source passes gates, the aggregate is labeled single_source.
  5. Confidence is derived from: sample size, source count, quality gate margins.

Output: ``AggregateResult`` per ticker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_SOURCE_CONTRIBUTION = 0.40  # no single source may contribute more than 40% of weight


@dataclass
class PerSourceResult:
    """Sentiment aggregate for one source."""

    source: str
    ticker: str
    sentiment_score: float       # weighted mean, -1.0 to +1.0
    positive_probability: float
    neutral_probability: float
    negative_probability: float
    sample_size: int
    engagement_weighted: bool
    quality_passed: bool
    quality_stats: dict[str, Any] = field(default_factory=dict)
    failure_reasons: list[str] = field(default_factory=list)
    scorer_unavailable_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ticker": self.ticker,
            "sentiment_score": self.sentiment_score,
            "positive_probability": self.positive_probability,
            "neutral_probability": self.neutral_probability,
            "negative_probability": self.negative_probability,
            "sample_size": self.sample_size,
            "engagement_weighted": self.engagement_weighted,
            "quality_passed": self.quality_passed,
            "quality_stats": self.quality_stats,
            "failure_reasons": list(self.failure_reasons),
            "scorer_unavailable_count": self.scorer_unavailable_count,
        }


@dataclass
class AggregateResult:
    """Cross-source aggregate sentiment for one ticker."""

    ticker: str
    sentiment_score: float        # cross-source weighted mean
    positive_probability: float
    neutral_probability: float
    negative_probability: float
    confidence: float             # 0.0 to 1.0
    source_count: int             # number of sources that passed gates + scoring
    total_posts: int
    is_single_source: bool
    sources_contributing: list[str]
    sources_failed: list[str]
    per_source: list[PerSourceResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "sentiment_score": self.sentiment_score,
            "positive_probability": self.positive_probability,
            "neutral_probability": self.neutral_probability,
            "negative_probability": self.negative_probability,
            "confidence": self.confidence,
            "source_count": self.source_count,
            "total_posts": self.total_posts,
            "is_single_source": self.is_single_source,
            "sources_contributing": list(self.sources_contributing),
            "sources_failed": list(self.sources_failed),
            "per_source": [s.to_dict() for s in self.per_source],
        }


def aggregate_source(
    records: list[dict[str, Any]],
    source: str,
    ticker: str,
    quality_result: Any,  # QualityGateResult
) -> PerSourceResult:
    """
    Compute per-source sentiment aggregate from a list of scored records.

    Only records with scorer="finbert" (not scorer_unavailable) contribute
    to the weighted sentiment. Engagement score is used as the weight.
    """
    if not quality_result.passed:
        return PerSourceResult(
            source=source, ticker=ticker,
            sentiment_score=0.0, positive_probability=0.0,
            neutral_probability=1.0, negative_probability=0.0,
            sample_size=len(records), engagement_weighted=False,
            quality_passed=False,
            quality_stats=quality_result.stats,
            failure_reasons=list(quality_result.failure_reasons),
        )

    scored = [r for r in records if r.get("scorer") == "finbert"]
    unavail_count = sum(1 for r in records if r.get("scorer") == "scorer_unavailable")

    if not scored:
        # All records scorer_unavailable — no sentiment, but quality passed.
        return PerSourceResult(
            source=source, ticker=ticker,
            sentiment_score=0.0, positive_probability=0.0,
            neutral_probability=1.0, negative_probability=0.0,
            sample_size=len(records), engagement_weighted=True,
            quality_passed=True,
            quality_stats=quality_result.stats,
            scorer_unavailable_count=unavail_count,
        )

    # Weighted aggregation (engagement score as weight, default to 1.0 if missing)
    total_weight = 0.0
    w_sentiment = 0.0
    w_pos = 0.0
    w_neu = 0.0
    w_neg = 0.0
    for r in scored:
        w = max(0.01, float(r.get("engagement_score") or 0.01))
        s = float(r.get("sentiment_score") or 0.0)
        pos = float(r.get("positive_probability") or 0.0)
        neu = float(r.get("neutral_probability") or 0.0)
        neg = float(r.get("negative_probability") or 0.0)
        total_weight += w
        w_sentiment += w * s
        w_pos += w * pos
        w_neu += w * neu
        w_neg += w * neg

    if total_weight == 0:
        total_weight = 1.0

    return PerSourceResult(
        source=source, ticker=ticker,
        sentiment_score=round(w_sentiment / total_weight, 4),
        positive_probability=round(w_pos / total_weight, 4),
        neutral_probability=round(w_neu / total_weight, 4),
        negative_probability=round(w_neg / total_weight, 4),
        sample_size=len(records),
        engagement_weighted=True,
        quality_passed=True,
        quality_stats=quality_result.stats,
        scorer_unavailable_count=unavail_count,
    )


def aggregate_cross_source(
    per_source_results: list[PerSourceResult],
    ticker: str,
) -> AggregateResult:
    """
    Combine per-source aggregates into a cross-source aggregate for one ticker.

    Cap: no single source may contribute more than MAX_SOURCE_CONTRIBUTION (0.40)
    of the total weight. Sources that failed quality gates are excluded.
    """
    contributing = [r for r in per_source_results if r.quality_passed and r.sample_size > 0]
    failed = [r for r in per_source_results if not r.quality_passed]

    if not contributing:
        return AggregateResult(
            ticker=ticker,
            sentiment_score=0.0, positive_probability=0.0,
            neutral_probability=1.0, negative_probability=0.0,
            confidence=0.0, source_count=0,
            total_posts=sum(r.sample_size for r in per_source_results),
            is_single_source=True,
            sources_contributing=[],
            sources_failed=[r.source for r in failed],
            per_source=per_source_results,
        )

    # Base weights: sample size, capped at MAX_SOURCE_CONTRIBUTION of total.
    total_posts = sum(r.sample_size for r in contributing)
    raw_weights = {r.source: r.sample_size / max(total_posts, 1) for r in contributing}

    # Apply cap
    capped_weights = _apply_source_cap(raw_weights, MAX_SOURCE_CONTRIBUTION)

    total_weight = sum(capped_weights.values()) or 1.0
    w_sentiment = 0.0
    w_pos = 0.0
    w_neu = 0.0
    w_neg = 0.0
    for r in contributing:
        w = capped_weights.get(r.source, 0.0)
        w_sentiment += w * r.sentiment_score
        w_pos += w * r.positive_probability
        w_neu += w * r.neutral_probability
        w_neg += w * r.negative_probability

    sentiment_score = round(w_sentiment / total_weight, 4)
    pos = round(w_pos / total_weight, 4)
    neu = round(w_neu / total_weight, 4)
    neg = round(w_neg / total_weight, 4)

    confidence = _compute_confidence(
        contributing, len(failed), total_posts
    )

    return AggregateResult(
        ticker=ticker,
        sentiment_score=sentiment_score,
        positive_probability=pos,
        neutral_probability=neu,
        negative_probability=neg,
        confidence=confidence,
        source_count=len(contributing),
        total_posts=total_posts + sum(r.sample_size for r in failed),
        is_single_source=len(contributing) == 1,
        sources_contributing=[r.source for r in contributing],
        sources_failed=[r.source for r in failed],
        per_source=per_source_results,
    )


def _apply_source_cap(weights: dict[str, float], cap: float) -> dict[str, float]:
    """
    Iteratively redistribute weight to enforce per-source cap.

    Sources exceeding `cap` are trimmed; the excess is redistributed equally
    among sources below the cap. Converges in at most len(weights) iterations.
    """
    w = dict(weights)
    for _ in range(len(w)):
        over = {s: v for s, v in w.items() if v > cap}
        if not over:
            break
        under = {s: v for s, v in w.items() if v <= cap}
        excess = sum(v - cap for v in over.values())
        for s in over:
            w[s] = cap
        if under:
            share = excess / len(under)
            for s in under:
                w[s] = min(cap, w[s] + share)
    # Normalize so weights sum to 1.0
    total = sum(w.values()) or 1.0
    return {s: v / total for s, v in w.items()}


def _compute_confidence(
    contributing: list[PerSourceResult],
    failed_count: int,
    total_posts: int,
) -> float:
    """
    Confidence [0.0, 1.0] based on:
      - Sample size (diminishing returns, asymptote at 1.0 around 100 posts)
      - Source count (more sources → higher confidence)
      - Scorer availability (scorer_unavailable records reduce confidence)
    """
    size_conf = min(1.0, total_posts / 100.0)
    source_conf = min(1.0, len(contributing) / 3.0)

    unavail_total = sum(r.scorer_unavailable_count for r in contributing)
    scored_total = sum(
        r.sample_size - r.scorer_unavailable_count for r in contributing
    )
    scorer_conf = scored_total / max(scored_total + unavail_total, 1)

    # Penalize if many sources failed gates
    gate_penalty = 0.0 if failed_count == 0 else min(0.3, failed_count * 0.1)

    raw = (size_conf * 0.4 + source_conf * 0.3 + scorer_conf * 0.3) - gate_penalty
    return round(max(0.0, min(1.0, raw)), 4)
