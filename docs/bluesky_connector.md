# Bluesky Connector

## Purpose

`portfolio_automation/social_sources/bluesky_connector.py` is a free, read-only
connector for fetching ticker-mentioning posts from Bluesky via the AT Protocol
`app.bsky.feed.searchPosts` endpoint. It emits `schema_version="2"` text records
for the social-sentiment lane.

---

## Two-Lane Governance

This module is part of the **simulation-active / production-gated / sandbox-only**
social-sentiment lane. Its posts never feed `outputs/latest/decision_plan.json`
and never touch any score semantics (`feeds_decision_engine=false`,
`sandbox_only=true`). It is default-disabled until both `crowd_radar.enabled` and
`crowd_radar.source_policy.bluesky.enabled` are true.

---

## Auth + Endpoints

- **Unauthenticated:** `GET https://public.api.bsky.app/.../searchPosts` — free,
  but the public AppView may be CDN-blocked on datacenter IPs.
- **Authenticated (preferred when creds present):** `createSession` →
  `accessJwt` → `GET https://bsky.social/.../searchPosts`, which routes through
  Bluesky's own servers and bypasses the CDN block.

Credentials are **optional** (falls back to unauthenticated): `BLUESKY_IDENTIFIER`
/ `BLUESKY_APP_PASSWORD` env vars (env takes priority over config `identifier` /
`app_password`). The token is created lazily on first use.

---

## Design Guarantees

- **Read-only, no scraping, never raises** into the daily run — fetch / probe
  errors return an honest `SourceStatus` (`OK` / `DEGRADED` /
  `INSUFFICIENT_DATA` / `ERROR` / `DISABLED`) and an empty record list.
- **Privacy:** author DIDs are hashed (`sha256[:12]`), post URIs hashed
  (`sha256[:16]`); text is bounded to 500 chars.
- **Politeness:** bounded pages (`max_pages`, default 2), `polite_delay_s`
  (default 0.5s) between requests, cursor-based pagination.
- **Injectable seams:** `http_get` / `sleep` constructor args keep tests off the
  network.

---

## Config (`crowd_radar.source_policy.bluesky`)

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `false` | per-source switch |
| `max_results_per_query` | `25` | clamped to `[1, 100]` |
| `max_pages` | `2` | pages per query |
| `polite_delay_s` | `0.5` | delay between requests |
| `search_templates` | `["${ticker}"]` | query templates |

---

## Key API

- `class BlueskyConnector(config=None, *, crowd_radar_enabled=False,
  http_get=None, sleep=None)` (`source_name="bluesky"`)
  - `fetch_for_ticker(ticker, company_name=None) -> SourceResult` — cashtag
    (`$TICKER`) + optional company-name search, de-duplicated by post hash.
  - `probe() / fetch() / health() / normalize(raw) / is_configured()`.

---

## Related Modules

`source_health` (factory + health collection) · `dev_doc_audit` (catalog entry) ·
`schema` (record shape) · `pipeline` (consumes `fetch_for_ticker`). Sibling
connectors: `mastodon_connector`, `lemmy_connector`.

---

## Tests

Covered under `tests/` with the social-sources suite
(`python -m pytest -q tests -k bluesky`).
