"""Tests for MastodonConnector — mocked HTTP, no live network."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portfolio_automation.social_intelligence.base import SourceStatus
from portfolio_automation.social_sources.mastodon_connector import (
    MastodonConnector,
    _extract_status,
    _hash_author,
    _strip_html,
)


def _make_status(post_id="12345", content="<p>$NVDA is looking bullish today!</p>",
                 acct="user@mastodon.social", created_at="2026-06-21T10:00:00.000Z",
                 likes=3, replies=1, reblogs=0, language="en"):
    return {
        "id": post_id,
        "content": content,
        "created_at": created_at,
        "account": {"acct": acct, "id": "999"},
        "favourites_count": likes,
        "replies_count": replies,
        "reblogs_count": reblogs,
        "language": language,
        "url": "https://mastodon.social/@user/12345",
    }


class TestHtmlStripping(unittest.TestCase):
    def test_strips_p_tags(self):
        result = _strip_html("<p>Hello world</p>")
        self.assertEqual(result, "Hello world")

    def test_strips_anchor_tags(self):
        result = _strip_html('<p>Visit <a href="x">here</a></p>')
        self.assertEqual(result, "Visit here")

    def test_unescapes_entities(self):
        result = _strip_html("AT&amp;T stock &lt;up&gt;")
        self.assertEqual(result, "AT&T stock <up>")

    def test_collapses_whitespace(self):
        result = _strip_html("<p>  a   b  </p>")
        self.assertEqual(result, "a b")


class TestHashAuthor(unittest.TestCase):
    def test_length_12(self):
        h = _hash_author("user@mastodon.social")
        self.assertEqual(len(h), 12)

    def test_deterministic(self):
        self.assertEqual(_hash_author("alice@host"), _hash_author("alice@host"))

    def test_different_accts_different_hashes(self):
        self.assertNotEqual(_hash_author("alice@host"), _hash_author("bob@host"))


class TestExtractStatus(unittest.TestCase):
    def test_extracts_valid_status(self):
        raw = _make_status()
        rec = _extract_status(raw, "NVDA", "mastodon.social")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["ticker"], "NVDA")
        self.assertEqual(rec["source"], "mastodon")
        self.assertEqual(rec["source_type"], "text")
        self.assertEqual(len(rec["post_id_hash"]), 16)
        self.assertEqual(len(rec["author_hash"]), 12)
        self.assertNotIn("<p>", rec["text"])  # HTML stripped
        self.assertIn("NVDA", rec["text"])
        self.assertEqual(rec["like_count"], 3)
        self.assertEqual(rec["instance"], "mastodon.social")

    def test_returns_none_for_empty_content(self):
        raw = _make_status(content="<p></p>")
        self.assertIsNone(_extract_status(raw, "NVDA", "mastodon.social"))

    def test_returns_none_for_missing_id(self):
        raw = _make_status()
        raw["id"] = ""
        self.assertIsNone(_extract_status(raw, "NVDA", "mastodon.social"))

    def test_returns_none_for_non_dict(self):
        self.assertIsNone(_extract_status("not_a_dict", "NVDA", "mastodon.social"))

    def test_text_capped_at_500(self):
        raw = _make_status(content="<p>" + "x" * 1000 + "</p>")
        rec = _extract_status(raw, "NVDA", "mastodon.social")
        self.assertLessEqual(len(rec["text"]), 500)

    def test_schema_version_is_2(self):
        rec = _extract_status(_make_status(), "AAPL", "mastodon.social")
        self.assertEqual(rec["schema_version"], "2")


class TestMastodonConnectorDisabled(unittest.TestCase):
    def _c(self, enabled=False, crowd=False):
        return MastodonConnector({"enabled": enabled, "instances": ["mastodon.social"]},
                                  crowd_radar_enabled=crowd)

    def test_health_disabled(self):
        self.assertEqual(self._c().health().status, SourceStatus.DISABLED)

    def test_fetch_disabled(self):
        self.assertEqual(self._c().fetch_for_ticker("NVDA").status, SourceStatus.DISABLED)

    def test_not_configured_no_instances(self):
        c = MastodonConnector({"enabled": True, "instances": []}, crowd_radar_enabled=True)
        self.assertFalse(c.is_configured())


class TestMastodonConnectorEnabled(unittest.TestCase):
    def _connector(self, http_fn, instances=None, delay=0):
        return MastodonConnector(
            {"enabled": True, "instances": instances or ["mastodon.social"], "polite_delay_s": delay},
            crowd_radar_enabled=True,
            http_get=http_fn,
            sleep=lambda _: None,
        )

    def test_probe_ok_on_list_response(self):
        c = self._connector(lambda u: [_make_status()])
        r = c.probe()
        self.assertEqual(r.status, SourceStatus.OK)

    def test_probe_error_on_exception(self):
        c = self._connector(lambda u: (_ for _ in ()).throw(ConnectionError("fail")))
        r = c.probe()
        self.assertEqual(r.status, SourceStatus.ERROR)

    def test_fetch_for_ticker_with_matching_content(self):
        statuses = [
            _make_status(post_id="1", content="<p>$NVDA is bullish!</p>"),
            _make_status(post_id="2", content="<p>NVDA beats earnings!</p>"),
        ]
        # search returns dict with statuses key
        def http_fn(url):
            if "api/v2/search" in url:
                return {"statuses": statuses}
            return statuses  # timeline fallback
        c = self._connector(http_fn)
        r = c.fetch_for_ticker("NVDA")
        self.assertIn(r.status, (SourceStatus.OK, SourceStatus.DEGRADED, SourceStatus.INSUFFICIENT_DATA))

    def test_fetch_for_ticker_deduplicates_same_id(self):
        status = _make_status(post_id="dup123", content="<p>$NVDA up!</p>")
        def http_fn(url):
            if "api/v2/search" in url:
                return {"statuses": [status]}
            return []
        c = self._connector(http_fn)
        r = c.fetch_for_ticker("NVDA", company_name="Nvidia")
        # Same post_id should not appear twice
        ids = [rec["post_id_hash"] for rec in r.records]
        self.assertEqual(len(ids), len(set(ids)))

    def test_fetch_falls_back_on_search_failure(self):
        """When search API fails, fallback to hashtag timeline."""
        calls = []
        def http_fn(url):
            calls.append(url)
            if "api/v2/search" in url:
                raise ConnectionError("search unavailable")
            return [_make_status(content="<p>$NVDA good</p>")]
        c = self._connector(http_fn)
        # Should not raise; tries fallback
        r = c.fetch_for_ticker("NVDA")
        self.assertIsNotNone(r)

    def test_multiple_instances_tried(self):
        called_instances = []
        def http_fn(url):
            for inst in ["mastodon.social", "fosstodon.org"]:
                if inst in url:
                    called_instances.append(inst)
            return [_make_status(content="<p>$NVDA great!</p>")]
        c = self._connector(http_fn, instances=["mastodon.social", "fosstodon.org"])
        c.fetch_for_ticker("NVDA")
        self.assertIn("mastodon.social", called_instances)
        self.assertIn("fosstodon.org", called_instances)


class TestMastodonNoScrapingPolicy(unittest.TestCase):
    def test_no_html_scraping_in_connector(self):
        import inspect
        import portfolio_automation.social_sources.mastodon_connector as mod
        src = inspect.getsource(mod)
        for forbidden in ("selenium", "playwright", "beautifulsoup", "lxml.html", "requests_html"):
            self.assertNotIn(forbidden, src.lower())
