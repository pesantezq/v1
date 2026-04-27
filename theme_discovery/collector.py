"""
Collect news articles from RSS feeds.

Reuses theme_engine.rss_collector.RSSCollector for fetching and deduplication.
Writes to a separate seen-cache so it does not interfere with theme_engine runs.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlparse

from theme_engine.rss_collector import RSSCollector
from theme_discovery.models import Article

logger = logging.getLogger(__name__)

_DEFAULT_FEEDS: list[str] = [
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC&region=US&lang=en-US",
    "https://feeds.marketwatch.com/marketwatch/topstories/",
]


def collect_articles(
    feeds: list[str] | None = None,
    root: Path | None = None,
    max_items: int = 100,
) -> list[Article]:
    """
    Fetch and deduplicate news articles from RSS feeds.

    Args:
        feeds:     Override feed URLs. Falls back to config.json then hardcoded defaults.
        root:      Project root used for cache path resolution.
        max_items: Maximum articles to return.

    Returns:
        List of Article objects. Empty list on error or no feeds.
    """
    root = root or Path.cwd()

    if not feeds:
        feeds = _feeds_from_config(root) or _DEFAULT_FEEDS

    cache_path = root / "data" / "theme_discovery_rss_seen.json"

    try:
        rc = RSSCollector(feeds=feeds, max_items=max_items, cache_path=str(cache_path))
        raw_items = rc.collect()
    except Exception as exc:
        logger.warning("theme_discovery.collector: RSS collection failed: %s", exc)
        return []

    articles: list[Article] = []
    for item in raw_items:
        title = item.get("title", "").strip()
        if not title:
            continue
        articles.append(Article(
            title=title,
            summary=item.get("summary", ""),
            link=item.get("link", ""),
            published=item.get("published", ""),
            source_domain=_domain(item.get("link", "")),
            item_hash=item.get("item_hash", ""),
        ))

    logger.info("theme_discovery.collector: %d articles from %d feeds", len(articles), len(feeds))
    return articles


def _domain(url: str) -> str:
    try:
        host = urlparse(url).netloc
        return host.removeprefix("www.") if host else "unknown"
    except Exception:
        return "unknown"


def _feeds_from_config(root: Path) -> list[str]:
    try:
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
        return cfg.get("theme_engine", {}).get("rss_feeds", [])
    except Exception:
        return []
