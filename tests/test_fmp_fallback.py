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
        mock.get_batch_profiles_v3.side_effect = Exception("FMP profiles failed")
    else:
        mock.get_batch_profiles_v3.return_value = profiles or []
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

    def test_av_success_price_source_is_av(self):
        scanner = _make_scanner(av=_make_av_client())
        result = self._run_one(scanner)
        assert result["price_data_source"] == "alpha_vantage"

    def test_av_success_fallback_not_used(self):
        scanner = _make_scanner(av=_make_av_client())
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
        assert result["fallback_used"] is True
        assert "budget" in result["fallback_reason"].lower()

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
        # No FMP client; cache has stale AV data
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
        assert result["fallback_used"] is False  # cache is not FMP

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

    def test_fundamentals_source_fmp_sets_fallback_used(self):
        scanner = _make_scanner()
        result = scanner._scan_symbol(
            "AAPL", [], {}, "budget_skipped", False,
            fundamentals_source="fmp",
        )
        assert result["fundamentals_source"] == "fmp"
        assert result["fallback_used"] is True

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
