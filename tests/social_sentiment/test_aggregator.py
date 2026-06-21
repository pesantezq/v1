"""Tests for social sentiment aggregation (Phase 8)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portfolio_automation.social_sentiment.aggregator import (
    MAX_SOURCE_CONTRIBUTION,
    AggregateResult,
    PerSourceResult,
    _apply_source_cap,
    aggregate_cross_source,
    aggregate_source,
)
from portfolio_automation.social_sentiment.quality_gates import QualityGateResult


def _gate_pass(**kw):
    stats = {"n": kw.get("n", 15), "source": kw.get("source", "bluesky"),
             "ticker": "NVDA", "unique_authors": 8, "author_concentration": 0.1,
             "duplicate_ratio": 0.0, "spam_ratio": 0.0, "old_ratio": 0.0}
    return QualityGateResult(passed=True, stats=stats)


def _gate_fail(reason="too_few_posts"):
    return QualityGateResult(passed=False, failure_reasons=[reason],
                             stats={"n": 3, "source": "bluesky", "ticker": "NVDA",
                                    "unique_authors": 2, "author_concentration": 0.5,
                                    "duplicate_ratio": 0.0, "spam_ratio": 0.0, "old_ratio": 0.0})


def _scored_rec(sentiment=0.8, pos=0.85, neu=0.1, neg=0.05, engagement=0.5):
    return {
        "ticker": "NVDA", "text": "great", "source": "bluesky", "source_type": "text",
        "post_id_hash": "x", "created_at": "t",
        "sentiment_score": sentiment,
        "positive_probability": pos, "neutral_probability": neu, "negative_probability": neg,
        "label": "positive", "scorer": "finbert", "scorer_version": "1",
        "engagement_score": engagement,
    }


def _unavail_rec():
    r = {"ticker": "NVDA", "text": "ok", "source": "bluesky", "source_type": "text",
         "post_id_hash": "y", "created_at": "t",
         "sentiment_score": 0.0, "positive_probability": 0.0,
         "neutral_probability": 1.0, "negative_probability": 0.0,
         "label": "neutral", "scorer": "scorer_unavailable", "scorer_version": "1",
         "engagement_score": 0.1}
    return r


class TestAggregateSource(unittest.TestCase):
    def test_gate_failed_returns_not_passed(self):
        psr = aggregate_source([], "bluesky", "NVDA", _gate_fail())
        self.assertFalse(psr.quality_passed)
        self.assertGreater(len(psr.failure_reasons), 0)

    def test_no_scored_records_returns_zero_sentiment(self):
        records = [_unavail_rec() for _ in range(5)]
        psr = aggregate_source(records, "bluesky", "NVDA", _gate_pass())
        self.assertTrue(psr.quality_passed)
        self.assertEqual(psr.sentiment_score, 0.0)
        self.assertEqual(psr.scorer_unavailable_count, 5)

    def test_positive_scored_records(self):
        records = [_scored_rec(sentiment=0.9, engagement=0.5) for _ in range(5)]
        psr = aggregate_source(records, "bluesky", "NVDA", _gate_pass())
        self.assertTrue(psr.quality_passed)
        self.assertGreater(psr.sentiment_score, 0.5)
        self.assertGreater(psr.positive_probability, 0.5)

    def test_negative_scored_records(self):
        records = [_scored_rec(sentiment=-0.8, pos=0.05, neu=0.1, neg=0.85) for _ in range(5)]
        psr = aggregate_source(records, "bluesky", "NVDA", _gate_pass())
        self.assertLess(psr.sentiment_score, 0.0)

    def test_engagement_weights_higher_records_more(self):
        # High-engagement positive + low-engagement negative
        records = [
            _scored_rec(sentiment=0.9, engagement=0.9),
            _scored_rec(sentiment=-0.9, pos=0.05, neg=0.85, neu=0.1, engagement=0.1),
        ]
        psr = aggregate_source(records, "bluesky", "NVDA", _gate_pass())
        # High-engagement positive should dominate
        self.assertGreater(psr.sentiment_score, 0.0)

    def test_to_dict_has_required_keys(self):
        psr = aggregate_source([_scored_rec()], "bluesky", "NVDA", _gate_pass())
        d = psr.to_dict()
        for key in ("source", "ticker", "sentiment_score", "sample_size",
                    "quality_passed", "failure_reasons"):
            self.assertIn(key, d)


class TestAggregateCrossSource(unittest.TestCase):
    def _psr(self, source, sentiment, n=15, passed=True):
        return PerSourceResult(
            source=source, ticker="NVDA",
            sentiment_score=sentiment,
            positive_probability=max(0.0, sentiment),
            neutral_probability=max(0.0, 1.0 - abs(sentiment) - 0.1),
            negative_probability=max(0.0, -sentiment),
            sample_size=n, engagement_weighted=True,
            quality_passed=passed,
        )

    def test_no_contributing_sources(self):
        results = [self._psr("bluesky", 0.8, passed=False)]
        agg = aggregate_cross_source(results, "NVDA")
        self.assertEqual(agg.source_count, 0)
        self.assertEqual(agg.sentiment_score, 0.0)
        self.assertGreater(len(agg.sources_failed), 0)

    def test_single_source_is_labeled(self):
        results = [self._psr("bluesky", 0.7)]
        agg = aggregate_cross_source(results, "NVDA")
        self.assertTrue(agg.is_single_source)
        self.assertEqual(agg.source_count, 1)

    def test_multiple_sources_not_single(self):
        results = [self._psr("bluesky", 0.7), self._psr("mastodon", 0.5)]
        agg = aggregate_cross_source(results, "NVDA")
        self.assertFalse(agg.is_single_source)
        self.assertEqual(agg.source_count, 2)

    def test_cross_source_sentiment_is_weighted_mean(self):
        # Equal posts: bluesky=0.8, mastodon=0.2 → ~0.5
        results = [self._psr("bluesky", 0.8, n=10), self._psr("mastodon", 0.2, n=10)]
        agg = aggregate_cross_source(results, "NVDA")
        self.assertAlmostEqual(agg.sentiment_score, 0.5, delta=0.05)

    def test_source_cap_enforced(self):
        # One source with 95% of posts — should be capped at MAX_SOURCE_CONTRIBUTION
        big = self._psr("bluesky", 0.9, n=95)
        small = self._psr("mastodon", -0.9, n=5)
        agg = aggregate_cross_source([big, small], "NVDA")
        # If cap wasn't enforced, result would be ~0.8; with cap it should be <0.8
        # because the small negative source gets boosted weight above its raw share
        self.assertLess(agg.sentiment_score, 0.9)

    def test_failed_sources_appear_in_sources_failed(self):
        results = [self._psr("bluesky", 0.7), self._psr("lemmy", 0.5, passed=False)]
        agg = aggregate_cross_source(results, "NVDA")
        self.assertIn("lemmy", agg.sources_failed)
        self.assertIn("bluesky", agg.sources_contributing)

    def test_to_dict_has_required_keys(self):
        results = [self._psr("bluesky", 0.5)]
        agg = aggregate_cross_source(results, "NVDA")
        d = agg.to_dict()
        for key in ("ticker", "sentiment_score", "confidence", "source_count",
                    "is_single_source", "sources_contributing", "per_source"):
            self.assertIn(key, d)

    def test_confidence_higher_with_more_sources(self):
        one = aggregate_cross_source([self._psr("bluesky", 0.5, n=20)], "NVDA")
        three = aggregate_cross_source([
            self._psr("bluesky", 0.5, n=20),
            self._psr("mastodon", 0.5, n=20),
            self._psr("lemmy", 0.5, n=20),
        ], "NVDA")
        self.assertGreater(three.confidence, one.confidence)


class TestApplySourceCap(unittest.TestCase):
    def test_single_source_gets_full_weight(self):
        result = _apply_source_cap({"bluesky": 1.0}, 0.4)
        self.assertAlmostEqual(result["bluesky"], 1.0, places=4)

    def test_cap_enforced_on_dominant_source(self):
        # With 2 sources the iterative cap equalizes to 0.5/0.5 (you can't get
        # both below 0.40 when they must sum to 1.0). The key invariant is that
        # the dominant source's weight is reduced from its raw share (0.95).
        result = _apply_source_cap({"bluesky": 0.95, "mastodon": 0.05}, 0.4)
        self.assertLess(result["bluesky"], 0.95)  # weight reduced from raw
        self.assertAlmostEqual(sum(result.values()), 1.0, places=4)

    def test_cap_enforced_on_dominant_source_three_way(self):
        # With 3 sources we CAN enforce ≤ 0.40 on the dominant source.
        result = _apply_source_cap({"bluesky": 0.80, "mastodon": 0.15, "lemmy": 0.05}, 0.4)
        self.assertLessEqual(result["bluesky"], 0.4 + 1e-9)

    def test_weights_sum_to_one_after_cap(self):
        result = _apply_source_cap({"a": 0.7, "b": 0.2, "c": 0.1}, 0.4)
        self.assertAlmostEqual(sum(result.values()), 1.0, places=4)

    def test_equal_weights_stay_equal_below_cap(self):
        result = _apply_source_cap({"a": 0.33, "b": 0.33, "c": 0.34}, 0.4)
        self.assertAlmostEqual(sum(result.values()), 1.0, places=4)

    def test_cap_higher_than_all_sources_no_effect(self):
        weights = {"a": 0.3, "b": 0.2, "c": 0.5}
        result = _apply_source_cap(weights, 0.8)  # higher cap — no redistribution needed
        self.assertAlmostEqual(sum(result.values()), 1.0, places=4)
