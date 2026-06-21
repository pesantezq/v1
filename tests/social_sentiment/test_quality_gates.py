"""Tests for anti-manipulation quality gates (Phase 7)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portfolio_automation.social_sentiment.quality_gates import QualityGateChecker


def _rec(ticker="NVDA", source="bluesky", author="abc123", text="NVDA is up today!",
         created_at="2026-06-21T10:00:00Z"):
    return {
        "ticker": ticker, "source": source, "author_hash": author,
        "text": text, "created_at": created_at,
    }


def _recs(n=12, *, unique_authors=6, created_at="2026-06-21T10:00:00Z"):
    """Generate n records with at least unique_authors distinct authors."""
    records = []
    for i in range(n):
        author = f"author_{i % unique_authors:03d}"
        records.append(_rec(author=author, text=f"Post {i}: NVDA analysis today",
                            created_at=created_at))
    return records


class TestQualityGateCheckerDefaults(unittest.TestCase):
    def setUp(self):
        self.checker = QualityGateChecker()

    def test_passes_healthy_batch(self):
        records = _recs(n=15, unique_authors=8)
        result = self.checker.check(records, source="bluesky", ticker="NVDA")
        self.assertTrue(result.passed)
        self.assertEqual(result.failure_reasons, [])

    def test_fails_too_few_posts(self):
        records = _recs(n=5, unique_authors=5)
        result = self.checker.check(records, source="bluesky", ticker="NVDA")
        self.assertFalse(result.passed)
        self.assertTrue(any("too_few_posts" in r for r in result.failure_reasons))

    def test_fails_too_few_unique_authors(self):
        # 10 posts, all from same author
        records = [_rec(author="single_author", text=f"Post {i}: NVDA") for i in range(15)]
        result = self.checker.check(records, source="bluesky", ticker="NVDA")
        self.assertFalse(result.passed)
        reasons = result.failure_reasons
        # Both author count and concentration should fire
        self.assertTrue(any("too_few_authors" in r for r in reasons) or
                        any("high_author_concentration" in r for r in reasons))

    def test_fails_high_author_concentration(self):
        # 20 posts: 16 from one author (80% > 20%)
        records = [_rec(author="dominant", text=f"NVDA post {i}") for i in range(16)]
        records += [_rec(author=f"other_{i}", text=f"NVDA other {i}") for i in range(4)]
        result = self.checker.check(records, source="bluesky", ticker="NVDA")
        self.assertFalse(result.passed)
        self.assertTrue(any("high_author_concentration" in r for r in result.failure_reasons))

    def test_fails_high_duplicate_ratio(self):
        # All posts have identical text — 100% duplicate ratio
        records = [_rec(author=f"author_{i}", text="NVDA is great! Buy now!")
                   for i in range(15)]
        result = self.checker.check(records, source="bluesky", ticker="NVDA")
        self.assertFalse(result.passed)
        self.assertTrue(any("high_duplicate_ratio" in r for r in result.failure_reasons))

    def test_fails_high_spam_ratio(self):
        # All posts are very short (< 20 chars) → spam
        records = [_rec(author=f"a_{i}", text="NVDA!!!")
                   for i in range(15)]
        result = self.checker.check(records, source="bluesky", ticker="NVDA")
        self.assertFalse(result.passed)
        self.assertTrue(any("high_spam_ratio" in r for r in result.failure_reasons))

    def test_fails_too_old(self):
        # All posts from 48 hours ago → old_ratio > 0.5
        records = _recs(n=15, unique_authors=8, created_at="2026-06-19T10:00:00Z")
        result = self.checker.check(records, source="bluesky", ticker="NVDA")
        self.assertFalse(result.passed)
        self.assertTrue(any("too_old" in r for r in result.failure_reasons))

    def test_empty_batch_fails(self):
        result = self.checker.check([], source="bluesky", ticker="NVDA")
        self.assertFalse(result.passed)
        self.assertIn("no_records", result.failure_reasons)

    def test_stats_always_present(self):
        result = self.checker.check(_recs(n=15, unique_authors=8))
        self.assertIn("n", result.stats)
        self.assertIn("unique_authors", result.stats)
        self.assertIn("author_concentration", result.stats)
        self.assertIn("duplicate_ratio", result.stats)

    def test_result_to_dict(self):
        result = self.checker.check(_recs(n=15))
        d = result.to_dict()
        self.assertIn("passed", d)
        self.assertIn("failure_reasons", d)
        self.assertIn("stats", d)


class TestQualityGateCheckerCustomConfig(unittest.TestCase):
    def test_custom_min_posts(self):
        checker = QualityGateChecker({"min_posts": 3})
        records = _recs(n=5, unique_authors=5)
        result = checker.check(records)
        # With min_posts=3, n=5 should pass that gate
        self.assertFalse(any("too_few_posts" in r for r in result.failure_reasons))

    def test_custom_min_unique_authors(self):
        checker = QualityGateChecker({"min_posts": 5, "min_unique_authors": 2,
                                       "max_author_concentration": 1.0,
                                       "max_duplicate_ratio": 1.0, "max_spam_ratio": 1.0})
        records = _recs(n=5, unique_authors=3)
        result = checker.check(records)
        self.assertFalse(any("too_few_authors" in r for r in result.failure_reasons))

    def test_all_gates_configurable_to_permissive(self):
        checker = QualityGateChecker({
            "min_posts": 1,
            "min_unique_authors": 1,
            "max_author_concentration": 1.0,
            "max_duplicate_ratio": 1.0,
            "max_spam_ratio": 1.0,
            "max_age_hours": 8760.0,  # 1 year
        })
        records = [_rec()]  # single record
        result = checker.check(records)
        self.assertTrue(result.passed)


class TestQualityGateMultipleFailures(unittest.TestCase):
    def test_collects_all_failure_reasons(self):
        """When multiple gates fail, all reasons are reported."""
        checker = QualityGateChecker()
        # Tiny batch, single author, identical short texts
        records = [_rec(author="one_author", text="BUY!!") for _ in range(3)]
        result = checker.check(records)
        self.assertFalse(result.passed)
        self.assertGreater(len(result.failure_reasons), 1)
