"""
S&P 500 Universe Manager

Thin wrapper around FMPClient that returns the current S&P 500 symbol list.
Caching is handled entirely by FMPClient (7-day TTL by default).
"""

from typing import List

from fmp_client import FMPClient


class SP500Universe:
    """Provides the current S&P 500 symbol list and constituent metadata."""

    def __init__(self, client: FMPClient) -> None:
        self._client = client

    def get_symbols(self, ttl_days: int = 7) -> List[str]:
        """Return sorted list of current S&P 500 ticker symbols."""
        constituents = self._client.get_sp500_constituents(ttl_days=ttl_days)
        return sorted(c['symbol'] for c in constituents if c.get('symbol'))

    def get_constituents(self, ttl_days: int = 7) -> List[dict]:
        """Return raw constituent dicts (includes sector, name, sub-sector)."""
        return self._client.get_sp500_constituents(ttl_days=ttl_days)
