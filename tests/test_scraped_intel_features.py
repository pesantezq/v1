"""
Tests for scraped_intel feature engineering (features.py + provenance.py).
"""

import unittest
from datetime import datetime, timezone
from scraped_intel.models import ScrapedRecord
from scraped_intel.features import compute_soft_signals, _HALF_LIFE_H
from scraped_intel.provenance import (
    compute_scraped_confidence,
    domain_weight,
    build_provenance_summary,
)


def _make_record(
    symbol="NVDA",
    domain="sec.gov",
    source_type="sec_filing",
    parse_quality=1.0,
    recency_hours=24.0,
    themes=None,
    sentiment=None,
):
    record_id = ScrapedRecord.make_record_id(f"https://{domain}/test", "Title", "2025-04-10")
    return ScrapedRecord(
        symbol=symbol,
        source_type=source_type,
        domain=domain,
        url=f"https://{domain}/test",
        published_at="2025-04-10T00:00:00Z",
        collected_at="2025-04-10T08:00:00Z",
        title="Test filing",
        excerpt="",
        extraction_status="ok",
        parse_quality=parse_quality,
        themes=themes or [],
        sentiment=sentiment,
        recency_hours=recency_hours,
        record_id=record_id,
        extra={},
    )


class TestComputeSoftSignals(unittest.TestCase):

    def test_empty_records_returns_zero_state(self):
        signals = compute_soft_signals("NVDA", [])
        self.assertEqual(signals.headline_count_7d, 0)
        self.assertEqual(signals.headline_count_30d, 0)
        self.assertEqual(signals.source_count, 0)
        self.assertIsNone(signals.avg_sentiment)
        self.assertEqual(signals.recency_score, 0.0)

    def test_headline_counts_correct_7d(self):
        records = [
            _make_record(recency_hours=12),    # within 7d (168h)
            _make_record(recency_hours=100),   # within 7d
            _make_record(recency_hours=200),   # within 30d, beyond 7d
        ]
        # Give each unique record_id
        for i, r in enumerate(records):
            r.record_id = f"id_{i}"
        signals = compute_soft_signals("NVDA", records)
        self.assertEqual(signals.headline_count_7d, 2)
        self.assertEqual(signals.headline_count_30d, 3)

    def test_source_diversity(self):
        records = [
            _make_record(domain="sec.gov"),
            _make_record(domain="reuters.com"),
            _make_record(domain="reuters.com"),   # duplicate domain
        ]
        for i, r in enumerate(records):
            r.record_id = f"id_{i}"
        signals = compute_soft_signals("NVDA", records)
        self.assertEqual(signals.source_count, 2)

    def test_avg_sentiment_computed(self):
        records = [
            _make_record(sentiment=0.5, recency_hours=10),
            _make_record(sentiment=-0.2, recency_hours=10),
            _make_record(sentiment=None, recency_hours=10),   # excluded
        ]
        for i, r in enumerate(records):
            r.record_id = f"id_{i}"
        signals = compute_soft_signals("NVDA", records)
        # mean of (0.5, -0.2) = 0.15
        self.assertIsNotNone(signals.avg_sentiment)
        self.assertAlmostEqual(signals.avg_sentiment, 0.15, places=3)

    def test_sentiment_none_when_no_scores(self):
        records = [_make_record(sentiment=None)]
        records[0].record_id = "id_0"
        signals = compute_soft_signals("NVDA", records)
        self.assertIsNone(signals.avg_sentiment)

    def test_theme_alignment_full_match(self):
        records = [
            _make_record(themes=["AI Infrastructure"]),
            _make_record(themes=["Semiconductors"]),
        ]
        for i, r in enumerate(records):
            r.record_id = f"id_{i}"
        signals = compute_soft_signals(
            "NVDA", records, known_themes=["AI Infrastructure", "Semiconductors"]
        )
        self.assertAlmostEqual(signals.theme_alignment_score, 1.0)

    def test_theme_alignment_partial_match(self):
        records = [
            _make_record(themes=["AI Infrastructure"]),
            _make_record(themes=[]),   # no theme match
        ]
        for i, r in enumerate(records):
            r.record_id = f"id_{i}"
        signals = compute_soft_signals(
            "NVDA", records, known_themes=["AI Infrastructure"]
        )
        self.assertAlmostEqual(signals.theme_alignment_score, 0.5)

    def test_mention_acceleration_positive_when_recent(self):
        """More 7d records than steady state → positive acceleration."""
        # 8 records in 7d window, only 8 total in 30d → 8 / (8/4) - 1 = 3 → capped 1.0
        records = [_make_record(recency_hours=100) for _ in range(8)]
        for i, r in enumerate(records):
            r.record_id = f"id_{i}"
        signals = compute_soft_signals("NVDA", records)
        self.assertGreater(signals.mention_acceleration, 0)

    def test_mention_acceleration_negative_when_fading(self):
        """Mostly old records → negative acceleration."""
        records = (
            [_make_record(recency_hours=500) for _ in range(7)]   # old
            + [_make_record(recency_hours=100) for _ in range(1)]  # 1 recent
        )
        for i, r in enumerate(records):
            r.record_id = f"id_{i}"
        signals = compute_soft_signals("NVDA", records)
        self.assertLess(signals.mention_acceleration, 0)

    def test_recency_score_bounded(self):
        records = [_make_record(recency_hours=h) for h in [1, 24, 72, 168, 500]]
        for i, r in enumerate(records):
            r.record_id = f"id_{i}"
        signals = compute_soft_signals("NVDA", records)
        self.assertGreaterEqual(signals.recency_score, 0.0)
        self.assertLessEqual(signals.recency_score, 1.0)

    def test_recency_score_fresher_higher(self):
        """Fresher records produce a higher recency score."""
        fresh = [_make_record(recency_hours=6)]
        stale = [_make_record(recency_hours=600)]
        fresh[0].record_id = "fresh_id"
        stale[0].record_id = "stale_id"
        s_fresh = compute_soft_signals("NVDA", fresh)
        s_stale = compute_soft_signals("NVDA", stale)
        self.assertGreater(s_fresh.recency_score, s_stale.recency_score)

    def test_evidence_items_populated(self):
        records = [_make_record()]
        records[0].record_id = "test_evidence_id"
        signals = compute_soft_signals("NVDA", records)
        self.assertIn("test_evidence_id", signals.evidence_items)


class TestDomainWeight(unittest.TestCase):

    def test_sec_is_highest(self):
        self.assertEqual(domain_weight("sec.gov"), 1.0)

    def test_reuters_high(self):
        self.assertGreater(domain_weight("reuters.com"), 0.8)

    def test_unknown_domain_gets_default(self):
        w = domain_weight("randomsite.xyz")
        self.assertGreater(w, 0)
        self.assertLessEqual(w, 0.6)

    def test_case_insensitive(self):
        # domain_weight lowercases internally
        self.assertEqual(domain_weight("SEC.GOV"), domain_weight("sec.gov"))

    def test_subdomain_fallback(self):
        # finance.yahoo.com should match yahoo.com entry
        w = domain_weight("finance.yahoo.com")
        self.assertGreater(w, 0)


class TestComputeScrapedConfidence(unittest.TestCase):

    def test_empty_records_returns_zero(self):
        self.assertEqual(compute_scraped_confidence([]), 0.0)

    def test_single_sec_record_high_confidence(self):
        r = _make_record(domain="sec.gov", parse_quality=1.0)
        conf = compute_scraped_confidence([r])
        # 0.70 × (1.0 × 1.0) + 0.30 × (1/8) = 0.70 + 0.0375 = 0.7375
        self.assertGreater(conf, 0.7)

    def test_low_quality_source_lower_confidence(self):
        r_sec = _make_record(domain="sec.gov", parse_quality=1.0)
        r_low = _make_record(domain="randomsite.xyz", parse_quality=0.3)
        r_low.record_id = "low_id"
        conf_sec = compute_scraped_confidence([r_sec])
        conf_low = compute_scraped_confidence([r_low])
        self.assertGreater(conf_sec, conf_low)

    def test_more_records_higher_confidence(self):
        """Count bonus: more records → higher scraped_confidence."""
        one = [_make_record(domain="reuters.com", parse_quality=0.8)]
        one[0].record_id = "id_0"
        many = [_make_record(domain="reuters.com", parse_quality=0.8) for _ in range(8)]
        for i, r in enumerate(many):
            r.record_id = f"id_{i}"
        conf_one = compute_scraped_confidence(one)
        conf_many = compute_scraped_confidence(many)
        self.assertGreater(conf_many, conf_one)

    def test_confidence_bounded(self):
        records = [_make_record(domain="sec.gov", parse_quality=1.0) for _ in range(20)]
        for i, r in enumerate(records):
            r.record_id = f"id_{i}"
        conf = compute_scraped_confidence(records)
        self.assertLessEqual(conf, 1.0)
        self.assertGreaterEqual(conf, 0.0)


class TestBuildProvenanceSummary(unittest.TestCase):

    def test_empty_records(self):
        summary = build_provenance_summary([])
        self.assertEqual(summary["record_count"], 0)
        self.assertEqual(summary["scraped_confidence"], 0.0)
        self.assertEqual(summary["sources"], [])

    def test_summary_contains_source_breakdown(self):
        records = [
            _make_record(domain="sec.gov"),
            _make_record(domain="reuters.com"),
        ]
        for i, r in enumerate(records):
            r.record_id = f"id_{i}"
        summary = build_provenance_summary(records)
        self.assertEqual(summary["record_count"], 2)
        domains = {s["domain"] for s in summary["sources"]}
        self.assertIn("sec.gov", domains)
        self.assertIn("reuters.com", domains)


if __name__ == "__main__":
    unittest.main()
