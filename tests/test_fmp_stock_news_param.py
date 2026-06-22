"""
Regression test for FMPClient.get_stock_news() request parameter.

Root cause (2026-06-22): the stable `news/stock` endpoint filters by `symbols=`,
NOT the legacy v3 `tickers=`. The code sent `tickers=`, which the stable endpoint
silently ignored — returning a generic latest-news feed (currently AAPL-saturated)
regardless of the requested tickers. Result: 24/25 watchlist symbols fetched 0 news
and scored as if newsless. Verified live:
  tickers=NVDA  -> 10 articles, all symbol=AAPL  (param ignored)
  symbols=NVDA  -> 10 NVDA articles             (correct)
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from fmp_client import FMPClient, FMP_STABLE_BASE_URL


def _make_client() -> FMPClient:
    return FMPClient(
        api_key="test_key",
        daily_budget=230,
        cache_dir=Path(tempfile.mkdtemp()),
    )


def _raw_article(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "title": f"{symbol} headline",
        "text": "body",
        "site": "example.com",
        "publishedDate": "2026-06-22 10:00:00",
    }


class TestGetStockNewsUsesSymbolsParam:

    def test_sends_symbols_param_not_tickers(self):
        client = _make_client()
        params_seen: list[dict] = []

        def mock_raw(endpoint, params, **kwargs):
            params_seen.append(dict(params))
            return [_raw_article("NVDA")]

        with patch.object(client, "_raw_get", side_effect=mock_raw):
            client.get_stock_news(["NVDA"], limit=50, ttl_hours=0)

        assert params_seen, "expected a request to be issued"
        p = params_seen[0]
        assert "symbols" in p, f"stable news endpoint requires symbols=, got {p}"
        assert "tickers" not in p, f"legacy tickers= is ignored by the stable endpoint, got {p}"

    def test_symbols_param_is_comma_joined(self):
        client = _make_client()
        params_seen: list[dict] = []

        def mock_raw(endpoint, params, **kwargs):
            params_seen.append(dict(params))
            return []

        with patch.object(client, "_raw_get", side_effect=mock_raw):
            client.get_stock_news(["NVDA", "TSLA", "MSFT"], limit=50, ttl_hours=0)

        assert params_seen[0]["symbols"] == "NVDA,TSLA,MSFT"

    def test_uses_stable_base_url(self):
        client = _make_client()
        kwargs_seen: list[dict] = []

        def mock_raw(endpoint, params, **kwargs):
            kwargs_seen.append(kwargs)
            return []

        with patch.object(client, "_raw_get", side_effect=mock_raw):
            client.get_stock_news(["NVDA"], limit=50, ttl_hours=0)

        assert kwargs_seen[0].get("base_url") == FMP_STABLE_BASE_URL
