"""
RSS News adapter — per-symbol news article scraping via feedparser.

Reuses feedparser (already a project dependency via theme_engine/rss_collector.py).
Filters articles by ticker mention in title + summary so only relevant items
are returned per symbol.

Feed configuration
------------------
Feeds are specified in config.json["scraped_intel"]["rss_feeds"] as a list
of URL templates.  Use {symbol} as the placeholder:

    "rss_feeds": [
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
    ]

Fallback generic feeds (without per-symbol filtering) can be added by omitting
the {symbol} placeholder.  In that case, articles are filtered post-hoc by
ticker mention.

Separation guarantee
--------------------
Sentiment is extracted only when feedparser returns a sentiment field or when
the title text can be naively scored via word lists.  It is always stored in
ScrapedRecord.sentiment and never written to any hard-data field.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from scraped_intel.base import SourceAdapter
from scraped_intel.models import ScrapedRecord

logger = logging.getLogger("scraped_intel.rss_news")

_REQUEST_DELAY_S = 0.25   # courtesy delay between feed fetches

# Very small word-list sentiment scorer (no external dependency)
_POSITIVE_WORDS = frozenset({
    "surge", "surges", "surged", "jump", "jumps", "jumped", "beat", "beats",
    "record", "rally", "rallies", "bullish", "gain", "gains", "strong",
    "upgrade", "upgraded", "outperform", "breakout", "buy", "revenue",
    "profit", "growth", "breakthrough", "raises", "guidance",
})
_NEGATIVE_WORDS = frozenset({
    "drop", "drops", "dropped", "fall", "falls", "fell", "miss", "misses",
    "missed", "warning", "downgrade", "downgraded", "underperform", "sell",
    "loss", "losses", "weak", "decline", "cut", "cuts", "crash", "layoff",
    "lawsuit", "recall", "investigation", "default", "bearish", "disappoints",
})


def _naive_sentiment(text: str) -> float:
    """
    Naive word-list sentiment in [–1, +1].
    Not a replacement for a proper NLP scorer — used only when no other
    sentiment signal is available.
    """
    words = re.findall(r"[a-z]+", text.lower())
    pos = sum(1 for w in words if w in _POSITIVE_WORDS)
    neg = sum(1 for w in words if w in _NEGATIVE_WORDS)
    total = pos + neg
    if total == 0:
        return 0.0
    return round((pos - neg) / total, 3)


def _parse_published(entry: dict) -> Optional[str]:
    """Extract published_at as ISO string from a feedparser entry."""
    # feedparser normalises to published_parsed (struct_time UTC)
    if entry.get("published_parsed"):
        try:
            dt = datetime(*entry["published_parsed"][:6], tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass
    # Fallback: raw string
    raw = entry.get("published") or entry.get("updated") or ""
    if raw:
        try:
            dt = parsedate_to_datetime(raw).astimezone(timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass
    return None


class RSSNewsAdapter(SourceAdapter):
    """
    Fetches recent news articles for a symbol via RSS/Atom feeds.

    Each feed URL is fetched once per adapter instantiation (cached in
    memory).  The adapter filters entries by ticker mention so one generic
    feed can serve multiple symbols without re-fetching.
    """

    source_type = "rss_article"
    domain = "rss"
    source_weight = 0.55   # aggregated RSS; lower than primary financial press

    def __init__(
        self,
        feeds: list[str],
        cache_dir: str = "data/scraped_cache",
        known_themes: Optional[list[str]] = None,
    ) -> None:
        super().__init__(cache_dir=cache_dir)
        self._feeds = feeds or []
        self._known_themes = [t.lower() for t in (known_themes or [])]
        self._feed_cache: dict[str, list[dict]] = {}   # url → list of entries

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch(self, symbol: str, lookback_days: int = 30) -> list[ScrapedRecord]:
        """Return recent news articles mentioning `symbol` from configured feeds."""
        try:
            import feedparser  # already a project dependency
        except ImportError:
            logger.warning("RSSNewsAdapter: feedparser not installed — skipping")
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        records: list[ScrapedRecord] = []
        seen_ids: set[str] = set()
        collected_at = self.now_iso()

        for feed_tmpl in self._feeds:
            feed_url = feed_tmpl.format(symbol=symbol) if "{symbol}" in feed_tmpl else feed_tmpl
            entries = self._fetch_feed(feedparser, feed_url)

            for entry in entries:
                title   = str(entry.get("title") or "").strip()
                summary = str(entry.get("summary") or "").strip()
                link    = str(entry.get("link") or "").strip() or None
                pub_iso = _parse_published(entry)

                # Date filter
                if pub_iso:
                    try:
                        pub_dt = datetime.fromisoformat(pub_iso.replace("Z", "+00:00"))
                        if pub_dt < cutoff:
                            continue
                    except ValueError:
                        pass

                # Symbol relevance filter — ticker must appear in title or summary
                combined = (title + " " + summary).upper()
                # Match whole-word ticker (avoids matching "AMD" inside "AMENDED")
                if not re.search(rf"\b{re.escape(symbol.upper())}\b", combined):
                    continue

                excerpt = summary[:500] if summary else title[:500]
                text_lower = (title + " " + summary).lower()

                # Theme matching from known_themes
                matched_themes = [
                    t for t in self._known_themes
                    if re.search(rf"\b{re.escape(t)}\b", text_lower)
                ]

                # Sentiment
                sentiment = _naive_sentiment(title + " " + summary)

                record_id = ScrapedRecord.make_record_id(link, title, pub_iso or "")
                if record_id in seen_ids:
                    continue
                seen_ids.add(record_id)

                # Parse quality: higher when we have a full summary + link
                parse_quality = (
                    0.9 if (summary and link) else
                    0.7 if summary else
                    0.5
                )

                records.append(ScrapedRecord(
                    symbol=symbol,
                    source_type=self.source_type,
                    domain=self._extract_domain(feed_url),
                    url=link,
                    published_at=pub_iso,
                    collected_at=collected_at,
                    title=title,
                    excerpt=excerpt,
                    extraction_status="ok",
                    parse_quality=parse_quality,
                    themes=matched_themes,
                    sentiment=sentiment,
                    recency_hours=self.recency_hours(pub_iso),
                    record_id=record_id,
                    extra={"feed_url": feed_url},
                ))

        logger.debug("RSSNewsAdapter: %d articles for %s", len(records), symbol)
        return records

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_feed(self, feedparser, url: str) -> list[dict]:
        """Fetch and cache (in memory) a feed URL; return its entries."""
        if url in self._feed_cache:
            return self._feed_cache[url]
        try:
            time.sleep(_REQUEST_DELAY_S)
            parsed = feedparser.parse(url)
            entries = list(parsed.entries or [])
        except Exception as exc:
            logger.warning("RSSNewsAdapter: failed to fetch %s: %s", url, exc)
            entries = []
        self._feed_cache[url] = entries
        return entries

    @staticmethod
    def _extract_domain(url: str) -> str:
        """Extract bare domain from a URL string."""
        try:
            from urllib.parse import urlparse
            host = urlparse(url).netloc
            return host.lstrip("www.") or "rss"
        except Exception:
            return "rss"
