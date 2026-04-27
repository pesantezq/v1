"""
Tests for universe/fmp_universe.py

All tests are offline — FMPClient is replaced with a mock that returns
pre-built fixture data so no network or API key is required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from universe.fmp_universe import FMPUniverse, _is_valid_equity_symbol, _passes_filters


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SP500_CONSTITUENTS = [
    {"symbol": "AAPL", "sector": "Technology"},
    {"symbol": "MSFT", "sector": "Technology"},
    {"symbol": "XOM",  "sector": "Energy"},
    {"symbol": "JPM",  "sector": "Financials"},
    {"symbol": "TSLA", "sector": "Consumer Discretionary"},
]

_PROFILES_V3 = [
    {"symbol": "AAPL", "mktCap": 3_000_000_000_000, "price": 185.0, "sector": "Technology"},
    {"symbol": "MSFT", "mktCap": 2_500_000_000_000, "price": 415.0, "sector": "Technology"},
    {"symbol": "XOM",  "mktCap":   450_000_000_000, "price":  95.0, "sector": "Energy"},
    {"symbol": "JPM",  "mktCap":   600_000_000_000, "price": 180.0, "sector": "Financials"},
    {"symbol": "TSLA", "mktCap":   800_000_000_000, "price": 170.0, "sector": "Consumer Discretionary"},
]


def _make_client(*, fail_profiles: bool = False, profiles_v3: list | None = None) -> MagicMock:
    client = MagicMock()
    client.get_sp500_constituents.return_value = _SP500_CONSTITUENTS
    if fail_profiles:
        client.get_batch_profiles_v3.side_effect = Exception("timeout")
    else:
        client.get_batch_profiles_v3.return_value = profiles_v3 if profiles_v3 is not None else _PROFILES_V3
    client.get_bulk_profiles.return_value = _PROFILES_V3
    return client


# ---------------------------------------------------------------------------
# _is_valid_equity_symbol
# ---------------------------------------------------------------------------

def test_valid_symbols():
    assert _is_valid_equity_symbol("AAPL")
    assert _is_valid_equity_symbol("MSFT")
    assert _is_valid_equity_symbol("A")


def test_invalid_symbols():
    assert not _is_valid_equity_symbol("")
    assert not _is_valid_equity_symbol("TOOLONGX")    # > 5 chars
    assert not _is_valid_equity_symbol("ABC.WS")      # non-alpha
    assert not _is_valid_equity_symbol("123")         # numeric


# ---------------------------------------------------------------------------
# _passes_filters
# ---------------------------------------------------------------------------

def test_passes_filters_happy_path():
    row = {"symbol": "AAPL", "mktCap": 3_000_000_000_000, "price": 185.0}
    assert _passes_filters(row, min_market_cap=500_000_000, min_price=5.0)


def test_passes_filters_below_market_cap():
    row = {"symbol": "TINY", "mktCap": 100_000_000, "price": 10.0}
    assert not _passes_filters(row, min_market_cap=500_000_000, min_price=5.0)


def test_passes_filters_penny_stock():
    row = {"symbol": "PENY", "mktCap": 800_000_000, "price": 1.50}
    assert not _passes_filters(row, min_market_cap=500_000_000, min_price=5.0)


def test_passes_filters_invalid_symbol():
    row = {"symbol": "BAD.WS", "mktCap": 1_000_000_000, "price": 20.0}
    assert not _passes_filters(row, min_market_cap=500_000_000, min_price=5.0)


# ---------------------------------------------------------------------------
# FMPUniverse.get_full_market_universe
# ---------------------------------------------------------------------------

def test_get_full_market_universe_returns_list():
    client = _make_client()
    universe = FMPUniverse(client)
    result = universe.get_full_market_universe(min_market_cap=500_000_000, min_price=5.0)
    assert isinstance(result, list)
    assert len(result) > 0


def test_min_market_cap_filter():
    # XOM has mktCap=450B < threshold of 500B → should be excluded
    client = _make_client()
    universe = FMPUniverse(client)
    result = universe.get_full_market_universe(min_market_cap=500_000_000_000, min_price=5.0)
    symbols = [r["symbol"] for r in result]
    assert "XOM" not in symbols
    assert "AAPL" in symbols


def test_penny_stock_filter():
    profiles_with_penny = _PROFILES_V3 + [
        {"symbol": "PENY", "mktCap": 600_000_000, "price": 1.50, "sector": "Technology"}
    ]
    client = _make_client(profiles_v3=profiles_with_penny)
    universe = FMPUniverse(client)
    result = universe.get_full_market_universe(min_market_cap=500_000_000, min_price=5.0)
    symbols = [r["symbol"] for r in result]
    assert "PENY" not in symbols


def test_max_symbols_limit():
    client = _make_client()
    universe = FMPUniverse(client)
    result = universe.get_full_market_universe(min_market_cap=0, min_price=0, max_symbols=2)
    assert len(result) == 2


def test_sorted_descending_by_market_cap():
    client = _make_client()
    universe = FMPUniverse(client)
    result = universe.get_full_market_universe(min_market_cap=0, min_price=0)
    caps = [r.get("mktCap", 0) for r in result]
    assert caps == sorted(caps, reverse=True)


def test_empty_fmp_response_returns_empty_list():
    client = _make_client()
    client.get_sp500_constituents.return_value = []
    universe = FMPUniverse(client)
    result = universe.get_full_market_universe()
    assert result == []


def test_profiles_failure_falls_back_to_constituents():
    client = _make_client(fail_profiles=True)
    universe = FMPUniverse(client)
    # Should not raise; returns constituent dicts with mktCap=0 → filtered out
    result = universe.get_full_market_universe(min_market_cap=500_000_000)
    # All rows have mktCap=0 so they fail the filter; result can be empty
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# FMPUniverse.get_symbols
# ---------------------------------------------------------------------------

def test_get_symbols_returns_sorted_list():
    client = _make_client()
    universe = FMPUniverse(client)
    symbols = universe.get_symbols(min_market_cap=500_000_000, min_price=5.0)
    assert isinstance(symbols, list)
    assert symbols == sorted(symbols)


# ---------------------------------------------------------------------------
# FMPUniverse.get_hybrid_universe
# ---------------------------------------------------------------------------

def test_get_hybrid_universe_combines_watchlist_and_fmp():
    client = _make_client()
    universe = FMPUniverse(client)
    watchlist = ["NVDA", "META"]   # not in FMP fixture
    result = universe.get_hybrid_universe(watchlist, min_market_cap=0, min_price=0)
    # Watchlist symbols appear first
    assert result[0] == "NVDA"
    assert result[1] == "META"
    # FMP symbols are appended after
    for sym in ["AAPL", "MSFT"]:
        assert sym in result


def test_get_hybrid_universe_deduplicates():
    client = _make_client()
    universe = FMPUniverse(client)
    watchlist = ["AAPL", "MSFT"]   # also in FMP fixture
    result = universe.get_hybrid_universe(watchlist, min_market_cap=0, min_price=0)
    # No duplicates
    assert len(result) == len(set(result))
    # Watchlist symbols still present
    assert "AAPL" in result
    assert "MSFT" in result


def test_get_hybrid_universe_respects_max_symbols():
    client = _make_client()
    universe = FMPUniverse(client)
    result = universe.get_hybrid_universe(["NVDA"], min_market_cap=0, min_price=0, max_symbols=3)
    assert len(result) <= 3


# ---------------------------------------------------------------------------
# Premium endpoint path
# ---------------------------------------------------------------------------

def test_premium_uses_bulk_profiles():
    client = _make_client()
    universe = FMPUniverse(client, use_premium=True)
    result = universe.get_full_market_universe(min_market_cap=0, min_price=0)
    client.get_bulk_profiles.assert_called_once()
    client.get_sp500_constituents.assert_not_called()
    assert len(result) > 0
