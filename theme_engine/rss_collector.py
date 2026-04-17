"""
RSS Collector — fetches and deduplicates RSS feed items.

Only stores headline + short snippet (≤280 chars) + link.
No full article text is ever saved (copyright compliance).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_SEEN_CACHE = 500  # rolling cap on seen-hashes cache


def _item_hash(link: str, title: str) -> str:
    """Stable 16-char hex hash for dedup — link + title."""
    raw = f"{link}||{title}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _truncate(text: str, max_chars: int = 280) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 1] + "…"


def _parse_feed(url: str) -> list[dict[str, Any]]:
    """Parse a single RSS feed URL.  Returns raw feedparser entries."""
    try:
        import feedparser  # optional dep; tested offline via mock
        parsed = feedparser.parse(url)
        return parsed.entries  # type: ignore[return-value]
    except Exception as exc:
        logger.warning("RSS parse error for %s: %s", url, exc)
        return []


class RSSCollector:
    """Collect new RSS headlines across multiple feeds.

    Args:
        feeds:      List of RSS feed URLs.
        max_items:  Maximum new items to return per run.
        cache_path: Path to JSON file tracking seen item hashes.
    """

    def __init__(
        self,
        feeds: list[str],
        max_items: int = 30,
        cache_path: str = "data/rss_seen.json",
    ) -> None:
        self.feeds = feeds
        self.max_items = max_items
        self.cache_path = Path(cache_path)
        self._seen: set[str] = self._load_seen()

    # ── Public API ────────────────────────────────────────────────────────────

    def collect(self) -> list[dict[str, Any]]:
        """Return up to max_items new, deduplicated headlines.

        Each item dict has keys:
            title, summary (≤280 chars), link, published (ISO str), item_hash
        """
        results: list[dict[str, Any]] = []

        for url in self.feeds:
            if len(results) >= self.max_items:
                break
            entries = _parse_feed(url)
            for entry in entries:
                if len(results) >= self.max_items:
                    break
                item = self._extract(entry)
                if item is None:
                    continue
                h = item["item_hash"]
                if h in self._seen:
                    continue
                self._seen.add(h)
                results.append(item)

        self._save_seen()
        logger.info("RSS collector: %d new items from %d feeds", len(results), len(self.feeds))
        return results

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _extract(self, entry: Any) -> dict[str, Any] | None:
        """Convert a feedparser entry to our item dict.  Returns None on failure."""
        try:
            title = getattr(entry, "title", "") or ""
            link = getattr(entry, "link", "") or ""
            if not title or not link:
                return None

            # summary / description
            summary_raw = (
                getattr(entry, "summary", "")
                or getattr(entry, "description", "")
                or ""
            )
            summary = _truncate(summary_raw)

            # published date
            pub = getattr(entry, "published", None)
            if pub is None:
                pub = datetime.now(timezone.utc).isoformat()

            return {
                "title": title.strip(),
                "summary": summary,
                "link": link.strip(),
                "published": str(pub),
                "item_hash": _item_hash(link, title),
            }
        except Exception as exc:
            logger.debug("RSS entry extraction error: %s", exc)
            return None

    def _load_seen(self) -> set[str]:
        if not self.cache_path.exists():
            return set()
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return set(data.get("hashes", []))
        except Exception:
            return set()

    def _save_seen(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            # Keep rolling cap to avoid unbounded growth
            hashes = list(self._seen)[-_MAX_SEEN_CACHE:]
            self.cache_path.write_text(
                json.dumps({"hashes": hashes}, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Could not save RSS seen cache: %s", exc)
