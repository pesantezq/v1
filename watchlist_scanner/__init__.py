"""
Watchlist Scanner — Alpha Vantage powered signal detection.

Scans a fixed ~20-stock watchlist for price momentum, volume spikes,
and news-driven themes. Designed for the Alpha Vantage free tier
(≤25 requests/day) with aggressive local caching.

Usage:
    py -m watchlist_scanner [--dry-run] [--debug]
"""
