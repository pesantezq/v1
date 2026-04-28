"""
Tests for FMPClient.get_batch_quotes() via the FMP stable/quote endpoint.

Covers:
  - stable single-symbol quote success
  - comma-separated NOT used (per-symbol fetches)
  - partial failures return available quotes
  - stale cache fallback on FMPError
  - get_batch_quotes returns dict keyed by symbol
  - changesPercentage aliased from changePercentage
  - budget exceeded → stale cache served, no API call
  - budget exceeded + no cache → symbol skipped gracefully
  - fresh cache hit → no API call
  - duplicate symbols deduplicated to one fetch
  - symbol casing normalised to uppercase
  - empty API response → symbol absent from result
  - _extract_stable_quote edge cases
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from fmp_client import (
    FMPClient,
    FMPError,
    FMP_STABLE_BASE_URL,
    _extract_stable_quote,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(*, budget: int = 230) -> FMPClient:
    return FMPClient(
        api_key="test_key",
        daily_budget=budget,
        cache_dir=Path(tempfile.mkdtemp()),
    )


def _stable_response(symbol: str, price: float = 150.0) -> list:
    """Minimal stable/quote list response for one symbol."""
    return [{
        "symbol": symbol,
        "price": price,
        "changePercentage": 1.5,
        "change": 2.25,
        "volume": 10_000_000,
        "avgVolume": 8_000_000,
        "dayLow": 148.0,
        "dayHigh": 152.0,
        "yearHigh": 200.0,
        "yearLow": 100.0,
        "marketCap": 2_500_000_000_000,
        "priceAvg50": 145.0,
        "priceAvg200": 140.0,
        "exchange": "NASDAQ",
        "open": 149.0,
        "previousClose": 147.75,
        "timestamp": 1700000000,
    }]


# ---------------------------------------------------------------------------
# _extract_stable_quote
# ---------------------------------------------------------------------------

class TestExtractStableQuote:
    def test_list_response_returns_dict(self):
        result = _extract_stable_quote(_stable_response("AAPL"))
        assert isinstance(result, dict)
        assert result["price"] == pytest.approx(150.0)

    def test_bare_dict_accepted(self):
        result = _extract_stable_quote(_stable_response("AAPL")[0])
        assert result is not None
        assert result["symbol"] == "AAPL"

    def test_empty_list_returns_none(self):
        assert _extract_stable_quote([]) is None

    def test_none_returns_none(self):
        assert _extract_stable_quote(None) is None

    def test_non_dict_non_list_returns_none(self):
        assert _extract_stable_quote("bad") is None
        assert _extract_stable_quote(42) is None

    def test_changespercentage_aliased(self):
        raw = [{"symbol": "AAPL", "price": 150.0, "changePercentage": 2.5}]
        result = _extract_stable_quote(raw)
        assert result["changesPercentage"] == pytest.approx(2.5)

    def test_changespercentage_not_overwritten_when_present(self):
        raw = [{"symbol": "AAPL", "price": 150.0, "changePercentage": 2.5, "changesPercentage": 3.0}]
        result = _extract_stable_quote(raw)
        assert result["changesPercentage"] == pytest.approx(3.0)

    def test_no_changepercentage_no_alias(self):
        raw = [{"symbol": "AAPL", "price": 150.0}]
        result = _extract_stable_quote(raw)
        assert "changesPercentage" not in result

    def test_original_dict_not_mutated(self):
        original = {"symbol": "AAPL", "price": 100.0, "changePercentage": 1.0}
        _ = _extract_stable_quote([original])
        assert "changesPercentage" not in original


# ---------------------------------------------------------------------------
# get_batch_quotes — basic contract
# ---------------------------------------------------------------------------

class TestGetBatchQuotesContract:
    def test_empty_symbols_returns_empty_dict(self):
        client = _make_client()
        assert client.get_batch_quotes([]) == {}

    def test_returns_dict_keyed_by_symbol(self):
        client = _make_client()
        with patch.object(client, '_raw_get', return_value=_stable_response("AAPL")):
            result = client.get_batch_quotes(["AAPL"])
        assert isinstance(result, dict)
        assert "AAPL" in result

    def test_price_present_in_quote(self):
        client = _make_client()
        with patch.object(client, '_raw_get', return_value=_stable_response("AAPL")):
            result = client.get_batch_quotes(["AAPL"])
        assert result["AAPL"]["price"] == pytest.approx(150.0)

    def test_changespercentage_aliased_in_result(self):
        client = _make_client()
        with patch.object(client, '_raw_get', return_value=_stable_response("AAPL")):
            result = client.get_batch_quotes(["AAPL"])
        assert "changesPercentage" in result["AAPL"]
        assert result["AAPL"]["changesPercentage"] == pytest.approx(1.5)

    def test_all_requested_symbols_in_result(self):
        client = _make_client()
        symbols = ["AAPL", "MSFT", "NVDA"]
        with patch.object(
            client, '_raw_get',
            side_effect=lambda e, p, **kw: _stable_response(p["symbol"]),
        ):
            result = client.get_batch_quotes(symbols)
        assert set(result.keys()) == {"AAPL", "MSFT", "NVDA"}


# ---------------------------------------------------------------------------
# get_batch_quotes — per-symbol fetching (not comma-separated)
# ---------------------------------------------------------------------------

class TestPerSymbolFetching:
    def test_stable_endpoint_used(self):
        """_raw_get must be called with base_url=FMP_STABLE_BASE_URL."""
        client = _make_client()
        recorded = []

        def mock_raw_get(endpoint, params, **kwargs):
            recorded.append({"endpoint": endpoint, "params": dict(params), **kwargs})
            return _stable_response(params["symbol"])

        with patch.object(client, '_raw_get', side_effect=mock_raw_get):
            client.get_batch_quotes(["AAPL"])

        assert len(recorded) == 1
        assert recorded[0]["endpoint"] == "quote"
        assert recorded[0].get("base_url") == FMP_STABLE_BASE_URL

    def test_no_comma_separated_calls(self):
        """Stable API doesn't support comma-separated — verify single-symbol calls."""
        client = _make_client()
        symbols = ["AAPL", "MSFT", "NVDA"]
        call_params = []

        def mock_raw_get(endpoint, params, **kwargs):
            call_params.append(params.get("symbol", ""))
            return _stable_response(params["symbol"])

        with patch.object(client, '_raw_get', side_effect=mock_raw_get):
            client.get_batch_quotes(symbols)

        assert all("," not in s for s in call_params), (
            f"Comma-separated call detected: {call_params}"
        )
        assert set(call_params) == {"AAPL", "MSFT", "NVDA"}

    def test_symbol_param_single_in_each_call(self):
        """Each _raw_get call carries exactly one symbol."""
        client = _make_client()
        symbols = ["AAPL", "MSFT"]
        params_seen = []

        def mock_raw_get(endpoint, params, **kwargs):
            params_seen.append(dict(params))
            return _stable_response(params["symbol"])

        with patch.object(client, '_raw_get', side_effect=mock_raw_get):
            client.get_batch_quotes(symbols)

        for p in params_seen:
            assert len(p["symbol"].split(",")) == 1


# ---------------------------------------------------------------------------
# get_batch_quotes — partial failures
# ---------------------------------------------------------------------------

class TestPartialFailures:
    def test_fmperror_for_one_does_not_stop_others(self):
        """When one symbol 403s, the rest are still returned."""
        def mock_raw_get(endpoint, params, **kwargs):
            sym = params.get("symbol", "")
            if sym == "FAIL":
                raise FMPError(f"HTTP 403 for {sym}")
            return _stable_response(sym)

        client = _make_client()
        with patch.object(client, '_raw_get', side_effect=mock_raw_get):
            result = client.get_batch_quotes(["AAPL", "FAIL", "MSFT"])

        assert "AAPL" in result
        assert "MSFT" in result
        assert "FAIL" not in result

    def test_exception_for_one_does_not_stop_others(self):
        """Generic Exception for one symbol still processes the rest."""
        def mock_raw_get(endpoint, params, **kwargs):
            sym = params.get("symbol", "")
            if sym == "ERR":
                raise RuntimeError("network timeout")
            return _stable_response(sym)

        client = _make_client()
        with patch.object(client, '_raw_get', side_effect=mock_raw_get):
            result = client.get_batch_quotes(["AAPL", "ERR"])

        assert "AAPL" in result
        assert "ERR" not in result

    def test_empty_response_symbol_absent(self):
        """Empty list response → symbol not included in result."""
        client = _make_client()
        with patch.object(client, '_raw_get', return_value=[]):
            result = client.get_batch_quotes(["AAPL"])
        assert "AAPL" not in result


# ---------------------------------------------------------------------------
# get_batch_quotes — stale cache fallback
# ---------------------------------------------------------------------------

class TestStaleCacheFallback:
    def test_fmperror_falls_back_to_stale_cache(self):
        client = _make_client()
        stale = _stable_response("AAPL", price=140.0)
        with patch.object(client._cache, 'get', return_value=None), \
             patch.object(client._cache, 'get_stale', return_value=stale), \
             patch.object(client, '_raw_get', side_effect=FMPError("403")):
            result = client.get_batch_quotes(["AAPL"])
        assert "AAPL" in result
        assert result["AAPL"]["price"] == pytest.approx(140.0)

    def test_fmperror_no_stale_symbol_absent(self):
        client = _make_client()
        with patch.object(client._cache, 'get', return_value=None), \
             patch.object(client._cache, 'get_stale', return_value=None), \
             patch.object(client, '_raw_get', side_effect=FMPError("403")):
            result = client.get_batch_quotes(["AAPL"])
        assert "AAPL" not in result

    def test_budget_exceeded_stale_served_no_api_call(self):
        """budget=0 forces stale-only path; _raw_get must not be called."""
        client = _make_client(budget=0)
        client._cache.set("quote_stable_AAPL", _stable_response("AAPL"))

        with patch.object(client, '_raw_get') as mock_get:
            result = client.get_batch_quotes(["AAPL"])

        mock_get.assert_not_called()
        assert "AAPL" in result
        assert result["AAPL"]["price"] == pytest.approx(150.0)

    def test_budget_exceeded_no_stale_symbol_skipped_no_crash(self):
        client = _make_client(budget=0)
        with patch.object(client, '_raw_get') as mock_get:
            result = client.get_batch_quotes(["AAPL"])
        mock_get.assert_not_called()
        assert "AAPL" not in result

    def test_stale_quote_changespercentage_aliased(self):
        client = _make_client()
        stale = [{"symbol": "AAPL", "price": 120.0, "changePercentage": -0.5}]
        with patch.object(client._cache, 'get', return_value=None), \
             patch.object(client._cache, 'get_stale', return_value=stale), \
             patch.object(client, '_raw_get', side_effect=FMPError("403")):
            result = client.get_batch_quotes(["AAPL"])
        assert result["AAPL"]["changesPercentage"] == pytest.approx(-0.5)


# ---------------------------------------------------------------------------
# get_batch_quotes — cache behaviour
# ---------------------------------------------------------------------------

class TestCacheBehaviour:
    def test_fresh_cache_hit_skips_api_call(self):
        client = _make_client()
        client._cache.set("quote_stable_AAPL", _stable_response("AAPL"))

        with patch.object(client, '_raw_get') as mock_get:
            result = client.get_batch_quotes(["AAPL"], ttl_hours=1)

        mock_get.assert_not_called()
        assert "AAPL" in result

    def test_duplicate_symbols_one_fetch(self):
        client = _make_client()
        call_count = {"n": 0}

        def mock_raw_get(endpoint, params, **kwargs):
            call_count["n"] += 1
            return _stable_response(params["symbol"])

        with patch.object(client, '_raw_get', side_effect=mock_raw_get):
            result = client.get_batch_quotes(["AAPL", "AAPL", "AAPL"])

        assert call_count["n"] == 1
        assert "AAPL" in result

    def test_symbol_normalized_to_uppercase(self):
        client = _make_client()
        with patch.object(
            client, '_raw_get',
            side_effect=lambda e, p, **kw: _stable_response(p["symbol"].upper()),
        ):
            result = client.get_batch_quotes(["aapl", "Msft"])

        assert "AAPL" in result
        assert "MSFT" in result

    def test_result_contains_all_stable_fields(self):
        client = _make_client()
        with patch.object(client, '_raw_get', return_value=_stable_response("AAPL")):
            result = client.get_batch_quotes(["AAPL"])
        q = result["AAPL"]
        for key in ("price", "volume", "dayHigh", "dayLow", "yearHigh", "yearLow",
                    "marketCap", "priceAvg50", "priceAvg200", "avgVolume"):
            assert key in q, f"missing key: {key}"
