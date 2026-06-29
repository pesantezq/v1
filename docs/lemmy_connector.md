# Lemmy Connector

## Purpose

`portfolio_automation/social_sources/lemmy_connector.py` is a free, no-auth,
read-only connector that fetches posts from a configurable allowlist of Lemmy
instances and finance communities, filtering for ticker mentions. It emits
`schema_version="2"` text records for the social-sentiment lane.

---

## Two-Lane Governance

This module is part of the **simulation-active / production-gated / sandbox-only**
social-sentiment lane. Its posts never feed `outputs/latest/decision_plan.json`
and never touch any score semantics (`feeds_decision_engine=false`,
`sandbox_only=true`). It is default-disabled until both `crowd_radar.enabled` and
`crowd_radar.source_policy.lemmy.enabled` are true.

---

## Endpoints (public, no auth)

- **RSS (preferred):** `{instance}/feeds/c/{community}.xml?limit=N`
- **API (fallback):** `{instance}/api/v3/post/list?community_name={community}&sort=New&limit=N`

`use_rss` (default `true`) selects the RSS path. RSS is parsed with `defusedxml`
when available (falls back to stdlib `ElementTree`), supporting both Atom and
RSS 2.0 entry shapes. The connector pulls a community feed, then filters posts
that mention the ticker (`TICKER`, `$TICKER`, `$ticker`, or company name).

---

## Design Guarantees

- **Read-only, no scraping, never raises** — per-community errors degrade to an
  honest `SourceStatus` and an empty record list.
- **HTML-stripped:** title + body combined and stripped to plain text before
  scoring.
- **Privacy:** creator `actor_id` (ActivityPub IRI) is hashed (`sha256[:12]`);
  post `ap_id` / RSS link hashed (`sha256[:16]`); text bounded to 500. RSS feeds do
  not reliably expose authors, so RSS records carry an empty `author_hash` (which
  interacts with the unique-author quality gate).
- **Politeness:** `polite_delay_s` (default 1.0s) between community fetches.
- **Injectable seams:** `http_get` / `sleep` keep tests off the network.

Records carry an extra `instance` field. RSS records have zeroed engagement
counts; API records derive `engagement_score` from upvotes.

---

## Config (`crowd_radar.source_policy.lemmy`)

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `false` | per-source switch |
| `instances` | `["lemmy.world"]` | instance allowlist |
| `communities` | `["stocks", "investing"]` | community allowlist |
| `max_results` | `40` | clamped to `[1, 50]` |
| `use_rss` | `true` | RSS-first vs API |
| `polite_delay_s` | `1.0` | delay between communities |

---

## Key API

- `class LemmyConnector(config=None, *, crowd_radar_enabled=False, http_get=None,
  sleep=None)` (`source_name="lemmy"`)
  - `fetch_for_ticker(ticker, company_name=None) -> SourceResult`
  - `probe() / fetch() / health() / normalize(raw) / is_configured()`.

---

## Related Modules

`source_health` · `dev_doc_audit` · `schema` · `pipeline`. Sibling connectors:
`bluesky_connector`, `mastodon_connector`.

---

## Tests

Covered under `tests/` with the social-sources suite
(`python -m pytest -q tests -k lemmy`).
