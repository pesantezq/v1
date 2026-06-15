# Crowd Source Dev-Doc Audit

_Validated against official developer documentation on 2026-06-14. Blogs and scraper marketplaces were rejected as non-authoritative. No-extra-cost policy: a source is `active` only if reachable with no paid plan and no approval._

- Active: **apewisdom**
- Probe-only: **fmp_social_sentiment, finnhub_social**
- Blocked / manual-review: **stocktwits, quiver_wsb**

## apewisdom — `active`
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

## fmp_social_sentiment — `probe_only`
- **Docs:** https://site.financialmodelingprep.com/developer/docs/social-sentiment-api (Legacy label) + FMP example repo
- **Endpoints:** /stable/historical/social-sentiment (current); /api/v4/historical/social-sentiment (legacy fallback); /stable/social-sentiments/trending; /stable/social-sentiments/change
- **Auth required:** True
- **Cost / entitlement:** PAID — Starter+ entitlement; NOT on free/Basic tier
- **Allowed under no-extra-cost:** False
- **Response fields:** date, symbol, stocktwitsPosts, twitterPosts, stocktwitsSentiment, twitterSentiment, sentiment, absoluteIndex, relativeIndex, generalPerception
- **Pagination:** page-based (0-indexed), optional limit
- **Rate limit:** plan-level (repo posture: 250/min subscription, daily cap removed)
- **Risk notes:** assume not_entitled until a live probe proves otherwise; never upgrade; registered in fmp_endpoint_registry (P3 premium_optional)
- **Connector behavior:** single entitlement probe vs /stable/ (v4 fallback); 200+rows=entitled, 402/403/empty/Error Message=not_entitled; budget-guard first; observe-only

## stocktwits — `requires_manual_review`
- **Docs:** https://api.stocktwits.com/developers (registrations CLOSED) + Firestream partner docs
- **Endpoints:** legacy /api/2/streams/symbol/{S}.json + /api/2/trending/symbols.json (docs now 404 — unsanctioned); current Firestream/Sentiment v2 (partner-gated, Basic-auth provisioned)
- **Auth required:** True
- **Cost / entitlement:** partner/commercial, approval-gated; no published free tier
- **Allowed under no-extra-cost:** False
- **Response fields:** messageVolume, sentiment, timeframes, (legacy) symbol stream messages
- **Pagination:** streaming SSE cursor (seq_id, 24h) / point-in-time sentiment snapshot
- **Rate limit:** not officially documented (community '200/hr' is unverified)
- **Risk notes:** free program suspended; legacy endpoints undocumented (private-endpoint use forbidden); storage/redistribution terms unpublished
- **Connector behavior:** NO network call; inert probe; manual partner inquiry (developers@stocktwits.com) is the only sanctioned path

## finnhub_social — `probe_only`
- **Docs:** https://finnhub.io/docs/api/social-sentiment + official SDK + issue tracker #557
- **Endpoints:** GET /api/v1/stock/social-sentiment?symbol=AAPL&token=KEY
- **Auth required:** True
- **Cost / entitlement:** PREMIUM-only; free key → HTTP 403 'You don't have access to this resource.'
- **Allowed under no-extra-cost:** False
- **Response fields:** data[] (or legacy reddit[]/twitter[]): atTime, mention, positiveMention, negativeMention, positiveScore, negativeScore, score
- **Pagination:** none (date-window bounded single object)
- **Rate limit:** free tier 60 calls/min (account-wide)
- **Risk notes:** 403 is the EXPECTED non-entitled state, not an error; skip entirely unless FINNHUB_API_KEY set; never buy premium
- **Connector behavior:** probe only if key present; 200+rows=entitled else not_entitled; cache verdict; stay dormant unless entitled

## quiver_wsb — `blocked_no_extra_cost`
- **Docs:** https://www.quiverquant.com/api-setup/ + pricing + official python-api source
- **Endpoints:** /beta/live/wallstreetbets; /beta/historical/wallstreetbets/{ticker}
- **Auth required:** True
- **Cost / entitlement:** PAID — Trader tier ~$75/mo for WSB; no free entitlement; no key in repo
- **Allowed under no-extra-cost:** False
- **Response fields:** Date/Time, Ticker, Mentions (unverified field name), Rank (unverified), Sentiment (unverified)
- **Pagination:** none (full array per call, client-side date filter)
- **Rate limit:** not documented
- **Risk notes:** recurring billing; auth scheme is 'Authorization: Token' (not Bearer); never auto-subscribe
- **Connector behavior:** inert; no network; activate only with a pre-existing QUIVER_API_KEY AND explicit allow_paid_sources opt-in

_Sandbox research intelligence only. Not a trade recommendation. No paid data sources enabled._
