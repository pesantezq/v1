"""
Watchlist Scanner — FMP-primary signal detection.

Scans a static + extended watchlist for price momentum, volume spikes,
and news-driven themes. Data is sourced from Financial Modeling Prep
(FMP) — quotes, historical prices, profiles, ratios, and news — with
local caching keyed to the FMP TTLs in config.py.

Usage:
    py -m watchlist_scanner [--dry-run] [--debug]
"""
