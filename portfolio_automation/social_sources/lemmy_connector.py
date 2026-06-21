"""
Lemmy connector — free, federated, configurable instance+community allowlist.

Verified against https://join-lemmy.org/api/classes/LemmyHttp.html
and community RSS feed behavior on lemmy.world.

Public endpoints (no auth):
  - RSS:  https://{instance}/feeds/c/{community}.xml?limit=20  (preferred)
  - API:  https://{instance}/api/v3/post/list?community_name={community}&sort=New&limit=40

Privacy: creator actor_id (ActivityPub IRI) is hashed (sha256[:12]) before storage.
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
try:
    import defusedxml.ElementTree as ET  # type: ignore[import]
except ImportError:  # pragma: no cover - defusedxml is in requirements.txt
    import xml.etree.ElementTree as ET  # type: ignore[assignment]  # fallback (Python 3.8+ mitigates XXE)
from typing import Any, Callable

from portfolio_automation.social_intelligence.base import SourceStatus
from portfolio_automation.social_sources.base import SourceResult

logger = logging.getLogger("stockbot.social_sources.lemmy")

_DEFAULT_INSTANCES = ["lemmy.world"]
_DEFAULT_COMMUNITIES = ["stocks", "investing"]
_USER_AGENT = "stockbot-crowd-radar/1.0 (observe-only research; contact: operator)"
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_RSS_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _strip_html(text: str) -> str:
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return " ".join(text.split())


def _hash_author(actor_id: str) -> str:
    return hashlib.sha256(actor_id.encode()).hexdigest()[:12]


def _hash_post_id(ap_id: str) -> str:
    return hashlib.sha256(ap_id.encode()).hexdigest()[:16]


def _default_http_get(url: str) -> Any:  # pragma: no cover - network
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


class LemmyConnector:
    """Read-only Lemmy connector (instance+community allowlist). Never raises."""

    source_name = "lemmy"

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
        _comm = cfg.get("communities", None)
        self._communities = list(_comm) if _comm is not None else list(_DEFAULT_COMMUNITIES)
        self._max_results = min(50, max(1, int(cfg.get("max_results", 40))))
        self._use_rss = bool(cfg.get("use_rss", True))
        self._delay = float(cfg.get("polite_delay_s", 1.0))
        self._http_get = http_get or _default_http_get
        self._sleep = sleep or time.sleep

    def is_configured(self) -> bool:
        return (
            self._crowd_radar_enabled
            and self._enabled
            and bool(self._instances)
            and bool(self._communities)
        )

    def probe(self) -> SourceResult:
        if not self.is_configured():
            return self._inert()
        instance = self._instances[0]
        community = self._communities[0]
        try:
            if self._use_rss:
                url = f"https://{instance}/feeds/c/{urllib.parse.quote(community)}.xml?limit=1"
                raw = self._http_get(url)
                ok = isinstance(raw, str) and ("<feed" in raw or "<rss" in raw)
            else:
                params = urllib.parse.urlencode(
                    {"community_name": community, "sort": "New", "limit": 1}
                )
                url = f"https://{instance}/api/v3/post/list?{params}"
                raw = self._http_get(url)
                data = json.loads(raw) if isinstance(raw, str) else raw
                ok = isinstance(data, dict) and "posts" in data
            return SourceResult(
                self.source_name,
                SourceStatus.OK if ok else SourceStatus.DEGRADED,
                meta={"probe_instance": instance, "probe_community": community},
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
        """Fetch posts from configured communities that mention a ticker."""
        if not self.is_configured():
            return self._inert()

        all_records: list[dict[str, Any]] = []
        warnings: list[str] = []
        seen_ids: set[str] = set()
        first = True

        for instance in self._instances:
            for community in self._communities:
                if not first:
                    self._sleep(self._delay)
                first = False

                records, w = self._fetch_community(instance, community, ticker, company_name, seen_ids)
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

    def _fetch_community(
        self,
        instance: str,
        community: str,
        ticker: str,
        company_name: str | None,
        seen_ids: set[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        records: list[dict[str, Any]] = []
        warnings: list[str] = []
        try:
            if self._use_rss:
                raw_records = self._fetch_rss(instance, community)
            else:
                raw_records = self._fetch_api(instance, community)
        except Exception as exc:
            warnings.append(f"fetch_error:{instance}/{community}:{type(exc).__name__}")
            return records, warnings

        # Filter records that mention the ticker
        ticker_upper = ticker.upper()
        cashtag = f"${ticker_upper}"
        keywords = {ticker_upper, cashtag, f"${ticker.lower()}"}
        if company_name:
            keywords.add(company_name.lower())

        for rec in raw_records:
            text = (rec.get("text") or "").lower()
            ticker_mentioned = any(kw.lower() in text for kw in keywords)
            if not ticker_mentioned:
                continue
            pid = rec.get("post_id_hash", "")
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
            rec["ticker"] = ticker_upper
            records.append(rec)

        return records, warnings

    def _fetch_rss(self, instance: str, community: str) -> list[dict[str, Any]]:
        url = (
            f"https://{instance}/feeds/c/{urllib.parse.quote(community)}.xml"
            f"?limit={self._max_results}"
        )
        raw = self._http_get(url)
        if not isinstance(raw, str):
            return []
        return _parse_rss(raw, instance)

    def _fetch_api(self, instance: str, community: str) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode(
            {"community_name": community, "sort": "New", "limit": self._max_results}
        )
        url = f"https://{instance}/api/v3/post/list?{params}"
        raw = self._http_get(url)
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, dict):
            return []
        return [_extract_api_post(p, instance) for p in data.get("posts", [])
                if _extract_api_post(p, instance) is not None]

    def fetch(self) -> SourceResult:
        return self.probe()

    def normalize(self, raw: SourceResult) -> SourceResult:
        return raw

    def health(self) -> SourceResult:
        if not self._crowd_radar_enabled or not self._enabled:
            return SourceResult(self.source_name, SourceStatus.DISABLED,
                               warnings=["crowd_radar or lemmy disabled"])
        return self.probe()

    def _inert(self) -> SourceResult:
        return SourceResult(self.source_name, SourceStatus.DISABLED,
                           warnings=["crowd_radar or lemmy disabled"])


def _parse_rss(xml_text: str, instance: str) -> list[dict[str, Any]]:
    """Parse Atom/RSS feed into normalized post records (no ticker filter yet)."""
    records: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return records

    # Atom feed
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = root.findall("atom:entry", ns)
    if not entries:
        # RSS 2.0 fallback
        entries = root.findall(".//item")

    for entry in entries:
        title = _get_text(entry, ["atom:title", "title"], ns)
        summary = _get_text(entry, ["atom:summary", "atom:content", "description"], ns)
        link = _get_text(entry, ["atom:id", "atom:link", "link", "guid"], ns)
        published = _get_text(entry, ["atom:updated", "atom:published", "pubDate"], ns)
        text = _strip_html(f"{title} {summary}").strip()
        if not text or not link:
            continue
        records.append({
            "schema_version": "2",
            "source": "lemmy",
            "source_type": "text",
            "ticker": "",  # filled by caller
            "post_id_hash": _hash_post_id(link),
            "author_hash": "",  # RSS feeds don't reliably expose author IDs
            "created_at": published or "",
            "text": text[:500],
            "text_len": len(text),
            "like_count": 0,
            "reply_count": 0,
            "repost_count": 0,
            "engagement_score": 0.0,
            "language": "en",
            "instance": instance,
        })
    return records


def _get_text(el: ET.Element, tags: list[str], ns: dict) -> str:
    for tag in tags:
        child = el.find(tag, ns)
        if child is not None:
            return (child.text or "").strip()
    return ""


def _extract_api_post(item: Any, instance: str) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    post = item.get("post") or {}
    creator = item.get("creator") or {}
    counts = item.get("counts") or {}
    title = str(post.get("name") or "")
    body = str(post.get("body") or "")
    text = _strip_html(f"{title} {body}").strip()
    ap_id = str(post.get("ap_id") or "")
    actor_id = str(creator.get("actor_id") or "")
    if not text or not ap_id:
        return None
    return {
        "schema_version": "2",
        "source": "lemmy",
        "source_type": "text",
        "ticker": "",  # filled by caller
        "post_id_hash": _hash_post_id(ap_id),
        "author_hash": _hash_author(actor_id) if actor_id else "",
        "created_at": str(post.get("published") or ""),
        "text": text[:500],
        "text_len": len(text),
        "like_count": int(counts.get("upvotes") or 0),
        "reply_count": int(counts.get("comments") or 0),
        "repost_count": 0,
        "engagement_score": round(min(1.0, (int(counts.get("upvotes") or 0)) / 50.0), 4),
        "language": "en",
        "instance": instance,
    }
