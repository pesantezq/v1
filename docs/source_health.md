# Crowd Source Health + Factory

## Purpose

`portfolio_automation/social_sources/source_health.py` is the factory and health
layer for the crowd-source connectors. It instantiates the active free connectors
from config, runs their `health()` probes, and classifies each source as active /
probe-only / blocked using the dev-doc audit. Only active **free** sources are
registered here; paid / partner-gated probes (FMP social sentiment, Finnhub,
Stocktwits, Quiver) were removed 2026-06-21 because they returned only inert
statuses.

---

## Two-Lane Governance

This module is part of the **simulation-active / production-gated / sandbox-only**
crowd / social-sentiment lane. Health output never feeds
`outputs/latest/decision_plan.json` and never touches any score semantics
(`feeds_decision_engine=false`, `sandbox_only=true`).

---

## Active Sources

| Source | Type | Auth |
|--------|------|------|
| ApeWisdom | attention / mention counts | free, no auth |
| Bluesky | text / sentiment | free, AT Protocol public API |
| Mastodon | text / sentiment | free, instance allowlist |
| Lemmy | text / sentiment | free, RSS + API |

All four are public APIs requiring **no credentials** — `credentials_present()`
intentionally returns an empty map (the expected, healthy state).

---

## Key API

- `build_sources(cfg) -> dict[str, connector]` — instantiates ApeWisdom plus the
  three text connectors (Bluesky/Mastodon/Lemmy, lazily imported). Each connector
  is created with `crowd_radar_enabled` and its `source_policy.<name>` config. If
  the text connectors are not importable, it degrades to attention-only mode.
- `collect_health(sources) -> list[SourceResult]` — runs `health()` on each
  source; never raises (defensive `ERROR` result on any unexpected raise).
- `classify_sources(cfg) -> {"active", "probe_only", "blocked"}` — splits sources
  using the audit-derived `implementation_status` combined with whether each
  source is `enabled` in config.
- `credentials_present() -> dict[str, bool]` — environment credential map
  (intentionally empty; no active free source needs credentials).
- `entitlements_from_health(health) -> dict[str, bool]` — maps source → confirmed
  entitlement from probe `meta.entitled` (relevant only for entitlement-bearing
  sources).

---

## Related Modules

`dev_doc_audit` (`SOURCE_AUDIT` supplies the `implementation_status`
classification) · the connectors `apewisdom_connector`, `bluesky_connector`,
`mastodon_connector`, `lemmy_connector` · `pipeline` (consumes the same
connectors).

---

## Tests

Covered under `tests/` with the social-sources suite
(`python -m pytest -q tests -k source`).
