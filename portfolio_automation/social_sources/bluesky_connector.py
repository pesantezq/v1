"""
Bluesky connector — free, no-auth, AT Protocol public AppView.

Verified against https://docs.bsky.app/docs/api/app-bsky-feed-search-posts
- Endpoint: GET https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts
- No authentication required for public read-only search.
- Rate limit: ~3000 req/5 min per IP (unauthenticated, shared AppView).
- Results: posts[].uri, .author.did, .record.text, .record.createdAt,
           .likeCount, .replyCount, .repostCount; cursor for pagination.

Privacy: author DIDs are hashed (sha256[:12]) before any storage.
Raw post text is passed through to the sentiment pipeline but never persisted.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

from portfolio_automation.social_intelligence.base import SourceStatus
from portfolio_automation.social_sources.base import SourceResult

logger = logging.getLogger("stockbot.social_sources.bluesky")

_BASE = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"
_DEFAULT_LIMIT = 25
_USER_AGENT = "stockbot-crowd-radar/1.0 (observe-only research; no auth; contact: operator)"


def _hash_author(did: str) -> str:
    """Privacy-safe author identifier: SHA-256 of the DID, first 12 hex chars."""
    return hashlib.sha256(did.encode()).hexdigest()[:12]


def _hash_post_id(uri: str) -> str:
    """Dedup key: SHA-256 of the AT-URI, first 16 hex chars."""
    return hashlib.sha256(uri.encode()).hexdigest()[:16]


def _default_http_get(url: str) -> dict[str, Any]:  # pragma: no cover - network
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


class BlueskyConnector:
    """Read-only Bluesky public search connector. Never raises; returns SourceResult."""

    source_name = "bluesky"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        crowd_radar_enabled: bool = False,
        http_get: Callable[[str], dict[str, Any]] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        cfg = config or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._crowd_radar_enabled = bool(crowd_radar_enabled)
        self._limit = max(1, min(100, int(cfg.get("max_results_per_query", _DEFAULT_LIMIT))))
        self._max_pages = max(1, int(cfg.get("max_pages", 2)))
        self._delay = float(cfg.get("polite_delay_s", 0.5))
        # search_templates: ["${ticker}", "{company}"] — ticker filled at fetch time
        self._search_templates = list(cfg.get("search_templates") or ["${ticker}"])
        self._http_get = http_get or _default_http_get
        self._sleep = sleep or time.sleep

    def is_configured(self) -> bool:
        return self._crowd_radar_enabled and self._enabled

    def probe(self) -> SourceResult:
        """One small search to confirm the public API is reachable."""
        if not self.is_configured():
            return self._inert()
        try:
            url = f"{_BASE}?q=%24SPY&limit=1"
            data = self._http_get(url)
            ok = isinstance(data, dict) and "posts" in data
            return SourceResult(
                self.source_name,
                SourceStatus.OK if ok else SourceStatus.DEGRADED,
                meta={"probe_query": "$SPY", "post_count": len(data.get("posts", []))},
                warnings=[] if ok else ["unexpected_probe_shape"],
            )
        except Exception as exc:
            return SourceResult(
                self.source_name, SourceStatus.ERROR,
                warnings=[f"probe_error:{type(exc).__name__}"],
            )

    def fetch_for_ticker(
        self,
        ticker: str,
        company_name: str | None = None,
    ) -> SourceResult:
        """
        Fetch posts mentioning a specific ticker from Bluesky.

        Searches for cashtag ($TICKER) and optionally company name.
        Returns raw post records suitable for normalization + sentiment scoring.
        """
        if not self.is_configured():
            return self._inert()

        queries = [f"${ticker}"]
        if company_name:
            queries.append(company_name)

        all_records: list[dict[str, Any]] = []
        warnings: list[str] = []
        seen_ids: set[str] = set()

        for query in queries:
            cursor: str | None = None
            for _page in range(self._max_pages):
                if all_records:  # polite delay after first request
                    self._sleep(self._delay)
                params: dict[str, Any] = {
                    "q": query,
                    "limit": self._limit,
                    "lang": "en",
                }
                if cursor:
                    params["cursor"] = cursor
                url = f"{_BASE}?{urllib.parse.urlencode(params)}"
                try:
                    data = self._http_get(url)
                except Exception as exc:
                    warnings.append(f"fetch_error:{query}:{type(exc).__name__}")
                    break
                if not isinstance(data, dict) or not isinstance(data.get("posts"), list):
                    warnings.append(f"bad_shape:{query}")
                    break
                for post in data["posts"]:
                    rec = _extract_post(post, ticker)
                    if rec is None:
                        continue
                    pid = rec["post_id_hash"]
                    if pid not in seen_ids:
                        seen_ids.add(pid)
                        all_records.append(rec)
                cursor = data.get("cursor")
                if not cursor:
                    break  # no more pages

        if not all_records:
            status = SourceStatus.DEGRADED if warnings else SourceStatus.INSUFFICIENT_DATA
            return SourceResult(
                self.source_name, status,
                warnings=warnings or [f"no_posts_for:{ticker}"],
                meta={"ticker": ticker},
            )
        return SourceResult(
            self.source_name,
            SourceStatus.DEGRADED if warnings else SourceStatus.OK,
            records=all_records,
            warnings=warnings,
            meta={"ticker": ticker, "post_count": len(all_records)},
        )

    def fetch(self) -> SourceResult:
        """Single-probe fetch with no ticker (for pipeline compatibility)."""
        return self.probe()

    def normalize(self, raw: SourceResult) -> SourceResult:
        """Records from fetch_for_ticker are already normalized; passthrough."""
        return raw

    def health(self) -> SourceResult:
        if not self._crowd_radar_enabled or not self._enabled:
            return SourceResult(self.source_name, SourceStatus.DISABLED,
                               warnings=["crowd_radar or bluesky disabled"])
        return self.probe()

    def _inert(self) -> SourceResult:
        return SourceResult(self.source_name, SourceStatus.DISABLED,
                           warnings=["crowd_radar or bluesky disabled"])


def _extract_post(post: Any, ticker: str) -> dict[str, Any] | None:
    """Extract normalized fields from a raw Bluesky post object."""
    if not isinstance(post, dict):
        return None
    uri = str(post.get("uri") or "")
    author = post.get("author") or {}
    did = str(author.get("did") or "")
    record = post.get("record") or {}
    text = str(record.get("text") or "")
    created_at = str(record.get("createdAt") or post.get("indexedAt") or "")
    if not (uri and text):
        return None
    return {
        "schema_version": "2",
        "source": "bluesky",
        "source_type": "text",
        "ticker": ticker.upper(),
        "post_id_hash": _hash_post_id(uri),
        "author_hash": _hash_author(did) if did else "",
        "created_at": created_at,
        "text": text[:500],  # bounded; never store raw indefinitely
        "text_len": len(text),
        "like_count": int(post.get("likeCount") or 0),
        "reply_count": int(post.get("replyCount") or 0),
        "repost_count": int(post.get("repostCount") or 0),
        "engagement_score": _engagement(post),
        "language": record.get("langs", ["en"])[0] if isinstance(record.get("langs"), list) else "en",
    }


def _engagement(post: dict[str, Any]) -> float:
    """Normalized engagement score [0, 1] based on likes + replies + reposts."""
    likes = int(post.get("likeCount") or 0)
    replies = int(post.get("replyCount") or 0)
    reposts = int(post.get("repostCount") or 0)
    raw = likes + replies * 2 + reposts * 1.5
    return round(min(1.0, raw / 100.0), 4)
