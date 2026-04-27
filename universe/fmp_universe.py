"""
FMP Market Universe

Provides a filtered, investment-grade US stock universe built from FMP data.

Free-tier strategy:
  - get_full_market_universe() uses get_sp500_constituents() as the primary
    source (1 call, free tier) then enriches with batch profiles for price +
    market-cap filtering.  Falls back to SP 500 symbols-only when profiles
    are unavailable.
  - Premium-tier users can pass use_premium=True to pull the full
    get_bulk_profiles() universe (~8 000 stocks) in a single API call.

Universe filtering rules:
  - market_cap > min_market_cap (default $500 M)  — removes micro/nano caps
  - price > min_price (default $5.00)             — removes penny stocks
  - symbol must be alphabetic and ≤ 5 chars       — removes warrants/notes
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from fmp_client import FMPClient, CallBudgetExceeded

logger = logging.getLogger("universe.fmp_universe")

_DEFAULT_MIN_MARKET_CAP: float = 500_000_000   # $500 M
_DEFAULT_MIN_PRICE: float = 5.0
_MAX_SYMBOL_LEN: int = 5


def _is_valid_equity_symbol(symbol: str) -> bool:
    """Accept only plain equity tickers (all alpha, ≤5 chars)."""
    return bool(symbol) and symbol.isalpha() and len(symbol) <= _MAX_SYMBOL_LEN


def _passes_filters(
    row: dict[str, Any],
    min_market_cap: float,
    min_price: float,
) -> bool:
    """Return True if the profile/bulk row meets market-cap and price filters."""
    symbol = str(row.get("symbol") or "")
    if not _is_valid_equity_symbol(symbol):
        return False

    mkt_cap = row.get("mktCap") or row.get("marketCap") or 0
    try:
        mkt_cap = float(mkt_cap)
    except (TypeError, ValueError):
        mkt_cap = 0.0

    price = row.get("price") or 0
    try:
        price = float(price)
    except (TypeError, ValueError):
        price = 0.0

    return mkt_cap >= min_market_cap and price >= min_price


class FMPUniverse:
    """
    Provides a filtered, investment-grade US equity symbol list.

    Args:
        client:       Initialised FMPClient instance.
        use_premium:  When True, use get_bulk_profiles() (1 premium call for
                      the full market).  When False (default), use
                      get_sp500_constituents() + get_batch_profiles_v3().
    """

    def __init__(self, client: FMPClient, use_premium: bool = False) -> None:
        self._client = client
        self._use_premium = use_premium

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_full_market_universe(
        self,
        min_market_cap: float = _DEFAULT_MIN_MARKET_CAP,
        min_price: float = _DEFAULT_MIN_PRICE,
        max_symbols: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """
        Return a filtered list of US equity dicts.

        Each dict contains at minimum: symbol, sector, mktCap, price.

        On premium tier (use_premium=True): pulls all ~8 000 stocks via
        v4/profile/all (1 call).

        On free tier: pulls S&P 500 constituents (1 call) then batch-fetches
        profiles for market-cap / price filtering (ceil(n/100) calls).

        Falls back to unfiltered S&P 500 symbol list when both profile
        endpoints fail or budget is exhausted.
        """
        profiles = self._fetch_profiles()
        universe = [
            row for row in profiles
            if _passes_filters(row, min_market_cap, min_price)
        ]

        # Sort descending by market cap for deterministic ordering
        universe.sort(key=lambda r: float(r.get("mktCap") or r.get("marketCap") or 0), reverse=True)

        if max_symbols is not None:
            universe = universe[:max_symbols]

        logger.info(
            "FMPUniverse: %d symbols after filtering (min_cap=$%.0fM, min_price=$%.2f)",
            len(universe), min_market_cap / 1e6, min_price,
        )
        return universe

    def get_symbols(
        self,
        min_market_cap: float = _DEFAULT_MIN_MARKET_CAP,
        min_price: float = _DEFAULT_MIN_PRICE,
        max_symbols: Optional[int] = None,
    ) -> list[str]:
        """Return a sorted list of filtered ticker symbols."""
        rows = self.get_full_market_universe(
            min_market_cap=min_market_cap,
            min_price=min_price,
            max_symbols=max_symbols,
        )
        return sorted(str(r["symbol"]) for r in rows if r.get("symbol"))

    def get_hybrid_universe(
        self,
        watchlist: list[str],
        min_market_cap: float = _DEFAULT_MIN_MARKET_CAP,
        min_price: float = _DEFAULT_MIN_PRICE,
        max_symbols: int = 500,
    ) -> list[str]:
        """
        Return a deduplicated union of the config watchlist + FMP universe.

        Watchlist symbols are always included (they bypass the market-cap /
        price filter — the operator has already decided they are worth scanning).
        FMP universe symbols are appended up to max_symbols total.
        """
        watchlist_upper = [s.upper() for s in watchlist if s]
        fmp_symbols = self.get_symbols(
            min_market_cap=min_market_cap,
            min_price=min_price,
        )

        seen: set[str] = set(watchlist_upper)
        combined = list(watchlist_upper)
        for sym in fmp_symbols:
            if sym not in seen and len(combined) < max_symbols:
                seen.add(sym)
                combined.append(sym)

        logger.info(
            "FMPUniverse hybrid: %d watchlist + %d FMP = %d total (cap %d)",
            len(watchlist_upper),
            len(combined) - len(watchlist_upper),
            len(combined),
            max_symbols,
        )
        return combined

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _fetch_profiles(self) -> list[dict[str, Any]]:
        """Fetch raw profile list; returns [] on hard failure."""
        if self._use_premium:
            return self._fetch_bulk_premium()
        return self._fetch_free_tier()

    def _fetch_bulk_premium(self) -> list[dict[str, Any]]:
        try:
            profiles = self._client.get_bulk_profiles()
            if isinstance(profiles, list):
                logger.info("FMPUniverse: %d profiles from premium bulk endpoint", len(profiles))
                return profiles
        except Exception as exc:
            logger.warning("FMPUniverse premium bulk_profiles failed: %s — trying free tier", exc)
        return self._fetch_free_tier()

    def _fetch_free_tier(self) -> list[dict[str, Any]]:
        """
        Free-tier strategy:
        1. Get S&P 500 constituent list (1 call → symbol + sector).
        2. Batch-fetch profiles for price + market-cap data.
        3. If profiles fail, return constituent dicts with mktCap=0 so
           callers can still work with the symbol list.
        """
        try:
            constituents = self._client.get_sp500_constituents()
        except Exception as exc:
            logger.warning("FMPUniverse: get_sp500_constituents failed: %s", exc)
            return []

        if not isinstance(constituents, list) or not constituents:
            return []

        symbols = [c["symbol"] for c in constituents if c.get("symbol")]

        # Build a sector lookup from constituents
        sector_map: dict[str, str] = {
            c["symbol"]: c.get("sector", "")
            for c in constituents
            if c.get("symbol")
        }

        # Try to enrich with batch profiles (adds mktCap, price, …)
        profile_map: dict[str, dict] = {}
        try:
            raw_profiles = self._client.get_batch_profiles_v3(symbols)
            if isinstance(raw_profiles, list):
                for p in raw_profiles:
                    if isinstance(p, dict) and p.get("symbol"):
                        profile_map[p["symbol"]] = p
        except (CallBudgetExceeded, Exception) as exc:
            logger.warning(
                "FMPUniverse: get_batch_profiles_v3 failed (%s) — "
                "returning constituents with mktCap=0",
                exc,
            )

        # Merge constituent sector data into profiles
        result: list[dict[str, Any]] = []
        for sym in symbols:
            if sym in profile_map:
                row = dict(profile_map[sym])
                # Prefer constituent sector when profile has none
                if not row.get("sector") and sector_map.get(sym):
                    row["sector"] = sector_map[sym]
                result.append(row)
            else:
                # No profile available — include with zeroed numeric fields
                # so the symbol survives if the caller doesn't filter on price/mktCap
                result.append({
                    "symbol":   sym,
                    "sector":   sector_map.get(sym, ""),
                    "mktCap":   0,
                    "price":    0,
                })

        return result
