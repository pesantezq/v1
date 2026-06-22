# Crowd Source Dev-Doc Audit

_Validated against official developer documentation. No-extra-cost policy: a source is `active` only if reachable with no paid plan and no approval. Updated 2026-06-21: removed paid/inert probes; added Bluesky, Mastodon, Lemmy._

- Active: **apewisdom, bluesky, mastodon, lemmy**
- Probe-only: **none**
- Blocked / manual-review: **none**
- Removed (paid/inert): **fmp_social_sentiment, finnhub_social, stocktwits, quiver_wsb**

## apewisdom — `active` (attention)
- **Docs:** https://apewisdom.io/api/ (+ live no-auth fetch)
- **Endpoints:** https://apewisdom.io/api/v1.0/filter/{filter}; https://apewisdom.io/api/v1.0/filter/{filter}/page/{pageNbr}
- **Auth required:** False
- **Cost / entitlement:** free, no API key (de facto; no written ToS/pricing)
- **Allowed under no-extra-cost:** True
- **Response fields:** rank, ticker, name, mentions, upvotes, rank_24h_ago, mentions_24h_ago, (top-level) count, pages, current_page
- **Pagination:** inline top-level pages/current_page/count; ~100 results/page; /page/{n}
- **Rate limit:** none published — be polite client-side (bounded pages + caching)
- **Risk notes:** no published rate limit/ToS or versioning guarantee — parse defensively, cache, throttle
- **Connector behavior:** read-only GET, no auth, bounded pages, degrade on missing keys, store aggregate counts only (no raw post text)

## bluesky — `active` (text)
- **Docs:** https://docs.bsky.app/docs/api/app-bsky-feed-search-posts (AT Protocol public AppView)
- **Endpoints:** https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?q={query}&limit=25; https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?q={query}&cursor={cursor}
- **Auth required:** False
- **Cost / entitlement:** free, no API key for public read-only search
- **Allowed under no-extra-cost:** True
- **Response fields:** posts[].uri, posts[].cid, posts[].author.did, posts[].author.handle, posts[].record.text, posts[].record.createdAt, posts[].likeCount, posts[].replyCount, posts[].repostCount, cursor
- **Pagination:** cursor-based; cursor returned in response; request next page with cursor=
- **Rate limit:** public AppView: no auth = shared rate limits; empirically ~3000 req/5 min per IP; be polite (1–2 req/s)
- **Risk notes:** no written SLA; rate limits may tighten; author DIDs are pseudonymous — hash before storage; pagination cursor may expire
- **Connector behavior:** cashtag search ($TICKER + company name), paginated, polite delay, author concentration tracking via hashed DID, store: post_id_hash (sha256 uri[:16]), author_hash (sha256 DID[:12]), text trimmed + PII-free, engagement scores, created_at; never store raw DID or handle

## mastodon — `active` (text)
- **Docs:** https://docs.joinmastodon.org/methods/search/ + https://docs.joinmastodon.org/methods/timelines/ (Mastodon v4 REST API — instance allowlist)
- **Endpoints:** {instance}/api/v2/search?q={hashtag}&type=statuses&limit=40; {instance}/api/v1/timelines/tag/{hashtag}?limit=40; {instance}/api/v1/timelines/public?limit=40
- **Auth required:** False
- **Cost / entitlement:** free, no API key required for public read-only timelines
- **Allowed under no-extra-cost:** True
- **Response fields:** id, content(HTML), created_at, account.acct(for author hash), account.id, favourites_count, replies_count, reblogs_count, language, url
- **Pagination:** Link header + max_id for older pages; or Link: <url>; rel=next
- **Rate limit:** per-instance: typically 300 req/5 min (unauthenticated); respect X-RateLimit-Remaining / Retry-After
- **Risk notes:** content is HTML — strip tags before sentiment; instances may block public unauthenticated search; use configurable allowlist; degrade gracefully on any instance failure
- **Connector behavior:** configurable instance allowlist, hashtag/cashtag search + public timeline, HTML-strip content before processing, author_hash = sha256(account.acct[:12]), author concentration tracking, never store acct/username directly

## lemmy — `active` (text)
- **Docs:** https://join-lemmy.org/api/classes/LemmyHttp.html (Lemmy REST API v3) + RSS feeds per community
- **Endpoints:** {instance}/feeds/c/{community}.xml?limit=20; {instance}/api/v3/post/list?community_name={community}&sort=New&limit=40
- **Auth required:** False
- **Cost / entitlement:** free, federated open-source; no API key for public read
- **Allowed under no-extra-cost:** True
- **Response fields:** post.name(title), post.body, post.url, post.ap_id(ActivityPub ID), post.published, creator.actor_id(AP ID for author hash), counts.upvotes, counts.comments
- **Pagination:** page parameter (1-indexed) or RSS cursoring by last item
- **Rate limit:** no official rate limit published; instances self-throttle; empirically generous for read-only RSS
- **Risk notes:** instance availability varies; prefer RSS feeds (lighter weight); content may be sparse in finance communities; use configurable instance+community allowlist; actor_id is an ActivityPub IRI — hash before storage
- **Connector behavior:** RSS-first, API fallback; configurable instance+community allowlist, author_hash = sha256(actor_id[:12]), title + body combined text for sentiment, never store actor_id or username directly

_Sandbox research intelligence only. Not a trade recommendation. No paid data sources enabled._
