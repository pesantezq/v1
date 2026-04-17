"""
Watchlist Scanner — static defaults and constants.

All values here are overridden by config.json["watchlist_scanner"] at runtime.
"""

from __future__ import annotations

# Default watchlist: ~20 high-liquidity stocks + ETFs
DEFAULT_WATCHLIST: list[str] = [
    "AAPL", "MSFT", "NVDA", "AMD", "META", "GOOGL", "TSLA", "AMZN",
    "SMCI", "AVGO", "PLTR", "COIN", "MARA", "RIOT",
    "SPY", "QQQ", "XLE", "XLF", "XLK", "IWM",
]

# Alpha Vantage free tier: 25 calls/day hard cap;
# use 20 as our limit to leave headroom for the portfolio engine.
MAX_DAILY_CALLS: int = 20

# Cache TTLs (seconds)
CACHE_TTL_DAILY_SECONDS: int = 86_400    # 24 h  — daily OHLCV
CACHE_TTL_NEWS_SECONDS: int = 14_400     # 4 h   — news/sentiment
CACHE_TTL_QUOTE_SECONDS: int = 1_800     # 30 min — real-time quote
CACHE_TTL_OVERVIEW_SECONDS: int = 604_800  # 7 days — company fundamentals

# Alert thresholds
PRICE_CHANGE_ALERT_PCT: float = 3.0     # % single-day move to flag
VOLUME_SPIKE_FACTOR: float = 1.5        # today_vol > avg_vol * factor
THEME_SCORE_THRESHOLD: float = 0.40    # per-theme score to include in output
MIN_SIGNAL_SCORE: float = 0.50         # overall score required to emit alert

# Confidence-aware alert filtering
# confidence >= HIGH  → normal alert rules apply
# MEDIUM <= conf < HIGH → higher signal bar (MEDIUM_CONF_MIN_SIGNAL)
# MIN <= conf < MEDIUM  → only exceptional signals pass (EXCEPTIONAL_SIGNAL_SCORE)
# conf < MIN           → suppressed entirely (except observable price/volume triggers → "watch")
CONFIDENCE_HIGH_THRESHOLD: float = 0.75
CONFIDENCE_MEDIUM_THRESHOLD: float = 0.60
CONFIDENCE_MIN_THRESHOLD: float = 0.50
MEDIUM_CONF_MIN_SIGNAL: float = 0.60    # higher bar required for medium-confidence alerts
EXCEPTIONAL_SIGNAL_SCORE: float = 0.85  # passes even at low confidence (if >= MIN_THRESHOLD)

# Alert cooldown — repeat unchanged alerts suppressed for this many days
ALERT_COOLDOWN_DAYS: int = 3

# Output / cache directories
DEFAULT_CACHE_DIR: str = "data/watchlist_cache"
DEFAULT_OUTPUT_DIR: str = "outputs/latest"
