"""
Mastodon connector — free, no-auth, configurable instance allowlist.

Verified against https://docs.joinmastodon.org/methods/search/ and
https://docs.joinmastodon.org/methods/timelines/

Public endpoints (no auth required):
  - /api/v2/search?q={term}&type=statuses&limit=40   (search, preferred)
  - /api/v1/timelines/tag/{hashtag}?limit=40          (hashtag timeline, fallback)

Privacy: account acct fields are hashed (sha256[:12]) before storage.
HTML content is stripped to plain text before processing.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

from portfolio_automation.social_intelligence.base import SourceStatus
from portfolio_automation.social_sources.base import SourceResult

logger = logging.getLogger("stockbot.social_sources.mastodon")

_DEFAULT_INSTANCES = ["mastodon.social"]
_MAX_RESULTS = 40
_USER_AGENT = "stockbot-crowd-radar/1.0 (observe-only research; contact: operator)"
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(content: str) -> str:
    """Strip HTML tags and unescape entities."""
    text = _HTML_TAG_RE.sub(" ", content)
    text = html.unescape(text)
    return " ".join(text.split())


def _hash_author(acct: str) -> str:
    return hashlib.sha256(acct.encode()).hexdigest()[:12]


def _hash_post_id(post_id: str, instance: str) -> str:
    return hashlib.sha256(f"{instance}:{post_id}".encode()).hexdigest()[:16]


def _default_http_get(url: str) -> Any:  # pragma: no cover - network
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


class MastodonConnector:
    """Read-only Mastodon connector (instance allowlist). Never raises."""

    source_name = "mastodon"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        crowd_radar_enabled: bool = False,
        http_get: Callable[[str], Any] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        cfg = config or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._crowd_radar_enabled = bool(crowd_radar_enabled)
        _inst = cfg.get("instances", None)
        self._instances = list(_inst) if _inst is not None else list(_DEFAULT_INSTANCES)
        self._max_results = min(40, max(1, int(cfg.get("max_results_per_hashtag", _MAX_RESULTS))))
        self._delay = float(cfg.get("polite_delay_s", 0.5))
        self._use_search = bool(cfg.get("search_hashtags", True))
        self._http_get = http_get or _default_http_get
        self._sleep = sleep or time.sleep

    def is_configured(self) -> bool:
        return self._crowd_radar_enabled and self._enabled and bool(self._instances)

    def probe(self) -> SourceResult:
        if not self.is_configured():
            return self._inert()
        instance = self._instances[0]
        try:
            url = f"https://{instance}/api/v1/timelines/public?limit=1"
            data = self._http_get(url)
            ok = isinstance(data, list)
            return SourceResult(
                self.source_name,
                SourceStatus.OK if ok else SourceStatus.DEGRADED,
                meta={"probe_instance": instance},
                warnings=[] if ok else ["unexpected_probe_shape"],
            )
        except Exception as exc:
            return SourceResult(
                self.source_name, SourceStatus.ERROR,
                warnings=[f"probe_error:{instance}:{type(exc).__name__}"],
            )

    def fetch_for_ticker(
        self,
        ticker: str,
        company_name: str | None = None,
    ) -> SourceResult:
        """Fetch statuses mentioning a ticker across configured instances."""
        if not self.is_configured():
            return self._inert()

        all_records: list[dict[str, Any]] = []
        warnings: list[str] = []
        seen_ids: set[str] = set()
        first = True

        for instance in self._instances:
            if not first:
                self._sleep(self._delay)
            first = False

            # Try search API first, fall back to hashtag timeline
            records, w = self._fetch_from_instance(instance, ticker, company_name, seen_ids)
            all_records.extend(records)
            warnings.extend(w)

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
            records=all_records, warnings=warnings,
            meta={"ticker": ticker, "post_count": len(all_records)},
        )

    def _fetch_from_instance(
        self,
        instance: str,
        ticker: str,
        company_name: str | None,
        seen_ids: set[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        records: list[dict[str, Any]] = []
        warnings: list[str] = []

        # Build search queries
        queries = [ticker.lower(), f"${ticker}"]
        if company_name:
            queries.append(company_name.lower()[:40])

        for query in queries[:2]:  # cap at 2 queries per instance
            try:
                statuses = self._search_or_timeline(instance, query)
            except Exception as exc:
                warnings.append(f"fetch_error:{instance}:{query}:{type(exc).__name__}")
                continue

            for s in statuses:
                rec = _extract_status(s, ticker, instance)
                if rec is None:
                    continue
                pid = rec["post_id_hash"]
                if pid not in seen_ids:
                    seen_ids.add(pid)
                    records.append(rec)

        return records, warnings

    def _search_or_timeline(self, instance: str, query: str) -> list[Any]:
        """Try /api/v2/search first; fall back to /api/v1/timelines/tag/{hashtag}."""
        try:
            params = urllib.parse.urlencode(
                {"q": query, "type": "statuses", "limit": self._max_results}
            )
            url = f"https://{instance}/api/v2/search?{params}"
            data = self._http_get(url)
            if isinstance(data, dict) and isinstance(data.get("statuses"), list):
                return data["statuses"]
        except Exception:
            pass

        # Fallback: hashtag timeline (strip $ and special chars)
        hashtag = re.sub(r"[^a-zA-Z0-9]", "", query)
        if not hashtag:
            return []
        url = f"https://{instance}/api/v1/timelines/tag/{hashtag}?limit={self._max_results}"
        data = self._http_get(url)
        return data if isinstance(data, list) else []

    def fetch(self) -> SourceResult:
        return self.probe()

    def normalize(self, raw: SourceResult) -> SourceResult:
        return raw

    def health(self) -> SourceResult:
        if not self._crowd_radar_enabled or not self._enabled:
            return SourceResult(self.source_name, SourceStatus.DISABLED,
                               warnings=["crowd_radar or mastodon disabled"])
        return self.probe()

    def _inert(self) -> SourceResult:
        return SourceResult(self.source_name, SourceStatus.DISABLED,
                           warnings=["crowd_radar or mastodon disabled"])


def _extract_status(status: Any, ticker: str, instance: str) -> dict[str, Any] | None:
    if not isinstance(status, dict):
        return None
    content = str(status.get("content") or "")
    text = _strip_html(content)
    if not text:
        return None
    post_id = str(status.get("id") or "")
    account = status.get("account") or {}
    acct = str(account.get("acct") or "")
    created_at = str(status.get("created_at") or "")
    if not post_id:
        return None
    return {
        "schema_version": "2",
        "source": "mastodon",
        "source_type": "text",
        "ticker": ticker.upper(),
        "post_id_hash": _hash_post_id(post_id, instance),
        "author_hash": _hash_author(acct) if acct else "",
        "created_at": created_at,
        "text": text[:500],
        "text_len": len(text),
        "like_count": int(status.get("favourites_count") or 0),
        "reply_count": int(status.get("replies_count") or 0),
        "repost_count": int(status.get("reblogs_count") or 0),
        "engagement_score": _engagement(status),
        "language": str(status.get("language") or "en"),
        "instance": instance,
    }


def _engagement(status: dict[str, Any]) -> float:
    likes = int(status.get("favourites_count") or 0)
    replies = int(status.get("replies_count") or 0)
    reposts = int(status.get("reblogs_count") or 0)
    raw = likes + replies * 2 + reposts * 1.5
    return round(min(1.0, raw / 50.0), 4)
