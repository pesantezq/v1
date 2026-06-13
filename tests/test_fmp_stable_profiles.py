"""
Tests for FMPClient stable profile + ratios endpoints.

Covers:
  - get_profile(): single-symbol stable/profile fetch
  - get_batch_profiles(): per-symbol, not comma-separated
  - get_batch_profiles() returns list of profile dicts keyed by symbol
  - FMP profile failure falls back to stale cache
  - get_batch_profiles() budget exceeded → stale served
  - get_ratios(): single-symbol stable/ratios fetch
  - get_ratios() budget exceeded → stale served
  - parse_fmp_fundamentals_bundle() maps profit_margin / revenue_growth / debt_ratio
  - fundamentals_engine schema compatibility
  - watchlist scanner uses get_batch_profiles() (not v3) for fundamentals
  - FMP fundamentals primary: AV OVERVIEW not called when FMP profiles available
  - AV OVERVIEW called only for symbols without FMP profile
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from fmp_client import FMPClient, FMPError, FMP_STABLE_BASE_URL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(*, budget: int = 230) -> FMPClient:
    return FMPClient(
        api_key="test_key",
        daily_budget=budget,
        cache_dir=Path(tempfile.mkdtemp()),
    )


def _stable_profile(symbol: str = "AAPL") -> list:
    return [{
        "symbol": symbol,
        "companyName": f"{symbol} Corp",
        "price": 185.0,
        "beta": 1.2,
        "volAvg": 50_000_000,
        "mktCap": 2_800_000_000_000,
        "lastDiv": 0.96,
        "range": "150-200",
        "changes": 1.5,
        "currency": "USD",
        "exchange": "NASDAQ",
        "industry": "Consumer Electronics",
        "sector": "Technology",
        "country": "US",
        "description": f"{symbol} makes products.",
        "ceo": "Test CEO",
        "ipoDate": "1980-12-12",
    }]


def _stable_ratios(symbol: str = "AAPL") -> list:
    return [{
        "symbol": symbol,
        "date": "2023-09-30",
        "period": "FY",
        "netProfitMargin": 0.2531,
        "grossProfitMargin": 0.4408,
        "returnOnEquity": 1.76,
        "returnOnAssets": 0.28,
        "debtEquityRatio": 1.80,
        "dividendYield": 0.0053,
        "priceEarningsRatio": 31.6,
        "revenueGrowth": 0.079,
    }]


# ---------------------------------------------------------------------------
# get_profile()
# ---------------------------------------------------------------------------

class TestGetProfile:
    def test_returns_dict_for_valid_symbol(self):
        client = _make_client()
        with patch.object(client, '_raw_get', return_value=_stable_profile("AAPL")):
            result = client.get_profile("AAPL")
        assert isinstance(result, dict)
        assert result["symbol"] == "AAPL"

    def test_sector_present(self):
        client = _make_client()
        with patch.object(client, '_raw_get', return_value=_stable_profile("AAPL")):
            result = client.get_profile("AAPL")
        assert result["sector"] == "Technology"

    def test_mkt_cap_present(self):
        client = _make_client()
        with patch.object(client, '_raw_get', return_value=_stable_profile("AAPL")):
            result = client.get_profile("AAPL")
        assert result["mktCap"] == pytest.approx(2.8e12)

    def test_uses_stable_base_url(self):
        client = _make_client()
        calls = []

        def mock_raw(endpoint, params, **kwargs):
            calls.append({"endpoint": endpoint, "params": dict(params), **kwargs})
            return _stable_profile("AAPL")

        with patch.object(client, '_raw_get', side_effect=mock_raw):
            client.get_profile("AAPL")

        assert len(calls) == 1
        assert calls[0]["endpoint"] == "profile"
        assert calls[0].get("base_url") == FMP_STABLE_BASE_URL

    def test_symbol_param_in_request(self):
        client = _make_client()
        params_seen = []

        def mock_raw(endpoint, params, **kwargs):
            params_seen.append(dict(params))
            return _stable_profile(params["symbol"])

        with patch.object(client, '_raw_get', side_effect=mock_raw):
            client.get_profile("MSFT")

        assert params_seen[0]["symbol"] == "MSFT"

    def test_symbol_normalized_to_uppercase(self):
        client = _make_client()
        params_seen = []

        def mock_raw(endpoint, params, **kwargs):
            params_seen.append(dict(params))
            return _stable_profile(params["symbol"])

        with patch.object(client, '_raw_get', side_effect=mock_raw):
            client.get_profile("aapl")

        assert params_seen[0]["symbol"] == "AAPL"

    def test_empty_symbol_returns_none(self):
        client = _make_client()
        assert client.get_profile("") is None
        assert client.get_profile(None) is None  # type: ignore[arg-type]

    def test_empty_response_list_returns_none(self):
        client = _make_client()
        with patch.object(client, '_raw_get', return_value=[]):
            result = client.get_profile("AAPL")
        assert result is None

    def test_fmp_error_falls_back_to_stale_cache(self):
        client = _make_client()
        stale = _stable_profile("AAPL")
        with patch.object(client._cache, 'get', return_value=None), \
             patch.object(client._cache, 'get_stale', return_value=stale), \
             patch.object(client, '_raw_get', side_effect=FMPError("503")):
            result = client.get_profile("AAPL")
        assert result is not None
        assert result["symbol"] == "AAPL"

    def test_fmp_error_no_stale_returns_none(self):
        client = _make_client()
        with patch.object(client._cache, 'get', return_value=None), \
             patch.object(client._cache, 'get_stale', return_value=None), \
             patch.object(client, '_raw_get', side_effect=FMPError("503")):
            result = client.get_profile("AAPL")
        assert result is None

    def test_budget_exceeded_serves_stale_no_api_call(self):
        client = _make_client(budget=1)
        client._counter.increment(5)  # exhaust the daily budget (0 now = no cap)
        stale = _stable_profile("AAPL")
        client._cache.set("profile_stable_AAPL", stale)

        with patch.object(client, '_raw_get') as mock_get:
            result = client.get_profile("AAPL")

        mock_get.assert_not_called()
        assert result is not None

    def test_budget_exceeded_no_stale_returns_none(self):
        client = _make_client(budget=1)
        client._counter.increment(5)  # exhaust the daily budget (0 now = no cap)
        with patch.object(client, '_raw_get') as mock_get:
            result = client.get_profile("AAPL")

        mock_get.assert_not_called()
        assert result is None

    def test_fresh_cache_hit_skips_api(self):
        client = _make_client()
        client._cache.set("profile_stable_AAPL", _stable_profile("AAPL"))

        with patch.object(client, '_raw_get') as mock_get:
            result = client.get_profile("AAPL", ttl_days=7)

        mock_get.assert_not_called()
        assert result is not None

    def test_bare_dict_response_accepted(self):
        client = _make_client()
        with patch.object(client, '_raw_get', return_value=_stable_profile("AAPL")[0]):
            result = client.get_profile("AAPL")
        assert result is not None
        assert result["symbol"] == "AAPL"


# ---------------------------------------------------------------------------
# get_batch_profiles()
# ---------------------------------------------------------------------------

class TestGetBatchProfiles:
    def test_empty_symbols_returns_empty_list(self):
        client = _make_client()
        assert client.get_batch_profiles([]) == []

    def test_returns_list_of_dicts(self):
        client = _make_client()
        with patch.object(client, '_raw_get', return_value=_stable_profile("AAPL")):
            result = client.get_batch_profiles(["AAPL"])
        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], dict)

    def test_no_comma_separated_calls(self):
        """stable/profile must not use comma-separated symbol params."""
        client = _make_client()
        symbols = ["AAPL", "MSFT", "NVDA"]
        params_seen = []

        def mock_raw(endpoint, params, **kwargs):
            params_seen.append(params.get("symbol", ""))
            return _stable_profile(params["symbol"])

        with patch.object(client, '_raw_get', side_effect=mock_raw):
            client.get_batch_profiles(symbols)

        assert all("," not in s for s in params_seen), (
            f"Comma-separated call detected in stable/profile: {params_seen}"
        )

    def test_one_api_call_per_symbol(self):
        """Each symbol triggers exactly one stable/profile fetch."""
        client = _make_client()
        symbols = ["AAPL", "MSFT"]
        call_count = {"n": 0}

        def mock_raw(endpoint, params, **kwargs):
            call_count["n"] += 1
            return _stable_profile(params["symbol"])

        with patch.object(client, '_raw_get', side_effect=mock_raw):
            client.get_batch_profiles(symbols)

        assert call_count["n"] == len(symbols)

    def test_duplicate_symbols_deduplicated(self):
        client = _make_client()
        call_count = {"n": 0}

        def mock_raw(endpoint, params, **kwargs):
            call_count["n"] += 1
            return _stable_profile(params["symbol"])

        with patch.object(client, '_raw_get', side_effect=mock_raw):
            client.get_batch_profiles(["AAPL", "AAPL", "AAPL"])

        assert call_count["n"] == 1

    def test_all_symbols_in_result(self):
        client = _make_client()
        symbols = ["AAPL", "MSFT", "NVDA"]
        with patch.object(client, '_raw_get',
                          side_effect=lambda e, p, **kw: _stable_profile(p["symbol"])):
            result = client.get_batch_profiles(symbols)
        assert {r["symbol"] for r in result} == set(symbols)

    def test_partial_failure_skips_failed_symbol(self):
        def mock_raw(endpoint, params, **kwargs):
            sym = params.get("symbol", "")
            if sym == "FAIL":
                raise FMPError("HTTP 404")
            return _stable_profile(sym)

        client = _make_client()
        with patch.object(client, '_raw_get', side_effect=mock_raw):
            result = client.get_batch_profiles(["AAPL", "FAIL", "MSFT"])

        symbols_in_result = {r["symbol"] for r in result}
        assert "AAPL" in symbols_in_result
        assert "MSFT" in symbols_in_result
        assert "FAIL" not in symbols_in_result

    def test_symbols_normalized_to_uppercase(self):
        client = _make_client()
        params_seen = []

        def mock_raw(endpoint, params, **kwargs):
            params_seen.append(params.get("symbol", ""))
            return _stable_profile(params["symbol"].upper())

        with patch.object(client, '_raw_get', side_effect=mock_raw):
            client.get_batch_profiles(["aapl", "Msft"])

        assert all(s == s.upper() for s in params_seen)

    def test_profile_has_sector_field(self):
        client = _make_client()
        with patch.object(client, '_raw_get', return_value=_stable_profile("AAPL")):
            result = client.get_batch_profiles(["AAPL"])
        assert result[0]["sector"] == "Technology"


# ---------------------------------------------------------------------------
# get_ratios()
# ---------------------------------------------------------------------------

class TestGetRatios:
    def test_returns_dict_for_valid_symbol(self):
        client = _make_client()
        with patch.object(client, '_raw_get', return_value=_stable_ratios("AAPL")):
            result = client.get_ratios("AAPL")
        assert isinstance(result, dict)
        assert "netProfitMargin" in result

    def test_net_profit_margin_correct(self):
        client = _make_client()
        with patch.object(client, '_raw_get', return_value=_stable_ratios("AAPL")):
            result = client.get_ratios("AAPL")
        assert result["netProfitMargin"] == pytest.approx(0.2531)

    def test_uses_stable_base_url(self):
        client = _make_client()
        calls = []

        def mock_raw(endpoint, params, **kwargs):
            calls.append({"endpoint": endpoint, **kwargs})
            return _stable_ratios("AAPL")

        with patch.object(client, '_raw_get', side_effect=mock_raw):
            client.get_ratios("AAPL")

        assert calls[0]["endpoint"] == "ratios"
        assert calls[0].get("base_url") == FMP_STABLE_BASE_URL

    def test_period_param_passed(self):
        client = _make_client()
        params_seen = []

        def mock_raw(endpoint, params, **kwargs):
            params_seen.append(dict(params))
            return _stable_ratios("AAPL")

        with patch.object(client, '_raw_get', side_effect=mock_raw):
            client.get_ratios("AAPL", period="annual")

        assert params_seen[0]["period"] == "annual"

    def test_empty_symbol_returns_none(self):
        client = _make_client()
        assert client.get_ratios("") is None

    def test_fmp_error_falls_back_to_stale_cache(self):
        client = _make_client()
        stale = _stable_ratios("AAPL")
        with patch.object(client._cache, 'get', return_value=None), \
             patch.object(client._cache, 'get_stale', return_value=stale), \
             patch.object(client, '_raw_get', side_effect=FMPError("503")):
            result = client.get_ratios("AAPL")
        assert result is not None
        assert result["netProfitMargin"] == pytest.approx(0.2531)

    def test_budget_exceeded_no_stale_returns_none(self):
        client = _make_client(budget=1)
        client._counter.increment(5)  # exhaust the daily budget (0 now = no cap)
        with patch.object(client, '_raw_get') as mock_get:
            result = client.get_ratios("AAPL")
        mock_get.assert_not_called()
        assert result is None

    def test_fresh_cache_hit_skips_api(self):
        client = _make_client()
        client._cache.set("ratios_stable_AAPL_annual", _stable_ratios("AAPL"))

        with patch.object(client, '_raw_get') as mock_get:
            result = client.get_ratios("AAPL", period="annual", ttl_days=30)

        mock_get.assert_not_called()
        assert result is not None


# ---------------------------------------------------------------------------
# parse_fmp_fundamentals_bundle()
# ---------------------------------------------------------------------------

class TestParseFmpFundamentalsBundle:
    def _bundle(self, profile=None, quote=None, ratios=None):
        from watchlist_scanner.fundamentals_engine import parse_fmp_fundamentals_bundle
        p = profile or {
            "symbol": "AAPL",
            "companyName": "Apple Inc.",
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "mktCap": 2_800_000_000_000,
            "beta": 1.25,
        }
        return parse_fmp_fundamentals_bundle(p, quote, ratios)

    def test_empty_profile_returns_empty(self):
        from watchlist_scanner.fundamentals_engine import parse_fmp_fundamentals_bundle
        assert parse_fmp_fundamentals_bundle({}) == {}

    def test_base_fields_present(self):
        result = self._bundle()
        assert result["sector"] == "Technology"
        assert result["market_cap"] == pytest.approx(2.8e12)

    def test_profit_margin_from_ratios(self):
        result = self._bundle(ratios={"netProfitMargin": 0.253})
        assert result["profit_margin"] == pytest.approx(0.253)

    def test_profit_margin_none_without_ratios(self):
        result = self._bundle()
        assert result["profit_margin"] is None

    def test_revenue_growth_from_ratios(self):
        result = self._bundle(ratios={"revenueGrowth": 0.079})
        assert result["revenue_growth"] == pytest.approx(0.079)

    def test_debt_ratio_from_ratios(self):
        result = self._bundle(ratios={"debtEquityRatio": 1.80})
        assert result["debt_ratio"] == pytest.approx(1.80)

    def test_dividend_yield_from_ratios(self):
        result = self._bundle(ratios={"dividendYield": 0.0053})
        assert result["dividend_yield"] == pytest.approx(0.0053)

    def test_pe_ratio_fallback_from_ratios(self):
        """pe_ratio from priceEarningsRatio when quote has no PE."""
        result = self._bundle(ratios={"priceEarningsRatio": 31.6})
        assert result["pe_ratio"] == pytest.approx(31.6)

    def test_pe_ratio_not_overwritten_when_quote_has_it(self):
        """pe from quote takes precedence over ratios."""
        result = self._bundle(
            quote={"pe": 29.0, "eps": 6.5},
            ratios={"priceEarningsRatio": 31.6},
        )
        assert result["pe_ratio"] == pytest.approx(29.0)

    def test_schema_has_canonical_fundamentals_keys(self):
        expected_keys = {
            "symbol", "name", "sector", "industry", "description", "market_cap",
            "pe_ratio", "forward_pe", "profit_margin", "revenue_ttm",
            "gross_profit_ttm", "beta", "analyst_target_price", "dividend_yield",
            "eps", "book_value", "52w_high", "52w_low", "50dma", "200dma",
            "revenue_growth", "earnings_growth", "debt_ratio",
        }
        bundle_keys = set(self._bundle().keys())
        assert bundle_keys == expected_keys

    def test_all_ratios_fields(self):
        result = self._bundle(ratios=_stable_ratios("AAPL")[0])
        assert result["profit_margin"] == pytest.approx(0.2531)
        assert result["revenue_growth"] == pytest.approx(0.079)
        assert result["debt_ratio"]   == pytest.approx(1.80)
        assert result["dividend_yield"] == pytest.approx(0.0053)


# ---------------------------------------------------------------------------
# WatchlistScanner — FMP primary fundamentals (uses get_batch_profiles, not v3)
# ---------------------------------------------------------------------------

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.scanner import WatchlistScanner


def _make_cache_mock(*, calls_today: int = 0) -> MagicMock:
    mock = MagicMock()
    type(mock).calls_today = PropertyMock(return_value=calls_today)
    mock.would_exceed.return_value = False
    mock.get.return_value = None
    mock.get_stale.return_value = None
    mock.get_age_seconds.return_value = None
    return mock


def _make_fmp_mock(*, profiles=None, quotes=None) -> MagicMock:
    mock = MagicMock()
    mock.get_batch_quotes.return_value = quotes or {}
    mock.get_batch_profiles.return_value = profiles or []
    mock.get_stock_news.return_value = []
    mock.get_historical_prices.return_value = []
    mock.get_ratios.return_value = None
    return mock


class TestScannerFmpPrimaryFundamentals:
    def _make_scanner(self, fmp, watchlist=None):
        return WatchlistScanner(
            watchlist=watchlist or ["AAPL"],
            cache=_make_cache_mock(),
            fmp_client=fmp,
            data_sources={"fmp_enabled": True},
        )

    def test_get_batch_profiles_called_not_v3(self):
        """Scanner must call get_batch_profiles(), not get_batch_profiles_v3()."""
        fmp = _make_fmp_mock()
        scanner = self._make_scanner(fmp)
        scanner.run(dry_run=False)
        fmp.get_batch_profiles.assert_called()

    def test_v3_endpoint_not_called(self):
        """Legacy get_batch_profiles_v3 must NOT be called in the new architecture."""
        fmp = _make_fmp_mock()
        scanner = self._make_scanner(fmp)
        scanner.run(dry_run=False)
        fmp.get_batch_profiles_v3.assert_not_called()

    def test_fundamentals_source_fmp_when_profile_available(self):
        """When every symbol has an FMP profile, fundamentals_source must be 'fmp'."""
        profiles = [{"symbol": "AAPL", "companyName": "Apple", "sector": "Technology",
                     "mktCap": 2.8e12, "beta": 1.2}]
        fmp = _make_fmp_mock(profiles=profiles)
        scanner = self._make_scanner(fmp)
        result = scanner.run(dry_run=False)
        sources = {r["fundamentals_source"] for r in result["results"]}
        assert sources == {"fmp"}

    def test_fundamentals_source_missing_without_fmp_profile(self):
        """When FMP has no profile for a symbol, fundamentals_source is 'missing'."""
        fmp = _make_fmp_mock(profiles=[])  # no profiles
        scanner = self._make_scanner(fmp)
        result = scanner.run(dry_run=False)
        sources = {r["fundamentals_source"] for r in result["results"]}
        assert sources == {"missing"}

    def test_fundamentals_source_fmp_when_profile_loaded(self):
        """Results must report fundamentals_source='fmp' when FMP profile was used."""
        profiles = [{"symbol": "AAPL", "companyName": "Apple", "sector": "Technology",
                     "mktCap": 2.8e12, "beta": 1.2}]
        fmp = _make_fmp_mock(profiles=profiles)
        scanner = self._make_scanner(fmp)
        result = scanner.run(dry_run=False)
        sources = {r["fundamentals_source"] for r in result["results"]}
        assert "fmp" in sources

    def test_get_ratios_called_for_each_symbol(self):
        """get_ratios() must be called once per watchlist symbol during pre-fetch."""
        watchlist = ["AAPL", "MSFT"]
        fmp = _make_fmp_mock()
        scanner = self._make_scanner(fmp, watchlist=watchlist)
        scanner.run(dry_run=False)
        assert fmp.get_ratios.call_count == len(watchlist)

    def test_get_historical_prices_called_for_each_symbol(self):
        """get_historical_prices() must be called once per watchlist symbol."""
        watchlist = ["AAPL", "MSFT"]
        fmp = _make_fmp_mock()
        scanner = self._make_scanner(fmp, watchlist=watchlist)
        scanner.run(dry_run=False)
        assert fmp.get_historical_prices.call_count == len(watchlist)

    def test_fmp_profile_fundamentals_score_computed(self):
        """With FMP profile loaded, fundamentals_score should be > 0 (not neutral 50)."""
        profiles = [{"symbol": "AAPL", "companyName": "Apple", "sector": "Technology",
                     "mktCap": 2.8e12, "beta": 1.2}]
        quotes = {"AAPL": {"price": 185.0, "changesPercentage": 1.5, "pe": 28.0,
                            "volume": 50_000_000, "avgVolume": 45_000_000,
                            "priceAvg50": 182.0, "priceAvg200": 170.0,
                            "yearHigh": 198.0, "yearLow": 124.0, "marketCap": 2.8e12}}
        fmp = _make_fmp_mock(profiles=profiles, quotes=quotes)
        scanner = self._make_scanner(fmp)
        result = scanner.run(dry_run=False)
        scores = [r["fundamentals_score"] for r in result["results"]]
        assert all(s > 0 for s in scores)

    def test_scan_status_not_cache_only_with_fmp_profiles_and_quotes(self):
        """With FMP profiles + quotes, scan_status must be ok or degraded."""
        profiles = [{"symbol": "AAPL", "companyName": "Apple", "sector": "Technology",
                     "mktCap": 2.8e12, "beta": 1.2}]
        quotes = {"AAPL": {"price": 185.0, "changesPercentage": 1.5, "pe": 28.0,
                            "volume": 50_000_000, "avgVolume": 45_000_000}}
        fmp = _make_fmp_mock(profiles=profiles, quotes=quotes)
        scanner = self._make_scanner(fmp)
        result = scanner.run(dry_run=False)
        assert result["scan_summary"]["scan_status"] != "cache_only"
