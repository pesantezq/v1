"""
Market data module for fetching asset prices.
Implements Alpha Vantage API with retry logic, caching, and throttling.
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from dataclasses import dataclass, field

import requests

from utils import get_env, Holding

try:
    from api_budget import AVDailyBudget as _AVDailyBudget
except ImportError:
    _AVDailyBudget = None  # type: ignore[assignment,misc]


logger = logging.getLogger('portfolio_automation.market_data')


@dataclass
class PriceCache:
    """TTL-based price cache for market data."""
    cache_dir: Path
    ttl_seconds: int = 300
    _cache: Dict[str, Tuple[float, datetime]] = field(default_factory=dict)
    
    def __post_init__(self):
        self.cache_dir = Path(self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._load_from_disk()
    
    def _cache_file(self) -> Path:
        return self.cache_dir / 'price_cache.json'
    
    def _load_from_disk(self) -> None:
        """Load cache from disk if exists."""
        cache_file = self._cache_file()
        if cache_file.exists():
            try:
                with open(cache_file, 'r') as f:
                    data = json.load(f)
                for symbol, entry in data.items():
                    price = entry['price']
                    timestamp = datetime.fromisoformat(entry['timestamp'])
                    self._cache[symbol] = (price, timestamp)
                logger.debug(f"Loaded {len(self._cache)} prices from cache")
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                logger.warning(f"Failed to load price cache: {e}")
                self._cache = {}
    
    def _save_to_disk(self) -> None:
        """Persist cache to disk."""
        data = {
            symbol: {
                'price': price,
                'timestamp': timestamp.isoformat()
            }
            for symbol, (price, timestamp) in self._cache.items()
        }
        try:
            with open(self._cache_file(), 'w') as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            logger.warning(f"Failed to save price cache: {e}")
    
    def get(self, symbol: str, allow_stale: bool = False) -> Optional[float]:
        """Get cached price if not expired. If allow_stale=True, return even expired prices."""
        if symbol not in self._cache:
            return None
        
        price, timestamp = self._cache[symbol]
        age = datetime.now() - timestamp
        
        if age > timedelta(seconds=self.ttl_seconds):
            if allow_stale:
                logger.warning(f"Using stale cache for {symbol} (age: {age})")
                return price
            logger.debug(f"Cache expired for {symbol}")
            return None
        
        logger.debug(f"Cache hit for {symbol}: ${price:.2f}")
        return price
    
    def set(self, symbol: str, price: float) -> None:
        """Store price in cache."""
        self._cache[symbol] = (price, datetime.now())
        self._save_to_disk()
        logger.debug(f"Cached price for {symbol}: ${price:.2f}")
    
    def clear(self) -> None:
        """Clear all cached prices."""
        self._cache = {}
        self._save_to_disk()


class RateLimiter:
    """Simple rate limiter for API calls."""
    
    def __init__(self, calls_per_minute: int = 5, min_interval: float = 12.0):
        self.calls_per_minute = calls_per_minute
        self.min_interval = min_interval  # Minimum seconds between calls
        self.call_timestamps: list[datetime] = []
        self.last_call: Optional[datetime] = None
    
    def wait_if_needed(self) -> None:
        """Block if rate limit would be exceeded."""
        now = datetime.now()
        
        # Enforce minimum interval between calls (Alpha Vantage needs ~12s for free tier)
        if self.last_call is not None:
            elapsed = (now - self.last_call).total_seconds()
            if elapsed < self.min_interval:
                wait_time = self.min_interval - elapsed
                logger.info(f"Rate limiting: waiting {wait_time:.1f}s before next API call")
                time.sleep(wait_time)
        
        # Remove timestamps older than 1 minute
        self.call_timestamps = [
            ts for ts in self.call_timestamps
            if now - ts < timedelta(minutes=1)
        ]
        
        if len(self.call_timestamps) >= self.calls_per_minute:
            oldest = self.call_timestamps[0]
            sleep_time = 60 - (now - oldest).total_seconds()
            if sleep_time > 0:
                logger.info(f"Rate limit reached, waiting {sleep_time:.1f}s")
                time.sleep(sleep_time + 1.0)
        
        self.call_timestamps.append(datetime.now())
        self.last_call = datetime.now()


class MarketDataError(Exception):
    """Custom exception for market data errors."""
    pass


class AlphaVantageClient:
    """Alpha Vantage API client with retry logic and rate limiting."""
    
    BASE_URL = "https://www.alphavantage.co/query"
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        cache_ttl: int = 86400,
        max_retries: int = 3,
        retry_base_delay: float = 2.0,
        timeout: int = 30,
        cache_dir: Optional[Path] = None,
        budget: Optional["_AVDailyBudget"] = None,
    ):
        self.api_key = api_key or get_env('ALPHA_VANTAGE_API_KEY', required=True)
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.timeout = timeout
        self._budget = budget

        # Alpha Vantage free tier: 5 calls/minute, 500 calls/day
        # Use 12-second minimum interval to be safe
        self.rate_limiter = RateLimiter(calls_per_minute=5, min_interval=12.0)

        cache_path = cache_dir or Path(__file__).parent / 'data'
        self.cache = PriceCache(cache_dir=cache_path, ttl_seconds=cache_ttl)

        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': 'PortfolioAutomation/1.0'
        })
    
    def _make_request(self, params: Dict[str, str]) -> Dict[str, Any]:
        """Make API request with retry logic and exponential backoff."""
        params['apikey'] = self.api_key
        
        last_error = None
        for attempt in range(self.max_retries):
            try:
                self.rate_limiter.wait_if_needed()
                
                response = self._session.get(
                    self.BASE_URL,
                    params=params,
                    timeout=self.timeout
                )
                response.raise_for_status()
                
                data = response.json()
                
                # Check for API-level errors
                if 'Error Message' in data:
                    raise MarketDataError(f"API error: {data['Error Message']}")
                
                if 'Note' in data:
                    # Rate limit message from Alpha Vantage
                    logger.warning(f"API rate limit hit: {data['Note'][:100]}...")
                    if attempt < self.max_retries - 1:
                        # Wait longer when we hit the rate limit note
                        delay = max(15, self.retry_base_delay * (2 ** attempt))
                        logger.info(f"Waiting {delay}s before retry due to rate limit")
                        time.sleep(delay)
                        continue
                    else:
                        raise MarketDataError(f"Rate limit exceeded after {self.max_retries} attempts")
                
                if 'Information' in data:
                    # Usually indicates API key issues or rate limits
                    raise MarketDataError(f"API info: {data['Information']}")
                
                return data
                
            except requests.exceptions.Timeout as e:
                last_error = e
                logger.warning(f"Request timeout (attempt {attempt + 1}/{self.max_retries})")
                
            except requests.exceptions.RequestException as e:
                last_error = e
                logger.warning(f"Request failed (attempt {attempt + 1}/{self.max_retries}): {e}")
            
            if attempt < self.max_retries - 1:
                delay = self.retry_base_delay * (2 ** attempt)
                logger.info(f"Retrying in {delay}s...")
                time.sleep(delay)
        
        raise MarketDataError(f"Failed after {self.max_retries} attempts: {last_error}")
    
    def get_quote(self, symbol: str) -> Optional[float]:
        """Get current price for a symbol."""
        # Check cache first
        cached_price = self.cache.get(symbol)
        if cached_price is not None:
            return cached_price

        # Shared AV budget guard — return stale cache if holdings quota exhausted
        if self._budget is not None and not self._budget.can_reserve("holdings", 1):
            logger.warning(
                "AV daily budget exhausted for holdings — returning stale cache for %s", symbol
            )
            return self.cache.get(symbol, allow_stale=True)

        logger.info(f"Fetching price for {symbol}")
        
        try:
            data = self._make_request({
                'function': 'GLOBAL_QUOTE',
                'symbol': symbol
            })
            
            quote = data.get('Global Quote', {})
            if not quote:
                logger.error(f"No quote data returned for {symbol}")
                # Try stale cache as fallback
                stale_price = self.cache.get(symbol, allow_stale=True)
                if stale_price:
                    return stale_price
                return None
            
            price_str = quote.get('05. price')
            if not price_str:
                logger.error(f"No price in quote for {symbol}")
                stale_price = self.cache.get(symbol, allow_stale=True)
                if stale_price:
                    return stale_price
                return None
            
            price = float(price_str)
            self.cache.set(symbol, price)
            if self._budget is not None:
                self._budget.reserve("holdings", 1)

            logger.info(f"Got price for {symbol}: ${price:.2f}")
            return price
            
        except (MarketDataError, ValueError, KeyError) as e:
            logger.error(f"Failed to get price for {symbol}: {e}")
            # Try stale cache as fallback
            stale_price = self.cache.get(symbol, allow_stale=True)
            if stale_price:
                logger.info(f"Using stale cached price for {symbol}: ${stale_price:.2f}")
                return stale_price
            return None
    
    def get_prices(self, symbols: list[str]) -> Dict[str, Optional[float]]:
        """Get prices for multiple symbols."""
        results = {}
        
        for symbol in symbols:
            try:
                price = self.get_quote(symbol)
                results[symbol] = price
            except Exception as e:
                logger.error(f"Error fetching {symbol}: {e}")
                results[symbol] = None
        
        return results


def create_market_client(
    config: Dict[str, Any],
    budget: Optional["_AVDailyBudget"] = None,
) -> AlphaVantageClient:
    """Factory function to create market data client from config."""
    return AlphaVantageClient(
        cache_ttl=config.get('cache_ttl_seconds', 86400),
        max_retries=config.get('max_retries', 3),
        retry_base_delay=config.get('retry_base_delay_seconds', 2.0),
        timeout=config.get('request_timeout_seconds', 30),
        budget=budget,
    )


def update_holdings_with_prices(
    holdings: list[Holding],
    market_client: AlphaVantageClient
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