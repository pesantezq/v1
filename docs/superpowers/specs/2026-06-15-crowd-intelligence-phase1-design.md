# Crowd Intelligence — Phase 1: Capability Registry + Probe (Design Spec)

- **Date:** 2026-06-15
- **Status:** Approved (design); implementing
- **Scope:** Phase 1 of the probe-gated FMP Crowd Intelligence layer — **observe-only
  capability discovery ONLY.** No adapters, scoring, signal builder, GUI/advisory
  integration, or decision-engine changes (those are Phase 2, scoped against this
  phase's live probe results).

## 1. Goal

Discover exactly which FMP endpoints the current Starter plan exposes, persist that
as ground truth, and bring every candidate path under the canonical compliance
layer — so Phase 2 builds adapters only for endpoints the probe confirms.

## 2. Keystone decisions (operator-approved)

1. **Probe-first phasing** — Phase 1 = registry + probe + live run; Phase 2 scoped to AVAILABLE endpoints.
2. **Generic governed call path** — Phase 2 adapters will use one additive `FMPClient.get_json(path, params, *, ttl_seconds, base_url)` via `governed_client(run_mode)`. (Phase 1's probe does NOT use it — see §6.)
3. **Crowd registry drives; register net-new in canonical** — `crowd_intelligence/endpoint_registry.py` is the rich source; any path not already in `fmp_endpoint_registry.REGISTRY` is added there too (compliance governs all FMP paths).

## 3. Confirmed Starter baseline (already proven, NOT probed)

These are registered + called today, so Phase 2 has a guaranteed working floor:
`stock_news` (`/stable/news/stock`), `ratings_snapshot` (`/stable/ratings-snapshot`),
`historical_ratings` (`/stable/historical-ratings`), plus supporting
`quote`/`profile`/`historical_prices`/`ratios`.

## 4. Files created (Phase 1)

```
portfolio_automation/crowd_intelligence/
  __init__.py
  endpoint_registry.py     # rich candidate registry (categories A–E)
  fmp_capability_probe.py  # PURE classifier + probe_all driver (HTTP injected)
  capability_store.py      # SQLite fmp_endpoint_capabilities (data/crowd_intelligence.db)
scripts/probe_fmp_crowd_endpoints.py   # CLI: load key, run probe, write artifacts + table
tests/test_crowd_intelligence_probe.py
```
Modified: `fmp_endpoint_registry.py` (net-new candidate paths), docs (§9).

## 5. Endpoint registry schema

Each `crowd_intelligence/endpoint_registry.py` entry:
`endpoint_id, provider="fmp", path, params_template (dict, {symbol} placeholders),
category, priority, expected_fields (list), min_plan_assumption (starter|premium|legacy|unknown),
legacy (bool), enabled_after_probe (bool, default False), ttl_seconds, run_modes (list)`.

Categories + candidate paths:
- **A. Direct social (legacy, probe-only):** `historical_social_sentiment` (`/stable/historical/social-sentiment`, legacy `/api/v4/historical/social-sentiment`), `social_sentiment_legacy` (`/api/v4/social-sentiment`), `stock_news_sentiment_rss` (`/api/v4/stock-news-sentiments-rss-feed`).
- **B. News (Starter-assumed):** `fmp_articles` (`/stable/fmp-articles`), `general_news` (`/stable/news/general-latest`), `stock_news_latest` (`/stable/news/stock-latest`), `stock_news_search` (`/stable/news/stock`), `crypto_news` (`/stable/news/crypto-latest`), `forex_news` (`/stable/news/forex-latest`).
- **C. Analyst:** `ratings_snapshot` (`/stable/ratings-snapshot`), `ratings_historical` (`/stable/historical-ratings`), `stock_grades` (`/stable/grades`), `grades_consensus` (`/stable/grades-consensus`).
- **D. Insider / congress:** `latest_insider_trading` (`/stable/insider-trading/latest`), `search_insider_trades` (`/stable/insider-trading/search`), `insider_trade_statistics` (`/stable/insider-trading/statistics`), `senate_trading` (`/stable/senate-trades`), `senate_trading_by_name` (`/stable/senate-trades-by-name`), `house_trading` (`/stable/house-trades`), `house_trading_by_name` (`/stable/house-trades-by-name`).
- **E. Market attention:** `biggest_gainers` (`/stable/biggest-gainers`), `biggest_losers` (`/stable/biggest-losers`), `most_active` (`/stable/most-actives`), `sector_performance_snapshot` (`/stable/sector-performance-snapshot`), `industry_performance_snapshot` (`/stable/industry-performance-snapshot`).

(Exact paths are best-effort; the probe's job is precisely to confirm/deny each. A
`NOT_FOUND` result flags a path that needs correcting — not a plan lock.)

## 6. Capability probe

**`fmp_capability_probe.py` (pure):**
- `classify(http_status, body, error) -> status` returning one of:
  `AVAILABLE` (200 + non-empty list/dict with expected shape), `EMPTY_OK` (200 + empty list — endpoint exists, no data for the probe symbol), `PLAN_LOCKED` (402/403), `AUTH_ERROR` (401), `NOT_FOUND` (404), `RATE_LIMITED` (429), `SCHEMA_CHANGED` (200 but shape/fields unexpected), `NETWORK_ERROR` (transport failure / status ≤ 0).
- `probe_all(registry, http_get_status, *, max_calls=80, symbol="AAPL") -> list[record]`
  iterates candidates (skipping the confirmed baseline), one tiny call each
  (`limit=1`, `page=0`, `symbol=AAPL` only where `params_template` needs it), stops
  at `max_calls`, wraps each in try/except → one failure never aborts the run.
  `http_get_status(url) -> (status_code, body, message)` is injected (pure-testable).

**`scripts/probe_fmp_crowd_endpoints.py` (CLI):**
- Loads the existing key via `portfolio_automation.env.get_secret("FMP_API_KEY")`.
- Builds a urllib status-returning `http_get_status` (reuses the social connector's
  pattern). **Direct HTTP, NOT the cache/governor path** — a capability check needs
  the raw status code and must not be cached or counted against runtime cache logic.
  (Runtime Phase-2 adapters DO go through `get_json` + governor.)
- Runs `probe_all` (hard cap **80 calls**), writes `outputs/latest/fmp_endpoint_capabilities.json`
  + `outputs/latest/fmp_crowd_probe_summary.md`, upserts the SQLite table.
- `observe_only: true` hardcoded in the JSON artifact.

## 7. Storage

Dedicated `data/crowd_intelligence.db` (mirrors `fmp_budget.db` isolation). Phase 1
creates ONE table:
`fmp_endpoint_capabilities(endpoint_id PK, status, http_status, response_bytes,
sample_fields, last_checked_at, error_summary)`.
(`crowd_raw_events`, `crowd_signal_daily` are Phase 2.)

## 8. Canonical registry additions (compliance)

Every candidate path not already in `fmp_endpoint_registry.REGISTRY` is added with the
existing schema (`endpoint, per_symbol, starter_safe, priority, required_daily=False,
classification, usage`). They are **NOT** added to `STABLE_METHOD_MAP` (they are
probe/adapter targets, not implemented client methods), so the compliance test —
which validates only `STABLE_METHOD_MAP` entries + daily-required invariants — stays
green, exactly as the existing `social_sentiment` REGISTRY entry does. Legacy
`/api/v4` candidates → `classification: legacy_optional`, `starter_safe: false`.
`required_daily` is `false` for all additions, so no daily-required-must-be-starter-safe
invariant is touched.

## 9. Tests (Phase 1)

`tests/test_crowd_intelligence_probe.py`:
- Classifier truth-table: each status from synthetic `(http_status, body, error)` inputs (200+rows→AVAILABLE; 200+[]→EMPTY_OK; 403/402→PLAN_LOCKED; 401→AUTH_ERROR; 404→NOT_FOUND; 429→RATE_LIMITED; 200+wrong-shape→SCHEMA_CHANGED; -1→NETWORK_ERROR).
- `probe_all` respects `max_calls`, skips the confirmed baseline, and never raises when `http_get_status` throws for one endpoint.
- Registry integrity: every entry has all required schema keys; net-new paths exist in `fmp_endpoint_registry.REGISTRY`.
- Capability store round-trips JSON + SQLite.
- The existing `tests/test_fmp_endpoint_registry_compliance.py` still passes after the additions (no new violations).

## 10. Observe-only invariants

- Writes only the two `outputs/latest/` artifacts + the capabilities table; touches no
  decision/scoring/allocation/portfolio state. `observe_only: true` hardcoded.
- No trading execution, no advisory integration, no decision-engine changes.
- The probe is a manual `scripts/` diagnostic — NOT wired into the daily cron in Phase 1.

## 11. Definition of done (Phase 1)

`python scripts/probe_fmp_crowd_endpoints.py` produces `fmp_endpoint_capabilities.json`
+ `fmp_crowd_probe_summary.md`, populates the SQLite table, and yields a concrete
AVAILABLE / EMPTY_OK / PLAN_LOCKED / … map. Tests pass. Then run it live and summarize
the status map to scope Phase 2 adapters against real Starter access.
