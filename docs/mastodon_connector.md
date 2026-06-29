# Mastodon Connector

## Purpose

`portfolio_automation/social_sources/mastodon_connector.py` is a free, no-auth,
read-only connector that fetches ticker-mentioning statuses from a configurable
allowlist of Mastodon instances. It emits `schema_version="2"` text records for
the social-sentiment lane.

---

## Two-Lane Governance

This module is part of the **simulation-active / production-gated / sandbox-only**
social-sentiment lane. Its posts never feed `outputs/latest/decision_plan.json`
and never touch any score semantics (`feeds_decision_engine=false`,
`sandbox_only=true`). It is default-disabled until both `crowd_radar.enabled` and
`crowd_radar.source_policy.mastodon.enabled` are true.

---

## Endpoints (public, no auth)

- **Search (preferred):** `{instance}/api/v2/search?q={term}&type=statuses&limit=40`
- **Hashtag timeline (fallback):** `{instance}/api/v1/timelines/tag/{hashtag}?limit=40`

`_search_or_timeline` tries search first and falls back to the hashtag timeline
(stripping `$` and non-alphanumerics) — the public `timelines/public` endpoint is
avoided since it requires auth on many instances since 2023.

---

## Design Guarantees

- **Read-only, no scraping, never raises** — per-instance errors degrade to an
  honest `SourceStatus` and an empty record list; one bad instance never sinks the
  batch.
- **HTML-stripped:** status `content` is HTML; tags are removed and entities
  unescaped before processing.
- **Privacy:** `account.acct` is hashed (`sha256[:12]`); post IDs are hashed with
  the instance prefix (`sha256("{instance}:{id}")[:16]`); text bounded to 500.
- **Politeness:** at most 2 queries per instance, `polite_delay_s` (default 0.5s)
  between instances.
- **Injectable seams:** `http_get` / `sleep` keep tests off the network.

The record carries an extra `instance` field identifying the source instance.

---

## Config (`crowd_radar.source_policy.mastodon`)

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `false` | per-source switch |
| `instances` | `["mastodon.social"]` | instance allowlist |
| `max_results_per_hashtag` | `40` | clamped to `[1, 40]` |
| `polite_delay_s` | `0.5` | delay between instances |
| `search_hashtags` | `true` | enable hashtag search |

---

## Key API

- `class MastodonConnector(config=None, *, crowd_radar_enabled=False,
  http_get=None, sleep=None)` (`source_name="mastodon"`)
  - `fetch_for_ticker(ticker, company_name=None) -> SourceResult`
  - `probe() / fetch() / health() / normalize(raw) / is_configured()`.

---

## Related Modules

`source_health` · `dev_doc_audit` · `schema` · `pipeline`. Sibling connectors:
`bluesky_connector`, `lemmy_connector`.

---

## Tests

Covered under `tests/` with the social-sources suite
(`python -m pytest -q tests -k mastodon`).
