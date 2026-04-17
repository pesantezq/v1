"""
Tests for scraped_intel schema normalization and model behaviour.
"""

import unittest
from scraped_intel.models import ScrapedRecord, SoftSignals, IntelBundle


class TestScrapedRecord(unittest.TestCase):

    def _make_record(self, **kwargs):
        defaults = dict(
            symbol="NVDA",
            source_type="sec_filing",
            domain="sec.gov",
            url="https://sec.gov/test",
            published_at="2025-04-10T00:00:00Z",
            collected_at="2025-04-10T08:00:00Z",
            title="NVDA — 8-K (2025-04-10)",
            excerpt="",
            extraction_status="ok",
            parse_quality=1.0,
            themes=[],
            sentiment=None,
            recency_hours=48.0,
            record_id="abc123",
            extra={},
        )
        defaults.update(kwargs)
        return ScrapedRecord(**defaults)

    def test_make_record_id_stable(self):
        """Same inputs produce the same record_id."""
        id1 = ScrapedRecord.make_record_id("https://example.com", "Test Title", "2025-04-10")
        id2 = ScrapedRecord.make_record_id("https://example.com", "Test Title", "2025-04-10")
        self.assertEqual(id1, id2)

    def test_make_record_id_different_urls(self):
        """Different URLs produce different IDs."""
        id1 = ScrapedRecord.make_record_id("https://a.com", "Same Title", "2025-04-10")
        id2 = ScrapedRecord.make_record_id("https://b.com", "Same Title", "2025-04-10")
        self.assertNotEqual(id1, id2)

    def test_make_record_id_none_url(self):
        """None URL is handled without error."""
        rid = ScrapedRecord.make_record_id(None, "Title", "2025-04-10")
        self.assertIsInstance(rid, str)
        self.assertEqual(len(rid), 24)

    def test_record_id_length(self):
        rid = ScrapedRecord.make_record_id("https://sec.gov/x", "8-K", "2025-01-01")
        self.assertEqual(len(rid), 24)

    def test_record_fields_preserved(self):
        r = self._make_record(themes=["AI Infrastructure"], sentiment=0.25)
        self.assertEqual(r.symbol, "NVDA")
        self.assertEqual(r.themes, ["AI Infrastructure"])
        self.assertAlmostEqual(r.sentiment, 0.25)

    def test_hard_data_not_present(self):
        """ScrapedRecord must NOT have fields that belong to hard data."""
        r = self._make_record()
        hard_fields = {"signal_score", "confidence_score", "price", "fundamentals"}
        for f in hard_fields:
            self.assertFalse(
                hasattr(r, f),
                f"ScrapedRecord must not have hard-data field '{f}'"
            )


class TestSoftSignals(unittest.TestCase):

    def test_defaults_are_zero(self):
        s = SoftSignals(symbol="AMD", as_of_date="2025-04-10")
        self.assertEqual(s.headline_count_7d, 0)
        self.assertEqual(s.headline_count_30d, 0)
        self.assertEqual(s.source_count, 0)
        self.assertIsNone(s.avg_sentiment)
        self.assertEqual(s.theme_alignment_score, 0.0)
        self.assertEqual(s.mention_acceleration, 0.0)
        self.assertEqual(s.recency_score, 0.0)
        self.assertEqual(s.scraped_confidence, 0.0)
        self.assertEqual(s.evidence_items, [])

    def test_hard_data_fields_absent(self):
        s = SoftSignals(symbol="AAPL", as_of_date="2025-04-10")
        for f in ("signal_score", "confidence_score", "price", "fundamentals"):
            self.assertFalse(hasattr(s, f))


class TestIntelBundle(unittest.TestCase):

    def _make_bundle(self):
        return IntelBundle(symbol="TSLA", as_of_date="2025-04-10")

    def test_to_dict_no_signals(self):
        bundle = self._make_bundle()
        d = bundle.to_dict()
        self.assertIsNone(d["soft_signals"])
        self.assertEqual(d["records_count"], 0)
        self.assertEqual(d["scraped_confidence"], 0.0)

    def test_to_dict_with_signals(self):
        bundle = self._make_bundle()
        bundle.signals = SoftSignals(
            symbol="TSLA",
            as_of_date="2025-04-10",
            headline_count_7d=5,
            headline_count_30d=12,
            scraped_confidence=0.72,
        )
        d = bundle.to_dict()
        self.assertIsNotNone(d["soft_signals"])
        self.assertEqual(d["soft_signals"]["headline_count_7d"], 5)
        self.assertAlmostEqual(d["scraped_confidence"], 0.72)

    def test_to_dict_caps_evidence_items(self):
        """evidence_items is capped at 10 in to_dict() output."""
        bundle = self._make_bundle()
        bundle.signals = SoftSignals(
            symbol="TSLA",
            as_of_date="2025-04-10",
            evidence_items=[f"id_{i}" for i in range(25)],
        )
        d = bundle.to_dict()
        self.assertLessEqual(len(d["soft_signals"]["evidence_items"]), 10)

    def test_to_dict_no_hard_data_keys(self):
        """to_dict() must not include any hard-data field names."""
        bundle = self._make_bundle()
        bundle.signals = SoftSignals(symbol="TSLA", as_of_date="2025-04-10")
        d = bundle.to_dict()
        hard_keys = {"signal_score", "confidence_score", "price", "fundamentals", "technicals"}
        self.assertTrue(hard_keys.isdisjoint(set(d.keys())))
        if d.get("soft_signals"):
            self.assertTrue(hard_keys.isdisjoint(set(d["soft_signals"].keys())))


if __name__ == "__main__":
    unittest.main()
