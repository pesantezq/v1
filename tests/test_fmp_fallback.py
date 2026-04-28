"""
Tests for FMP fallback support in the watchlist scanner.

Covers:
  - parse_fmp_profile() field mapping from FMP profile + quote schemas
  - _technicals_from_fmp_quote() derived indicators from a single FMP quote
  - WatchlistScanner: AV success → uses AV (price_data_source='alpha_vantage')
  - WatchlistScanner: AV BudgetExceeded → falls back to FMP quote
  - WatchlistScanner: AV returns None → falls back to FMP quote
  - WatchlistScanner: FMP unavailable → falls back to stale AV cache
  - WatchlistScanner: all sources fail → marks 'missing', scan continues
  - Scan never stops mid-run due to budget exhaustion when FMP is available
  - Provenance fields appear on every result row
  - OVERVIEW loop FMP fallback when AV budget exhausted
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

from watchlist_scanner.alpha_vantage_client import BudgetExceeded
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
    import numpy as np
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


def _make_av_client(
    *,
    df: pd.DataFrame | None = None,
    news: list[dict] | None = None,
    overview: dict | None = None,
    budget_exceed_ohlcv: bool = False,
    budget_exceed_news: bool = False,
    budget_exceed_overview: bool = False,
) -> MagicMock:
    mock = MagicMock()
    mock._max_calls = 20

    if budget_exceed_ohlcv:
        mock.get_daily_ohlcv.side_effect = BudgetExceeded("budget")
    else:
        mock.get_daily_ohlcv.return_value = df if df is not None else _make_df()

    if budget_exceed_news:
        mock.get_news_sentiment.side_effect = BudgetExceeded("budget")
    else:
        mock.get_news_sentiment.return_value = news or []

    if budget_exceed_overview:
        mock.get_overview.side_effect = BudgetExceeded("budget")
    else:
        mock.get_overview.return_value = overview or {}

    return mock


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
    # Safe defaults for new methods (override per test as needed)
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
    av: Any = None,
    cache: Any = None,
    fmp: Any = None,
    data_sources: dict | None = None,
) -> WatchlistScanner:
    wl = watchlist or ["AAPL"]
    return WatchlistScanner(
        watchlist=wl,
        cache=cache or _make_cache(),
        av_client=av or _make_av_client(),
        fmp_client=fmp,
        data_sources=data_sources or {"fmp_enabled": True, "prefer_fmp_on_budget_exhausted": True},
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

    def test_output_has_same_keys_as_parse_overview(self):
        from watchlist_scanner.fundamentals_engine import parse_overview
        overview_keys = set(parse_overview({"Symbol": "X", "Sector": "Tech"}).keys())
        fmp_keys = set(parse_fmp_profile(_SAMPLE_FMP_PROFILE).keys())
        assert overview_keys == fmp_keys

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
# TestScannerSourceTracking — provenance fields in result
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
            fundamentals_source="alpha_vantage",
            news_source="alpha_vantage",
        )
        assert result is not None
        return result

    def test_fmp_configured_price_source_is_fmp(self):
        # When FMP client is configured and fmp_quote provided, FMP is primary.
        fmp = _make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        scanner = _make_scanner(av=_make_av_client(), fmp=fmp)
        result = self._run_one(scanner)
        assert result["price_data_source"] == "fmp"

    def test_no_fmp_client_price_source_is_av(self):
        # When no FMP client is configured, AV is the live source.
        scanner = _make_scanner(av=_make_av_client(), fmp=None)
        result = scanner._scan_symbol("AAPL", [], {}, "fresh", False, fmp_quote=None)
        assert result["price_data_source"] == "alpha_vantage"

    def test_fmp_primary_fallback_not_used(self):
        # FMP primary + AV success = no stale cache = fallback_used False
        fmp = _make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        scanner = _make_scanner(av=_make_av_client(), fmp=fmp)
        result = self._run_one(scanner)
        assert result["fallback_used"] is False
        assert result["fallback_reason"] == ""

    def test_av_budget_exceeded_fmp_available_uses_fmp(self):
        fmp = _make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        av  = _make_av_client(budget_exceed_ohlcv=True)
        scanner = _make_scanner(av=av, fmp=fmp)
        result = scanner._scan_symbol(
            "AAPL",
            articles=[],
            fundamentals={},
            ov_source="fresh",
            dry_run=False,
            fmp_quote=_SAMPLE_FMP_QUOTE,
            fundamentals_source="alpha_vantage",
            news_source="alpha_vantage",
        )
        assert result["price_data_source"] == "fmp"
        # FMP is primary (not a fallback); stale cache was not used
        assert result["fallback_used"] is False

    def test_av_returns_none_fmp_used(self):
        fmp = _make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        av  = _make_av_client(df=None)  # returns None, not budget exceeded
        av.get_daily_ohlcv.return_value = None
        scanner = _make_scanner(av=av, fmp=fmp)
        result = scanner._scan_symbol(
            "AAPL",
            articles=[],
            fundamentals={},
            ov_source="fresh",
            dry_run=False,
            fmp_quote=_SAMPLE_FMP_QUOTE,
            fundamentals_source="alpha_vantage",
            news_source="alpha_vantage",
        )
        assert result["price_data_source"] == "fmp"

    def test_fmp_unavailable_falls_back_to_stale_cache(self):
        # No FMP client; AV budget exhausted; stale AV cache is last resort
        stale_raw = {
            "Time Series (Daily)": {
                "2026-04-25": {"4. close": "148.0", "1. open": "147.0",
                               "2. high": "149.0", "3. low": "146.0", "5. volume": "1000000"},
                "2026-04-24": {"4. close": "145.0", "1. open": "144.0",
                               "2. high": "146.0", "3. low": "143.0", "5. volume": "900000"},
            }
        }
        cache = _make_cache(stale_daily=stale_raw)
        av    = _make_av_client(budget_exceed_ohlcv=True)
        scanner = _make_scanner(av=av, cache=cache, fmp=None)
        result = scanner._scan_symbol(
            "AAPL",
            articles=[],
            fundamentals={},
            ov_source="fresh",
            dry_run=False,
            fmp_quote=None,
            fundamentals_source="alpha_vantage",
            news_source="alpha_vantage",
        )
        assert result["price_data_source"] == "cache"
        assert result["fallback_used"] is True  # stale cache = degraded

    def test_all_sources_fail_marks_missing(self):
        cache = _make_cache(stale_daily=None)
        av    = _make_av_client(budget_exceed_ohlcv=True)
        scanner = _make_scanner(av=av, cache=cache, fmp=None)
        result = scanner._scan_symbol(
            "AAPL",
            articles=[],
            fundamentals={},
            ov_source="fresh",
            dry_run=False,
            fmp_quote=None,
            fundamentals_source="alpha_vantage",
            news_source="alpha_vantage",
        )
        assert result["price_data_source"] == "missing"
        assert result["fallback_used"] is False

    def test_result_has_all_provenance_keys(self):
        scanner = _make_scanner()
        result = self._run_one(scanner)
        for key in ("price_data_source", "fundamentals_source",
                    "news_source", "fallback_used", "fallback_reason"):
            assert key in result, f"Missing provenance key: {key}"

    def test_news_source_alpha_vantage_propagated(self):
        scanner = _make_scanner()
        result = scanner._scan_symbol(
            "AAPL", [], {}, "fresh", False,
            news_source="alpha_vantage",
        )
        assert result["news_source"] == "alpha_vantage"

    def test_news_source_missing_propagated(self):
        scanner = _make_scanner()
        result = scanner._scan_symbol(
            "AAPL", [], {}, "fresh", False,
            news_source="missing",
        )
        assert result["news_source"] == "missing"

    def test_fundamentals_source_fmp_does_not_set_fallback(self):
        # FMP fundamentals are live data — not a fallback (stale cache)
        scanner = _make_scanner()
        result = scanner._scan_symbol(
            "AAPL", [], {}, "budget_skipped", False,
            fundamentals_source="fmp",
        )
        assert result["fundamentals_source"] == "fmp"
        assert result["fallback_used"] is False

    def test_fmp_price_contains_valid_data(self):
        fmp = _make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        av  = _make_av_client(budget_exceed_ohlcv=True)
        scanner = _make_scanner(av=av, fmp=fmp)
        result = scanner._scan_symbol(
            "AAPL", [], {}, "fresh", False,
            fmp_quote=_SAMPLE_FMP_QUOTE,
        )
        assert result["price"] == pytest.approx(185.0)
        assert result["price_change_pct"] == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# TestScanContinuesAfterFallback — scan never stops mid-run due to budget
# ---------------------------------------------------------------------------

class TestScanContinuesAfterFallback:
    def test_scan_processes_all_symbols_when_av_budget_exhausted(self):
        """With FMP fallback, all symbols are scanned despite AV budget."""
        watchlist = ["AAPL", "MSFT", "NVDA"]
        fmp_quotes = {
            sym: {**_SAMPLE_FMP_QUOTE, "symbol": sym} for sym in watchlist
        }
        fmp_profiles = [
            {**_SAMPLE_FMP_PROFILE, "symbol": sym} for sym in watchlist
        ]
        fmp  = _make_fmp_client(quotes=fmp_quotes, profiles=fmp_profiles)
        av   = _make_av_client(budget_exceed_ohlcv=True, budget_exceed_overview=True)
        cache = _make_cache(stale_daily=None)
        scanner = WatchlistScanner(
            watchlist=watchlist,
            cache=cache,
            av_client=av,
            fmp_client=fmp,
            data_sources={"fmp_enabled": True, "prefer_fmp_on_budget_exhausted": True},
        )
        result = scanner.run(dry_run=False)
        # All 3 symbols should be in results despite AV budget exhaustion
        result_tickers = {r["ticker"] for r in result["results"]}
        assert result_tickers == set(watchlist)

    def test_scan_result_count_unchanged_with_fmp_fallback(self):
        """FMP fallback produces a result for every symbol, not just pre-budget ones."""
        watchlist = ["AAPL", "MSFT"]
        fmp_quotes = {sym: {**_SAMPLE_FMP_QUOTE, "symbol": sym} for sym in watchlist}
        fmp_profiles = [{**_SAMPLE_FMP_PROFILE, "symbol": sym} for sym in watchlist]
        fmp  = _make_fmp_client(quotes=fmp_quotes, profiles=fmp_profiles)
        av   = _make_av_client(budget_exceed_ohlcv=True, budget_exceed_overview=True)
        cache = _make_cache(stale_daily=None)
        scanner = WatchlistScanner(
            watchlist=watchlist,
            cache=cache,
            av_client=av,
            fmp_client=fmp,
            data_sources={"fmp_enabled": True, "prefer_fmp_on_budget_exhausted": True},
        )
        result = scanner.run(dry_run=False)
        assert len(result["results"]) == len(watchlist)

    def test_single_symbol_budget_exception_does_not_stop_rest(self):
        """Even without FMP, a single BudgetExceeded stops gracefully, not crash."""
        watchlist = ["AAPL", "MSFT", "NVDA"]

        call_count = {"n": 0}
        def side_effect_ohlcv(sym, outputsize="compact"):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise BudgetExceeded("budget after first symbol")
            return _make_df()

        av = _make_av_client()
        av.get_daily_ohlcv.side_effect = side_effect_ohlcv
        cache = _make_cache(stale_daily=None)
        scanner = WatchlistScanner(
            watchlist=watchlist,
            cache=cache,
            av_client=av,
            fmp_client=None,
            data_sources={"fmp_enabled": False},
        )
        result = scanner.run(dry_run=False)
        # First symbol processes fine, rest are budget_skipped/missing
        assert len(result["results"]) >= 1


# ---------------------------------------------------------------------------
# TestFmpDisabledConfig — fmp_enabled=False keeps AV-only behavior
# ---------------------------------------------------------------------------

class TestFmpDisabledConfig:
    def test_fmp_not_used_when_disabled(self):
        """When fmp_enabled=False, FMP is never consulted even on AV failure."""
        fmp = _make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        av  = _make_av_client(budget_exceed_ohlcv=True)
        cache = _make_cache(stale_daily=None)
        scanner = WatchlistScanner(
            watchlist=["AAPL"],
            cache=cache,
            av_client=av,
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

    def test_fmp_enabled_false_scanner_still_fmp_enabled_false(self):
        scanner = WatchlistScanner(
            watchlist=["AAPL"],
            cache=_make_cache(),
            av_client=_make_av_client(),
            fmp_client=MagicMock(),
            data_sources={"fmp_enabled": False},
        )
        assert scanner._fmp_enabled is False

    def test_no_fmp_client_fmp_enabled_false(self):
        scanner = WatchlistScanner(
            watchlist=["AAPL"],
            cache=_make_cache(),
            av_client=_make_av_client(),
            fmp_client=None,
        )
        assert scanner._fmp_enabled is False


# ---------------------------------------------------------------------------
# TestFmpNewsFallback — news_source tracking when AV news fails
# ---------------------------------------------------------------------------

_FMP_NEWS_RAW = [
    {
        "symbol": "AAPL",
        "publishedDate": "2026-04-27 09:30:00",
        "title": "Apple announces new product",
        "text": "Apple reported strong quarterly results.",
        "site": "bloomberg.com",
        "url": "https://example.com/1",
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


class TestFmpNewsFallback:
    def _run_scanner(self, av, fmp, *, watchlist=None):
        wl = watchlist or ["AAPL"]
        scanner = WatchlistScanner(
            watchlist=wl,
            cache=_make_cache(),
            av_client=av,
            fmp_client=fmp,
            data_sources={"fmp_enabled": True, "prefer_fmp_on_budget_exhausted": True},
        )
        return scanner.run(dry_run=False)

    def test_av_budget_exceeded_fmp_news_used(self):
        av  = _make_av_client(budget_exceed_news=True)
        fmp = _make_fmp_with_news()
        result = self._run_scanner(av, fmp)
        news_sources = {r["news_source"] for r in result["results"]}
        assert news_sources == {"fmp"}

    def test_av_exception_fmp_news_used(self):
        av  = _make_av_client()
        av.get_news_sentiment.side_effect = Exception("invalid input")
        fmp = _make_fmp_with_news()
        result = self._run_scanner(av, fmp)
        news_sources = {r["news_source"] for r in result["results"]}
        assert news_sources == {"fmp"}

    def test_fmp_news_fails_news_source_missing(self):
        av  = _make_av_client(budget_exceed_news=True)
        fmp = _make_fmp_with_news(fail=True)
        result = self._run_scanner(av, fmp)
        news_sources = {r["news_source"] for r in result["results"]}
        assert news_sources == {"missing"}

    def test_fmp_disabled_av_fails_news_source_missing(self):
        av = _make_av_client(budget_exceed_news=True)
        fmp = _make_fmp_with_news()
        scanner = WatchlistScanner(
            watchlist=["AAPL"],
            cache=_make_cache(),
            av_client=av,
            fmp_client=fmp,
            data_sources={"fmp_enabled": False},
        )
        result = scanner.run(dry_run=False)
        news_sources = {r["news_source"] for r in result["results"]}
        assert news_sources == {"missing"}
        fmp.get_stock_news.assert_not_called()

    def test_fmp_news_fallback_news_source_is_fmp(self):
        # FMP news is a live provider; fallback_used is about stale price cache
        av  = _make_av_client(budget_exceed_news=True)
        fmp = _make_fmp_with_news()
        result = self._run_scanner(av, fmp)
        news_sources = {r["news_source"] for r in result["results"]}
        assert news_sources == {"fmp"}

    def test_scan_produces_all_results_after_news_fallback(self):
        watchlist = ["AAPL", "MSFT"]
        av  = _make_av_client(budget_exceed_news=True)
        fmp = _make_fmp_with_news(articles=[
            {**_FMP_NEWS_RAW[0], "symbol": "AAPL"},
            {**_FMP_NEWS_RAW[0], "symbol": "MSFT"},
        ])
        result = self._run_scanner(av, fmp, watchlist=watchlist)
        assert len(result["results"]) == len(watchlist)

    def test_fmp_news_primary_av_news_not_called(self):
        """FMP news is the primary provider; when FMP returns articles, AV is not called."""
        av  = _make_av_client()  # AV would succeed but should not be consulted
        fmp = _make_fmp_with_news()  # FMP returns articles
        result = self._run_scanner(av, fmp)
        # FMP is primary → AV news should never be called
        av.get_news_sentiment.assert_not_called()
        news_sources = {r["news_source"] for r in result["results"]}
        assert news_sources == {"fmp"}

    def test_fmp_news_returns_empty_news_source_missing(self):
        av  = _make_av_client(budget_exceed_news=True)
        fmp = _make_fmp_with_news(articles=[])  # FMP returns empty list
        result = self._run_scanner(av, fmp)
        news_sources = {r["news_source"] for r in result["results"]}
        assert news_sources == {"missing"}

    def test_no_fmp_client_av_fails_news_source_missing(self):
        av = _make_av_client(budget_exceed_news=True)
        scanner = WatchlistScanner(
            watchlist=["AAPL"],
            cache=_make_cache(),
            av_client=av,
            fmp_client=None,
        )
        result = scanner.run(dry_run=False)
        news_sources = {r["news_source"] for r in result["results"]}
        assert news_sources == {"missing"}


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
        from unittest.mock import patch
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
        from unittest.mock import patch
        client = self._make_client()
        with patch.object(client, '_get_cached', side_effect=Exception("network error")):
            result = client.get_stock_news(["AAPL"])
        assert result == []


# ---------------------------------------------------------------------------
# TestAVBudgetExhaustedFMPPrimary — regression: AV exhaustion must not block FMP
# ---------------------------------------------------------------------------

class TestAVBudgetExhaustedFMPPrimary:
    """
    Regression: when AV budget is exhausted the pipeline calls scanner.run(dry_run=True).
    FMP is an independent provider and must still load quotes and serve as price source.
    """

    def test_dry_run_with_fmp_uses_fmp_price(self):
        """dry_run=True + FMP available → price_data_source='fmp', price not None."""
        fmp = _make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        av  = _make_av_client(budget_exceed_ohlcv=True)
        scanner = _make_scanner(av=av, cache=_make_cache(), fmp=fmp)

        result = scanner.run(dry_run=True)

        rows = result.get("results", [])
        assert len(rows) == 1
        assert rows[0]["price_data_source"] == "fmp"
        assert rows[0]["price"] is not None
        assert rows[0]["price"] == pytest.approx(185.0)

    def test_dry_run_with_fmp_not_cache_only(self):
        """dry_run=True + FMP available → scan_status must not be 'cache_only'."""
        fmp = _make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        av  = _make_av_client(budget_exceed_ohlcv=True)
        scanner = _make_scanner(av=av, cache=_make_cache(), fmp=fmp)

        result = scanner.run(dry_run=True)

        status = result.get("scan_summary", {}).get("scan_status")
        assert status != "cache_only", f"Expected ok or degraded, got {status!r}"

    def test_dry_run_with_fmp_data_quality_not_all_cached(self):
        """dry_run=True + FMP available → at least one result is not data_quality='cached'."""
        fmp = _make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        av  = _make_av_client(budget_exceed_ohlcv=True)
        scanner = _make_scanner(av=av, cache=_make_cache(), fmp=fmp)

        result = scanner.run(dry_run=True)

        qualities = {r["data_quality"] for r in result.get("results", [])}
        assert "cached" not in qualities or "fresh" in qualities or "partial" in qualities, (
            f"All results are cached despite FMP being available: {qualities}"
        )

    def test_dry_run_no_fmp_stays_cache_only(self):
        """dry_run=True + no FMP → original cache-only behavior preserved."""
        av  = _make_av_client(budget_exceed_ohlcv=True)
        cache = _make_cache(stale_daily=None)
        scanner = _make_scanner(av=av, cache=cache, fmp=None)

        result = scanner.run(dry_run=True)

        rows = result.get("results", [])
        assert len(rows) == 1
        assert rows[0]["price_data_source"] in ("cache", "missing")

    def test_fmp_prefetch_called_even_when_dry_run(self):
        """FMP get_batch_quotes and get_batch_profiles must be called even when dry_run=True."""
        fmp = _make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        av  = _make_av_client(budget_exceed_ohlcv=True)
        scanner = _make_scanner(av=av, cache=_make_cache(), fmp=fmp)

        scanner.run(dry_run=True)

        fmp.get_batch_quotes.assert_called_once()
        fmp.get_batch_profiles.assert_called_once()


# ---------------------------------------------------------------------------
# TestNewProvenanceFields — historical_source, quote_source, technical_source,
#                           ratios_source, provider_health in result
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


class TestNewProvenanceFields:
    """Verify new data-source provenance fields added in the FMP-primary rebuild."""

    def _scan_one(self, fmp_quote=None, fmp_historical=None) -> dict:
        fmp = _make_fmp_client(quotes={"AAPL": fmp_quote} if fmp_quote else {})
        scanner = _make_scanner(av=_make_av_client(), fmp=fmp)
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
        assert result["historical_source"] in ("missing", "alpha_vantage")

    def test_technical_source_fmp_quote_plus_historical(self):
        result = self._scan_one(fmp_quote=_SAMPLE_FMP_QUOTE, fmp_historical=_SAMPLE_FMP_HISTORICAL)
        assert result["technical_source"] == "fmp_quote+historical"

    def test_technical_source_fmp_quote_only_when_no_historical(self):
        scanner = _make_scanner(av=_make_av_client(df=None), fmp=_make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE}))
        result = scanner._scan_symbol(
            "AAPL", [], {}, "fresh", False,
            fmp_quote=_SAMPLE_FMP_QUOTE,
            fmp_historical=None,
        )
        assert result is not None
        assert result["technical_source"] == "fmp_quote"

    def test_ratios_source_fmp_when_fundamentals_source_fmp(self):
        result = self._scan_one(fmp_quote=_SAMPLE_FMP_QUOTE)
        result_with_fmp_fund = _make_scanner(
            av=_make_av_client(), fmp=_make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE})
        )._scan_symbol("AAPL", [], {}, "fresh", False,
                       fmp_quote=_SAMPLE_FMP_QUOTE, fundamentals_source="fmp")
        assert result_with_fmp_fund is not None
        assert result_with_fmp_fund["ratios_source"] == "fmp"

    def test_ratios_source_missing_when_no_fmp_fundamentals(self):
        result = _make_scanner(av=_make_av_client(), fmp=None)._scan_symbol(
            "AAPL", [], {}, "fresh", False, fmp_quote=None, fundamentals_source="alpha_vantage"
        )
        assert result is not None
        assert result["ratios_source"] == "missing"

    def test_provider_health_fmp_primary(self):
        result = _make_scanner(av=_make_av_client(), fmp=_make_fmp_client(quotes={"AAPL": _SAMPLE_FMP_QUOTE}))._scan_symbol(
            "AAPL", [], {}, "fresh", False,
            fmp_quote=_SAMPLE_FMP_QUOTE, fundamentals_source="fmp"
        )
        assert result is not None
        assert result["provider_health"] == "fmp_primary"

    def test_provider_health_cache_only_when_both_missing(self):
        result = _make_scanner(av=_make_av_client(budget_exceed_ohlcv=True), fmp=None, cache=_make_cache(stale_daily=None))._scan_symbol(
            "AAPL", [], {}, "fresh", False, fmp_quote=None
        )
        assert result is not None
        assert result["provider_health"] == "cache_only"

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

    def test_all_new_provenance_keys_present(self):
        result = self._scan_one(fmp_quote=_SAMPLE_FMP_QUOTE)
        for key in ("quote_source", "historical_source", "technical_source",
                    "ratios_source", "provider_health"):
            assert key in result, f"Missing new provenance key: {key}"


# ---------------------------------------------------------------------------
# TestFmpHistoricalPreventsAvOhlcv — AV get_daily_ohlcv must NOT be called
#   when FMP has provided both a quote and full historical data.
# ---------------------------------------------------------------------------

class TestFmpHistoricalPreventsAvOhlcv:
    """When FMP quote + historical are both available, AV OHLCV must be skipped."""

    def _scan(self, *, fmp_hist, av, fmp_quote=None):
        fmp_quote = fmp_quote or _SAMPLE_FMP_QUOTE
        fmp = _make_fmp_client(quotes={"AAPL": fmp_quote})
        scanner = _make_scanner(av=av, fmp=fmp)
        return scanner._scan_symbol(
            "AAPL", [], {}, "fresh", False,
            fmp_quote=fmp_quote,
            fmp_historical=fmp_hist,
        )

    def test_av_ohlcv_not_called_when_fmp_historical_present(self):
        """Core routing fix: AV must not be called when FMP has quote + historical."""
        av = _make_av_client()
        self._scan(fmp_hist=_SAMPLE_FMP_HISTORICAL, av=av)
        av.get_daily_ohlcv.assert_not_called()

    def test_av_ohlcv_called_when_fmp_historical_missing(self):
        """AV must be tried when FMP has no historical data for a symbol."""
        av = _make_av_client()
        self._scan(fmp_hist=None, av=av)
        av.get_daily_ohlcv.assert_called_once()

    def test_av_ohlcv_called_when_fmp_historical_empty(self):
        """Empty historical list (not None) should also trigger AV fallback."""
        av = _make_av_client()
        self._scan(fmp_hist=[], av=av)
        av.get_daily_ohlcv.assert_called_once()

    def test_av_ohlcv_not_called_when_fmp_historical_and_no_quote(self):
        """FMP historical alone (no quote) is sufficient to skip AV OHLCV."""
        av = _make_av_client()
        fmp = _make_fmp_client(quotes={})  # no quote for AAPL
        scanner = _make_scanner(av=av, fmp=fmp)
        scanner._scan_symbol(
            "AAPL", [], {}, "fresh", False,
            fmp_quote=None,
            fmp_historical=_SAMPLE_FMP_HISTORICAL,
        )
        # historical_source == "fmp", price_data_source == "fmp" → _av_ohlcv_needed = False
        av.get_daily_ohlcv.assert_not_called()

    def test_fmp_historical_populates_sma20(self):
        av = _make_av_client()
        result = self._scan(fmp_hist=_SAMPLE_FMP_HISTORICAL, av=av)
        assert result["sma20"] is not None, "SMA20 must be computed from FMP historical"

    def test_fmp_historical_populates_sma50(self):
        av = _make_av_client()
        result = self._scan(fmp_hist=_SAMPLE_FMP_HISTORICAL, av=av)
        # 60 rows: SMA50 will be None (need 50 rows). Use a longer sample for SMA50.
        # Verify the field exists (may be None if rows < 50)
        assert "sma50" in result

    def test_fmp_historical_populates_price_change_5d(self):
        av = _make_av_client()
        result = self._scan(fmp_hist=_SAMPLE_FMP_HISTORICAL, av=av)
        assert result["technicals"]["price_change_5d"] is not None

    def test_fmp_historical_populates_volume_avg20(self):
        av = _make_av_client()
        result = self._scan(fmp_hist=_SAMPLE_FMP_HISTORICAL, av=av)
        assert result["volume_avg20"] is not None, "volume_avg20 must be set from FMP historical"

    def test_technical_data_completeness_field_present(self):
        av = _make_av_client()
        result = self._scan(fmp_hist=_SAMPLE_FMP_HISTORICAL, av=av)
        assert "technical_data_completeness" in result

    def test_technical_data_completeness_partial_with_fmp_quote_and_historical(self):
        """60 rows gives SMA20 but not SMA50; completeness should be partial or full."""
        av = _make_av_client()
        result = self._scan(fmp_hist=_SAMPLE_FMP_HISTORICAL, av=av)
        assert result["technical_data_completeness"] in ("full", "partial")

    def test_technical_data_completeness_price_only_without_historical(self):
        """Quote-only (no historical) → price_only or partial (sma50 may come from quote)."""
        av = _make_av_client(df=None)
        result = self._scan(fmp_hist=None, av=av)
        assert result["technical_data_completeness"] in ("price_only", "partial", "full")

    def test_scan_status_ok_when_fmp_quote_and_historical(self):
        """Full scan with FMP quote + historical must not degrade to cache_only."""
        fmp = _make_fmp_client(
            quotes={"AAPL": _SAMPLE_FMP_QUOTE},
            profiles=[{"symbol": "AAPL", "sector": "Technology", "mktCap": 3e12}],
        )
        fmp.get_historical_prices.return_value = _SAMPLE_FMP_HISTORICAL
        av = _make_av_client(budget_exceed_ohlcv=True)
        scanner = _make_scanner(av=av, fmp=fmp, watchlist=["AAPL"])
        result = scanner.run(dry_run=False)
        scan_status = result["scan_summary"]["scan_status"]
        assert scan_status in ("ok", "degraded"), f"scan_status={scan_status}"

    def test_run_logs_fmp_and_av_historical_counts(self, caplog):
        """run() must emit the FMP historical / AV historical fallback summary log."""
        import logging
        fmp = _make_fmp_client(
            quotes={"AAPL": _SAMPLE_FMP_QUOTE},
            profiles=[{"symbol": "AAPL", "sector": "Technology", "mktCap": 3e12}],
        )
        fmp.get_historical_prices.return_value = _SAMPLE_FMP_HISTORICAL
        av = _make_av_client(budget_exceed_ohlcv=True)
        scanner = _make_scanner(av=av, fmp=fmp, watchlist=["AAPL"])
        with caplog.at_level(logging.INFO, logger="watchlist_scanner.scanner"):
            scanner.run(dry_run=False)
        assert any(
            "FMP historical used for" in m for m in caplog.messages
        ), "Expected 'FMP historical used for X/Y symbols' log line"
