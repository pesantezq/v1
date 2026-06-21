"""Tests for the normalized social record schema (Phase 4)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portfolio_automation.social_sentiment.schema import (
    SCHEMA_VERSION,
    attach_sentiment,
    is_sentiment_scored,
    is_valid_text_record,
    make_text_record,
)


class TestMakeTextRecord(unittest.TestCase):
    def _minimal(self, **kw):
        defaults = dict(
            source="bluesky", ticker="NVDA", post_id_hash="abc123def456efab",
            author_hash="abc123def456", created_at="2026-06-21T10:00:00Z",
            text="Nvidia looks great", text_len=18,
        )
        defaults.update(kw)
        return make_text_record(**defaults)

    def test_schema_version_is_2(self):
        self.assertEqual(self._minimal()["schema_version"], SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, "2")

    def test_ticker_uppercased(self):
        r = self._minimal(ticker="nvda")
        self.assertEqual(r["ticker"], "NVDA")

    def test_text_capped_at_500(self):
        r = self._minimal(text="x" * 1000, text_len=1000)
        self.assertLessEqual(len(r["text"]), 500)

    def test_post_id_hash_capped_at_16(self):
        r = self._minimal(post_id_hash="a" * 50)
        self.assertEqual(len(r["post_id_hash"]), 16)

    def test_author_hash_capped_at_12(self):
        r = self._minimal(author_hash="b" * 50)
        self.assertEqual(len(r["author_hash"]), 12)

    def test_engagement_clamped_0_to_1(self):
        r = self._minimal(engagement_score=5.0)
        self.assertLessEqual(r["engagement_score"], 1.0)
        r2 = self._minimal(engagement_score=-1.0)
        self.assertGreaterEqual(r2["engagement_score"], 0.0)

    def test_source_type_is_text(self):
        r = self._minimal()
        self.assertEqual(r["source_type"], "text")

    def test_no_sentiment_fields_initially(self):
        r = self._minimal()
        self.assertNotIn("sentiment_score", r)
        self.assertNotIn("scorer", r)

    def test_extra_fields_merged(self):
        r = self._minimal(extra={"instance": "mastodon.social"})
        self.assertEqual(r["instance"], "mastodon.social")

    def test_extra_does_not_override_required_fields(self):
        r = self._minimal(extra={"ticker": "HACKER"})
        self.assertEqual(r["ticker"], "NVDA")

    def test_defaults_for_optional_fields(self):
        r = self._minimal()
        self.assertEqual(r["like_count"], 0)
        self.assertEqual(r["language"], "en")


class TestIsValidTextRecord(unittest.TestCase):
    def _good(self):
        return {
            "source": "bluesky", "source_type": "text",
            "ticker": "NVDA", "post_id_hash": "abc",
            "created_at": "2026-06-21T10:00:00Z", "text": "hello",
        }

    def test_valid_record(self):
        self.assertTrue(is_valid_text_record(self._good()))

    def test_missing_required_field(self):
        r = self._good()
        del r["ticker"]
        self.assertFalse(is_valid_text_record(r))

    def test_empty_ticker_is_invalid(self):
        r = self._good()
        r["ticker"] = ""
        self.assertFalse(is_valid_text_record(r))

    def test_empty_text_is_invalid(self):
        r = self._good()
        r["text"] = ""
        self.assertFalse(is_valid_text_record(r))

    def test_non_dict_is_invalid(self):
        self.assertFalse(is_valid_text_record("not_a_dict"))
        self.assertFalse(is_valid_text_record(None))
        self.assertFalse(is_valid_text_record(42))

    def test_extra_fields_are_tolerated(self):
        r = self._good()
        r["future_field_v3"] = "unknown"
        self.assertTrue(is_valid_text_record(r))


class TestAttachSentiment(unittest.TestCase):
    def test_attaches_all_fields(self):
        rec = {"ticker": "NVDA", "text": "good", "source": "bluesky",
               "source_type": "text", "post_id_hash": "x", "created_at": "t"}
        attach_sentiment(
            rec, sentiment_score=0.7, positive_probability=0.8,
            neutral_probability=0.15, negative_probability=0.05,
            label="positive", scorer="finbert", scorer_version="finbert-1.0",
        )
        self.assertEqual(rec["sentiment_score"], 0.7)
        self.assertEqual(rec["label"], "positive")
        self.assertEqual(rec["scorer"], "finbert")
        self.assertTrue(is_sentiment_scored(rec))

    def test_rounds_to_4_decimals(self):
        rec = {"ticker": "X", "text": "t", "source": "s", "source_type": "text",
               "post_id_hash": "x", "created_at": "t"}
        attach_sentiment(
            rec, sentiment_score=0.123456789, positive_probability=0.9,
            neutral_probability=0.05, negative_probability=0.05,
            label="positive", scorer="finbert", scorer_version="1",
        )
        self.assertEqual(rec["sentiment_score"], 0.1235)

    def test_is_sentiment_scored_false_before_attach(self):
        rec = {"ticker": "X", "text": "hello"}
        self.assertFalse(is_sentiment_scored(rec))

    def test_is_sentiment_scored_true_after_attach(self):
        rec = {"ticker": "X", "text": "t", "source": "s", "source_type": "text",
               "post_id_hash": "x", "created_at": "t"}
        attach_sentiment(rec, sentiment_score=0.0, positive_probability=0.0,
                        neutral_probability=1.0, negative_probability=0.0,
                        label="neutral", scorer="finbert", scorer_version="1")
        self.assertTrue(is_sentiment_scored(rec))
