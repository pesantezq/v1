"""
FMP (Financial Modeling Prep) API Client

Centralized HTTP helper with:
  - Retry / exponential backoff on transient HTTP errors
  - Disk cache with per-endpoint TTL (JSON files in data/fmp_cache/)
  - Daily call counter with budget guard (never exceeds fmp_daily_calls_budget)
  - 500 ms minimum interval between outbound requests (rate limit courtesy)

Security: the API key is read exclusively from the FMP_API_KEY environment
variable — never from config files or hardcoded values.

Usage:
    from fmp_client import FMPClient, CallBudgetExceeded, FMPError
    fmp = FMPClient(daily_budget=230)
    symbols = [c['symbol'] for c in fmp.get_sp500_constituents()]
"""

import hashlib
import json
import logging
import os
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger('portfolio_automation.fmp_client')

FMP_BASE_URL = "https://financialmodelingprep.com/api"
FMP_STABLE_BASE_URL = "https://financialmodelingprep.com/stable"
_DEFAULT_CACHE_DIR = Path("data/fmp_cache")


def _extract_stable_quote(raw: Any) -> Optional[Dict]:
    """
    Normalise a raw stable/quote API response to a quote dict.

    Accepts the list-wrapped response from the live API or a previously
    cached value (list or bare dict).  Returns None for empty/invalid input.

    Field normalisation applied:
      changePercentage → changesPercentage  (alias added when v3 key absent)
    """
    if isinstance(raw, list):
        if not raw:
            return None
        q = raw[0]
    elif isinstance(raw, dict):
        q = raw
    else:
        return None
    if not isinstance(q, dict):
        return None
    normalized = dict(q)
    if "changesPercentage" not in normalized and "changePercentage" in normalized:
        normalized["changesPercentage"] = normalized["changePercentage"]
    return normalized


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class FMPError(Exception):
    """Non-recoverable FMP API error (auth failure, malformed response, …)."""


class CallBudgetExceeded(Exception):
    """Adding another call would push today's count past the daily budget."""


# ---------------------------------------------------------------------------
# Daily call counter (persisted to disk)
# ---------------------------------------------------------------------------

class _CallCounter:
    """
    Persists today's API call count as a JSON file.
    Automatically resets when the calendar date changes.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> Dict[str, Any]:
        today = date.today().isoformat()
        if self._path.exists():
            try:
                d = json.loads(self._path.read_text(encoding='utf-8'))
                if d.get('date') == today:
                    return d
            except Exception:
                pass
        return {'date': today, 'count': 0}

    def _save(self, d: Dict[str, Any]) -> None:
        try:
            self._path.write_text(json.dumps(d), encoding='utf-8')
        except OSError:
            pass  # Non-fatal — worst case count resets on restart

    @property
    def today_count(self) -> int:
        return self._load().get('count', 0)

    def increment(self, n: int = 1) -> int:
        d = self._load()
        d['count'] += n
        self._save(d)
        return d['count']

    def would_exceed(self, budget: int, additional: int = 1) -> bool:
        return self.today_count + additional > budget


# ---------------------------------------------------------------------------
# Disk cache with TTL
# ---------------------------------------------------------------------------

class _DiskCache:
    """
    Simple JSON disk cache keyed by arbitrary strings.
    Each entry is stored as a separate file containing the data and a
    stored_at timestamp for TTL comparison.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Sanitise key to a safe filename (≤80 chars)
        safe = ''.join(c if c.isalnum() or c in '-_' else '_' for c in key)
        return self._dir / f"{safe[:80]}.json"

    def get(self, key: str, ttl_seconds: int) -> Optional[Any]:
        """Return cached data if it exists and is within TTL; else None."""
        p = self._path(key)
        if not p.exists():
            return None
        try:
            d = json.loads(p.read_text(encoding='utf-8'))
            stored_at = datetime.fromisoformat(d['stored_at'])
            if (datetime.now() - stored_at).total_seconds() > ttl_seconds:
                return None
            return d['data']
        except Exception:
            return None

    def get_stale(self, key: str) -> Optional[Any]:
        """Return cached data regardless of age (budget-exceeded fallback)."""
        p = self._path(key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding='utf-8'))['data']
        except Exception:
            return None

    def set(self, key: str, data: Any) -> None:
        p = self._path(key)
        try:
            p.write_text(
                json.dumps(
                    {'stored_at': datetime.now().isoformat(), 'data': data},
                    ensure_ascii=False,
                ),
                encoding='utf-8',
            )
        except OSError as e:
            logger.warning(f"FMP cache write failed for {key!r}: {e}")


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------

class FMPClient:
    """
    Centralised FMP API client.

    Reads FMP_API_KEY exclusively from the environment (never config or
    hardcoded).  Counts every outbound HTTP call against a daily budget and
    falls back to stale cached data rather than exceeding it.

    Args:
        api_key:       Override for testing only; production always uses env var.
        daily_budget:  Max API calls per calendar day (default 230 — leaves
                       headroom below the typical 250-call free/starter limit).
        retry_max:     Number of retry attempts on transient errors.
        retry_base_delay: Base delay (seconds) for exponential backoff.
        timeout:       HTTP request timeout in seconds.
        cache_dir:     Override cache directory (useful for tests).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        daily_budget: int = 230,
        retry_max: int = 3,
        retry_base_delay: float = 2.0,
        timeout: int = 30,
        cache_dir: Optional[Path] = None,
    ) -> None:
        self._api_key = api_key or os.environ.get('FMP_API_KEY')
        if not self._api_key:
            raise FMPError(
                "FMP_API_KEY environment variable is not set. "
                "Add  FMP_API_KEY=<your_key>  to your .env file. "
                "Get a free key at https://financialmodelingprep.com/developer/docs/"
            )
        self._budget = daily_budget
        self._retry_max = retry_max
        self._retry_base = retry_base_delay
        self._timeout = timeout
        _dir = cache_dir or _DEFAULT_CACHE_DIR
        self._cache = _DiskCache(_dir)
        self._counter = _CallCounter(_dir / 'call_counter.json')
        self._last_call_ts: float = 0.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rate_limit(self) -> None:
        """Enforce 500 ms minimum gap between requests."""
        elapsed = time.monotonic() - self._last_call_ts
        if elapsed < 0.5:
            time.sleep(0.5 - elapsed)
        self._last_call_ts = time.monotonic()

    def _raw_get(
        self,
        endpoint: str,
        params: Dict[str, str],
        *,
        base_url: str = FMP_BASE_URL,
    ) -> Any:
        """Make one HTTP GET with retry / exponential backoff."""
        params = {**params, 'apikey': self._api_key}
        url = f"{base_url}/{endpoint}?{urllib.parse.urlencode(params)}"
        last_err: Optional[Exception] = None

        for attempt in range(self._retry_max):
            self._rate_limit()
            try:
                req = urllib.request.Request(
                    url, headers={'User-Agent': 'PortfolioBot/1.0'}
                )
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                if isinstance(data, dict) and 'Error Message' in data:
                    raise FMPError(f"FMP API error: {data['Error Message']}")
                self._counter.increment()
                return data
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    wait = self._retry_base * (2 ** attempt)
                    logger.warning(
                        "FMP rate-limited (429) for %s/%s; waiting %.0fs "
                        "(attempt %d/%d)",
                        base_url, endpoint, wait, attempt + 1, self._retry_max,
                    )
                    time.sleep(wait)
                    last_err = exc
                    continue
                if exc.code in (401, 403):
                    raise FMPError(
                        f"FMP authentication failed (HTTP {exc.code}) "
                        f"for {base_url}/{endpoint}. "
                        "Verify FMP_API_KEY in your .env file."
                    )
                last_err = exc
                if attempt < self._retry_max - 1:
                    time.sleep(self._retry_base * (2 ** attempt))
            except (urllib.error.URLError, OSError) as exc:
                last_err = exc
                if attempt < self._retry_max - 1:
                    time.sleep(self._retry_base * (2 ** attempt))

        raise FMPError(
            f"FMP request failed after {self._retry_max} attempts "
            f"for {base_url}/{endpoint}: {last_err}"
        )

    def _get_cached(
        self,
        cache_key: str,
        endpoint: str,
        ttl_seconds: int,
        params: Optional[Dict[str, str]] = None,
    ) -> Any:
        """
        Return fresh cached data if available; otherwise fetch, cache, return.
        Falls back to stale cache if the daily budget would be exceeded.
        """
        cached = self._cache.get(cache_key, ttl_seconds)
        if cached is not None:
            logger.debug(f"FMP cache hit: {cache_key!r}")
            return cached

        if self._counter.would_exceed(self._budget):
            stale = self._cache.get_stale(cache_key)
            if stale is not None:
                logger.warning(
                    f"FMP daily budget ({self._budget} calls) would be exceeded — "
                    f"using stale cached data for {cache_key!r}"
                )
                return stale
            raise CallBudgetExceeded(
                f"Daily budget ({self._budget} calls) would be exceeded and "
                f"no cached data exists for {cache_key!r}. "
                "Run  --run-mode monthly  first to warm the cache."
            )

        data = self._raw_get(endpoint, params or {})
        self._cache.set(cache_key, data)
        logger.debug(
            f"FMP fetched + cached: {cache_key!r} "
            f"({self._counter.today_count}/{self._budget} calls today)"
        )
        return data

    # ------------------------------------------------------------------
    # Public API — bulk endpoints
    # ------------------------------------------------------------------

    @property
    def calls_today(self) -> int:
        """Number of API calls made so far today."""
        return self._counter.today_count

    def get_sp500_constituents(self, ttl_days: int = 7) -> List[Dict]:
        """
        S&P 500 constituent list.
        1 API call; cached for ttl_days (default 7).

        Returns list of dicts with keys: symbol, name, sector, subSector, …
        """
        return self._get_cached(
            'sp500_constituents',
            'v3/sp500_constituent',
            ttl_seconds=ttl_days * 86400,
        )

    def get_bulk_profiles(self, ttl_days: int = 7) -> List[Dict]:
        """
        Bulk profile data (market cap, sector, price, …) for ALL stocks.
        1 API call (premium endpoint); cached for ttl_days.

        Returns list of dicts with keys: symbol, mktCap, sector, price, …
        """
        return self._get_cached(
            'bulk_profiles',
            'v4/profile/all',
            ttl_seconds=ttl_days * 86400,
        )

    def get_bulk_key_metrics(self, ttl_days: int = 7) -> List[Dict]:
        """
        Bulk key metrics (annual) for ALL stocks.
        1 API call (premium endpoint); cached for ttl_days.

        Returns list of dicts with keys: symbol, revenueGrowth, roe,
        freeCashFlowYield, peRatio, debtToEquity, …
        """
        return self._get_cached(
            'bulk_key_metrics',
            'v4/key-metrics-bulk',
            ttl_seconds=ttl_days * 86400,
            params={'period': 'annual'},
        )

    def get_profile(
        self,
        symbol: str,
        ttl_days: int = 7,
    ) -> Optional[Dict]:
        """
        Fetch company profile for a single symbol via stable/profile endpoint.

        GET https://financialmodelingprep.com/stable/profile?symbol=AAPL

        Returns dict with keys: symbol, companyName, sector, industry, mktCap,
        beta, price, description, exchange, country, and more.
        Returns None if not available, on error, or when budget exceeded with
        no cached data.

        Cached individually per symbol for ttl_days (default 7).
        """
        if not symbol:
            return None
        sym = symbol.upper()
        cache_key = f"profile_stable_{sym}"
        ttl_seconds = ttl_days * 86400

        def _unwrap(raw: Any) -> Optional[Dict]:
            if isinstance(raw, list):
                return raw[0] if raw and isinstance(raw[0], dict) else None
            return raw if isinstance(raw, dict) else None

        cached = self._cache.get(cache_key, ttl_seconds)
        if cached is not None:
            return _unwrap(cached)

        if self._counter.would_exceed(self._budget):
            stale = self._cache.get_stale(cache_key)
            return _unwrap(stale) if stale is not None else None

        try:
            raw = self._raw_get("profile", {"symbol": sym}, base_url=FMP_STABLE_BASE_URL)
            self._cache.set(cache_key, raw)
            result = _unwrap(raw)
            logger.debug("FMP stable/profile %s: loaded (sector=%s)", sym,
                         result.get("sector") if result else "n/a")
            return result
        except (FMPError, Exception) as exc:
            logger.warning("FMP get_profile(%s) failed: %s", sym, exc)
            stale = self._cache.get_stale(cache_key)
            return _unwrap(stale) if stale is not None else None

    def get_batch_profiles(
        self,
        symbols: List[str],
        ttl_days: int = 7,
    ) -> List[Dict]:
        """
        Fetch company profiles for a list of symbols via stable/profile endpoint.

        Calls ``stable/profile?symbol={sym}`` once per symbol — the stable API
        does not support comma-separated batch requests.  Each symbol is cached
        individually for ttl_days (default 7).

        Returns list of profile dicts. Missing symbols are silently skipped.
        """
        if not symbols:
            return []
        unique_syms = list(dict.fromkeys(s.upper() for s in symbols if s))
        result: List[Dict] = []
        n_missing = 0

        for sym in unique_syms:
            profile = self.get_profile(sym, ttl_days=ttl_days)
            if profile:
                result.append(profile)
            else:
                n_missing += 1
                logger.debug("FMP get_batch_profiles: %s not available", sym)

        logger.info(
            "FMP get_batch_profiles: %d/%d profiles loaded (missing=%d, endpoint=stable/profile)",
            len(result), len(unique_syms), n_missing,
        )
        return result

    def get_ratios(
        self,
        symbol: str,
        period: str = "annual",
        limit: int = 1,
        ttl_days: int = 30,
    ) -> Optional[Dict]:
        """
        Fetch financial ratios for a single symbol via stable/ratios endpoint.

        Returns the most recent ratios dict. Key fields: netProfitMargin,
        grossProfitMargin, returnOnEquity, debtEquityRatio, dividendYield,
        priceEarningsRatio.  Returns None if unavailable.

        Cached individually for ttl_days (default 30 — annual data is stable).
        """
        if not symbol:
            return None
        sym = symbol.upper()
        cache_key = f"ratios_stable_{sym}_{period}"
        ttl_seconds = ttl_days * 86400

        def _unwrap(raw: Any) -> Optional[Dict]:
            if isinstance(raw, list):
                return raw[0] if raw and isinstance(raw[0], dict) else None
            return raw if isinstance(raw, dict) else None

        cached = self._cache.get(cache_key, ttl_seconds)
        if cached is not None:
            return _unwrap(cached)

        if self._counter.would_exceed(self._budget):
            stale = self._cache.get_stale(cache_key)
            return _unwrap(stale) if stale is not None else None

        try:
            raw = self._raw_get(
                "ratios",
                {"symbol": sym, "period": period, "limit": str(limit)},
                base_url=FMP_STABLE_BASE_URL,
            )
            self._cache.set(cache_key, raw)
            result = _unwrap(raw)
            logger.debug("FMP stable/ratios %s: netProfitMargin=%s", sym,
                         result.get("netProfitMargin") if result else "n/a")
            return result
        except (FMPError, Exception) as exc:
            logger.warning("FMP get_ratios(%s) failed: %s", sym, exc)
            stale = self._cache.get_stale(cache_key)
            return _unwrap(stale) if stale is not None else None

    def get_batch_profiles_v3(
        self,
        symbols: List[str],
        batch_size: int = 100,
        ttl_days: int = 7,
    ) -> List[Dict]:
        """
        Fetch company profiles using free-tier v3/profile/{symbols} endpoint.

        Calls /api/v3/profile/{sym1,sym2,...} in batches of batch_size.
        Drop-in replacement for get_bulk_profiles() when use_premium_endpoints=False.
        Caches the combined result for ttl_days under key 'profiles_v3_all'.

        Budget: ceil(len(symbols) / batch_size) calls.

        Returns list of dicts with keys: symbol, mktCap, sector, …
        """
        cache_key = 'profiles_v3_all'
        cached = self._cache.get(cache_key, ttl_days * 86400)
        if cached is not None:
            logger.debug("get_batch_profiles_v3: cache hit (%d profiles)", len(cached))
            return cached

        n_batches = (len(symbols) + batch_size - 1) // batch_size
        if self._counter.would_exceed(self._budget, additional=n_batches):
            stale = self._cache.get_stale(cache_key)
            if stale is not None:
                logger.warning(
                    "FMP budget would be exceeded; using stale profiles_v3 cache"
                )
                return stale
            raise CallBudgetExceeded(
                f"Daily budget ({self._budget} calls) would be exceeded fetching "
                "v3 profiles and no stale cache is available."
            )

        all_profiles: List[Dict] = []
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            sym_str = ','.join(batch)
            data = self._raw_get(f'v3/profile/{sym_str}', {})
            if isinstance(data, list):
                all_profiles.extend(data)

        self._cache.set(cache_key, all_profiles)
        logger.info(
            "get_batch_profiles_v3: fetched %d profiles (%d calls, %d/%d budget used)",
            len(all_profiles), n_batches, self._counter.today_count, self._budget,
        )
        return all_profiles

    def get_fundamentals_v3(
        self,
        symbols: List[str],
        ttl_days: int = 7,
    ) -> List[Dict]:
        """
        Fetch per-ticker fundamentals using free-tier v3 endpoints.

        For each symbol calls:
          v3/key-metrics/{symbol}?limit=1&period=annual  → roe, peRatio, freeCashFlowYield
          v3/financial-growth/{symbol}?limit=1           → revenueGrowth

        Returns list of dicts with the same field names as get_bulk_key_metrics()
        so the scanner can consume them without modification.

        Budget: up to 2 calls per symbol (each cached individually for ttl_days).
        """
        result: List[Dict] = []
        for symbol in symbols:
            row: Dict[str, Any] = {'symbol': symbol}

            km_data = self._get_cached(
                f'km_v3_{symbol}',
                f'v3/key-metrics/{symbol}',
                ttl_seconds=ttl_days * 86400,
                params={'limit': '1', 'period': 'annual'},
            )
            if isinstance(km_data, list) and km_data:
                km = km_data[0]
                row['roe'] = km.get('roe')
                row['peRatio'] = km.get('peRatio')
                row['freeCashFlowYield'] = km.get('freeCashFlowYield')

            fg_data = self._get_cached(
                f'fin_growth_v3_{symbol}',
                f'v3/financial-growth/{symbol}',
                ttl_seconds=ttl_days * 86400,
                params={'limit': '1'},
            )
            if isinstance(fg_data, list) and fg_data:
                fg = fg_data[0]
                row['revenueGrowth'] = fg.get('revenueGrowth')

            result.append(row)

        logger.info(
            "get_fundamentals_v3: fetched metrics for %d symbols (%d/%d budget used)",
            len(symbols), self._counter.today_count, self._budget,
        )
        return result

    def get_batch_quotes(
        self,
        symbols: List[str],
        chunk_size: int = 50,  # kept for API compatibility; unused in stable path
        ttl_hours: int = 1,
    ) -> Dict[str, Dict]:
        """
        Fetch quote data for a list of symbols using the FMP stable/quote endpoint.

        Calls ``stable/quote?symbol={sym}`` once per symbol — the stable API
        does not support comma-separated batch requests.  Each symbol is cached
        individually for ttl_hours (default 1).

        When the daily budget would be exceeded, serves stale cached data for
        each symbol that has a prior entry; symbols with no cache are skipped
        rather than raising.

        Returns: {symbol: quote_dict}
        Quote dict keys include: price, changesPercentage (aliased from
        changePercentage), priceAvg200, priceAvg50, marketCap, volume,
        avgVolume, dayHigh, dayLow, yearHigh, yearLow, timestamp, …
        """
        result: Dict[str, Dict] = {}
        if not symbols:
            return result

        unique_syms = list(dict.fromkeys(s.upper() for s in symbols if s))
        ttl_seconds = ttl_hours * 3600
        n_total = len(unique_syms)
        n_cached = n_fetched = n_stale = n_missing = 0

        logger.info(
            "FMP get_batch_quotes: %d symbols via stable/quote (per-symbol)",
            n_total,
        )

        for sym in unique_syms:
            cache_key = f"quote_stable_{sym}"

            # Fresh cache hit — skip network call entirely
            cached = self._cache.get(cache_key, ttl_seconds)
            if cached is not None:
                quote = _extract_stable_quote(cached)
                if quote:
                    result[sym] = quote
                    n_cached += 1
                    logger.debug(
                        "FMP stable/quote %s: cache hit (source=fresh_cache)", sym
                    )
                    continue

            # Budget guard — serve stale rather than exceeding daily limit
            if self._counter.would_exceed(self._budget):
                stale = self._cache.get_stale(cache_key)
                quote = _extract_stable_quote(stale) if stale is not None else None
                if quote:
                    result[sym] = quote
                    n_stale += 1
                    logger.debug(
                        "FMP stable/quote %s: budget exceeded — "
                        "using stale cache (endpoint=stable/quote, source=stale)",
                        sym,
                    )
                else:
                    n_missing += 1
                    logger.debug(
                        "FMP stable/quote %s: budget exceeded, no stale cache — "
                        "skipping (endpoint=stable/quote)",
                        sym,
                    )
                continue

            # Live fetch
            try:
                raw = self._raw_get(
                    "quote",
                    {"symbol": sym},
                    base_url=FMP_STABLE_BASE_URL,
                )
                self._cache.set(cache_key, raw)
                quote = _extract_stable_quote(raw)
                if quote:
                    result[sym] = quote
                    n_fetched += 1
                    logger.debug(
                        "FMP stable/quote %s: price=%s "
                        "(endpoint=stable/quote, source=fresh)",
                        sym, quote.get("price"),
                    )
                else:
                    n_missing += 1
                    logger.debug(
                        "FMP stable/quote %s: empty response "
                        "(endpoint=stable/quote, source=fresh)",
                        sym,
                    )
            except FMPError as exc:
                stale = self._cache.get_stale(cache_key)
                quote = _extract_stable_quote(stale) if stale is not None else None
                if quote:
                    result[sym] = quote
                    n_stale += 1
                    logger.warning(
                        "FMP stable/quote %s: fetch failed — using stale cache "
                        "(endpoint=stable/quote, error=%s, source=stale)",
                        sym, exc,
                    )
                else:
                    n_missing += 1
                    logger.warning(
                        "FMP stable/quote %s: fetch failed, no stale cache "
                        "(endpoint=stable/quote, error=%s)",
                        sym, exc,
                    )
            except Exception as exc:
                n_missing += 1
                logger.warning(
                    "FMP stable/quote %s: unexpected error — %s", sym, exc
                )

        n_with_price = len(result)
        logger.info(
            "FMP get_batch_quotes: %d/%d symbols have price data "
            "(cached=%d, fetched=%d, stale=%d, missing=%d)",
            n_with_price, n_total, n_cached, n_fetched, n_stale, n_missing,
        )
        return result

    def get_stock_news(
        self,
        tickers: List[str],
        limit: int = 50,
        ttl_hours: int = 4,
    ) -> List[Dict]:
        """
        Fetch stock news articles for a list of tickers.

        Uses /v3/stock_news?tickers={sym1,sym2,...}&limit={n}.
        One API call for all tickers; cached for ttl_hours (default 4).

        Returns list of article dicts normalized to the same shape as AV
        NEWS_SENTIMENT articles so the scanner can consume them without
        modification:
            title, summary, source, time_published,
            overall_sentiment_score (0.0 — FMP does not provide sentiment),
            overall_sentiment_label ("Neutral"),
            ticker_sentiment: [{ticker, relevance_score,
                               ticker_sentiment_score, ticker_sentiment_label}]

        Returns empty list on any error or budget exceeded without stale cache.
        """
        if not tickers:
            return []

        sym_str = ','.join(sorted(tickers))
        cache_key = f"fmp_news_{sym_str[:60]}_{limit}"
        ttl_seconds = ttl_hours * 3600

        cached = self._cache.get(cache_key, ttl_seconds)
        if cached is not None:
            return cached if isinstance(cached, list) else []

        if self._counter.would_exceed(self._budget):
            stale = self._cache.get_stale(cache_key)
            return stale if isinstance(stale, list) else []

        try:
            raw = self._raw_get(
                "news/stock",
                {"tickers": ','.join(tickers), "limit": str(limit)},
                base_url=FMP_STABLE_BASE_URL,
            )
        except Exception as exc:
            logger.warning(f"FMP get_stock_news failed: {exc}")
            stale = self._cache.get_stale(cache_key)
            return stale if isinstance(stale, list) else []

        if not isinstance(raw, list):
            return []

        normalized: List[Dict] = []
        for art in raw:
            if not isinstance(art, dict) or not art.get('title'):
                continue
            sym = str(art.get('symbol') or '')
            normalized.append({
                'title': art.get('title', ''),
                'summary': art.get('text', ''),
                'source': art.get('site', ''),
                'time_published': art.get('publishedDate', ''),
                'overall_sentiment_score': 0.0,
                'overall_sentiment_label': 'Neutral',
                'ticker_sentiment': [
                    {
                        'ticker': sym.upper(),
                        'relevance_score': '0.5',
                        'ticker_sentiment_score': '0.0',
                        'ticker_sentiment_label': 'Neutral',
                    }
                ] if sym else [],
            })
        self._cache.set(cache_key, normalized)
        logger.debug(f"FMP get_stock_news: {len(normalized)} articles for {len(tickers)} tickers")
        return normalized

    def get_historical_prices(
        self,
        symbol: str,
        years: int = 5,
        ttl_days: int = 1,
    ) -> List[Dict]:
        """
        Fetch daily historical OHLCV data for a single symbol.

        Uses stable/historical-price-eod/full?symbol=X&from=YYYY-MM-DD.
        1 API call per symbol; cached for ttl_days (default 1 day).

        Returns list of dicts (newest-first order from FMP) with keys:
            date, open, high, low, close, adjClose, volume, change, changePercent
        Returns empty list on any error or when budget exceeded without cache.
        """
        if not symbol:
            return []
        sym = symbol.upper()
        from_date = (date.today() - timedelta(days=years * 365)).isoformat()
        cache_key = f"hist_stable_{sym}_{years}y"
        ttl_seconds = ttl_days * 86400

        cached = self._cache.get(cache_key, ttl_seconds)
        if cached is not None:
            return cached if isinstance(cached, list) else []

        if self._counter.would_exceed(self._budget):
            stale = self._cache.get_stale(cache_key)
            return stale if isinstance(stale, list) else []

        try:
            raw = self._raw_get(
                "historical-price-eod/full",
                {"symbol": sym, "from": from_date},
                base_url=FMP_STABLE_BASE_URL,
            )
        except Exception as exc:
            logger.warning(f"FMP get_historical_prices({symbol!r}) failed: {exc}")
            stale = self._cache.get_stale(cache_key)
            return stale if isinstance(stale, list) else []

        # stable endpoint returns list directly; v3 wraps in {"historical": [...]}
        if isinstance(raw, list):
            rows = raw
        elif isinstance(raw, dict) and "historical" in raw:
            rows = raw["historical"] or []
        else:
            rows = []

        self._cache.set(cache_key, rows)
        logger.debug(
            "FMP stable/historical-price-eod/full %s: %d rows (from %s)",
            sym, len(rows), from_date,
        )
        return rows
