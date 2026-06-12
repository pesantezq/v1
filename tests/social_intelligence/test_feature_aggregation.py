"""Tests for feature aggregation: mention velocity, dd_density, concentration."""
from __future__ import annotations

from portfolio_automation.social_intelligence.base import RawPost
from portfolio_automation.social_intelligence.feature_aggregation import (
    aggregate_ticker_features,
    mention_velocity_zscore,
)

UNIVERSE = {"NVDA", "GME", "TSLA"}


def _post(pid, title, body="", author="a", flair=None):
    return RawPost(post_id=pid, source="reddit", community="stocks", created_utc=0.0,
                   title=title, body=body, author_hash=author, flair=flair)


def test_mention_velocity_zscore_basic():
    # today=10 vs baseline mostly ~1 → strongly positive z.
    z = mention_velocity_zscore(10, [1, 1, 2, 1, 1])
    assert z > 2.0


def test_mention_velocity_zscore_insufficient_history():
    assert mention_velocity_zscore(10, []) == 0.0
    assert mention_velocity_zscore(10, [5]) == 0.0


def test_mention_velocity_zscore_zero_variance():
    assert mention_velocity_zscore(5, [5, 5, 5]) == 0.0


def test_dd_density_calculation():
    posts = [
        _post("1", "$NVDA valuation thesis DCF earnings", author="a"),
        _post("2", "$NVDA fundamentals balance sheet", author="b"),
        _post("3", "$NVDA to the moon rocket", author="c"),  # no DD markers
    ]
    feats = aggregate_ticker_features(posts, known_universe=UNIVERSE)
    nvda = next(f for f in feats if f.ticker == "NVDA")
    # 2 of 3 posts carry DD markers.
    assert abs(nvda.dd_density - (2 / 3)) < 1e-6


def test_author_concentration():
    # Single author dominates → concentration near 1.0.
    posts = [_post(str(i), "$GME yolo", author="whale") for i in range(4)]
    feats = aggregate_ticker_features(posts, known_universe=UNIVERSE)
    gme = next(f for f in feats if f.ticker == "GME")
    assert gme.unique_author_count == 1
    assert gme.author_concentration == 1.0


def test_meme_language_score():
    posts = [
        _post("1", "$GME to the moon rocket diamond hands", author="a"),
        _post("2", "$GME yolo tendies squeeze", author="b"),
    ]
    feats = aggregate_ticker_features(posts, known_universe=UNIVERSE)
    gme = next(f for f in feats if f.ticker == "GME")
    assert gme.meme_language_score == 1.0


def test_external_news_context_lifts_evidence():
    posts = [_post(str(i), "$TSLA earnings guidance", author=f"a{i}") for i in range(3)]
    feats = aggregate_ticker_features(
        posts, known_universe=UNIVERSE,
        market_context={"TSLA": {"external_news_match": True}},
    )
    tsla = next(f for f in feats if f.ticker == "TSLA")
    assert tsla.external_news_match is True
    assert tsla.evidence_score > tsla.dd_density * 0.6 - 1e-9  # news adds to evidence


def test_no_posts_returns_empty():
    assert aggregate_ticker_features([], known_universe=UNIVERSE) == []
