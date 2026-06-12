"""
Reddit connector — API-compliant, feature-gated, graceful-disabled.

Design goals (from the feature spec):
- Use Reddit's official OAuth API (no scraping, no ToS bypass).
- Fail gracefully: missing credentials / rate limits / network errors return an
  honest status (``no_credentials`` / ``rate_limited`` / ``error``) and an empty
  post list — they NEVER raise into the daily run.
- Collect only the minimal allowed fields (see source_registry).
- No new hard dependency: uses ``requests`` (already vendored) when present, and
  degrades to ``disabled`` if it is somehow absent.

Author handles are hashed before they leave this module; raw bodies are returned
in transient :class:`RawPost` objects for in-process feature extraction and are
persisted by the orchestrator ONLY if the source permits raw-text storage.
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass

from portfolio_automation.social_intelligence.base import (
    RawPost,
    SourceStatus,
    utc_now_iso,
)

logger = logging.getLogger("stockbot.social_intelligence.reddit_connector")

_OAUTH_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_API_BASE = "https://oauth.reddit.com"


def _hash_author(author: str | None) -> str:
    if not author:
        return ""
    return "rh_" + hashlib.sha256(author.encode("utf-8")).hexdigest()[:16]


@dataclass
class RedditCredentials:
    client_id: str
    client_secret: str
    user_agent: str

    @classmethod
    def from_env(cls) -> "RedditCredentials | None":
        cid = (os.environ.get("REDDIT_CLIENT_ID") or "").strip()
        secret = (os.environ.get("REDDIT_CLIENT_SECRET") or "").strip()
        ua = (os.environ.get("REDDIT_USER_AGENT") or "").strip()
        if not (cid and secret and ua):
            return None
        return cls(client_id=cid, client_secret=secret, user_agent=ua)


@dataclass
class FetchResult:
    """Outcome of a fetch attempt."""

    status: SourceStatus
    posts: list[RawPost]
    warnings: list[str]


def fetch_subreddit_posts(
    subreddits: list[str],
    *,
    limit_per_sub: int = 100,
    credentials: RedditCredentials | None = None,
    http_get=None,
    oauth_token_fn=None,
) -> FetchResult:
    """
    Fetch recent posts from *subreddits* via the official OAuth API.

    Parameters
    ----------
    credentials:
        If None, attempts ``RedditCredentials.from_env()``. Absent → status
        ``no_credentials`` with an empty post list (never raises).
    http_get / oauth_token_fn:
        Dependency-injection seams for testing (so tests never touch the network).
        ``oauth_token_fn(creds) -> str`` returns a bearer token; ``http_get(url,
        headers, params) -> dict`` returns parsed JSON.

    This function NEVER raises; all failure modes map to a FetchResult status.
    """
    warnings: list[str] = []
    creds = credentials or RedditCredentials.from_env()
    if creds is None:
        return FetchResult(SourceStatus.NO_CREDENTIALS, [], ["REDDIT_* credentials not set"])

    # Resolve the HTTP + auth implementations (real or injected).
    if http_get is None or oauth_token_fn is None:
        try:
            import requests  # noqa: F401
        except Exception:
            return FetchResult(SourceStatus.DISABLED, [], ["requests not available"])
        oauth_token_fn = oauth_token_fn or _default_oauth_token
        http_get = http_get or _default_http_get

    try:
        token = oauth_token_fn(creds)
    except _RateLimited:
        return FetchResult(SourceStatus.RATE_LIMITED, [], ["oauth token rate-limited"])
    except Exception as exc:  # pragma: no cover - network failure path
        logger.warning("reddit oauth failed: %s", exc)
        return FetchResult(SourceStatus.ERROR, [], [f"oauth_error: {exc}"])

    posts: list[RawPost] = []
    collected_at = utc_now_iso()
    headers = {"Authorization": f"Bearer {token}", "User-Agent": creds.user_agent}

    for sub in subreddits:
        try:
            data = http_get(
                f"{_API_BASE}/r/{sub}/new",
                headers=headers,
                params={"limit": min(100, int(limit_per_sub))},
            )
        except _RateLimited:
            warnings.append(f"rate_limited:{sub}")
            return FetchResult(SourceStatus.RATE_LIMITED, posts, warnings)
        except Exception as exc:  # pragma: no cover - network failure path
            logger.warning("reddit fetch r/%s failed: %s", sub, exc)
            warnings.append(f"fetch_error:{sub}:{exc}")
            continue

        for child in (data or {}).get("data", {}).get("children", []):
            d = child.get("data", {}) if isinstance(child, dict) else {}
            try:
                posts.append(RawPost(
                    post_id=str(d.get("id", "")),
                    source="reddit",
                    community=str(d.get("subreddit", sub)),
                    created_utc=float(d.get("created_utc", 0.0) or 0.0),
                    title=str(d.get("title", "") or ""),
                    body=str(d.get("selftext", "") or ""),
                    flair=(d.get("link_flair_text") or None),
                    score=int(d.get("score", 0) or 0),
                    comment_count=int(d.get("num_comments", 0) or 0),
                    upvote_ratio=(float(d["upvote_ratio"]) if d.get("upvote_ratio") is not None else None),
                    url=str(d.get("permalink", "") or ""),
                    author_hash=_hash_author(d.get("author")),
                    collection_timestamp=collected_at,
                ))
            except Exception as exc:  # malformed record — skip, don't crash
                warnings.append(f"parse_error:{exc}")
                continue

    status = SourceStatus.OK if posts else SourceStatus.DEGRADED
    if not posts and not warnings:
        warnings.append("no_posts_returned")
    return FetchResult(status, posts, warnings)


# ---------------------------------------------------------------------------
# Default (real) HTTP implementations — only used when not injected.
# ---------------------------------------------------------------------------

class _RateLimited(Exception):
    pass


def _default_oauth_token(creds: RedditCredentials) -> str:  # pragma: no cover - network
    import requests

    resp = requests.post(
        _OAUTH_TOKEN_URL,
        auth=(creds.client_id, creds.client_secret),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": creds.user_agent},
        timeout=15,
    )
    if resp.status_code == 429:
        raise _RateLimited()
    resp.raise_for_status()
    return resp.json()["access_token"]


def _default_http_get(url: str, *, headers: dict, params: dict) -> dict:  # pragma: no cover - network
    import requests

    resp = requests.get(url, headers=headers, params=params, timeout=15)
    if resp.status_code == 429:
        raise _RateLimited()
    resp.raise_for_status()
    return resp.json()
