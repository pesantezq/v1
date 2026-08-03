"""
S&P 500 Universe Manager

Returns the current S&P 500 symbol list, resolved through a fail-closed provider
chain (FMP → free public source → last-good cache) rather than a single endpoint.

Why the chain exists: until 2026-08-03 this was a thin wrapper over
``FMPClient.get_sp500_constituents()``. FMP then retired the v3 legacy API for
this key, so that call returns HTTP 403 and the wrapper raised — which took out
``CandidateScanner.full_scan()``, the only scanner path able to ADD a symbol to
the watchlist. The universe could only shrink from that point on. See
``universe/sp500_constituents.py`` for the full account.

Provenance is exposed via ``last_resolution`` so a degraded (cache-served) read
is distinguishable from a live one. Caching is handled by the resolver; FMPClient
still caches its own responses with a 7-day TTL.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, List, Optional

from universe.sp500_constituents import (
    DEFAULT_CACHE_PATH,
    ConstituentResolution,
    resolve_constituents,
)


class SP500Universe:
    """Provides the current S&P 500 symbol list and constituent metadata.

    Args:
        client:     FMPClient (or any object exposing ``get_sp500_constituents``).
                    Tried first; skipped when it fails or returns a short list.
        cache_path: Where the last-good constituent list is persisted.
        fetcher:    Override for the free source (injected by tests).
    """

    def __init__(
        self,
        client: Any,
        cache_path: Path | str = DEFAULT_CACHE_PATH,
        fetcher: Optional[Callable[[], list[dict]]] = None,
    ) -> None:
        self._client = client
        self._cache_path = cache_path
        self._fetcher = fetcher
        # None until the first resolve; callers use it to report provenance.
        self.last_resolution: Optional[ConstituentResolution] = None

    def resolve(self, ttl_days: int = 7, now: str | None = None) -> ConstituentResolution:
        """Resolve constituents and record provenance.

        Raises ``ConstituentSourceError`` when no source yields a plausible list —
        never returns an empty universe, which downstream guards would misread as
        "healthy, just empty".
        """
        resolution = resolve_constituents(
            client=self._client,
            cache_path=self._cache_path,
            fetcher=self._fetcher,
            ttl_days=ttl_days,
            now=now,
        )
        self.last_resolution = resolution
        return resolution

    def get_symbols(self, ttl_days: int = 7) -> List[str]:
        """Return sorted list of current S&P 500 ticker symbols."""
        return self.resolve(ttl_days=ttl_days).symbols

    def get_constituents(self, ttl_days: int = 7) -> List[dict]:
        """Return raw constituent dicts (includes sector, name, sub-sector)."""
        return self.resolve(ttl_days=ttl_days).rows
