"""Tests for BlueskyConnector — mocked HTTP, no live network."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portfolio_automation.social_intelligence.base import SourceStatus
from portfolio_automation.social_sources.bluesky_connector import (
    BlueskyConnector,
    _extract_post,
    _hash_author,
    _hash_post_id,
)


def _make_post(uri="at://did:plc:test/app.bsky.feed.post/abc123",
               did="did:plc:testauthor",
               text="$NVDA looking good today",
               created_at="2026-06-21T10:00:00.000Z",
               likes=5, replies=2, reposts=1):
    return {
        "uri": uri,
        "author": {"did": did, "handle": "user.bsky.social"},
        "record": {"text": text, "createdAt": created_at, "langs": ["en"]},
        "likeCount": likes,
        "replyCount": replies,
        "repostCount": reposts,
    }


class TestHashFunctions(unittest.TestCase):
    def test_hash_author_12_chars(self):
        h = _hash_author("did:plc:abc123")
        self.assertEqual(len(h), 12)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))

    def test_hash_post_id_16_chars(self):
        h = _hash_post_id("at://did:plc:test/post/xyz")
        self.assertEqual(len(h), 16)

    def test_different_dids_different_hashes(self):
        self.assertNotEqual(_hash_author("did:plc:aaa"), _hash_author("did:plc:bbb"))

    def test_same_did_deterministic(self):
        self.assertEqual(_hash_author("did:plc:test"), _hash_author("did:plc:test"))


class TestExtractPost(unittest.TestCase):
    def test_extracts_valid_post(self):
        raw = _make_post()
        rec = _extract_post(raw, "NVDA")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["ticker"], "NVDA")
        self.assertEqual(rec["source"], "bluesky")
        self.assertEqual(rec["source_type"], "text")
        self.assertEqual(len(rec["post_id_hash"]), 16)
        self.assertEqual(len(rec["author_hash"]), 12)
        self.assertIn("text", rec)
        self.assertLessEqual(len(rec["text"]), 500)
        self.assertEqual(rec["like_count"], 5)
        self.assertEqual(rec["reply_count"], 2)
        self.assertEqual(rec["repost_count"], 1)
        self.assertGreater(rec["engagement_score"], 0.0)
        self.assertLessEqual(rec["engagement_score"], 1.0)

    def test_returns_none_for_missing_text(self):
        raw = _make_post()
        raw["record"]["text"] = ""
        self.assertIsNone(_extract_post(raw, "NVDA"))

    def test_returns_none_for_missing_uri(self):
        raw = _make_post(uri="")
        self.assertIsNone(_extract_post(raw, "NVDA"))

    def test_returns_none_for_non_dict(self):
        self.assertIsNone(_extract_post("not_a_dict", "NVDA"))

    def test_text_capped_at_500(self):
        raw = _make_post(text="x" * 1000)
        rec = _extract_post(raw, "NVDA")
        self.assertEqual(len(rec["text"]), 500)

    def test_ticker_uppercased(self):
        rec = _extract_post(_make_post(), "nvda")
        self.assertEqual(rec["ticker"], "NVDA")

    def test_schema_version_is_2(self):
        rec = _extract_post(_make_post(), "AAPL")
        self.assertEqual(rec["schema_version"], "2")

    def test_missing_did_gives_empty_author_hash(self):
        raw = _make_post()
        raw["author"]["did"] = ""
        rec = _extract_post(raw, "NVDA")
        self.assertEqual(rec["author_hash"], "")


class TestBlueskyConnectorDisabled(unittest.TestCase):
    def _connector(self, enabled=False, crowd=False):
        return BlueskyConnector(
            {"enabled": enabled},
            crowd_radar_enabled=crowd,
            http_get=lambda u: ({}),  # should not be called
        )

    def test_not_configured_when_disabled(self):
        c = self._connector(enabled=False, crowd=True)
        self.assertFalse(c.is_configured())

    def test_not_configured_when_crowd_disabled(self):
        c = self._connector(enabled=True, crowd=False)
        self.assertFalse(c.is_configured())

    def test_health_disabled_returns_disabled_status(self):
        c = self._connector()
        r = c.health()
        self.assertEqual(r.status, SourceStatus.DISABLED)

    def test_fetch_for_ticker_returns_disabled(self):
        c = self._connector()
        r = c.fetch_for_ticker("NVDA")
        self.assertEqual(r.status, SourceStatus.DISABLED)

    def test_probe_returns_disabled(self):
        c = self._connector()
        r = c.probe()
        self.assertEqual(r.status, SourceStatus.DISABLED)


class TestBlueskyConnectorEnabled(unittest.TestCase):
    def _make_response(self, posts, cursor=None):
        d = {"posts": posts}
        if cursor:
            d["cursor"] = cursor
        return d

    def _connector(self, http_fn, max_pages=1, limit=25, delay=0):
        return BlueskyConnector(
            {"enabled": True, "max_results_per_query": limit,
             "max_pages": max_pages, "polite_delay_s": delay},
            crowd_radar_enabled=True,
            http_get=http_fn,
            sleep=lambda _: None,
        )

    def test_probe_ok_when_posts_returned(self):
        posts = [_make_post()]
        c = self._connector(lambda u: {"posts": posts})
        r = c.probe()
        self.assertEqual(r.status, SourceStatus.OK)

    def test_probe_degraded_on_bad_shape(self):
        c = self._connector(lambda u: {"wrong": "key"})
        r = c.probe()
        self.assertEqual(r.status, SourceStatus.DEGRADED)

    def test_probe_error_on_exception(self):
        def bad(u):
            raise ConnectionError("timeout")
        c = self._connector(bad)
        r = c.probe()
        self.assertEqual(r.status, SourceStatus.ERROR)

    def test_fetch_for_ticker_with_results(self):
        posts = [_make_post(text="$NVDA is bullish"),
                 _make_post(uri="at://test2", text="Nvidia earnings beat")]
        c = self._connector(lambda u: {"posts": posts})
        r = c.fetch_for_ticker("NVDA")
        self.assertEqual(r.status, SourceStatus.OK)
        self.assertEqual(len(r.records), 2)
        self.assertEqual(r.meta["ticker"], "NVDA")

    def test_fetch_for_ticker_deduplicates_same_uri(self):
        post = _make_post()
        # Return same post for both cashtag and company name queries
        c = self._connector(lambda u: {"posts": [post]})
        r = c.fetch_for_ticker("NVDA", company_name="Nvidia")
        self.assertEqual(len(r.records), 1)  # deduplicated

    def test_fetch_for_ticker_insufficient_data_on_no_results(self):
        c = self._connector(lambda u: {"posts": []})
        r = c.fetch_for_ticker("NVDA")
        self.assertEqual(r.status, SourceStatus.INSUFFICIENT_DATA)

    def test_fetch_for_ticker_degraded_on_error(self):
        call_count = [0]
        def flaky(u):
            call_count[0] += 1
            if call_count[0] > 1:
                raise TimeoutError("timeout")
            return {"posts": [_make_post()]}
        c = self._connector(flaky)
        r = c.fetch_for_ticker("NVDA", company_name="Nvidia")
        # First query succeeded, second failed — degraded with records
        self.assertIn(r.status, (SourceStatus.OK, SourceStatus.DEGRADED))

    def test_pagination_stops_on_no_cursor(self):
        call_urls = []
        def tracking(u):
            call_urls.append(u)
            return {"posts": [_make_post()]}
        c = self._connector(tracking, max_pages=3)
        c.fetch_for_ticker("NVDA")
        # Should only make 1 request per query (no cursor = no next page)
        self.assertEqual(len(call_urls), 1)

    def test_engagement_score_is_bounded(self):
        high_engage = _make_post(likes=9999, replies=9999, reposts=9999)
        rec = _extract_post(high_engage, "AAPL")
        self.assertLessEqual(rec["engagement_score"], 1.0)
        self.assertGreaterEqual(rec["engagement_score"], 0.0)


class TestBlueskyNoRedditScraping(unittest.TestCase):
    """Ensure the connector never references Reddit/Discord/Telegram."""
    def test_no_reddit_in_connector(self):
        import inspect
        import portfolio_automation.social_sources.bluesky_connector as mod
        src = inspect.getsource(mod)
        for forbidden in ("reddit.com", "discord.com", "t.me", "telegram"):
            self.assertNotIn(forbidden, src.lower(), f"Found {forbidden} in bluesky_connector")
