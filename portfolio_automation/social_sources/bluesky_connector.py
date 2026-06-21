"""
Bluesky connector — AT Protocol, authenticated via app password when available.

Verified against https://docs.bsky.app/docs/api/app-bsky-feed-search-posts
- Unauthenticated: GET https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts
  (may be blocked by CDN on datacenter IPs)
- Authenticated:   POST bsky.social createSession → accessJwt →
                   GET https://bsky.social/xrpc/app.bsky.feed.searchPosts
  (routes through Bluesky's own servers, bypasses CDN block)

Credentials (optional — falls back to unauthenticated if absent):
  BLUESKY_IDENTIFIER   env var or config["identifier"]   (handle or email)
  BLUESKY_APP_PASSWORD env var or config["app_password"] (Settings → App Passwords)

Privacy: author DIDs are hashed (sha256[:12]) before any storage.
Raw post text is passed through to the sentiment pipeline but never persisted.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

from portfolio_automation.social_intelligence.base import SourceStatus
from portfolio_automation.social_sources.base import SourceResult

logger = logging.getLogger("stockbot.social_sources.bluesky")

_BASE_UNAUTH = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"
_BASE_AUTH = "https://bsky.social/xrpc/app.bsky.feed.searchPosts"
_AUTH_ENDPOINT = "https://bsky.social/xrpc/com.atproto.server.createSession"
_DEFAULT_LIMIT = 25
_USER_AGENT = "stockbot-crowd-radar/1.0 (observe-only research; contact: operator)"


def _hash_author(did: str) -> str:
    return hashlib.sha256(did.encode()).hexdigest()[:12]


def _hash_post_id(uri: str) -> str:
    return hashlib.sha256(uri.encode()).hexdigest()[:16]


def _http_get_with_token(url: str, token: str | None) -> dict[str, Any]:  # pragma: no cover
    headers = {"User-Agent": _USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _default_http_get(url: str) -> dict[str, Any]:  # pragma: no cover
    return _http_get_with_token(url, None)


def _create_session(identifier: str, password: str) -> str | None:  # pragma: no cover
    """Authenticate and return accessJwt, or None on failure."""
    body = json.dumps({"identifier": identifier, "password": password}).encode()
    req = urllib.request.Request(
        _AUTH_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("accessJwt")
    except Exception as exc:
        logger.warning("Bluesky auth failed: %s", exc)
        return None


class BlueskyConnector:
    """Read-only Bluesky connector. Uses app-password auth when configured."""

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
        self._search_templates = list(cfg.get("search_templates") or ["${ticker}"])

        # Credentials: env vars take priority over config
        self._identifier = (
            os.environ.get("BLUESKY_IDENTIFIER") or cfg.get("identifier") or ""
        ).strip()
        self._app_password = (
            os.environ.get("BLUESKY_APP_PASSWORD") or cfg.get("app_password") or ""
        ).strip()
        self._token: str | None = None  # set lazily on first authenticated request
        self._auth_attempted = False

        self._http_get = http_get or _default_http_get
        self._sleep = sleep or time.sleep

    @property
    def _has_credentials(self) -> bool:
        return bool(self._identifier and self._app_password)

    def _ensure_token(self) -> None:  # pragma: no cover - network
        """Lazily authenticate on first use if credentials are present."""
        if self._auth_attempted or not self._has_credentials:
            return
        self._auth_attempted = True
        self._token = _create_session(self._identifier, self._app_password)
        if self._token:
            logger.info("Bluesky authenticated as %s", self._identifier)
        else:
            logger.warning("Bluesky auth failed — falling back to unauthenticated")

    def _get(self, url: str) -> dict[str, Any]:  # pragma: no cover - network
        """HTTP GET, injecting auth token when available."""
        self._ensure_token()
        if self._http_get is not _default_http_get:
            # Injected (test) http_get — pass through as-is
            return self._http_get(url)
        return _http_get_with_token(url, self._token)

    def _search_base(self) -> str:
        """Return the appropriate search endpoint based on auth state."""
        return _BASE_AUTH if self._token else _BASE_UNAUTH

    def is_configured(self) -> bool:
        return self._crowd_radar_enabled and self._enabled

    def probe(self) -> SourceResult:
        """One small search to confirm the API is reachable (authenticated if configured)."""
        if not self.is_configured():
            return self._inert()
        try:
            self._ensure_token()
            base = self._search_base()
            url = f"{base}?q=%24SPY&limit=1"
            data = self._get(url)
            ok = isinstance(data, dict) and "posts" in data
            return SourceResult(
                self.source_name,
                SourceStatus.OK if ok else SourceStatus.DEGRADED,
                meta={"probe_query": "$SPY", "authenticated": bool(self._token),
                      "post_count": len(data.get("posts", []))},
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

        self._ensure_token()
        base = self._search_base()

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
                url = f"{base}?{urllib.parse.urlencode(params)}"
                try:
                    data = self._get(url)
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
