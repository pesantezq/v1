# Crowd Intelligence (FMP) — probe-gated, observe-only

A probe-gated FMP crowd-context layer. It uses only endpoints the live capability
probe confirms on the current plan, and **never feeds `decision_plan.json`,
scoring, allocation, promotion, or trade execution** — it is observe-only context.

> **Phase status:** Phase 1 (capability registry + probe) is implemented. Phase 2
> (adapters, signal builder, portfolio-tab crowd context, advisory explanations) is
> scoped against the probe results below and built separately.

## FMP plan assumptions (Starter)

- 300 API calls/min, ~20 GB trailing-30-day bandwidth.
- Includes financial market news, crypto/forex, US coverage, 5y history, annual
  fundamentals/ratios, company/reference data.
- **Bulk/batch delivery is Ultimate-tier** — not depended on (probe-gated).
- **Direct social-sentiment endpoints are legacy/premium** — treated as optional.

## Capability probe

Run it manually (free; ~22 tiny calls, hard-capped at 80):

```bash
cd /opt/stockbot && set -a; . ./.env; set +a
.venv/bin/python scripts/probe_fmp_crowd_endpoints.py
```

Outputs:
- `outputs/latest/fmp_endpoint_capabilities.json` — full per-endpoint status (`observe_only:true`).
- `outputs/latest/fmp_crowd_probe_summary.md` — human-readable summary.
- `data/crowd_intelligence.db` → `fmp_endpoint_capabilities` table.

Statuses: `AVAILABLE`, `EMPTY_OK` (endpoint works, no data for probe symbol),
`PLAN_LOCKED` (402/403 or FMP 200 "Error Message"), `AUTH_ERROR` (401),
`NOT_FOUND` (404 — path likely wrong), `RATE_LIMITED` (429), `SCHEMA_CHANGED`
(200 but unexpected shape), `NETWORK_ERROR`, `SKIPPED_CAP` (call cap reached).

The probe uses **direct HTTP** (not the cache/governor path) because a capability
check needs the raw status code and must not be cached. Phase-2 runtime adapters
go through the governed `FMPClient.get_json` (budget + cache + ledger).

## Confirmed capability map (probe run 2026-06-15, Starter plan)

**AVAILABLE on Starter** (Phase 2 will build adapters for these):
- News: `fmp_articles`, `general_news`, `stock_news_latest`, `crypto_news`, `forex_news` (+ baseline `stock_news`)
- Analyst: `stock_grades`, `grades_consensus` (+ baseline `ratings_snapshot`, `historical_ratings`)
- Insider: `latest_insider_trading`, `search_insider_trades`, `insider_trade_statistics`
- Congress: `senate_trading`, `house_trading`, `house_trading_by_name` (`senate_trading_by_name` = EMPTY_OK — works, no data for the probe name)
- Market attention: `biggest_gainers`, `biggest_losers`, `most_active`, `sector_performance_snapshot`, `industry_performance_snapshot`

**PLAN_LOCKED (403) on Starter** — legacy `/api/v4` direct social/RSS sentiment:
- `historical_social_sentiment`, `social_sentiment_legacy`, `stock_news_sentiment_rss`

**Implication:** direct social sentiment is unavailable on Starter, but news,
analyst, insider, congressional, and market-attention context are all available —
so the Phase-2 crowd layer degrades gracefully and still produces signal without
any paid upgrade.

## Registries

- `portfolio_automation/crowd_intelligence/endpoint_registry.py` — rich candidate
  registry (probe/adapter source). `enabled_after_probe` reflects confirmed access.
- `fmp_endpoint_registry.py` — every crowd path is mirrored here for compliance
  coverage (NOT in `STABLE_METHOD_MAP`; `required_daily=False`), so the canonical
  compliance test governs all crowd paths without being bypassed.

## Rerunning after a plan change

If you change FMP tiers, re-run the probe; `enabled_after_probe` / the canonical
`starter_safe` flags should be updated to match the new capability map, and Phase-2
adapters automatically pick up newly-AVAILABLE endpoints.
