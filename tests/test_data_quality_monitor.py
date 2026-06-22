"""
Tests for portfolio_automation.data_quality_monitor.

Contracts verified:
- empty records produce available=False report
- healthy record (fresh price, fundamentals, news) produces no issues
- missing price produces critical issue MISSING_PRICE
- stale quote (quote_age_minutes > threshold) produces STALE_PRICE warning
- stale price (data_quality='partial') produces STALE_PRICE warning
- cache-only (data_quality='cached') produces CACHE_ONLY warning
- fallback usage (data_mode='fallback') produces FALLBACK_USED warning
- missing fundamentals produces MISSING_FUNDAMENTALS warning
- missing news produces MISSING_NEWS info
- mixed source (data_mode='mixed') produces MIXED_SOURCE info
- source error field produces SOURCE_ERROR warning
- unknown source produces UNKNOWN_SOURCE warning
- excessive fallback rate exceeds threshold → EXCESSIVE_FALLBACK_RATE warning
- excessive missing price rate exceeds threshold → EXCESSIVE_MISSING_PRICE_RATE critical
- flexible ticker/symbol field handling
- missing/extra fields are tolerated without raising
- summary healthy/warning/critical counts are correct
- observe_only is always True
- JSON artifact written to outputs/latest/ (OutputNamespace.LATEST)
- MD artifact written to outputs/latest/
- no backtest/live/policy output path used
- JSON artifact is structurally valid
- markdown artifact contains summary line
- non-dict records in list are skipped gracefully
- multiple issues accumulate per symbol
- DataQualityConfig thresholds are respected
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from portfolio_automation.data_quality_monitor import (
    ISSUE_CACHE_ONLY,
    ISSUE_DEGRADED_MODE,
    ISSUE_EXCESSIVE_FALLBACK_RATE,
    ISSUE_EXCESSIVE_MISSING_PRICE_RATE,
    ISSUE_FALLBACK_USED,
    ISSUE_INSUFFICIENT_DATA,
    ISSUE_MISSING_FUNDAMENTALS,
    ISSUE_MISSING_NEWS,
    ISSUE_MISSING_PRICE,
    ISSUE_MIXED_SOURCE,
    ISSUE_SOURCE_ERROR,
    ISSUE_STALE_PRICE,
    ISSUE_UNKNOWN_SOURCE,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    DataQualityConfig,
    DataQualityIssue,
    DataQualitySummary,
    DataQualitySymbolReport,
    build_data_quality_markdown,
    evaluate_data_quality,
    summary_to_dict,
    write_data_quality_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _healthy_record(ticker: str = "AAPL") -> dict:
    """A record that should produce no issues."""
    return {
        "ticker": ticker,
        "price": 150.0,
        "data_quality": "fresh",
        "data_mode": "live",
        "fundamentals": {"sector": "Technology", "market_cap": 3_000_000_000},
        "news": {"headline_count": 5, "avg_sentiment": 0.3},
        "news_count": 5,
    }


def _issues_of_type(summary: DataQualitySummary, issue_type: str) -> list[DataQualityIssue]:
    result = list(summary.issues)
    for r in summary.symbols:
        result.extend(r.issues)
    return [i for i in result if i.issue_type == issue_type]


# ---------------------------------------------------------------------------
# Empty / insufficient data
# ---------------------------------------------------------------------------

class TestEmptyRecords(unittest.TestCase):

    def test_empty_list_returns_valid_summary(self):
        summary = evaluate_data_quality([])
        self.assertIsInstance(summary, DataQualitySummary)

    def test_empty_list_available_false(self):
        summary = evaluate_data_quality([])
        self.assertFalse(summary.available)

    def test_empty_list_observe_only_true(self):
        summary = evaluate_data_quality([])
        self.assertTrue(summary.observe_only)

    def test_empty_list_totals_zero(self):
        summary = evaluate_data_quality([])
        self.assertEqual(summary.total_symbols, 0)
        self.assertEqual(summary.healthy_symbols, 0)
        self.assertEqual(summary.critical_symbols, 0)

    def test_empty_list_has_insufficient_data_issue(self):
        summary = evaluate_data_quality([])
        types = [i.issue_type for i in summary.issues]
        self.assertIn(ISSUE_INSUFFICIENT_DATA, types)

    def test_empty_list_summary_line_mentions_insufficient(self):
        summary = evaluate_data_quality([])
        self.assertIn("insufficient", summary.summary_line.lower())

    def test_non_dict_records_skipped(self):
        summary = evaluate_data_quality(["not_a_dict", 42, None])  # type: ignore
        self.assertEqual(summary.total_symbols, 0)


# ---------------------------------------------------------------------------
# Healthy records
# ---------------------------------------------------------------------------

class TestHealthyRecords(unittest.TestCase):

    def test_healthy_record_no_issues(self):
        summary = evaluate_data_quality([_healthy_record()])
        sym = summary.symbols[0]
        self.assertEqual(len(sym.issues), 0)

    def test_healthy_record_counted_as_healthy(self):
        summary = evaluate_data_quality([_healthy_record()])
        self.assertEqual(summary.healthy_symbols, 1)
        self.assertEqual(summary.warning_symbols, 0)
        self.assertEqual(summary.critical_symbols, 0)

    def test_healthy_summary_line(self):
        summary = evaluate_data_quality([_healthy_record()])
        self.assertIn("healthy", summary.summary_line.lower())

    def test_multiple_healthy_records(self):
        records = [_healthy_record(t) for t in ("AAPL", "MSFT", "NVDA")]
        summary = evaluate_data_quality(records)
        self.assertEqual(summary.healthy_symbols, 3)
        self.assertEqual(summary.total_symbols, 3)


# ---------------------------------------------------------------------------
# Missing price — critical
# ---------------------------------------------------------------------------

class TestMissingPrice(unittest.TestCase):

    def test_none_price_raises_critical(self):
        record = {"ticker": "XYZ", "price": None}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_MISSING_PRICE)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, SEVERITY_CRITICAL)

    def test_zero_price_raises_critical(self):
        record = {"ticker": "XYZ", "price": 0}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_MISSING_PRICE)
        self.assertEqual(len(issues), 1)

    def test_missing_price_key_raises_critical(self):
        record = {"ticker": "XYZ"}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_MISSING_PRICE)
        self.assertEqual(len(issues), 1)

    def test_missing_price_counted(self):
        records = [{"ticker": "A", "price": None}, _healthy_record("B")]
        summary = evaluate_data_quality(records)
        self.assertEqual(summary.missing_price_count, 1)

    def test_missing_price_symbol_counted_as_critical(self):
        summary = evaluate_data_quality([{"ticker": "XYZ", "price": None}])
        self.assertEqual(summary.critical_symbols, 1)
        self.assertEqual(summary.healthy_symbols, 0)


# ---------------------------------------------------------------------------
# Stale price
# ---------------------------------------------------------------------------

class TestStalePrice(unittest.TestCase):

    def test_quote_age_over_threshold_warning(self):
        record = {"ticker": "AAPL", "price": 150.0, "quote_age_minutes": 2000}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_STALE_PRICE)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, SEVERITY_WARNING)

    def test_quote_age_under_threshold_no_stale(self):
        record = {"ticker": "AAPL", "price": 150.0, "quote_age_minutes": 60}
        summary = evaluate_data_quality([record])
        stale_issues = _issues_of_type(summary, ISSUE_STALE_PRICE)
        self.assertEqual(len(stale_issues), 0)

    def test_data_quality_partial_raises_stale(self):
        record = {"ticker": "AAPL", "price": 150.0, "data_quality": "partial"}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_STALE_PRICE)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, SEVERITY_WARNING)

    def test_custom_threshold_respected(self):
        cfg = DataQualityConfig(stale_quote_minutes=30)
        record = {"ticker": "AAPL", "price": 150.0, "quote_age_minutes": 60}
        summary = evaluate_data_quality([record], config=cfg)
        issues = _issues_of_type(summary, ISSUE_STALE_PRICE)
        self.assertEqual(len(issues), 1)

    def test_stale_count_tracked(self):
        record = {"ticker": "AAPL", "price": 150.0, "data_quality": "partial"}
        summary = evaluate_data_quality([record])
        self.assertEqual(summary.stale_price_count, 1)


# ---------------------------------------------------------------------------
# Cache-only
# ---------------------------------------------------------------------------

class TestCacheOnly(unittest.TestCase):

    def test_data_quality_cached_raises_cache_only(self):
        record = {"ticker": "AAPL", "price": 150.0, "data_quality": "cached"}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_CACHE_ONLY)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, SEVERITY_WARNING)

    def test_cached_count_tracked(self):
        record = {"ticker": "AAPL", "price": 150.0, "data_quality": "cached"}
        summary = evaluate_data_quality([record])
        self.assertEqual(summary.cached_count, 1)

    def test_fresh_does_not_raise_cache_only(self):
        record = {"ticker": "AAPL", "price": 150.0, "data_quality": "fresh"}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_CACHE_ONLY)
        self.assertEqual(len(issues), 0)

    def test_cached_not_also_stale_without_age_info(self):
        # cached without quote_age_minutes should produce CACHE_ONLY, not STALE_PRICE
        record = {"ticker": "AAPL", "price": 150.0, "data_quality": "cached"}
        summary = evaluate_data_quality([record])
        stale_issues = _issues_of_type(summary, ISSUE_STALE_PRICE)
        self.assertEqual(len(stale_issues), 0)


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

class TestFallbackUsed(unittest.TestCase):

    def test_data_mode_fallback_raises_warning(self):
        record = {"ticker": "AAPL", "price": 150.0, "data_mode": "fallback", "data_quality": "fresh"}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_FALLBACK_USED)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, SEVERITY_WARNING)

    def test_fallback_used_field_true_raises_warning(self):
        record = {"ticker": "AAPL", "price": 150.0, "fallback_used": True}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_FALLBACK_USED)
        self.assertEqual(len(issues), 1)

    def test_fallback_reason_triggers_warning(self):
        record = {"ticker": "AAPL", "price": 150.0, "fallback_reason": "FMP timeout"}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_FALLBACK_USED)
        self.assertEqual(len(issues), 1)

    def test_fallback_count_tracked(self):
        records = [
            {"ticker": "A", "price": 100.0, "fallback_used": True},
            _healthy_record("B"),
        ]
        summary = evaluate_data_quality(records)
        self.assertEqual(summary.fallback_count, 1)


# ---------------------------------------------------------------------------
# Empty-string fallback_reason (real scanner shape) — must NOT flag fallback
# ---------------------------------------------------------------------------

def _scanner_shape_record(ticker: str = "AAPL", fallback_reason: str = "") -> dict:
    """Mirror watchlist_scanner.scanner per-symbol result shape for a *live* symbol.

    The scanner emits ``fallback_reason: ""`` (empty string, not None) when no
    fallback occurred (watchlist_scanner/scanner.py:1165). A live, healthy
    symbol must not be reported as having used a fallback source.
    """
    return {
        "ticker": ticker,
        "price": 150.0,
        "data_quality": "fresh",
        "data_mode": "live",
        "price_data_source": "fmp",
        "fundamentals": {"sector": "Technology", "market_cap": 3_000_000_000},
        "fundamentals_source": "fmp",
        "news": {"headline_count": 5},
        "news_count": 5,
        "fallback_used": False,
        "fallback_reason": fallback_reason,
    }


class TestEmptyStringFallbackReason(unittest.TestCase):
    """Regression: empty-string fallback_reason must not be treated as fallback.

    Root cause: data_quality_monitor used ``fallback_reason is not None``, but
    the scanner uses ``""`` (not None) as its no-fallback sentinel, so every
    live symbol was spuriously flagged FALLBACK_USED — cascading to
    EXCESSIVE_FALLBACK_RATE and DEGRADED_MODE across the whole universe.
    """

    def test_empty_string_fallback_reason_not_flagged(self):
        summary = evaluate_data_quality([_scanner_shape_record()])
        issues = _issues_of_type(summary, ISSUE_FALLBACK_USED)
        self.assertEqual(len(issues), 0)

    def test_whitespace_fallback_reason_not_flagged(self):
        summary = evaluate_data_quality([_scanner_shape_record(fallback_reason="   ")])
        issues = _issues_of_type(summary, ISSUE_FALLBACK_USED)
        self.assertEqual(len(issues), 0)

    def test_scanner_shape_symbol_is_healthy(self):
        summary = evaluate_data_quality([_scanner_shape_record()])
        self.assertEqual(summary.healthy_symbols, 1)
        self.assertEqual(summary.fallback_count, 0)

    def test_live_universe_no_spurious_aggregate_warnings(self):
        records = [_scanner_shape_record(t) for t in ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL")]
        summary = evaluate_data_quality(records)
        agg_types = [i.issue_type for i in summary.issues]
        self.assertNotIn(ISSUE_EXCESSIVE_FALLBACK_RATE, agg_types)
        self.assertNotIn(ISSUE_DEGRADED_MODE, agg_types)
        self.assertEqual(summary.warning_symbols, 0)
        self.assertEqual(summary.healthy_symbols, 5)

    def test_nonempty_fallback_reason_still_flags(self):
        """A genuine fallback reason must still be detected (no over-correction)."""
        rec = _scanner_shape_record(fallback_reason="AV OHLCV budget exhausted")
        summary = evaluate_data_quality([rec])
        issues = _issues_of_type(summary, ISSUE_FALLBACK_USED)
        self.assertEqual(len(issues), 1)


# ---------------------------------------------------------------------------
# Missing fundamentals
# ---------------------------------------------------------------------------

class TestMissingFundamentals(unittest.TestCase):

    def test_none_fundamentals_raises_warning(self):
        record = {"ticker": "AAPL", "price": 150.0, "data_quality": "fresh",
                  "fundamentals": None}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_MISSING_FUNDAMENTALS)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, SEVERITY_WARNING)

    def test_absent_fundamentals_key_raises_warning(self):
        record = {"ticker": "AAPL", "price": 150.0}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_MISSING_FUNDAMENTALS)
        self.assertEqual(len(issues), 1)

    def test_empty_fundamentals_dict_raises_warning(self):
        record = {"ticker": "AAPL", "price": 150.0,
                  "fundamentals": {"sector": None, "market_cap": None}}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_MISSING_FUNDAMENTALS)
        self.assertEqual(len(issues), 1)

    def test_populated_fundamentals_no_warning(self):
        record = {"ticker": "AAPL", "price": 150.0,
                  "fundamentals": {"sector": "Technology", "market_cap": 3_000_000_000}}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_MISSING_FUNDAMENTALS)
        self.assertEqual(len(issues), 0)

    def test_missing_fundamentals_count_tracked(self):
        records = [
            {"ticker": "A", "price": 100.0, "fundamentals": None},
            _healthy_record("B"),
        ]
        summary = evaluate_data_quality(records)
        self.assertEqual(summary.missing_fundamentals_count, 1)


# ---------------------------------------------------------------------------
# Missing news
# ---------------------------------------------------------------------------

class TestMissingNews(unittest.TestCase):

    def test_zero_news_count_raises_info(self):
        record = {"ticker": "AAPL", "price": 150.0, "news_count": 0}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_MISSING_NEWS)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, SEVERITY_INFO)

    def test_absent_news_key_raises_info(self):
        record = {"ticker": "AAPL", "price": 150.0, "fundamentals": {"sector": "Tech"}}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_MISSING_NEWS)
        self.assertEqual(len(issues), 1)

    def test_news_dict_with_zero_headlines_raises_info(self):
        record = {"ticker": "AAPL", "price": 150.0,
                  "news": {"headline_count": 0, "avg_sentiment": 0.0}}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_MISSING_NEWS)
        self.assertEqual(len(issues), 1)

    def test_news_count_positive_no_info(self):
        record = {"ticker": "AAPL", "price": 150.0, "news_count": 3,
                  "news": {"headline_count": 3}}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_MISSING_NEWS)
        self.assertEqual(len(issues), 0)

    def test_missing_news_count_tracked(self):
        records = [
            {"ticker": "A", "price": 100.0, "news_count": 0},
            _healthy_record("B"),
        ]
        summary = evaluate_data_quality(records)
        self.assertEqual(summary.missing_news_count, 1)


# ---------------------------------------------------------------------------
# Mixed source
# ---------------------------------------------------------------------------

class TestMixedSource(unittest.TestCase):

    def test_data_mode_mixed_raises_info(self):
        record = {"ticker": "AAPL", "price": 150.0, "data_mode": "mixed", "data_quality": "fresh"}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_MIXED_SOURCE)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, SEVERITY_INFO)

    def test_data_mode_live_no_mixed_issue(self):
        record = {"ticker": "AAPL", "price": 150.0, "data_mode": "live"}
        summary = evaluate_data_quality([record])
        mixed_issues = _issues_of_type(summary, ISSUE_MIXED_SOURCE)
        self.assertEqual(len(mixed_issues), 0)


# ---------------------------------------------------------------------------
# Source error
# ---------------------------------------------------------------------------

class TestSourceError(unittest.TestCase):

    def test_error_field_raises_warning(self):
        record = {"ticker": "AAPL", "price": 150.0, "error": "FMP timeout"}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_SOURCE_ERROR)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, SEVERITY_WARNING)

    def test_warning_field_raises_source_error(self):
        record = {"ticker": "AAPL", "price": 150.0, "warning": "rate limited"}
        summary = evaluate_data_quality([record])
        issues = _issues_of_type(summary, ISSUE_SOURCE_ERROR)
        self.assertEqual(len(issues), 1)


# ---------------------------------------------------------------------------
# Unknown source
# ---------------------------------------------------------------------------

class TestUnknownSource(unittest.TestCase):

    def test_no_source_fields_with_price_raises_unknown(self):
        # price present, no data_quality / data_mode / price_data_source
        record = {"ticker": "AAPL", "price": 150.0}
        summary = evaluate_data_quality([record])
        unknown_issues = _issues_of_type(summary, ISSUE_UNKNOWN_SOURCE)
        self.assertEqual(len(unknown_issues), 1)
        self.assertEqual(unknown_issues[0].severity, SEVERITY_WARNING)

    def test_known_source_field_suppresses_unknown(self):
        record = {"ticker": "AAPL", "price": 150.0, "data_quality": "fresh"}
        summary = evaluate_data_quality([record])
        unknown_issues = _issues_of_type(summary, ISSUE_UNKNOWN_SOURCE)
        self.assertEqual(len(unknown_issues), 0)


# ---------------------------------------------------------------------------
# Aggregate issues
# ---------------------------------------------------------------------------

class TestAggressiveRates(unittest.TestCase):

    def test_excessive_fallback_rate_warning(self):
        """>30% fallback rate triggers aggregate EXCESSIVE_FALLBACK_RATE warning."""
        records = [
            {"ticker": "A", "price": 100.0, "fallback_used": True},
            {"ticker": "B", "price": 100.0, "fallback_used": True},
            _healthy_record("C"),
            _healthy_record("D"),
            _healthy_record("E"),
        ]
        summary = evaluate_data_quality(records)
        agg = [i for i in summary.issues if i.issue_type == ISSUE_EXCESSIVE_FALLBACK_RATE]
        self.assertEqual(len(agg), 1)
        self.assertEqual(agg[0].severity, SEVERITY_WARNING)

    def test_fallback_rate_below_threshold_no_aggregate(self):
        """<30% fallback rate does not trigger aggregate."""
        records = [
            {"ticker": "A", "price": 100.0, "fallback_used": True},
            _healthy_record("B"),
            _healthy_record("C"),
            _healthy_record("D"),
            _healthy_record("E"),
        ]
        summary = evaluate_data_quality(records)
        agg = [i for i in summary.issues if i.issue_type == ISSUE_EXCESSIVE_FALLBACK_RATE]
        self.assertEqual(len(agg), 0)

    def test_excessive_missing_price_rate_critical(self):
        """Missing price rate >10% triggers aggregate EXCESSIVE_MISSING_PRICE_RATE critical."""
        records = [{"ticker": f"X{i}", "price": None} for i in range(2)]
        records += [_healthy_record(f"H{i}") for i in range(8)]
        summary = evaluate_data_quality(records)
        agg = [i for i in summary.issues if i.issue_type == ISSUE_EXCESSIVE_MISSING_PRICE_RATE]
        self.assertEqual(len(agg), 1)
        self.assertEqual(agg[0].severity, SEVERITY_CRITICAL)

    def test_missing_price_rate_below_threshold_no_aggregate(self):
        """<10% missing price rate does not trigger aggregate."""
        records = [{"ticker": "X0", "price": None}]
        records += [_healthy_record(f"H{i}") for i in range(15)]
        summary = evaluate_data_quality(records)
        agg = [i for i in summary.issues if i.issue_type == ISSUE_EXCESSIVE_MISSING_PRICE_RATE]
        self.assertEqual(len(agg), 0)

    def test_custom_fallback_threshold_respected(self):
        cfg = DataQualityConfig(max_fallback_rate_warning=0.10)
        records = [
            {"ticker": "A", "price": 100.0, "fallback_used": True},
            _healthy_record("B"),
            _healthy_record("C"),
            _healthy_record("D"),
            _healthy_record("E"),
        ]
        summary = evaluate_data_quality(records, config=cfg)
        agg = [i for i in summary.issues if i.issue_type == ISSUE_EXCESSIVE_FALLBACK_RATE]
        self.assertEqual(len(agg), 1)


# ---------------------------------------------------------------------------
# Field flexibility
# ---------------------------------------------------------------------------

class TestFieldFlexibility(unittest.TestCase):

    def test_ticker_field_used(self):
        record = {"ticker": "AAPL", "price": 150.0, "data_quality": "fresh",
                  "fundamentals": {"sector": "Tech"}, "news_count": 3}
        summary = evaluate_data_quality([record])
        self.assertEqual(summary.symbols[0].symbol, "AAPL")

    def test_symbol_field_used_when_no_ticker(self):
        record = {"symbol": "MSFT", "price": 200.0, "data_quality": "fresh",
                  "fundamentals": {"sector": "Tech"}, "news_count": 3}
        summary = evaluate_data_quality([record])
        self.assertEqual(summary.symbols[0].symbol, "MSFT")

    def test_ticker_preferred_over_symbol(self):
        record = {"ticker": "AAPL", "symbol": "WRONG", "price": 150.0,
                  "data_quality": "fresh", "fundamentals": {"sector": "Tech"},
                  "news_count": 3}
        summary = evaluate_data_quality([record])
        self.assertEqual(summary.symbols[0].symbol, "AAPL")

    def test_completely_empty_dict_tolerated(self):
        summary = evaluate_data_quality([{}])
        # Should not raise; produces a symbol report for UNKNOWN
        self.assertEqual(summary.total_symbols, 1)

    def test_extra_unknown_fields_tolerated(self):
        record = {"ticker": "AAPL", "price": 150.0, "data_quality": "fresh",
                  "fundamentals": {"sector": "Tech"}, "news_count": 3,
                  "an_unknown_future_field": "some_value",
                  "another_extra": 42}
        summary = evaluate_data_quality([record])
        self.assertEqual(summary.total_symbols, 1)

    def test_missing_all_fields_produces_report_not_exception(self):
        summary = evaluate_data_quality([{"completely": "unrelated"}])
        self.assertIsInstance(summary, DataQualitySummary)


# ---------------------------------------------------------------------------
# Summary counts
# ---------------------------------------------------------------------------

class TestSummaryCounts(unittest.TestCase):

    def setUp(self):
        records = [
            _healthy_record("H1"),
            _healthy_record("H2"),
            {"ticker": "W1", "price": 100.0, "data_quality": "cached"},  # CACHE_ONLY warning
            {"ticker": "C1", "price": None},  # MISSING_PRICE critical
        ]
        self.summary = evaluate_data_quality(records)

    def test_total_symbols_correct(self):
        self.assertEqual(self.summary.total_symbols, 4)

    def test_healthy_count_correct(self):
        self.assertEqual(self.summary.healthy_symbols, 2)

    def test_warning_count_correct(self):
        self.assertEqual(self.summary.warning_symbols, 1)

    def test_critical_count_correct(self):
        self.assertEqual(self.summary.critical_symbols, 1)

    def test_healthy_plus_warning_plus_critical_equals_total(self):
        s = self.summary
        self.assertEqual(
            s.healthy_symbols + s.warning_symbols + s.critical_symbols,
            s.total_symbols,
        )

    def test_observe_only_always_true(self):
        self.assertTrue(self.summary.observe_only)

    def test_available_true_when_records_present(self):
        self.assertTrue(self.summary.available)

    def test_generated_at_populated(self):
        self.assertIsNotNone(self.summary.generated_at)
        self.assertTrue(len(self.summary.generated_at) > 0)

    def test_custom_generated_at(self):
        summary = evaluate_data_quality([_healthy_record()], generated_at="2025-01-01T00:00:00Z")
        self.assertEqual(summary.generated_at, "2025-01-01T00:00:00Z")


# ---------------------------------------------------------------------------
# Source counts
# ---------------------------------------------------------------------------

class TestSourceCounts(unittest.TestCase):

    def test_source_counts_populated(self):
        records = [
            {"ticker": "A", "price": 150.0, "data_quality": "fresh"},
            {"ticker": "B", "price": 150.0, "data_quality": "fresh"},
            {"ticker": "C", "price": 150.0, "data_quality": "cached"},
        ]
        summary = evaluate_data_quality(records)
        self.assertIn("fresh", summary.source_counts)
        self.assertEqual(summary.source_counts["fresh"], 2)
        self.assertEqual(summary.source_counts["cached"], 1)


# ---------------------------------------------------------------------------
# Artifact writing
# ---------------------------------------------------------------------------

class TestWriteArtifacts(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, records=None):
        if records is None:
            records = [_healthy_record()]
        summary = evaluate_data_quality(records)
        return write_data_quality_report(summary, base_dir=self.base)

    def test_json_artifact_written_to_latest(self):
        json_path, _ = self._write()
        self.assertIn("latest", str(json_path))
        self.assertTrue(json_path.exists())

    def test_json_filename_correct(self):
        json_path, _ = self._write()
        self.assertEqual(json_path.name, "data_quality_report.json")

    def test_md_artifact_written_to_latest(self):
        _, md_path = self._write()
        self.assertIn("latest", str(md_path))
        self.assertTrue(md_path.exists())

    def test_md_filename_correct(self):
        _, md_path = self._write()
        self.assertEqual(md_path.name, "data_quality_report.md")

    def test_no_backtest_path_used(self):
        json_path, md_path = self._write()
        self.assertNotIn("backtest", str(json_path))
        self.assertNotIn("backtest", str(md_path))

    def test_no_live_path_used(self):
        json_path, md_path = self._write()
        self.assertNotIn("/live/", str(json_path))
        self.assertNotIn("/live/", str(md_path))

    def test_no_policy_path_used(self):
        json_path, md_path = self._write()
        self.assertNotIn("policy", str(json_path))
        self.assertNotIn("policy", str(md_path))

    def test_json_is_valid_and_has_required_keys(self):
        json_path, _ = self._write()
        data = json.loads(json_path.read_text())
        for key in (
            "generated_at", "observe_only", "available", "total_symbols",
            "healthy_symbols", "warning_symbols", "critical_symbols",
            "summary_line", "issues", "symbols",
        ):
            self.assertIn(key, data, f"Missing key: {key}")

    def test_json_observe_only_true(self):
        json_path, _ = self._write()
        data = json.loads(json_path.read_text())
        self.assertTrue(data["observe_only"])

    def test_md_contains_summary_line(self):
        summary = evaluate_data_quality([_healthy_record()])
        _, md_path = write_data_quality_report(summary, base_dir=self.base)
        content = md_path.read_text()
        self.assertIn(summary.summary_line, content)

    def test_md_contains_data_quality_header(self):
        _, md_path = self._write()
        content = md_path.read_text()
        self.assertIn("Data Quality Report", content)

    def test_md_contains_observe_only_note(self):
        _, md_path = self._write()
        content = md_path.read_text()
        self.assertIn("observe-only", content)

    def test_write_empty_records_produces_artifacts(self):
        summary = evaluate_data_quality([])
        json_path, md_path = write_data_quality_report(summary, base_dir=self.base)
        self.assertTrue(json_path.exists())
        self.assertTrue(md_path.exists())
        data = json.loads(json_path.read_text())
        self.assertFalse(data["available"])


# ---------------------------------------------------------------------------
# summary_to_dict
# ---------------------------------------------------------------------------

class TestSummaryToDict(unittest.TestCase):

    def test_summary_to_dict_returns_dict(self):
        summary = evaluate_data_quality([_healthy_record()])
        d = summary_to_dict(summary)
        self.assertIsInstance(d, dict)

    def test_symbols_key_is_list(self):
        summary = evaluate_data_quality([_healthy_record()])
        d = summary_to_dict(summary)
        self.assertIsInstance(d["symbols"], list)

    def test_issues_key_is_list(self):
        summary = evaluate_data_quality([_healthy_record()])
        d = summary_to_dict(summary)
        self.assertIsInstance(d["issues"], list)

    def test_symbol_entry_has_issue_count(self):
        record = {"ticker": "X", "price": None}
        summary = evaluate_data_quality([record])
        d = summary_to_dict(summary)
        sym = d["symbols"][0]
        self.assertIn("issue_count", sym)
        # Missing price also triggers missing-fundamentals and missing-news issues
        self.assertGreaterEqual(sym["issue_count"], 1)
        types = [i["issue_type"] for i in sym["issues"]]
        self.assertIn(ISSUE_MISSING_PRICE, types)


# ---------------------------------------------------------------------------
# Markdown builder
# ---------------------------------------------------------------------------

class TestMarkdownBuilder(unittest.TestCase):

    def test_markdown_contains_per_symbol_table(self):
        summary = evaluate_data_quality([_healthy_record("AAPL")])
        md = build_data_quality_markdown(summary)
        self.assertIn("AAPL", md)
        self.assertIn("Per-Symbol Table", md)

    def test_markdown_contains_issues_table_when_issues_exist(self):
        summary = evaluate_data_quality([{"ticker": "X", "price": None}])
        md = build_data_quality_markdown(summary)
        self.assertIn("Issues", md)
        self.assertIn("MISSING_PRICE", md)

    def test_markdown_observe_only_footer(self):
        summary = evaluate_data_quality([_healthy_record()])
        md = build_data_quality_markdown(summary)
        self.assertIn("observe-only", md)

    def test_markdown_empty_summary_no_crash(self):
        summary = evaluate_data_quality([])
        md = build_data_quality_markdown(summary)
        self.assertIsInstance(md, str)
        self.assertGreater(len(md), 0)


# ---------------------------------------------------------------------------
# Info-severity issues must not inflate the warning count (severity-bucket fix)
# ---------------------------------------------------------------------------

def _missing_news_only_record(ticker: str = "XLK") -> dict:
    """Price/fundamentals healthy; news empty. Only issue is info-severity MISSING_NEWS."""
    return {
        "ticker": ticker,
        "price": 150.0,
        "data_quality": "fresh",
        "data_mode": "live",
        "fundamentals": {"sector": "Technology", "market_cap": 1_000_000_000},
        "news": {"headline_count": 0, "avg_sentiment": 0.0},
        "news_count": 0,
    }


class TestInfoSeverityNotCountedAsWarning(unittest.TestCase):
    """A symbol whose only issue is info-severity (e.g. missing news) must not
    be reported as a warning. Regression guard for the '24 warnings' misreport
    where info-level MISSING_NEWS flags inflated warning_symbols."""

    def test_missing_news_only_is_not_a_warning(self):
        summary = evaluate_data_quality([_missing_news_only_record()])
        self.assertEqual(summary.warning_symbols, 0)

    def test_missing_news_only_counted_as_info(self):
        summary = evaluate_data_quality([_missing_news_only_record()])
        self.assertEqual(summary.info_symbols, 1)

    def test_missing_news_only_not_healthy(self):
        # It has an issue, so it is not "healthy", but it is not a warning either.
        summary = evaluate_data_quality([_missing_news_only_record()])
        self.assertEqual(summary.healthy_symbols, 0)

    def test_many_missing_news_no_warnings_in_summary_line(self):
        records = [_missing_news_only_record(t) for t in ("XLK", "XLF", "SPY", "QQQ")]
        summary = evaluate_data_quality(records)
        self.assertEqual(summary.warning_symbols, 0)
        self.assertNotIn("warning", summary.summary_line.lower())

    def test_real_warning_still_counted(self):
        # A genuine warning-severity issue (stale price) must still count.
        record = {
            "ticker": "ZZZ",
            "price": 100.0,
            "data_quality": "stale",
            "data_mode": "live",
            "quote_age_minutes": 9999,
            "fundamentals": {"sector": "Tech", "market_cap": 1_000_000_000},
            "news": {"headline_count": 3},
            "news_count": 3,
        }
        summary = evaluate_data_quality([record])
        self.assertEqual(summary.warning_symbols, 1)
        self.assertEqual(summary.info_symbols, 0)

    def test_warning_plus_info_buckets_disjoint(self):
        # One stale (warning) + two missing-news-only (info) + one healthy.
        stale = {
            "ticker": "ZZZ", "price": 100.0, "data_quality": "stale",
            "data_mode": "live", "quote_age_minutes": 9999,
            "fundamentals": {"sector": "Tech", "market_cap": 1_000_000_000},
            "news": {"headline_count": 3}, "news_count": 3,
        }
        records = [
            stale,
            _missing_news_only_record("XLK"),
            _missing_news_only_record("XLF"),
            _healthy_record("AAPL"),
        ]
        summary = evaluate_data_quality(records)
        self.assertEqual(summary.total_symbols, 4)
        self.assertEqual(summary.healthy_symbols, 1)
        self.assertEqual(summary.info_symbols, 2)
        self.assertEqual(summary.warning_symbols, 1)
        self.assertEqual(summary.critical_symbols, 0)
        # buckets partition the total
        self.assertEqual(
            summary.healthy_symbols + summary.info_symbols
            + summary.warning_symbols + summary.critical_symbols,
            summary.total_symbols,
        )

    def test_summary_dict_exposes_info_symbols(self):
        summary = evaluate_data_quality([_missing_news_only_record()])
        d = summary_to_dict(summary)
        self.assertIn("info_symbols", d)
        self.assertEqual(d["info_symbols"], 1)


if __name__ == "__main__":
    unittest.main()
