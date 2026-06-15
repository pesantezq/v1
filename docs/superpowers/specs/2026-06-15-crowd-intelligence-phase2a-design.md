# Crowd Intelligence — Phase 2A: Backend Adapters + Normalized Artifacts (Design Spec)

- **Date:** 2026-06-15
- **Status:** Approved; implementing
- **Scope:** Backend only. 5 category adapters → normalized per-symbol crowd context
  artifacts. **No GUI, no advisory/decision/allocation/trading changes.** Observe-only.
- **Builds on:** Phase 1 capability probe (`fmp_endpoint_capabilities.json`).

## 1. Prerequisite — governed `FMPClient.get_json` (additive)

Adapters must use the governed client (AST guard forbids direct construction; spec
requires no raw HTTP). The proxy works by method name, and no generic fetch exists,
so add ONE additive method:

```python
FMPClient.get_json(path, params=None, *, ttl_seconds=3600, base_url=None) -> Any
```
- Cache-first (`_DiskCache`, key from path+sorted params), budget-guard
  (`would_exceed` → serve stale-or-None), `_raw_get` (sets `last_response_bytes`,
  so the governor ledger counts it), `cache.set`. `base_url` defaults to the FMP
  domain; `endpoint = path.lstrip("/")` (works for `/stable/...` and `/api/v4/...`).
- Adapters call `governed_client("daily").get_json(path, params, ttl_seconds=ttl)`.

## 2. Files

```
portfolio_automation/crowd_intelligence/
  schemas.py            # NormalizedEvent, CategoryResult, CrowdSignal (dataclasses)
  normalization.py      # clamp[-1,1], winsorize, confidence, composite weights
  adapters/__init__.py
  adapters/news_adapter.py
  adapters/analyst_adapter.py
  adapters/insider_adapter.py
  adapters/congress_adapter.py
  adapters/attention_adapter.py
  crowd_signal_builder.py
  artifact_writer.py
  capability_store.py   # EXTEND: + crowd_raw_events, crowd_signal_daily tables
fmp_client.py           # EXTEND: + get_json
tests/test_crowd_intelligence_phase2a.py
```

## 3. Storage (extend `data/crowd_intelligence.db`)

`crowd_raw_events(id PK AUTOINCREMENT, provider, endpoint_id, symbol, category,
event_time, normalized_event_type, raw_json, fetched_at)`.
`crowd_signal_daily(id PK, symbol, signal_date, news_score, analyst_score,
insider_score, congress_score, attention_score, social_sentiment_score,
composite_crowd_score, confidence, enabled_sources_json, disabled_sources_json,
explanation_json, created_at)` — unique on `(symbol, signal_date)` (upsert).

## 4. Capability gating

The signal builder loads `fmp_endpoint_capabilities.json` (Phase 1). An endpoint is
usable iff its status ∈ {`AVAILABLE`, `EMPTY_OK`} **and** (for EMPTY_OK) the response
parses to the expected shape. `PLAN_LOCKED`/`NOT_FOUND`/`AUTH_ERROR` → skipped, added
to `disabled_sources`. If the capabilities file is absent, fall back to the registry's
`min_plan_assumption=="starter" and not legacy` as the optimistic default, and warn.

## 5. Category adapters

Each adapter: `fetch(symbols, *, client, capabilities, registry) -> CategoryResult`
with `{score (float in [-1,1]), confidence_inputs, reasons[], warnings[], records[],
enabled_endpoints[], disabled_endpoints[]}`. Returns a valid **neutral/empty** result
(score 0.0, empty records) when no endpoint is usable. All FMP via `get_json`.

- **News** (`stock_news`, `stock_news_latest`, `fmp_articles`, `general_news`,
  `crypto_news`, `forex_news`): per-ticker news count, 24h/7d velocity (recent vs
  trailing baseline), distinct source count, headline relevance (ticker/company match).
  **Directional score = neutral 0.0** (no sentiment field on Starter — RSS-sentiment is
  PLAN_LOCKED); velocity/relevance feed confidence + reasons, NOT direction. Risk-event
  keyword flags (bankruptcy/SEC/halt/recall/lawsuit) → `warnings` only, never buy/sell.
- **Analyst** (`ratings_snapshot`, `historical_ratings`, `stock_grades`,
  `grades_consensus`): score from consensus distribution
  `clamp((strongBuy+buy − sell−strongSell)/max(1,total))`; upgrade/downgrade direction
  from recent grade `action`; consensus pressure; explanation string.
- **Insider** (`latest_insider_trading`, `search_insider_trades`,
  `insider_trade_statistics`): buy vs sell pressure from acquired/disposed;
  `net = clamp((buys−sells)/max(1,buys+sells))`; 30d/90d windows when dates present;
  **winsorize** per-filing notional (cap at p90) so one filing can't dominate.
- **Congress** (`senate_trading`, `senate_trading_by_name`, `house_trading`,
  `house_trading_by_name`): activity score from disclosed purchase/sale direction,
  **dampened ×0.5 and clamped to ±0.5** (low-weight, context-only). Explanations avoid
  causality/privilege language ("disclosed congressional trades", not "insiders know").
- **Attention** (`biggest_gainers`, `biggest_losers`, `most_active`,
  `sector_performance_snapshot`, `industry_performance_snapshot`): directional from the
  symbol appearing in gainers (+) / losers (−) and its change%; non-directional
  most-active presence raises confidence; sector/industry change adds mild context.
  Explicitly labeled "attention, not a recommendation".

## 6. Normalization + composite + confidence

- `clamp(x) = max(-1.0, min(1.0, x))`; `winsorize(values, p=0.90)`.
- **Composite weights:** news 0.25, analyst 0.25, insider 0.15, congress 0.10,
  attention 0.25, **social 0.00 (PLAN_LOCKED)**. `composite = Σ wᵢ·scoreᵢ`, clamped.
  Categories with no data contribute score 0.0 (neutral).
- **Confidence** ∈ [0,1] = mean of: (a) enabled-source coverage = usable categories /
  5; (b) freshness = fraction of records newer than their TTL window; (c) agreement =
  1 − stdev(non-zero category scores) normalized; (d) completeness = categories with
  ≥1 record / 5. Low coverage → low confidence.

## 7. Artifacts (observe_only:true)

- `outputs/latest/crowd_intelligence.json` — `{generated_at, observe_only, weights,
  symbols:[{symbol, composite_crowd_score, confidence, category_scores{news,analyst,
  insider,congress,attention,social_sentiment}, enabled_sources, disabled_sources,
  top_reasons, warnings, data_freshness, source_records_count}]}`.
- `outputs/latest/crowd_intelligence.md` — per-symbol readable summary.
- `outputs/latest/crowd_intelligence_status.json` — `{overall_status, symbols_count,
  enabled_categories, disabled_categories (incl social PLAN_LOCKED), fmp_calls_estimate,
  warnings}`.

Universe for the live run = portfolio holdings (from config). Non-per-symbol endpoints
(news-latest, gainers/losers/most-active, sector/industry) are fetched once and shared.

## 8. Guardrails (hard invariants)

Context-only. **MUST NOT** create BUY/SELL/HOLD, change decision-engine recommendations,
alter allocations, bypass risk guardrails, or affect trade execution. Writes ONLY the 3
crowd artifacts + the 2 SQLite tables. `decision_plan.json` and all existing decision
artifacts are untouched. API keys redacted from logs/artifacts/errors (raw_json stored
is the response body, which carries no key; URLs/errors never include the key). No cron
wiring in 2A (manual entrypoint; Phase 2B/GUI later).

## 9. Tests

Adapter: each skips PLAN_LOCKED endpoints; each returns valid neutral/empty output with
no data; FMP reached only via a (mock) `get_json` client (no raw HTTP). Normalization:
clamps to [-1,1]; winsorize caps outliers. Composite: social score stays 0/neutral;
congress is low-weight (|contribution| ≤ 0.05). Builder: composite is a separate artifact
and writing it does not read or mutate `decision_plan.json` (assert file unchanged).
Persistence: raw events + daily signals round-trip in SQLite. Artifacts: expected schema
keys present. Regression: `test_fmp_endpoint_registry_compliance` + governor
`test_data_budget_no_direct_construction` still pass (adapters use governed client).

## 10. Definition of done

`crowd_intelligence.json` / `.md` / `_status.json` produced for the holdings universe;
adapters use only AVAILABLE/EMPTY_OK endpoints; social gracefully disabled; tests pass;
no GUI/advisory/decision/allocation/trading change.
