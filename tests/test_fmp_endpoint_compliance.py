"""
Endpoint compliance tests — verify every FMP method uses the correct
stable/ endpoint path and NOT a paywalled v3/v4 path.

Each test:
  1. Creates a real FMPClient with a temp cache dir.
  2. Patches _cache.get → None (cache miss) so the live-fetch branch runs.
  3. Patches _counter.would_exceed → False (budget available).
  4. Patches _raw_get to capture call args and return a minimal valid payload.
  5. Asserts that _raw_get was called with the expected stable path and
     base_url=FMP_STABLE_BASE_URL.

If any method regresses to a v3/v4 path these tests will fail immediately.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from fmp_client import (
    FMPClient,
    FMP_STABLE_BASE_URL,
    _EP_QUOTE,
    _EP_PROFILE,
    _EP_RATIOS,
    _EP_HISTORICAL,
    _EP_NEWS_STOCK,
    _EP_INCOME_STMT,
    _EP_KEY_METRICS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client() -> FMPClient:
    return FMPClient(api_key="test_key", cache_dir=Path(tempfile.mkdtemp()))


def _patch_for_live_fetch(client: FMPClient):
    """Return a context-manager triple that forces the live-fetch branch."""
    cache_miss = patch.object(client._cache, "get", return_value=None)
    budget_ok  = patch.object(client._counter, "would_exceed", return_value=False)
    cache_set  = patch.object(client._cache, "set")
    return cache_miss, budget_ok, cache_set


# ---------------------------------------------------------------------------
# quote
# ---------------------------------------------------------------------------

class TestQuoteUsesStableEndpoint:
    def test_endpoint_path_is_stable_quote(self):
        client = _make_client()
        raw_resp = [{"symbol": "AAPL", "price": 170.0, "changesPercentage": 1.0}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp) as mock_rg:
            client.get_batch_quotes(["AAPL"])
        mock_rg.assert_called_once_with(
            _EP_QUOTE, {"symbol": "AAPL"}, base_url=FMP_STABLE_BASE_URL
        )

    def test_endpoint_is_not_v3(self):
        client = _make_client()
        raw_resp = [{"symbol": "AAPL", "price": 170.0, "changesPercentage": 1.0}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp) as mock_rg:
            client.get_batch_quotes(["AAPL"])
        called_endpoint = mock_rg.call_args[0][0]
        assert "v3" not in called_endpoint, f"Expected stable endpoint, got: {called_endpoint}"
        assert "v4" not in called_endpoint, f"Expected stable endpoint, got: {called_endpoint}"

    def test_base_url_is_stable(self):
        client = _make_client()
        raw_resp = [{"symbol": "MSFT", "price": 300.0}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp) as mock_rg:
            client.get_batch_quotes(["MSFT"])
        assert mock_rg.call_args[1]["base_url"] == FMP_STABLE_BASE_URL


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------

class TestProfileUsesStableEndpoint:
    def test_endpoint_path_is_stable_profile(self):
        client = _make_client()
        raw_resp = [{"symbol": "AAPL", "sector": "Technology", "mktCap": 3e12}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp) as mock_rg:
            client.get_profile("AAPL")
        mock_rg.assert_called_once_with(
            _EP_PROFILE, {"symbol": "AAPL"}, base_url=FMP_STABLE_BASE_URL
        )

    def test_endpoint_is_not_v3(self):
        client = _make_client()
        raw_resp = [{"symbol": "AAPL", "sector": "Technology"}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp) as mock_rg:
            client.get_profile("AAPL")
        called_endpoint = mock_rg.call_args[0][0]
        assert "v3" not in called_endpoint

    def test_no_comma_separated_batch_in_path(self):
        """stable/profile must be called per-symbol, not with AAPL,MSFT in the path."""
        client = _make_client()
        raw_resp = [{"symbol": "AAPL", "sector": "Technology"}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp) as mock_rg:
            client.get_batch_profiles(["AAPL", "MSFT"])
        for c in mock_rg.call_args_list:
            endpoint_arg = c[0][0]
            assert "," not in endpoint_arg, f"Comma-sep symbols in endpoint path: {endpoint_arg}"


# ---------------------------------------------------------------------------
# historical prices
# ---------------------------------------------------------------------------

class TestHistoricalUsesStableEndpoint:
    def test_endpoint_path_is_stable_historical(self):
        client = _make_client()
        raw_resp = [{"date": "2025-01-01", "close": 150.0, "volume": 1_000_000}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp) as mock_rg:
            client.get_historical_prices("AAPL", years=1)
        called_endpoint = mock_rg.call_args[0][0]
        assert called_endpoint == _EP_HISTORICAL, f"Expected {_EP_HISTORICAL!r}, got {called_endpoint!r}"

    def test_endpoint_is_not_v3_historical_price_full(self):
        client = _make_client()
        raw_resp = [{"date": "2025-01-01", "close": 150.0}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp) as mock_rg:
            client.get_historical_prices("AAPL", years=1)
        called_endpoint = mock_rg.call_args[0][0]
        assert "historical-price-full" not in called_endpoint, (
            "Old v3/historical-price-full path used — must use stable endpoint"
        )
        assert "v3" not in called_endpoint

    def test_symbol_passed_as_query_param_not_path(self):
        client = _make_client()
        raw_resp = [{"date": "2025-01-01", "close": 150.0}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp) as mock_rg:
            client.get_historical_prices("AAPL", years=1)
        params = mock_rg.call_args[0][1]
        assert "symbol" in params, "symbol must be a query param, not embedded in path"
        assert params["symbol"] == "AAPL"

    def test_base_url_is_stable(self):
        client = _make_client()
        raw_resp = [{"date": "2025-01-01", "close": 150.0}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp) as mock_rg:
            client.get_historical_prices("NVDA", years=1)
        assert mock_rg.call_args[1]["base_url"] == FMP_STABLE_BASE_URL


# ---------------------------------------------------------------------------
# ratios
# ---------------------------------------------------------------------------

class TestRatiosUsesStableEndpoint:
    def test_endpoint_path_is_stable_ratios(self):
        client = _make_client()
        raw_resp = [{"symbol": "AAPL", "netProfitMargin": 0.25}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp) as mock_rg:
            client.get_ratios("AAPL")
        mock_rg.assert_called_once_with(
            _EP_RATIOS,
            {"symbol": "AAPL", "period": "annual", "limit": "1"},
            base_url=FMP_STABLE_BASE_URL,
        )

    def test_endpoint_is_not_v3(self):
        client = _make_client()
        raw_resp = [{"symbol": "AAPL", "netProfitMargin": 0.25}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp) as mock_rg:
            client.get_ratios("AAPL")
        assert "v3" not in mock_rg.call_args[0][0]


# ---------------------------------------------------------------------------
# key metrics
# ---------------------------------------------------------------------------

class TestKeyMetricsUsesStableEndpoint:
    def test_endpoint_path_is_stable_key_metrics(self):
        client = _make_client()
        raw_resp = [{"symbol": "AAPL", "returnOnEquity": 0.18, "priceEarningsRatio": 28.0}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp) as mock_rg:
            client.get_key_metrics("AAPL")
        mock_rg.assert_called_once_with(
            _EP_KEY_METRICS,
            {"symbol": "AAPL", "period": "annual", "limit": "1"},
            base_url=FMP_STABLE_BASE_URL,
        )

    def test_endpoint_is_not_v3(self):
        client = _make_client()
        raw_resp = [{"symbol": "AAPL", "returnOnEquity": 0.18}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp) as mock_rg:
            client.get_key_metrics("AAPL")
        assert "v3" not in mock_rg.call_args[0][0]

    def test_returns_first_element_from_list(self):
        client = _make_client()
        raw_resp = [{"symbol": "AAPL", "returnOnEquity": 0.18}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp):
            result = client.get_key_metrics("AAPL")
        assert result["returnOnEquity"] == 0.18

    def test_returns_none_on_empty_response(self):
        client = _make_client()
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=[]):
            result = client.get_key_metrics("AAPL")
        assert result is None

    def test_returns_none_on_error(self):
        client = _make_client()
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", side_effect=Exception("HTTP 403")):
            result = client.get_key_metrics("AAPL")
        assert result is None


# ---------------------------------------------------------------------------
# income statement
# ---------------------------------------------------------------------------

class TestIncomeStatementUsesStableEndpoint:
    def test_endpoint_path_is_stable_income_statement(self):
        client = _make_client()
        raw_resp = [{"symbol": "AAPL", "revenue": 400e9, "netIncome": 100e9}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp) as mock_rg:
            client.get_income_statement("AAPL")
        mock_rg.assert_called_once_with(
            _EP_INCOME_STMT,
            {"symbol": "AAPL", "period": "annual", "limit": "1"},
            base_url=FMP_STABLE_BASE_URL,
        )

    def test_endpoint_is_not_v3(self):
        client = _make_client()
        raw_resp = [{"symbol": "AAPL", "revenue": 400e9}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp) as mock_rg:
            client.get_income_statement("AAPL")
        assert "v3" not in mock_rg.call_args[0][0]

    def test_returns_first_element_from_list(self):
        client = _make_client()
        raw_resp = [{"symbol": "MSFT", "revenue": 230e9}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp):
            result = client.get_income_statement("MSFT")
        assert result["revenue"] == 230e9

    def test_returns_none_on_empty_response(self):
        client = _make_client()
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=[]):
            result = client.get_income_statement("AAPL")
        assert result is None

    def test_returns_none_on_error(self):
        client = _make_client()
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", side_effect=Exception("HTTP 403")):
            result = client.get_income_statement("AAPL")
        assert result is None

    def test_cache_key_is_per_symbol_and_period(self):
        """Different symbols must not share a cache key."""
        client = _make_client()
        captured_keys: list[str] = []

        orig_get = client._cache.get
        def tracking_get(key, ttl):
            captured_keys.append(key)
            return None  # always miss
        client._cache.get = tracking_get

        raw_resp = [{"symbol": "AAPL", "revenue": 400e9}]
        with patch.object(client._counter, "would_exceed", return_value=False), \
             patch.object(client._cache, "set"), \
             patch.object(client, "_raw_get", return_value=raw_resp):
            client.get_income_statement("AAPL")
            client.get_income_statement("MSFT")

        assert captured_keys[0] != captured_keys[1], "Cache keys must differ per symbol"
        assert "AAPL" in captured_keys[0]
        assert "MSFT" in captured_keys[1]


# ---------------------------------------------------------------------------
# news
# ---------------------------------------------------------------------------

class TestNewsUsesStableEndpoint:
    def test_endpoint_path_is_stable_news_stock(self):
        client = _make_client()
        raw_resp = [{"title": "Apple news", "symbol": "AAPL", "publishedDate": "2025-01-01"}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp) as mock_rg:
            client.get_stock_news(["AAPL"])
        called_endpoint = mock_rg.call_args[0][0]
        assert called_endpoint == _EP_NEWS_STOCK, (
            f"Expected {_EP_NEWS_STOCK!r}, got {called_endpoint!r}"
        )

    def test_endpoint_is_not_v3_stock_news(self):
        client = _make_client()
        raw_resp = [{"title": "Apple news", "symbol": "AAPL"}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp) as mock_rg:
            client.get_stock_news(["AAPL"])
        called_endpoint = mock_rg.call_args[0][0]
        assert "stock_news" not in called_endpoint, (
            "Old v3/stock_news path used — must use stable/news/stock"
        )
        assert "v3" not in called_endpoint

    def test_base_url_is_stable(self):
        client = _make_client()
        raw_resp = [{"title": "News", "symbol": "AAPL", "publishedDate": "2025-01-01"}]
        cm, bk, cs = _patch_for_live_fetch(client)
        with cm, bk, cs, patch.object(client, "_raw_get", return_value=raw_resp) as mock_rg:
            client.get_stock_news(["AAPL"])
        assert mock_rg.call_args[1]["base_url"] == FMP_STABLE_BASE_URL


# ---------------------------------------------------------------------------
# get_fundamentals_v3 prefers stable
# ---------------------------------------------------------------------------

class TestFundamentalsV3PrefersStable:
    def test_get_key_metrics_called_before_v3_fallback(self):
        """When stable/key-metrics succeeds, the legacy v3 endpoints must NOT
        be called. revenueGrowth is still sourced from stable/financial-growth
        (key-metrics does not carry it)."""
        client = _make_client()
        km_stable = {"returnOnEquity": 0.18, "priceEarningsRatio": 28.0}
        endpoints = []

        def mock_raw(endpoint, params, **kwargs):
            endpoints.append(endpoint)
            if endpoint == "financial-growth":
                return [{"revenueGrowth": 0.21}]
            return []

        with patch.object(client, "get_key_metrics", return_value=km_stable) as mock_km, \
             patch.object(client, "_raw_get", side_effect=mock_raw):
            result = client.get_fundamentals_v3(["AAPL"])

        mock_km.assert_called_once_with("AAPL", period="annual", ttl_days=7)
        # No legacy v3 endpoint may be hit when the stable path succeeds.
        assert not any(str(e).startswith("v3/") for e in endpoints)
        assert result[0]["roe"] == 0.18
        # revenueGrowth must come from the stable financial-growth endpoint.
        assert result[0]["revenueGrowth"] == 0.21

    def test_v3_fallback_used_when_stable_returns_none(self):
        """When stable/key-metrics fails, v3 endpoints are tried as fallback."""
        client = _make_client()
        v3_km_data = [{"roe": 0.12, "peRatio": 22.0, "freeCashFlowYield": 0.05}]

        with patch.object(client, "get_key_metrics", return_value=None), \
             patch.object(client, "_get_cached", side_effect=[v3_km_data, []]):
            result = client.get_fundamentals_v3(["AAPL"])

        assert result[0]["roe"] == 0.12
