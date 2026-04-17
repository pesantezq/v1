"""
Alpha Vantage client for the watchlist scanner.

Wraps three endpoints with per-type caching and a shared daily call budget:
  TIME_SERIES_DAILY_ADJUSTED — 24 h cache
  NEWS_SENTIMENT             — 4 h cache
  GLOBAL_QUOTE               — 30 min cache

The API key is read exclusively from the ALPHA_VANTAGE_API_KEY environment
variable — never from config files or hardcoded values.

Security: the key is redacted from any log output.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any, Optional

import pandas as pd
import requests

try:
    from api_budget import AVDailyBudget as _AVDailyBudget
except ImportError:
    _AVDailyBudget = None  # type: ignore[assignment,misc]

from watchlist_scanner.cache_manager import CacheManager
from watchlist_scanner.config import (
    CACHE_TTL_DAILY_SECONDS,
    CACHE_TTL_NEWS_SECONDS,
    CACHE_TTL_OVERVIEW_SECONDS,
    CACHE_TTL_QUOTE_SECONDS,
    MAX_DAILY_CALLS,
)

logger = logging.getLogger("watchlist_scanner.av_client")

_AV_BASE = "https://www.alphavantage.co/query"
_MIN_INTERVAL_SECONDS = 12.0   # free tier: 5 calls/min → 12 s gap


class AVClientError(Exception):
    """Non-recoverable Alpha Vantage error."""


class BudgetExceeded(Exception):
    """Daily call limit reached."""


class WatchlistAVClient:
    """
    Alpha Vantage client scoped to the watchlist scanner.

    Args:
        cache:       CacheManager instance (shared across the scan run).
        max_calls:   Maximum API calls per calendar day (default 20).
        timeout:     HTTP request timeout in seconds.
        max_retries: Retry attempts on transient errors.
    """

    def __init__(
        self,
        cache: CacheManager,
        max_calls: int = MAX_DAILY_CALLS,
        timeout: int = 30,
        max_retries: int = 3,
        budget: Optional["_AVDailyBudget"] = None,
    ) -> None:
        self._api_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
        if not self._api_key:
            raise AVClientError(
                "ALPHA_VANTAGE_API_KEY environment variable is not set. "
                "Add it to your .env file."
            )
        self._cache = cache
        self._max_calls = max_calls
        self._timeout = timeout
        self._max_retries = max_retries
        self._budget = budget
        self._last_call_ts: float = 0.0
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "StockBotWatchlistScanner/1.0"})

    # ── Internal helpers ───────────────────────────────────────────────────

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_call_ts
        if elapsed < _MIN_INTERVAL_SECONDS:
            wait = _MIN_INTERVAL_SECONDS - elapsed
            logger.debug("Rate limit: sleeping %.1fs", wait)
            time.sleep(wait)
        self._last_call_ts = time.monotonic()

    def _raw_get(self, params: dict[str, str]) -> dict[str, Any]:
        """Make one authenticated GET with retry / exponential backoff.
        The apikey is injected here and never logged."""
        params = {**params, "apikey": self._api_key}
        last_err: Optional[Exception] = None

        for attempt in range(self._max_retries):
            self._rate_limit()
            try:
                resp = self._session.get(_AV_BASE, params=params, timeout=self._timeout)
                resp.raise_for_status()
                data = resp.json()

                if "Error Message" in data:
                    raise AVClientError(f"AV API error: {data['Error Message']}")
                if "Information" in data:
                    raise AVClientError(f"AV API limit/info: {data['Information'][:120]}")
                if "Note" in data:
                    logger.warning("AV rate-limit note received — waiting 15 s")
                    time.sleep(15)
                    continue

                self._cache.increment_calls()
                if self._budget is not None:
                    self._budget.reserve("scanner", 1)
                logger.debug(
                    "AV call OK (%d/%d today, function=%s)",
                    self._cache.calls_today, self._max_calls,
                    params.get("function", "?"),
                )
                return data

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                last_err = exc
                logger.warning("AV request failed (attempt %d/%d): %s", attempt + 1, self._max_retries, exc)
            except AVClientError:
                raise
            except Exception as exc:
                last_err = exc
                logger.warning("AV unexpected error (attempt %d/%d): %s", attempt + 1, self._max_retries, exc)

            if attempt < self._max_retries - 1:
                time.sleep(2.0 * (2 ** attempt))

        raise AVClientError(f"AV request failed after {self._max_retries} attempts: {last_err}")

    def _get_cached(
        self,
        cache_key: str,
        params: dict[str, str],
        ttl_seconds: int,
    ) -> Optional[dict[str, Any]]:
        """Return fresh cache or fetch; return stale cache on budget exhaustion."""
        cached = self._cache.get(cache_key, ttl_seconds)
        if cached is not None:
            return cached

        budget_exhausted = (
            (self._budget is not None and not self._budget.can_reserve("scanner", 1))
            or self._cache.would_exceed(self._max_calls)
        )
        if budget_exhausted:
            stale = self._cache.get_stale(cache_key)
            if stale is not None:
                logger.warning(
                    "AV daily budget (%d calls) reached; using stale cache for %r",
                    self._max_calls, cache_key,
                )
                return stale
            raise BudgetExceeded(
                f"Daily budget ({self._max_calls} calls) exhausted and no cached data "
                f"exists for {cache_key!r}."
            )

        data = self._raw_get(params)
        self._cache.set(cache_key, data)
        return data

    # ── Public API ─────────────────────────────────────────────────────────

    def get_daily_ohlcv(
        self,
        symbol: str,
        outputsize: str = "compact",
    ) -> Optional[pd.DataFrame]:
        """
        Fetch TIME_SERIES_DAILY for symbol (free-tier endpoint).

        Returns a DataFrame with columns [open, high, low, close, adj_close,
        volume] indexed by date (newest first), or None on error.
        adj_close mirrors close (no split adjustment on free tier).

        Cached for 24 h (one call per symbol per day).
        """
        cache_key = f"daily_{symbol}"
        try:
            data = self._get_cached(
                cache_key,
                {
                    "function": "TIME_SERIES_DAILY",
                    "symbol": symbol,
                    "outputsize": outputsize,
                },
                CACHE_TTL_DAILY_SECONDS,
            )
        except BudgetExceeded:
            raise
        except AVClientError as exc:
            logger.warning("get_daily_ohlcv(%s) failed: %s", symbol, exc)
            return None

        if data is None:
            return None

        ts = data.get("Time Series (Daily)", {})
        if not ts:
            logger.warning("get_daily_ohlcv(%s): empty time series in response", symbol)
            return None

        rows = []
        for date_str, v in sorted(ts.items(), reverse=True):
            try:
                close = float(v.get("4. close", 0))
                rows.append({
                    "date":      pd.to_datetime(date_str),
                    "open":      float(v.get("1. open", 0)),
                    "high":      float(v.get("2. high", 0)),
                    "low":       float(v.get("3. low", 0)),
                    "close":     close,
                    "adj_close": close,          # free tier has no adjusted close
                    "volume":    float(v.get("5. volume", 0)),
                })
            except (ValueError, TypeError):
                continue

        if not rows:
            return None

        df = pd.DataFrame(rows).set_index("date")
        return df

    def get_news_sentiment(
        self,
        tickers: list[str],
        limit: int = 50,
    ) -> Optional[list[dict]]:
        """
        Fetch NEWS_SENTIMENT for a batch of tickers.

        One API call covers up to ~50 tickers. Cached for 4 h.

        Returns list of article dicts with keys:
            title, summary, source, time_published, overall_sentiment_score,
            overall_sentiment_label, ticker_sentiment
        or None on error.
        """
        # Stable cache key: sorted tickers + limit
        key_tickers = ",".join(sorted(tickers))
        cache_key = f"news_{key_tickers[:60]}_{limit}"

        try:
            data = self._get_cached(
                cache_key,
                {
                    "function": "NEWS_SENTIMENT",
                    "tickers": ",".join(tickers),
                    "limit": str(limit),
                    "sort": "LATEST",
                },
                CACHE_TTL_NEWS_SECONDS,
            )
        except BudgetExceeded:
            raise
        except AVClientError as exc:
            logger.warning("get_news_sentiment failed: %s", exc)
            return None

        if data is None:
            return None

        articles = data.get("feed", [])
        logger.debug("get_news_sentiment: %d articles", len(articles))
        return articles

    def get_quote(self, symbol: str) -> Optional[dict[str, Any]]:
        """
        Fetch GLOBAL_QUOTE for symbol.

        Cached for 30 min. Returns dict with keys:
            price, change, change_pct, volume, prev_close, open, high, low
        or None on error.
        """
        cache_key = f"quote_{symbol}"
        try:
            data = self._get_cached(
                cache_key,
                {"function": "GLOBAL_QUOTE", "symbol": symbol},
                CACHE_TTL_QUOTE_SECONDS,
            )
        except BudgetExceeded:
            raise
        except AVClientError as exc:
            logger.warning("get_quote(%s) failed: %s", symbol, exc)
            return None

        if data is None:
            return None

        q = data.get("Global Quote", {})
        if not q:
            logger.warning("get_quote(%s): empty Global Quote", symbol)
            return None

        try:
            return {
                "price":       float(q.get("05. price", 0) or 0),
                "open":        float(q.get("02. open", 0) or 0),
                "high":        float(q.get("03. high", 0) or 0),
                "low":         float(q.get("04. low", 0) or 0),
                "prev_close":  float(q.get("08. previous close", 0) or 0),
                "change":      float(q.get("09. change", 0) or 0),
                "change_pct":  float((q.get("10. change percent", "0%") or "0%").rstrip("%")),
                "volume":      float(q.get("06. volume", 0) or 0),
                "latest_day":  q.get("07. latest trading day", ""),
            }
        except (ValueError, TypeError) as exc:
            logger.warning("get_quote(%s) parse error: %s", symbol, exc)
            return None

    def get_overview(self, symbol: str) -> Optional[dict[str, Any]]:
        """
        Fetch OVERVIEW (company fundamentals) for symbol.

        Cached for 7 days — one call per symbol per week.
        Returns the raw OVERVIEW dict or None on error / budget exceeded without stale cache.

        Note: ETFs (SPY, QQQ, etc.) return a minimal or empty OVERVIEW; callers
        should handle empty / missing fields gracefully.
        """
        cache_key = f"overview_{symbol}"
        try:
            data = self._get_cached(
                cache_key,
                {"function": "OVERVIEW", "symbol": symbol},
                CACHE_TTL_OVERVIEW_SECONDS,
            )
        except BudgetExceeded:
            raise
        except AVClientError as exc:
            logger.warning("get_overview(%s) failed: %s", symbol, exc)
            return None

        if not data:
            logger.debug("get_overview(%s): empty response (ETF or unknown symbol)", symbol)
            return None

        return data
