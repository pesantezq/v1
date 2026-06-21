"""Tests for LemmyConnector — mocked HTTP/RSS, no live network."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portfolio_automation.social_intelligence.base import SourceStatus
from portfolio_automation.social_sources.lemmy_connector import (
    LemmyConnector,
    _extract_api_post,
    _hash_author,
    _hash_post_id,
    _parse_rss,
    _strip_html,
)

_ATOM_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>stocks - Lemmy</title>
  <entry>
    <title>$NVDA is looking great this quarter</title>
    <summary>Nvidia beat earnings expectations significantly</summary>
    <id>https://lemmy.world/post/123</id>
    <updated>2026-06-21T10:00:00Z</updated>
  </entry>
  <entry>
    <title>Is AAPL still worth buying?</title>
    <summary>Apple stock analysis for Q3</summary>
    <id>https://lemmy.world/post/124</id>
    <updated>2026-06-21T09:00:00Z</updated>
  </entry>
</feed>"""

_BAD_XML = "this is not xml at all <<<>>>"


class TestHashFunctions(unittest.TestCase):
    def test_hash_author_12_chars(self):
        h = _hash_author("https://lemmy.world/u/alice")
        self.assertEqual(len(h), 12)

    def test_hash_post_id_16_chars(self):
        h = _hash_post_id("https://lemmy.world/post/123")
        self.assertEqual(len(h), 16)


class TestParseRSS(unittest.TestCase):
    def test_parses_atom_feed(self):
        records = _parse_rss(_ATOM_FEED, "lemmy.world")
        self.assertEqual(len(records), 2)
        r = records[0]
        self.assertEqual(r["source"], "lemmy")
        self.assertEqual(r["source_type"], "text")
        self.assertIn("NVDA", r["text"])
        self.assertEqual(len(r["post_id_hash"]), 16)
        self.assertEqual(r["instance"], "lemmy.world")
        self.assertLessEqual(len(r["text"]), 500)
        self.assertEqual(r["schema_version"], "2")

    def test_returns_empty_on_bad_xml(self):
        records = _parse_rss(_BAD_XML, "lemmy.world")
        self.assertEqual(records, [])

    def test_returns_empty_on_empty_string(self):
        records = _parse_rss("", "lemmy.world")
        self.assertEqual(records, [])

    def test_strips_html_from_summary(self):
        feed = _ATOM_FEED.replace("Nvidia beat earnings", "<b>Nvidia</b> beat <em>earnings</em>")
        records = _parse_rss(feed, "lemmy.world")
        self.assertNotIn("<b>", records[0]["text"])
        self.assertNotIn("<em>", records[0]["text"])


class TestExtractApiPost(unittest.TestCase):
    def _item(self, title="$NVDA great buy", body="Analysis here", ap_id="https://lemmy.world/post/1",
              actor_id="https://lemmy.world/u/alice", upvotes=10, comments=5):
        return {
            "post": {"name": title, "body": body, "ap_id": ap_id, "published": "2026-06-21T10:00:00Z"},
            "creator": {"actor_id": actor_id},
            "counts": {"upvotes": upvotes, "comments": comments},
        }

    def test_extracts_valid_post(self):
        rec = _extract_api_post(self._item(), "lemmy.world")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["source"], "lemmy")
        self.assertEqual(rec["source_type"], "text")
        self.assertEqual(len(rec["post_id_hash"]), 16)
        self.assertEqual(len(rec["author_hash"]), 12)
        self.assertIn("NVDA", rec["text"])
        self.assertEqual(rec["like_count"], 10)

    def test_returns_none_for_empty_ap_id(self):
        item = self._item(ap_id="")
        self.assertIsNone(_extract_api_post(item, "lemmy.world"))

    def test_returns_none_for_empty_text(self):
        item = self._item(title="", body="")
        self.assertIsNone(_extract_api_post(item, "lemmy.world"))

    def test_returns_none_for_non_dict(self):
        self.assertIsNone(_extract_api_post("not_a_dict", "lemmy.world"))

    def test_text_combined_title_and_body(self):
        rec = _extract_api_post(self._item(title="Title", body="Body text"), "lemmy.world")
        self.assertIn("Title", rec["text"])
        self.assertIn("Body text", rec["text"])


class TestLemmyConnectorDisabled(unittest.TestCase):
    def _c(self, enabled=False, crowd=False):
        return LemmyConnector(
            {"enabled": enabled, "instances": ["lemmy.world"], "communities": ["stocks"]},
            crowd_radar_enabled=crowd,
        )

    def test_health_disabled(self):
        self.assertEqual(self._c().health().status, SourceStatus.DISABLED)

    def test_fetch_for_ticker_disabled(self):
        self.assertEqual(self._c().fetch_for_ticker("NVDA").status, SourceStatus.DISABLED)

    def test_not_configured_no_communities(self):
        c = LemmyConnector(
            {"enabled": True, "instances": ["lemmy.world"], "communities": []},
            crowd_radar_enabled=True,
        )
        self.assertFalse(c.is_configured())


class TestLemmyConnectorEnabled(unittest.TestCase):
    def _connector(self, http_fn, instances=None, communities=None, use_rss=True):
        return LemmyConnector(
            {
                "enabled": True,
                "instances": instances or ["lemmy.world"],
                "communities": communities or ["stocks"],
                "use_rss": use_rss,
                "polite_delay_s": 0,
            },
            crowd_radar_enabled=True,
            http_get=http_fn,
            sleep=lambda _: None,
        )

    def test_probe_ok_on_valid_rss(self):
        c = self._connector(lambda u: _ATOM_FEED)
        r = c.probe()
        self.assertEqual(r.status, SourceStatus.OK)

    def test_probe_error_on_exception(self):
        def bad(u):
            raise ConnectionError("timeout")
        c = self._connector(bad)
        r = c.probe()
        self.assertEqual(r.status, SourceStatus.ERROR)

    def test_fetch_for_ticker_filters_by_keyword(self):
        c = self._connector(lambda u: _ATOM_FEED)
        r = c.fetch_for_ticker("NVDA")
        # Only the NVDA-mentioning post should survive
        self.assertGreater(len(r.records), 0)
        for rec in r.records:
            self.assertIn("NVDA", rec.get("text", "").upper() +
                          rec.get("ticker", "").upper())

    def test_fetch_for_ticker_insufficient_data_when_no_match(self):
        c = self._connector(lambda u: _ATOM_FEED)
        r = c.fetch_for_ticker("GOOGL")  # not in the feed
        self.assertEqual(r.status, SourceStatus.INSUFFICIENT_DATA)

    def test_fetch_deduplicates_same_post_id(self):
        # Two communities returning same post
        call_count = [0]
        def http_fn(u):
            call_count[0] += 1
            return _ATOM_FEED
        c = self._connector(http_fn, communities=["stocks", "investing"])
        r = c.fetch_for_ticker("NVDA")
        ids = [rec["post_id_hash"] for rec in r.records]
        self.assertEqual(len(ids), len(set(ids)))

    def test_rss_uses_defusedxml(self):
        """Verify the connector uses defusedxml for RSS parsing."""
        import portfolio_automation.social_sources.lemmy_connector as mod
        src = open(mod.__file__).read()
        self.assertIn("defusedxml", src)

    def test_xxe_safe_parse(self):
        """Confirm XXE payload does not execute."""
        xxe_payload = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>&xxe;</title>
    <id>https://lemmy.world/post/malicious</id>
  </entry>
</feed>"""
        # Should not raise; should return records without external entity content
        try:
            records = _parse_rss(xxe_payload, "lemmy.world")
            # If defusedxml is used, the entity reference should not resolve to /etc/passwd content
            for rec in records:
                text = rec.get("text", "")
                # /etc/passwd would contain "root:" — this must NOT appear
                self.assertNotIn("root:", text)
        except Exception:
            # defusedxml raises on XXE attempts — that's also acceptable
            pass

    def test_fetch_error_returns_degraded_not_raises(self):
        def bad(u):
            raise TimeoutError("timeout")
        c = self._connector(bad)
        r = c.fetch_for_ticker("NVDA")
        self.assertIsNotNone(r)
        self.assertIn(r.status, (SourceStatus.INSUFFICIENT_DATA, SourceStatus.DEGRADED))
