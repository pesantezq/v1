"""
Crowd-source developer-doc audit — the validated, research-backed source catalog.

Each entry was verified against OFFICIAL developer documentation (blogs /
scraper marketplaces were rejected as non-authoritative). Updated 2026-06-21
to remove paid/partner-gated probes and add free text sources.

No-extra-cost policy: a source is only ``active`` if it is reachable with no
paid plan and no approval.

Active sources (as of 2026-06-21):
  - apewisdom  — free, no auth, attention/mentions
  - bluesky    — free, AT Protocol public API, text/sentiment
  - mastodon   — free, public instance APIs, text/sentiment
  - lemmy      — free, RSS + API, text/sentiment

Removed (paid/inert) 2026-06-21:
  - fmp_social_sentiment (Starter+ paid plan, never entitled)
  - finnhub_social       (premium-gated, 403 on free key)
  - stocktwits           (partner-gated, developer program suspended)
  - quiver_wsb           (paid ~$75/mo Trader tier)
"""
from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "2"

SOURCE_AUDIT: list[dict[str, Any]] = [
    {
        "source_name": "apewisdom",
        "official_docs_url_or_source": "https://apewisdom.io/api/ (+ live no-auth fetch)",
        "endpoint_candidates": [
            "https://apewisdom.io/api/v1.0/filter/{filter}",
            "https://apewisdom.io/api/v1.0/filter/{filter}/page/{pageNbr}",
        ],
        "auth_required": False,
        "known_params": {
            "filter": ["all-stocks", "wallstreetbets", "stocks", "options", "investing", "4chan"],
            "pageNbr": "int (optional)",
        },
        "response_fields": [
            "rank", "ticker", "name", "mentions", "upvotes",
            "rank_24h_ago", "mentions_24h_ago",
            "(top-level) count", "pages", "current_page",
        ],
        "pagination_model": "inline top-level pages/current_page/count; ~100 results/page; /page/{n}",
        "rate_limit_notes_if_available": "none published — be polite client-side (bounded pages + caching)",
        "cost_or_entitlement_status": "free, no API key (de facto; no written ToS/pricing)",
        "allowed_under_no_extra_cost": True,
        "implementation_status": "active",
        "source_type": "attention",
        "risk_notes": "no published rate limit/ToS or versioning guarantee — parse defensively, cache, throttle",
        "recommended_connector_behavior": (
            "read-only GET, no auth, bounded pages, degrade on missing keys, "
            "store aggregate counts only (no raw post text)"
        ),
    },
    {
        "source_name": "bluesky",
        "official_docs_url_or_source": (
            "https://docs.bsky.app/docs/api/app-bsky-feed-search-posts "
            "(AT Protocol public AppView)"
        ),
        "endpoint_candidates": [
            "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?q={query}&limit=25",
            "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?q={query}&cursor={cursor}",
        ],
        "auth_required": False,
        "known_params": {
            "q": "search query (e.g. $NVDA or nvidia)",
            "limit": "int (1-100, default 25)",
            "cursor": "pagination cursor from prior response",
            "lang": "optional language filter (e.g. en)",
            "since": "ISO-8601 datetime lower bound",
        },
        "response_fields": [
            "posts[].uri", "posts[].cid", "posts[].author.did", "posts[].author.handle",
            "posts[].record.text", "posts[].record.createdAt",
            "posts[].likeCount", "posts[].replyCount", "posts[].repostCount",
            "cursor",
        ],
        "pagination_model": "cursor-based; cursor returned in response; request next page with cursor=",
        "rate_limit_notes_if_available": (
            "public AppView: no auth = shared rate limits; "
            "empirically ~3000 req/5 min per IP; be polite (1–2 req/s)"
        ),
        "cost_or_entitlement_status": "free, no API key for public read-only search",
        "allowed_under_no_extra_cost": True,
        "implementation_status": "active",
        "source_type": "text",
        "risk_notes": (
            "no written SLA; rate limits may tighten; "
            "author DIDs are pseudonymous — hash before storage; "
            "pagination cursor may expire"
        ),
        "recommended_connector_behavior": (
            "cashtag search ($TICKER + company name), paginated, polite delay, "
            "author concentration tracking via hashed DID, "
            "store: post_id_hash (sha256 uri[:16]), author_hash (sha256 DID[:12]), "
            "text trimmed + PII-free, engagement scores, created_at; "
            "never store raw DID or handle"
        ),
    },
    {
        "source_name": "mastodon",
        "official_docs_url_or_source": (
            "https://docs.joinmastodon.org/methods/search/ + "
            "https://docs.joinmastodon.org/methods/timelines/ "
            "(Mastodon v4 REST API — instance allowlist)"
        ),
        "endpoint_candidates": [
            "{instance}/api/v2/search?q={hashtag}&type=statuses&limit=40",
            "{instance}/api/v1/timelines/tag/{hashtag}?limit=40",
            "{instance}/api/v1/timelines/public?limit=40",
        ],
        "auth_required": False,
        "known_params": {
            "q": "search query (hashtag or cashtag)",
            "type": "statuses (for search endpoint)",
            "limit": "int (max 40 per page)",
            "max_id": "pagination cursor (ID of oldest status for next page)",
        },
        "response_fields": [
            "id", "content" "(HTML)", "created_at",
            "account.acct" "(for author hash)",
            "account.id",
            "favourites_count", "replies_count", "reblogs_count",
            "language", "url",
        ],
        "pagination_model": "Link header + max_id for older pages; or Link: <url>; rel=next",
        "rate_limit_notes_if_available": (
            "per-instance: typically 300 req/5 min (unauthenticated); "
            "respect X-RateLimit-Remaining / Retry-After"
        ),
        "cost_or_entitlement_status": "free, no API key required for public read-only timelines",
        "allowed_under_no_extra_cost": True,
        "implementation_status": "active",
        "source_type": "text",
        "risk_notes": (
            "content is HTML — strip tags before sentiment; "
            "instances may block public unauthenticated search; "
            "use configurable allowlist; degrade gracefully on any instance failure"
        ),
        "recommended_connector_behavior": (
            "configurable instance allowlist, hashtag/cashtag search + public timeline, "
            "HTML-strip content before processing, "
            "author_hash = sha256(account.acct[:12]), "
            "author concentration tracking, "
            "never store acct/username directly"
        ),
    },
    {
        "source_name": "lemmy",
        "official_docs_url_or_source": (
            "https://join-lemmy.org/api/classes/LemmyHttp.html "
            "(Lemmy REST API v3) + RSS feeds per community"
        ),
        "endpoint_candidates": [
            "{instance}/feeds/c/{community}.xml?limit=20",
            "{instance}/api/v3/post/list?community_name={community}&sort=New&limit=40",
        ],
        "auth_required": False,
        "known_params": {
            "community_name": "e.g. stocks, investing",
            "sort": "New | Hot | TopDay",
            "limit": "int (max 50)",
            "page": "int (1-indexed)",
            "limit (RSS)": "int (max 50)",
        },
        "response_fields": [
            "post.name" "(title)", "post.body", "post.url",
            "post.ap_id" "(ActivityPub ID)", "post.published",
            "creator.actor_id" "(AP ID for author hash)",
            "counts.upvotes", "counts.comments",
        ],
        "pagination_model": "page parameter (1-indexed) or RSS cursoring by last item",
        "rate_limit_notes_if_available": (
            "no official rate limit published; instances self-throttle; "
            "empirically generous for read-only RSS"
        ),
        "cost_or_entitlement_status": "free, federated open-source; no API key for public read",
        "allowed_under_no_extra_cost": True,
        "implementation_status": "active",
        "source_type": "text",
        "risk_notes": (
            "instance availability varies; prefer RSS feeds (lighter weight); "
            "content may be sparse in finance communities; "
            "use configurable instance+community allowlist; "
            "actor_id is an ActivityPub IRI — hash before storage"
        ),
        "recommended_connector_behavior": (
            "RSS-first, API fallback; configurable instance+community allowlist, "
            "author_hash = sha256(actor_id[:12]), "
            "title + body combined text for sentiment, "
            "never store actor_id or username directly"
        ),
    },
]


def build_dev_doc_audit(*, run_id: str, run_mode: str, created_at: str) -> dict[str, Any]:
    """Build the crowd_source_dev_doc_audit.json payload."""
    active = [s["source_name"] for s in SOURCE_AUDIT if s["implementation_status"] == "active"]
    probe = [s["source_name"] for s in SOURCE_AUDIT if s["implementation_status"] == "probe_only"]
    blocked = [
        s["source_name"] for s in SOURCE_AUDIT
        if s["implementation_status"] in ("blocked_no_extra_cost", "requires_manual_review")
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "crowd_source_dev_doc_audit",
        "run_id": run_id,
        "run_mode": run_mode,
        "created_at": created_at,
        "source_status": "ok",
        "observe_only": True,
        "sandbox_only": True,
        "no_trade": True,
        "cost_policy": "no_extra_cost",
        "audited_count": len(SOURCE_AUDIT),
        "active_sources": active,
        "probe_only_sources": probe,
        "blocked_sources": blocked,
        "removed_sources": [
            "fmp_social_sentiment", "finnhub_social", "stocktwits", "quiver_wsb"
        ],
        "warnings": [],
        "records": SOURCE_AUDIT,
    }


def render_dev_doc_audit_md(payload: dict[str, Any]) -> str:
    """Render docs/CROWD_SOURCE_DEV_DOC_AUDIT.md from the audit payload."""
    lines = [
        "# Crowd Source Dev-Doc Audit",
        "",
        "_Validated against official developer documentation. No-extra-cost policy: "
        "a source is `active` only if reachable with no paid plan and no approval. "
        "Updated 2026-06-21: removed paid/inert probes; added Bluesky, Mastodon, Lemmy._",
        "",
        f"- Active: **{', '.join(payload['active_sources']) or 'none'}**",
        f"- Probe-only: **{', '.join(payload['probe_only_sources']) or 'none'}**",
        f"- Blocked / manual-review: **{', '.join(payload['blocked_sources']) or 'none'}**",
        f"- Removed (paid/inert): **{', '.join(payload.get('removed_sources', []))}**",
        "",
    ]
    for s in payload["records"]:
        stype = s.get("source_type", "unknown")
        lines.append(f"## {s['source_name']} — `{s['implementation_status']}` ({stype})")
        lines.append(f"- **Docs:** {s['official_docs_url_or_source']}")
        lines.append(f"- **Endpoints:** {'; '.join(s['endpoint_candidates'])}")
        lines.append(f"- **Auth required:** {s['auth_required']}")
        lines.append(f"- **Cost / entitlement:** {s['cost_or_entitlement_status']}")
        lines.append(f"- **Allowed under no-extra-cost:** {s['allowed_under_no_extra_cost']}")
        lines.append(f"- **Response fields:** {', '.join(s['response_fields'])}")
        lines.append(f"- **Pagination:** {s['pagination_model']}")
        lines.append(f"- **Rate limit:** {s['rate_limit_notes_if_available']}")
        lines.append(f"- **Risk notes:** {s['risk_notes']}")
        lines.append(f"- **Connector behavior:** {s['recommended_connector_behavior']}")
        lines.append("")
    lines.append(
        "_Sandbox research intelligence only. Not a trade recommendation. "
        "No paid data sources enabled._"
    )
    return "\n".join(lines) + "\n"
