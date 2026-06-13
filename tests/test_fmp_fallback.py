"""
Tests for FMP-primary data sourcing in the watchlist scanner.

(Historically this file covered an Alpha Vantage → FMP fallback. AlphaVantage
has been excised; the scanner is now FMP-primary. The AV-success / AV-budget /
AV-stale-cache cases were rewritten as FMP-primary cases — symbols FMP cannot
supply are marked "missing", not routed to a second provider.)

Covers:
  - parse_fmp_profile() field mapping from FMP profile + quote schemas
  - _technicals_from_fmp_quote() derived indicators from a single FMP quote
  - WatchlistScanner: FMP quote present → price_data_source='fmp'
  - WatchlistScanner: no FMP data → marks 'missing', scan continues
  - WatchlistScanner: news_source is 'fmp' when FMP supplies news, else 'none'
  - Scan never stops mid-run when FMP is available
  - Provenance fields appear on every result row (fmp / missing only)
  - FMP historical enrichment of technicals
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, PropertyMock

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.fundamentals_engine import parse_fmp_profile
from watchlist_scanner.scanner import (
    WatchlistScanner,
    _technicals_from_fmp_quote,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_df(n_rows: int = 30, price: float = 150.0) -> pd.DataFrame:
    """Return a minimal OHLCV DataFrame (newest first)."""
    dates = pd.date_range("2026-01-01", periods=n_rows, freq="B")[::-1]
    prices = [price + i * 0.1 for i in range(n_rows)]
    df = pd.DataFrame({
        "open":      prices,
        "high":      [p * 1.01 for p in prices],
        "low":       [p * 0.99 for p in prices],
        "close":     prices,
        "adj_close": prices,
        "volume":    [1_000_000] * n_rows,
    }, index=dates)
    return df


def _make_cache(*, calls_today: int = 0, stale_daily: dict | None = None) -> MagicMock:
    mock = MagicMock()
    type(mock).calls_today = PropertyMock(return_value=calls_today)
    mock.would_exceed.return_value = False
    mock.get.return_value = None
    mock.get_stale.return_value = stale_daily
    mock.get_age_seconds.return_value = None
    mock.increment_calls.return_value = calls_today + 1
    return mock


def _make_fmp_client(
    *,
    quotes: dict[str, dict] | None = None,
    profiles: list[dict] | None = None,
    quotes_fail: bool = False,
    profiles_fail: bool = False,
) -> MagicMock:
    mock = MagicMock()
    if quotes_fail:
        mock.get_batch_quotes.side_effect = Exception("FMP batch quotes failed")
    else:
        mock.get_batch_quotes.return_value = quotes or {}
    if profiles_fail:
        mock.get_batch_profiles.side_effect = Exception("FMP profiles failed")
    else:
        mock.get_batch_profiles.return_value = profiles or []
    # Safe defaults for the other FMP methods (override per test as needed)
    mock.get_stock_news.return_value = []       # no FMP news by default
    mock.get_historical_prices.return_value = [] # no historical by default
    mock.get_ratios.return_value = None          # no ratios by default
    return mock


_SAMPLE_FMP_PROFILE = {
    "symbol": "AAPL",
    "companyName": "Apple Inc.",
    "sector": "Technology",
    "industry": "Consumer Electronics",
    "description": "Apple makes iPhones.",
    "mktCap": 2_870_000_000_000,
    "beta": 1.25,
}

_SAMPLE_FMP_QUOTE = {
    "symbol": "AAPL",
    "price": 185.0,
    "changesPercentage": 1.5,
    "change": 2.75,
    "volume": 55_000_000,
    "avgVolume": 45_000_000,
    "priceAvg50": 182.0,
    "priceAvg200": 170.0,
    "pe": 28.5,
    "eps": 6.50,
    "yearHigh": 198.23,
    "yearLow": 124.17,
    "marketCap": 2_870_000_000_000,
}


def _make_scanner(
    *,
    watchlist: list[str] | None = None,
    cache: Any = None,
    fmp: Any = None,
    data_sources: dict | None = None,
) -> WatchlistScanner:
    wl = watchlist or ["AAPL"]
    return WatchlistScanner(
        watchlist=wl,
        cache=cache or _make_cache(),
        fmp_client=fmp,
        data_sources=data_sources or {"fmp_enabled": True},
    )


# ---------------------------------------------------------------------------
# TestParseFmpProfile
# ---------------------------------------------------------------------------

class TestParseFmpProfile:
    def test_empty_profile_returns_empty(self):
        assert parse_fmp_profile({}) == {}

    def test_sector_mapped(self):
        result = parse_fmp_profile(_SAMPLE_FMP_PROFILE)
        assert result["sector"] == "Technology"

    def test_name_mapped(self):
        result = parse_fmp_profile(_SAMPLE_FMP_PROFILE)
        assert result["name"] == "Apple Inc."

    def test_market_cap_mapped(self):
        result = parse_fmp_profile(_SAMPLE_FMP_PROFILE)
        assert result["market_cap"] == pytest.approx(2.87e12)

    def test_beta_mapped(self):
        result = parse_fmp_profile(_SAMPLE_FMP_PROFILE)
        assert result["beta"] == pytest.approx(1.25)

    def test_pe_from_quote(self):
        result = parse_fmp_profile(_SAMPLE_FMP_PROFILE, _SAMPLE_FMP_QUOTE)
        assert result["pe_ratio"] == pytest.approx(28.5)

    def test_50dma_from_quote(self):
        result = parse_fmp_profile(_SAMPLE_FMP_PROFILE, _SAMPLE_FMP_QUOTE)
        assert result["50dma"] == pytest.approx(182.0)

    def test_200dma_from_quote(self):
        result = parse_fmp_profile(_SAMPLE_FMP_PROFILE, _SAMPLE_FMP_QUOTE)
        assert result["200dma"] == pytest.approx(170.0)

    def test_52w_range_from_quote(self):
        result = parse_fmp_profile(_SAMPLE_FMP_PROFILE, _SAMPLE_FMP_QUOTE)
        assert result["52w_high"] == pytest.approx(198.23)
        assert result["52w_low"] == pytest.approx(124.17)

    def test_eps_from_quote(self):
        result = parse_fmp_profile(_SAMPLE_FMP_PROFILE, _SAMPLE_FMP_QUOTE)
        assert result["eps"] == pytest.approx(6.50)

    def test_unavailable_fields_are_none(self):
        result = parse_fmp_profile(_SAMPLE_FMP_PROFILE)
        for field in ("forward_pe", "profit_margin", "revenue_ttm",
                      "gross_profit_ttm", "analyst_target_price",
                      "dividend_yield", "book_value"):
            assert result[field] is None, f"{field} should be None"

    def test_output_has_expected_fundamentals_keys(self):
        # The FMP profile parser must produce the canonical fundamentals schema.
        expected_keys = {
            "symbol", "name", "sector", "industry", "description", "market_cap",
            "pe_ratio", "forward_pe", "profit_margin", "revenue_ttm",
            "gross_profit_ttm", "beta", "analyst_target_price", "dividend_yield",
            "eps", "book_value", "52w_high", "52w_low", "50dma", "200dma",
            "revenue_growth", "earnings_growth", "debt_ratio",
        }
        fmp_keys = set(parse_fmp_profile(_SAMPLE_FMP_PROFILE).keys())
        assert fmp_keys == expected_keys

    def test_none_quote_safe(self):
        result = parse_fmp_profile(_SAMPLE_FMP_PROFILE, None)
        assert result["sector"] == "Technology"
        assert result["pe_ratio"] is None

    def test_market_cap_fallback_to_quote_marketCap(self):
        profile = {"symbol": "X", "sector": "Tech"}  # no mktCap
        quote = {"marketCap": 1_000_000_000}
        result = parse_fmp_profile(profile, quote)
        assert result["market_cap"] == pytest.approx(1e9)


# ---------------------------------------------------------------------------
# TestTechnicalsFromFmpQuote
# ---------------------------------------------------------------------------

class TestTechnicalsFromFmpQuote:
    def test_empty_quote_returns_empty(self):
        assert _technicals_from_fmp_quote({}) == {}

    def test_zero_price_returns_empty(self):
        assert _technicals_from_fmp_quote({"price": 0}) == {}

    def test_price_mapped(self):
        tech = _technicals_from_fmp_quote(_SAMPLE_FMP_QUOTE)
        assert tech["price"] == pytest.approx(185.0)

    def test_1d_change_mapped(self):
        tech = _technicals_from_fmp_quote(_SAMPLE_FMP_QUOTE)
        assert tech["price_change_1d"] == pytest.approx(1.5)

    def test_5d_change_is_none(self):
        tech = _technicals_from_fmp_quote(_SAMPLE_FMP_QUOTE)
        assert tech["price_change_5d"] is None

    def test_sma20_is_none(self):
        tech = _technicals_from_fmp_quote(_SAMPLE_FMP_QUOTE)
        assert tech["sma20"] is None

    def test_sma50_mapped(self):
        tech = _technicals_from_fmp_quote(_SAMPLE_FMP_QUOTE)
        assert tech["sma50"] == pytest.approx(182.0)

    def test_above_sma20_is_false(self):
        tech = _technicals_from_fmp_quote(_SAMPLE_FMP_QUOTE)
        assert tech["above_sma20"] is False

    def test_above_sma50_true_when_price_above(self):
        q = {**_SAMPLE_FMP_QUOTE, "price": 190.0, "priceAvg50": 182.0}
        tech = _technicals_from_fmp_quote(q)
        assert tech["above_sma50"] is True

    def test_above_sma50_false_when_price_below(self):
        q = {**_SAMPLE_FMP_QUOTE, "price": 175.0, "priceAvg50": 182.0}
        tech = _technicals_from_fmp_quote(q)
        assert tech["above_sma50"] is False

    def test_volume_spike_true(self):
        q = {**_SAMPLE_FMP_QUOTE, "volume": 70_000_000, "avgVolume": 40_000_000}
        tech = _technicals_from_fmp_quote(q, spike_factor=1.5)
        assert tech["volume_spike"] is True  # 70M > 40M * 1.5 = 60M

    def test_volume_spike_false(self):
        q = {**_SAMPLE_FMP_QUOTE, "volume": 50_000_000, "avgVolume": 40_000_000}
        tech = _technicals_from_fmp_quote(q, spike_factor=1.5)
        assert tech["volume_spike"] is False  # 50M < 60M

    def test_data_days_is_1(self):
        tech = _technicals_from_fmp_quote(_SAMPLE_FMP_QUOTE)
        assert tech["data_days"] == 1

    def test_required_keys_present(self):
        tech = _technicals_from_fmp_quote(_SAMPLE_FMP_QUOTE)
        for key in ("price", "price_change_1d", "price_change_5d",
                    "sma20", "sma50", "above_sma20", "above_sma50",
                    "volume_today", "volume_avg20", "volume_spike", "data_days"):
            assert key in tech


# ---------------------------------------------------------------------------
# TestScannerSourceTracking — provenance fields in result (FMP / missing only)
# ---------------------------------------------------------------------------

class TestScannerSourceTracking:
    def _run_one(self, scanner: WatchlistScanner) -> dict:
        result = scanner._scan_symbol(
            "AAPL",
            articles=[],
            fundamentals={},
            ov_source="fresh",
            dry_run=False,
            fmp_quote=_SAMPLE_FMP_QUOTE,
            fundamentals_source="missing",
            news_source="none",
        )
        assert result is not None
        return result

    def test_fmp_configured_price_source_is_fmp(self):
        # When FMP client is configured and fmp_quote provided, FMP is primary.
        fmp = _make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        scanner = _make_scanner(fmp=fmp)
        result = self._run_one(scanner)
        assert result["price_data_source"] == "fmp"

    def test_no_fmp_quote_price_source_is_missing(self):
        # No FMP quote and no FMP client → no live price source.
        scanner = _make_scanner(fmp=None)
        result = scanner._scan_symbol("AAPL", [], {}, "fresh", False, fmp_quote=None)
        assert result["price_data_source"] == "missing"

    def test_fmp_primary_fallback_not_used(self):
        # FMP-primary path never sets a fallback flag (no stale-cache provider).
        fmp = _make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        scanner = _make_scanner(fmp=fmp)
        result = self._run_one(scanner)
        assert result["fallback_used"] is False
        assert result["fallback_reason"] == ""

    def test_no_fmp_data_marks_missing(self):
        cache = _make_cache(stale_daily=None)
        scanner = _make_scanner(cache=cache, fmp=None)
        result = scanner._scan_symbol(
            "AAPL",
            articles=[],
            fundamentals={},
            ov_source="missing",
            dry_run=False,
            fmp_quote=None,
            fundamentals_source="missing",
            news_source="none",
        )
        assert result["price_data_source"] == "missing"
        assert result["fallback_used"] is False

    def test_result_has_all_provenance_keys(self):
        scanner = _make_scanner()
        result = self._run_one(scanner)
        for key in ("price_data_source", "fundamentals_source",
                    "news_source", "fallback_used", "fallback_reason"):
            assert key in result, f"Missing provenance key: {key}"

    def test_news_source_fmp_propagated(self):
        scanner = _make_scanner()
        result = scanner._scan_symbol(
            "AAPL", [], {}, "fresh", False,
            news_source="fmp",
        )
        assert result["news_source"] == "fmp"

    def test_news_source_none_propagated(self):
        scanner = _make_scanner()
        result = scanner._scan_symbol(
            "AAPL", [], {}, "fresh", False,
            news_source="none",
        )
        assert result["news_source"] == "none"

    def test_fundamentals_source_fmp_does_not_set_fallback(self):
        # FMP fundamentals are live data — not a fallback.
        scanner = _make_scanner()
        result = scanner._scan_symbol(
            "AAPL", [], {}, "missing", False,
            fundamentals_source="fmp",
        )
        assert result["fundamentals_source"] == "fmp"
        assert result["fallback_used"] is False

    def test_fmp_price_contains_valid_data(self):
        fmp = _make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        scanner = _make_scanner(fmp=fmp)
        result = scanner._scan_symbol(
            "AAPL", [], {}, "fresh", False,
            fmp_quote=_SAMPLE_FMP_QUOTE,
        )
        assert result["price"] == pytest.approx(185.0)
        assert result["price_change_pct"] == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# TestScanContinues — scan processes every symbol via FMP
# ---------------------------------------------------------------------------

class TestScanContinues:
    def test_scan_processes_all_symbols(self):
        """FMP supplies all symbols → all are scanned."""
        watchlist = ["AAPL", "MSFT", "NVDA"]
        fmp_quotes = {
            sym: {**_SAMPLE_FMP_QUOTE, "symbol": sym} for sym in watchlist
        }
        fmp_profiles = [
            {**_SAMPLE_FMP_PROFILE, "symbol": sym} for sym in watchlist
        ]
        fmp  = _make_fmp_client(quotes=fmp_quotes, profiles=fmp_profiles)
        cache = _make_cache(stale_daily=None)
        scanner = WatchlistScanner(
            watchlist=watchlist,
            cache=cache,
            fmp_client=fmp,
            data_sources={"fmp_enabled": True},
        )
        result = scanner.run(dry_run=False)
        result_tickers = {r["ticker"] for r in result["results"]}
        assert result_tickers == set(watchlist)

    def test_scan_result_count_matches_watchlist(self):
        """FMP produces a result for every symbol."""
        watchlist = ["AAPL", "MSFT"]
        fmp_quotes = {sym: {**_SAMPLE_FMP_QUOTE, "symbol": sym} for sym in watchlist}
        fmp_profiles = [{**_SAMPLE_FMP_PROFILE, "symbol": sym} for sym in watchlist]
        fmp  = _make_fmp_client(quotes=fmp_quotes, profiles=fmp_profiles)
        cache = _make_cache(stale_daily=None)
        scanner = WatchlistScanner(
            watchlist=watchlist,
            cache=cache,
            fmp_client=fmp,
            data_sources={"fmp_enabled": True},
        )
        result = scanner.run(dry_run=False)
        assert len(result["results"]) == len(watchlist)

    def test_no_fmp_client_still_produces_results(self):
        """Without FMP, every symbol still produces a 'missing'-sourced result."""
        watchlist = ["AAPL", "MSFT", "NVDA"]
        cache = _make_cache(stale_daily=None)
        scanner = WatchlistScanner(
            watchlist=watchlist,
            cache=cache,
            fmp_client=None,
            data_sources={"fmp_enabled": False},
        )
        result = scanner.run(dry_run=False)
        assert len(result["results"]) == len(watchlist)
        assert all(r["price_data_source"] == "missing" for r in result["results"])


# ---------------------------------------------------------------------------
# TestFmpDisabledConfig — fmp_enabled=False disables FMP entirely
# ---------------------------------------------------------------------------

class TestFmpDisabledConfig:
    def test_fmp_not_used_when_disabled(self):
        """When fmp_enabled=False, FMP is never consulted."""
        fmp = _make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        cache = _make_cache(stale_daily=None)
        scanner = WatchlistScanner(
            watchlist=["AAPL"],
            cache=cache,
            fmp_client=fmp,
            data_sources={"fmp_enabled": False},
        )
        result = scanner._scan_symbol(
            "AAPL", [], {}, "fresh", False,
            fmp_quote=_SAMPLE_FMP_QUOTE,
        )
        assert result["price_data_source"] == "missing"
        # FMP client pre-fetch should not be called during run() when disabled
        fmp.get_batch_quotes.assert_not_called()

    def test_fmp_enabled_false_when_data_sources_disabled(self):
        scanner = WatchlistScanner(
            watchlist=["AAPL"],
            cache=_make_cache(),
            fmp_client=MagicMock(),
            data_sources={"fmp_enabled": False},
        )
        assert scanner._fmp_enabled is False

    def test_no_fmp_client_fmp_enabled_false(self):
        scanner = WatchlistScanner(
            watchlist=["AAPL"],
            cache=_make_cache(),
            fmp_client=None,
        )
        assert scanner._fmp_enabled is False


# ---------------------------------------------------------------------------
# TestFmpNews — news_source tracking (FMP is the sole news provider)
# ---------------------------------------------------------------------------

_FMP_NEWS_RAW = [
    {
        "symbol": "AAPL",
        "publishedDate": "2026-04-27 09:30:00",
        "title": "Apple announces new product",
        "text": "Apple reported strong quarterly results.",
        "site": "bloomberg.com",
        "url": "https://example.com/1",
        "ticker_sentiment": [{"ticker": "AAPL"}],
    }
]


def _make_fmp_with_news(
    *,
    articles: list | None = None,
    fail: bool = False,
) -> MagicMock:
    """FMP mock pre-wired with get_stock_news."""
    mock = _make_fmp_client()
    if fail:
        mock.get_stock_news.side_effect = Exception("FMP news failed")
    else:
        mock.get_stock_news.return_value = articles if articles is not None else _FMP_NEWS_RAW
    return mock


class TestFmpNews:
    def _run_scanner(self, fmp, *, watchlist=None):
        wl = watchlist or ["AAPL"]
        scanner = WatchlistScanner(
            watchlist=wl,
            cache=_make_cache(),
            fmp_client=fmp,
            data_sources={"fmp_enabled": True},
        )
        return scanner.run(dry_run=False)

    def test_fmp_news_used(self):
        fmp = _make_fmp_with_news()
        result = self._run_scanner(fmp)
        news_sources = {r["news_source"] for r in result["results"]}
        assert news_sources == {"fmp"}

    def test_fmp_news_fails_news_source_none(self):
        fmp = _make_fmp_with_news(fail=True)
        result = self._run_scanner(fmp)
        news_sources = {r["news_source"] for r in result["results"]}
        assert news_sources == {"none"}

    def test_fmp_disabled_news_source_none(self):
        fmp = _make_fmp_with_news()
        scanner = WatchlistScanner(
            watchlist=["AAPL"],
            cache=_make_cache(),
            fmp_client=fmp,
            data_sources={"fmp_enabled": False},
        )
        result = scanner.run(dry_run=False)
        news_sources = {r["news_source"] for r in result["results"]}
        assert news_sources == {"none"}
        fmp.get_stock_news.assert_not_called()

    def test_scan_produces_all_results_with_news(self):
        watchlist = ["AAPL", "MSFT"]
        fmp = _make_fmp_with_news(articles=[
            {**_FMP_NEWS_RAW[0], "symbol": "AAPL"},
            {**_FMP_NEWS_RAW[0], "symbol": "MSFT", "ticker_sentiment": [{"ticker": "MSFT"}]},
        ])
        result = self._run_scanner(fmp, watchlist=watchlist)
        assert len(result["results"]) == len(watchlist)

    def test_fmp_news_returns_empty_news_source_none(self):
        fmp = _make_fmp_with_news(articles=[])  # FMP returns empty list
        result = self._run_scanner(fmp)
        news_sources = {r["news_source"] for r in result["results"]}
        assert news_sources == {"none"}

    def test_no_fmp_client_news_source_none(self):
        scanner = WatchlistScanner(
            watchlist=["AAPL"],
            cache=_make_cache(),
            fmp_client=None,
        )
        result = scanner.run(dry_run=False)
        news_sources = {r["news_source"] for r in result["results"]}
        assert news_sources == {"none"}


# ---------------------------------------------------------------------------
# TestFmpGetStockNewsNormalization — normalized article shape
# ---------------------------------------------------------------------------

class TestFmpGetStockNewsNormalization:
    """
    Tests the normalization logic of FMPClient.get_stock_news() by running
    it on a patched _get_cached to bypass HTTP and budget logic.
    """

    def _make_client(self) -> Any:
        from fmp_client import FMPClient
        return FMPClient(api_key="test_key", cache_dir=Path(tempfile.mkdtemp()))

    def _call_with_raw(self, raw, *, tickers=None) -> list:
        client = self._make_client()
        # get_stock_news now uses inline caching; mock cache-miss + network call
        with patch.object(client._cache, 'get', return_value=None), \
             patch.object(client._counter, 'would_exceed', return_value=False), \
             patch.object(client._cache, 'set'), \
             patch.object(client, '_raw_get', return_value=raw):
            return client.get_stock_news(tickers or ["AAPL"])

    def test_empty_tickers_returns_empty(self):
        client = self._make_client()
        assert client.get_stock_news([]) == []

    def test_non_list_response_returns_empty(self):
        result = self._call_with_raw({"error": "oops"})
        assert result == []

    def test_none_response_returns_empty(self):
        result = self._call_with_raw(None)
        assert result == []

    def test_article_without_title_skipped(self):
        raw = [{"symbol": "AAPL", "text": "no title", "site": "site.com",
                "publishedDate": "2026-01-01"}]
        result = self._call_with_raw(raw)
        assert result == []

    def test_title_mapped(self):
        result = self._call_with_raw(_FMP_NEWS_RAW)
        assert result[0]["title"] == "Apple announces new product"

    def test_summary_mapped_from_text(self):
        result = self._call_with_raw(_FMP_NEWS_RAW)
        assert result[0]["summary"] == "Apple reported strong quarterly results."

    def test_source_mapped_from_site(self):
        result = self._call_with_raw(_FMP_NEWS_RAW)
        assert result[0]["source"] == "bloomberg.com"

    def test_time_published_mapped(self):
        result = self._call_with_raw(_FMP_NEWS_RAW)
        assert result[0]["time_published"] == "2026-04-27 09:30:00"

    def test_sentiment_score_is_zero(self):
        result = self._call_with_raw(_FMP_NEWS_RAW)
        assert result[0]["overall_sentiment_score"] == 0.0

    def test_sentiment_label_is_neutral(self):
        result = self._call_with_raw(_FMP_NEWS_RAW)
        assert result[0]["overall_sentiment_label"] == "Neutral"

    def test_ticker_sentiment_ticker_uppercased(self):
        result = self._call_with_raw(_FMP_NEWS_RAW)
        assert result[0]["ticker_sentiment"][0]["ticker"] == "AAPL"

    def test_ticker_sentiment_empty_when_no_symbol(self):
        raw = [{**_FMP_NEWS_RAW[0], "symbol": ""}]
        result = self._call_with_raw(raw)
        assert result[0]["ticker_sentiment"] == []

    def test_required_keys_present(self):
        result = self._call_with_raw(_FMP_NEWS_RAW)
        for key in ("title", "summary", "source", "time_published",
                    "overall_sentiment_score", "overall_sentiment_label",
                    "ticker_sentiment"):
            assert key in result[0], f"missing key: {key}"

    def test_exception_in_get_cached_returns_empty(self):
        client = self._make_client()
        with patch.object(client, '_get_cached', side_effect=Exception("network error")):
            result = client.get_stock_news(["AAPL"])
        assert result == []


# ---------------------------------------------------------------------------
# TestDryRunFMPPrimary — FMP loads even in dry_run (independent of any AV cap)
# ---------------------------------------------------------------------------

class TestDryRunFMPPrimary:
    """
    When a run is invoked dry (cache-only), FMP is still an active provider and
    must still load quotes and serve as the price source.
    """

    def test_dry_run_with_fmp_uses_fmp_price(self):
        """dry_run=True + FMP available → price_data_source='fmp', price not None."""
        fmp = _make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        scanner = _make_scanner(cache=_make_cache(), fmp=fmp)

        result = scanner.run(dry_run=True)

        rows = result.get("results", [])
        assert len(rows) == 1
        assert rows[0]["price_data_source"] == "fmp"
        assert rows[0]["price"] is not None
        assert rows[0]["price"] == pytest.approx(185.0)

    def test_dry_run_with_fmp_not_cache_only(self):
        """dry_run=True + FMP available → scan_status must not be 'cache_only'."""
        fmp = _make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        scanner = _make_scanner(cache=_make_cache(), fmp=fmp)

        result = scanner.run(dry_run=True)

        status = result.get("scan_summary", {}).get("scan_status")
        assert status != "cache_only", f"Expected ok or degraded, got {status!r}"

    def test_dry_run_with_fmp_data_quality_not_all_cached(self):
        """dry_run=True + FMP available → at least one result is not data_quality='cached'."""
        fmp = _make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        scanner = _make_scanner(cache=_make_cache(), fmp=fmp)

        result = scanner.run(dry_run=True)

        qualities = {r["data_quality"] for r in result.get("results", [])}
        assert "cached" not in qualities or "fresh" in qualities or "partial" in qualities, (
            f"All results are cached despite FMP being available: {qualities}"
        )

    def test_dry_run_no_fmp_marks_missing(self):
        """dry_run=True + no FMP → no live price source available."""
        cache = _make_cache(stale_daily=None)
        scanner = _make_scanner(cache=cache, fmp=None)

        result = scanner.run(dry_run=True)

        rows = result.get("results", [])
        assert len(rows) == 1
        assert rows[0]["price_data_source"] == "missing"

    def test_fmp_prefetch_called_even_when_dry_run(self):
        """FMP get_batch_quotes and get_batch_profiles must be called even when dry_run=True."""
        fmp = _make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        scanner = _make_scanner(cache=_make_cache(), fmp=fmp)

        scanner.run(dry_run=True)

        fmp.get_batch_quotes.assert_called_once()
        fmp.get_batch_profiles.assert_called_once()


# ---------------------------------------------------------------------------
# TestProvenanceFields — historical_source, quote_source, technical_source,
#                        ratios_source, provider_health (FMP / missing only)
# ---------------------------------------------------------------------------

_SAMPLE_FMP_HISTORICAL = [
    {
        "date": f"2026-0{max(1, 4 - i // 30):01d}-{max(1, 28 - i % 28):02d}",
        "open": 183.0 + i * 0.1,
        "high": 185.0 + i * 0.1,
        "low":  182.0 + i * 0.1,
        "close": 184.0 + i * 0.1,
        "adjClose": 184.0 + i * 0.1,
        "volume": 50_000_000,
    }
    for i in range(60)  # 60 days so SMA20 is computable
]


class TestProvenanceFields:
    """Verify data-source provenance fields in the FMP-primary scanner."""

    def _scan_one(self, fmp_quote=None, fmp_historical=None) -> dict:
        fmp = _make_fmp_client(quotes={"AAPL": fmp_quote} if fmp_quote else {})
        scanner = _make_scanner(fmp=fmp)
        result = scanner._scan_symbol(
            "AAPL", [], {}, "fresh", False,
            fmp_quote=fmp_quote,
            fmp_historical=fmp_historical,
        )
        assert result is not None
        return result

    def test_quote_source_alias_equals_price_data_source(self):
        result = self._scan_one(fmp_quote=_SAMPLE_FMP_QUOTE)
        assert result["quote_source"] == result["price_data_source"]

    def test_historical_source_fmp_when_historical_provided(self):
        result = self._scan_one(fmp_quote=_SAMPLE_FMP_QUOTE, fmp_historical=_SAMPLE_FMP_HISTORICAL)
        assert result["historical_source"] == "fmp"

    def test_historical_source_missing_when_no_historical(self):
        result = self._scan_one(fmp_quote=_SAMPLE_FMP_QUOTE, fmp_historical=None)
        assert result["historical_source"] == "missing"

    def test_technical_source_fmp_quote_plus_historical(self):
        result = self._scan_one(fmp_quote=_SAMPLE_FMP_QUOTE, fmp_historical=_SAMPLE_FMP_HISTORICAL)
        assert result["technical_source"] == "fmp_quote+historical"

    def test_technical_source_fmp_quote_only_when_no_historical(self):
        scanner = _make_scanner(fmp=_make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE}))
        result = scanner._scan_symbol(
            "AAPL", [], {}, "fresh", False,
            fmp_quote=_SAMPLE_FMP_QUOTE,
            fmp_historical=None,
        )
        assert result is not None
        assert result["technical_source"] == "fmp_quote"

    def test_ratios_source_fmp_when_fundamentals_source_fmp(self):
        result_with_fmp_fund = _make_scanner(
            fmp=_make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        )._scan_symbol("AAPL", [], {}, "fresh", False,
                       fmp_quote=_SAMPLE_FMP_QUOTE, fundamentals_source="fmp")
        assert result_with_fmp_fund is not None
        assert result_with_fmp_fund["ratios_source"] == "fmp"

    def test_ratios_source_missing_when_no_fmp_fundamentals(self):
        result = _make_scanner(fmp=None)._scan_symbol(
            "AAPL", [], {}, "missing", False, fmp_quote=None, fundamentals_source="missing"
        )
        assert result is not None
        assert result["ratios_source"] == "missing"

    def test_provider_health_fmp_primary(self):
        result = _make_scanner(fmp=_make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE}))._scan_symbol(
            "AAPL", [], {}, "fresh", False,
            fmp_quote=_SAMPLE_FMP_QUOTE, fundamentals_source="fmp"
        )
        assert result is not None
        assert result["provider_health"] == "fmp_primary"

    def test_provider_health_missing_when_no_data(self):
        result = _make_scanner(fmp=None, cache=_make_cache(stale_daily=None))._scan_symbol(
            "AAPL", [], {}, "missing", False, fmp_quote=None, fundamentals_source="missing"
        )
        assert result is not None
        assert result["provider_health"] == "missing"

    def test_fmp_historical_enriches_sma20(self):
        """FMP historical data must populate sma20 when 20+ days are provided."""
        result = self._scan_one(fmp_quote=_SAMPLE_FMP_QUOTE, fmp_historical=_SAMPLE_FMP_HISTORICAL)
        assert result["sma20"] is not None

    def test_fmp_historical_enriches_price_change_5d(self):
        """FMP historical data must populate price_change_5d when 6+ days are provided."""
        result = self._scan_one(fmp_quote=_SAMPLE_FMP_QUOTE, fmp_historical=_SAMPLE_FMP_HISTORICAL)
        assert result["technicals"]["price_change_5d"] is not None

    def test_above_sma20_recomputed_with_fmp_price(self):
        """above_sma20 must be based on the FMP quote price, not the historical close."""
        result = self._scan_one(fmp_quote=_SAMPLE_FMP_QUOTE, fmp_historical=_SAMPLE_FMP_HISTORICAL)
        sma20 = result["sma20"]
        price = result["price"]
        if sma20 is not None and price is not None:
            assert result["above_sma20"] == (price > sma20)

    def test_all_provenance_keys_present(self):
        result = self._scan_one(fmp_quote=_SAMPLE_FMP_QUOTE)
        for key in ("quote_source", "historical_source", "technical_source",
                    "ratios_source", "provider_health"):
            assert key in result, f"Missing provenance key: {key}"


# ---------------------------------------------------------------------------
# TestFmpHistoricalEnrichment — FMP historical populates time-series indicators
# ---------------------------------------------------------------------------

class TestFmpHistoricalEnrichment:
    """FMP quote + historical together produce full technicals."""

    def _scan(self, *, fmp_hist, fmp_quote=None):
        fmp_quote = fmp_quote or _SAMPLE_FMP_QUOTE
        fmp = _make_fmp_client(quotes={"AAPL": fmp_quote})
        scanner = _make_scanner(fmp=fmp)
        return scanner._scan_symbol(
            "AAPL", [], {}, "fresh", False,
            fmp_quote=fmp_quote,
            fmp_historical=fmp_hist,
        )

    def test_fmp_historical_alone_is_price_source(self):
        """FMP historical alone (no quote) is sufficient to set price_data_source=fmp."""
        fmp = _make_fmp_client(quotes={})  # no quote for AAPL
        scanner = _make_scanner(fmp=fmp)
        result = scanner._scan_symbol(
            "AAPL", [], {}, "fresh", False,
            fmp_quote=None,
            fmp_historical=_SAMPLE_FMP_HISTORICAL,
        )
        assert result["price_data_source"] == "fmp"
        assert result["historical_source"] == "fmp"

    def test_fmp_historical_populates_sma20(self):
        result = self._scan(fmp_hist=_SAMPLE_FMP_HISTORICAL)
        assert result["sma20"] is not None, "SMA20 must be computed from FMP historical"

    def test_fmp_historical_sma50_field_present(self):
        result = self._scan(fmp_hist=_SAMPLE_FMP_HISTORICAL)
        # 60 rows: SMA50 will be None (need 50 rows). Verify the field exists.
        assert "sma50" in result

    def test_fmp_historical_populates_price_change_5d(self):
        result = self._scan(fmp_hist=_SAMPLE_FMP_HISTORICAL)
        assert result["technicals"]["price_change_5d"] is not None

    def test_fmp_historical_populates_volume_avg20(self):
        result = self._scan(fmp_hist=_SAMPLE_FMP_HISTORICAL)
        assert result["volume_avg20"] is not None, "volume_avg20 must be set from FMP historical"

    def test_technical_data_completeness_field_present(self):
        result = self._scan(fmp_hist=_SAMPLE_FMP_HISTORICAL)
        assert "technical_data_completeness" in result

    def test_technical_data_completeness_partial_or_full(self):
        """60 rows gives SMA20 but not SMA50; completeness should be partial or full."""
        result = self._scan(fmp_hist=_SAMPLE_FMP_HISTORICAL)
        assert result["technical_data_completeness"] in ("full", "partial")

    def test_technical_data_completeness_price_only_without_historical(self):
        """Quote-only (no historical) → price_only or partial (sma50 may come from quote)."""
        result = self._scan(fmp_hist=None)
        assert result["technical_data_completeness"] in ("price_only", "partial", "full")

    def test_scan_status_ok_when_fmp_quote_and_historical(self):
        """Full scan with FMP quote + historical must not degrade to cache_only."""
        fmp = _make_fmp_client(
            quotes={"AAPL": _SAMPLE_FMP_QUOTE},
            profiles=[{"symbol": "AAPL", "sector": "Technology", "mktCap": 3e12}],
        )
        fmp.get_historical_prices.return_value = _SAMPLE_FMP_HISTORICAL
        scanner = _make_scanner(fmp=fmp, watchlist=["AAPL"])
        result = scanner.run(dry_run=False)
        scan_status = result["scan_summary"]["scan_status"]
        assert scan_status in ("ok", "degraded"), f"scan_status={scan_status}"

    def test_run_logs_fmp_historical_counts(self, caplog):
        """run() must emit the FMP historical summary log."""
        import logging
        fmp = _make_fmp_client(
            quotes={"AAPL": _SAMPLE_FMP_QUOTE},
            profiles=[{"symbol": "AAPL", "sector": "Technology", "mktCap": 3e12}],
        )
        fmp.get_historical_prices.return_value = _SAMPLE_FMP_HISTORICAL
        scanner = _make_scanner(fmp=fmp, watchlist=["AAPL"])
        with caplog.at_level(logging.INFO, logger="watchlist_scanner.scanner"):
            scanner.run(dry_run=False)
        assert any(
            "FMP historical used for" in m for m in caplog.messages
        ), "Expected 'FMP historical used for X/Y symbols' log line"


# ---------------------------------------------------------------------------
# TestExtendedWatchlistInclusion — issue #2 + #2b
#   The AV-era headroom guard (max_calls=20 minus the ~22-symbol static list)
#   was always negative, so extended_watchlist promotions were never scored.
#   FMP-primary: active extended symbols must ALWAYS be included on the live
#   path, and must carry the finer "discovery:<theme>" source label.
# ---------------------------------------------------------------------------

class _CapturingScanner:
    """Fake scanner that returns one result per watchlist symbol."""
    last_watchlist: list[str] = []

    def __init__(self, *args, watchlist=None, **kwargs):
        type(self).last_watchlist = list(watchlist or kwargs.get("watchlist") or [])
        self._wl = type(self).last_watchlist

    def run(self, dry_run: bool = False):
        return {
            "alerts": [],
            "results": [{"ticker": sym} for sym in self._wl],
            "generated_at": "2026-04-14T00:00:00",
            "run_date": "2026-04-14",
            "calls_used": 0,
            "scan_summary": {"scan_status": "ok"},
        }


class _StubExtendedWatchlist:
    """Stub ExtendedWatchlist returning two active discovery symbols."""
    _ACTIVE = [
        {"symbol": "XOM", "theme_name": "Energy Transition",
         "theme_names": ["Energy Transition"], "theme_confidence": 0.9},
        {"symbol": "NOC", "theme_name": "Defense",
         "theme_names": ["Defense"], "theme_confidence": 0.85},
    ]

    def __init__(self, *args, **kwargs):
        pass

    def get_active_symbols(self):
        return [dict(e) for e in self._ACTIVE]

    def record_scan(self, *args, **kwargs):
        pass


class TestExtendedWatchlistInclusion:
    def _run(self, *, dry_run=False, static=None):
        from watchlist_scanner.__main__ import run as ws_run

        static = static if static is not None else ["AAPL", "MSFT"]
        with patch("watchlist_scanner.extended_watchlist.ExtendedWatchlist",
                   _StubExtendedWatchlist), \
             patch("watchlist_scanner.cache_manager.CacheManager") as cache_cls, \
             patch("watchlist_scanner.scanner.WatchlistScanner", _CapturingScanner):
            cache = MagicMock()
            type(cache).calls_today = PropertyMock(return_value=0)
            cache_cls.return_value = cache
            _CapturingScanner.last_watchlist = []
            result = ws_run(
                config={"watchlist": static},
                dry_run=dry_run,
                output_dir="/tmp/ws_test_out",
                extended_watchlist_config={"enabled": True},
                scraped_intel_config={"enabled": False},
                data_sources_config={"fmp_enabled": False},
            )
        return result

    def test_extended_symbols_included_in_scan_watchlist(self):
        """Issue #2: active extended symbols are added despite the old AV headroom guard."""
        self._run()
        assert "XOM" in _CapturingScanner.last_watchlist
        assert "NOC" in _CapturingScanner.last_watchlist

    def test_extended_symbols_not_included_in_dry_run(self):
        """Cache-only (dry-run) mode still skips extended symbols."""
        self._run(dry_run=True)
        assert "XOM" not in _CapturingScanner.last_watchlist
        assert "NOC" not in _CapturingScanner.last_watchlist

    def test_extended_symbols_carry_finer_discovery_label(self):
        """Issue #2b: extended symbols carry 'discovery:<theme_name>'."""
        result = self._run()
        by_ticker = {r["ticker"]: r["watchlist_source"] for r in result["results"]}
        assert by_ticker["XOM"] == "discovery:Energy Transition"
        assert by_ticker["NOC"] == "discovery:Defense"

    def test_static_symbols_keep_static_label(self):
        result = self._run()
        by_ticker = {r["ticker"]: r["watchlist_source"] for r in result["results"]}
        assert by_ticker["AAPL"] == "static"
        assert by_ticker["MSFT"] == "static"

    def test_extended_meta_lists_extended_tickers(self):
        result = self._run()
        meta = result.get("extended_watchlist_meta", {})
        assert set(meta.get("extended_tickers", [])) == {"XOM", "NOC"}
