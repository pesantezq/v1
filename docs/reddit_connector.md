# Reddit Connector (optional, currently inactive)

## Purpose

`portfolio_automation/social_intelligence/reddit_connector.py` is an
API-compliant, feature-gated, graceful-disabled connector for fetching recent
posts from subreddits via Reddit's official OAuth API. It is an **optional,
currently inactive source** — not a hard dependency of the daily run.

---

## Current Status (inactive)

As of 2026-06-16, Reddit's legacy Data API gates *new* app credentials behind an
approved moderation use case, which this research workload does not have. The
connector therefore stays in its graceful-disabled state
(`SourceStatus.NO_CREDENTIALS`) unless official OAuth credentials are provided via
`REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` / `REDDIT_USER_AGENT`.

It is **not scraped** and **not used by the Unified Crowd Intelligence bus** —
retail attention is instead sourced from ApeWisdom (a free, no-auth Reddit
aggregator) via `crowd_multi_source_velocity.json`. No ToS bypass is performed.

---

## Two-Lane Governance

This is a research/simulation-side data source within `social_intelligence`. It
is observe-only context and never feeds the production decision engine. Any
production use of crowd/social context flows through the human-approved
`sim_governance` promotion workflow.

---

## Design Guarantees

- Official OAuth API only (no scraping, no ToS bypass).
- Fails gracefully: missing credentials / rate limits / network errors return an
  honest status (`no_credentials` / `rate_limited` / `error` / `disabled`) and an
  empty post list — they **never raise** into the daily run.
- Author handles are hashed (`rh_<sha256[:16]>`) before they leave the module;
  raw bodies are returned only in transient `RawPost` objects for in-process
  feature extraction (persisted by the orchestrator only if the source permits
  raw-text storage).
- No new hard dependency: uses `requests` when present, degrades to `disabled`
  if absent.

---

## Key API

- `fetch_subreddit_posts(subreddits, *, limit_per_sub=100, credentials=None,
  http_get=None, oauth_token_fn=None) -> FetchResult` — fetches recent posts;
  `http_get` / `oauth_token_fn` are dependency-injection seams so tests never
  touch the network. Never raises.
- `@dataclass RedditCredentials` with `from_env()` — returns `None` when any of
  the three `REDDIT_*` env vars is missing.
- `@dataclass FetchResult` — `{status: SourceStatus, posts: [RawPost],
  warnings: [str]}`.
- `_hash_author`, `_default_oauth_token`, `_default_http_get` — internal helpers
  (the defaults are only used when the seams are not injected).

---

## Tests

Covered under `tests/` with the social-intelligence suite
(`python -m pytest -q tests -k reddit`).
