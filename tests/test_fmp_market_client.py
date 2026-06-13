"""Tests for the FMP-backed holdings-price client (AlphaVantage replacement).

market_data.FMPMarketClient replaces AlphaVantageClient as the holdings-price
source for the live decision pipeline (main.py run_portfolio_update). It must
satisfy the same get_prices(symbols)->{sym: price|None} seam that
update_holdings_with_prices depends on, but source prices from FMP's
get_batch_quotes instead of Alpha Vantage GLOBAL_QUOTE.
"""
import market_data
from utils import Holding


class _FakeFMP:
    """Minimal stand-in for fmp_client.FMPClient.get_batch_quotes."""

    def __init__(self, quotes: dict[str, float]):
        self._q = {k.upper(): v for k, v in quotes.items()}
        self.calls: list[list[str]] = []

    def get_batch_quotes(self, symbols, **_kw):
        self.calls.append(list(symbols))
        return {s.upper(): {"price": self._q[s.upper()]}
                for s in symbols if s.upper() in self._q}


def _h(symbol, shares):
    return Holding(symbol=symbol, shares=shares, target_weight=0.0, asset_class="equity")


def test_get_prices_maps_batch_quotes_in_one_call():
    fmp = _FakeFMP({"QQQ": 500.0, "AAPL": 200.0})
    client = market_data.FMPMarketClient(fmp_client=fmp)
    prices = client.get_prices(["QQQ", "AAPL", "MISSING"])
    assert prices["QQQ"] == 500.0
    assert prices["AAPL"] == 200.0
    assert prices["MISSING"] is None
    # Batched: a single get_batch_quotes call, not one per symbol.
    assert fmp.calls == [["QQQ", "AAPL", "MISSING"]]


def test_get_quote_single_symbol():
    fmp = _FakeFMP({"QQQ": 500.0})
    client = market_data.FMPMarketClient(fmp_client=fmp)
    assert client.get_quote("QQQ") == 500.0
    assert client.get_quote("NOPE") is None


def test_update_holdings_with_prices_via_fmp():
    fmp = _FakeFMP({"QQQ": 500.0})
    client = market_data.FMPMarketClient(fmp_client=fmp)
    holdings = [_h("QQQ", 2.0), _h("ZZZ", 1.0)]
    updated, failed = market_data.update_holdings_with_prices(holdings, client)
    qqq = next(h for h in updated if h.symbol == "QQQ")
    assert qqq.current_price == 500.0
    assert qqq.market_value == 1000.0
    assert failed == ["ZZZ"]


def test_create_market_client_is_fmp_backed():
    """create_market_client must return the FMP-backed client (no AlphaVantage)."""
    fmp = _FakeFMP({"QQQ": 1.0})
    client = market_data.create_market_client(
        {"fmp_daily_calls_budget": 0}, fmp_client=fmp
    )
    assert isinstance(client, market_data.FMPMarketClient)
    assert client.get_quote("QQQ") == 1.0
