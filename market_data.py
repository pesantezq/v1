"""
Market data module for fetching holdings prices.

FMP is the system's sole price provider. ``FMPMarketClient`` implements the
``get_prices(symbols) -> {sym: price|None}`` seam consumed by
``update_holdings_with_prices`` (and thus the live decision pipeline in
``main.py``), sourcing prices from FMP's ``get_batch_quotes`` (stable/quote).

The legacy Alpha Vantage client (and its rate limiter / price cache / daily
budget) was removed once FMP became the primary data provider — FMP has its
own disk cache, rate limiting, and budget counter inside ``fmp_client``.
"""

import logging
from typing import Any, Dict, Optional, Tuple

from utils import Holding

logger = logging.getLogger('portfolio_automation.market_data')


class FMPMarketClient:
    """FMP-backed holdings-price client.

    Implements the ``get_prices(symbols) -> {sym: price|None}`` seam that
    ``update_holdings_with_prices`` depends on, sourcing prices from FMP's
    ``get_batch_quotes`` (stable/quote).
    """

    def __init__(self, fmp_client: Any = None, daily_budget: int = 0):
        if fmp_client is None:
            from fmp_client import FMPClient
            # daily_budget<=0 means uncapped (matches config.json convention).
            fmp_client = FMPClient(daily_budget=daily_budget if daily_budget > 0 else 1_000_000)
        self._fmp = fmp_client

    def get_prices(self, symbols: list[str]) -> Dict[str, Optional[float]]:
        """Fetch current prices for symbols in a single batched FMP call."""
        if not symbols:
            return {}
        quotes = self._fmp.get_batch_quotes(symbols)
        out: Dict[str, Optional[float]] = {}
        for sym in symbols:
            q = quotes.get(sym.upper())
            price = q.get("price") if isinstance(q, dict) else None
            try:
                out[sym] = float(price) if price is not None else None
            except (TypeError, ValueError):
                out[sym] = None
        return out

    def get_quote(self, symbol: str) -> Optional[float]:
        """Get current price for a single symbol."""
        return self.get_prices([symbol]).get(symbol)


def create_market_client(
    config: Dict[str, Any],
    budget: Any = None,  # legacy AV-budget arg, ignored (FMP has its own budget)
    fmp_client: Any = None,
) -> "FMPMarketClient":
    """Factory: build the FMP-backed holdings-price client from config.

    The legacy AlphaVantage path has been removed; FMP is the sole provider.
    ``budget`` is accepted for backward compatibility and ignored.
    """
    return FMPMarketClient(
        fmp_client=fmp_client,
        daily_budget=int(config.get('fmp_daily_calls_budget', 0) or 0),
    )


def update_holdings_with_prices(
    holdings: list[Holding],
    market_client: "FMPMarketClient",
) -> Tuple[list[Holding], list[str]]:
    """
    Update holdings with current market prices.
    Returns tuple of (updated_holdings, failed_symbols).
    """
    symbols = [h.symbol for h in holdings]
    prices = market_client.get_prices(symbols)

    failed_symbols = []

    for holding in holdings:
        price = prices.get(holding.symbol)
        if price is not None:
            holding.current_price = price
            holding.market_value = price * holding.shares
        else:
            failed_symbols.append(holding.symbol)
            # Keep existing values or set to None
            if holding.current_price is None:
                holding.market_value = None

    return holdings, failed_symbols
