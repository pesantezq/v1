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

## Phase 2A — backend adapters + normalized artifacts (observe-only)

Five category adapters turn the AVAILABLE endpoints into a normalized per-symbol
crowd context. **Context only — never creates or changes a BUY/SELL/HOLD,
allocation, score, or trade.**

- **Call path:** every runtime FMP call goes through the governed
  `FMPClient.get_json(path, params, ttl_seconds=…)` via `governed_client("discovery")`
  (cache + budget + ledger). No raw HTTP in adapters.
- **Adapters** (`portfolio_automation/crowd_intelligence/adapters/`): `news`
  (velocity/attention; **directional score neutral** — no sentiment on Starter; risk
  keywords → warnings only), `analyst` (consensus distribution + recent grade
  direction), `insider` (net buy/sell pressure, winsorized), `congress` (disclosed
  activity, **dampened ×0.5, capped ±0.5, low weight**, no causal language),
  `attention` (gainer/loser membership + most-active + sector/industry context).
- **Composite** (`normalization.py`): news 0.25, analyst 0.25, insider 0.15,
  congress 0.10, attention 0.25, **social 0.00 (PLAN_LOCKED)**; clamped to [-1,1].
- **Confidence** = mean(source coverage, freshness, cross-category agreement,
  completeness), [0,1].
- **Artifacts:** `outputs/latest/crowd_intelligence.json` / `.md` /
  `crowd_intelligence_status.json` (all `observe_only:true`). Persisted to
  `data/crowd_intelligence.db` → `crowd_raw_events` + `crowd_signal_daily`.

**Universe** (`_load_universe`, capped 60): advisory picks (decision_plan) **+**
holdings (config) **+** daily watchlist single-names (`watchlist_signals.json`) —
all free Starter artifacts; ticker-shape filtered (synthetic decision entries like
`EMERGENCY_FUND_*` dropped). ETFs yield thin context; single names carry the signal.

**Trend** (`composite_trend` / `trend_label`): each daily run compares a symbol's
composite to its most-recent prior `crowd_signal_daily` row → `rising`/`falling`/`flat`
(±0.05) or `building` until ≥2 days of history exist. Surfaced on the GUI context card.

Run it (manual; also cron-wired as Stage 7d3):
```bash
cd /opt/stockbot && set -a; . ./.env; set +a
.venv/bin/python -m portfolio_automation.crowd_intelligence.artifact_writer   # holdings universe
```
Example (AAPL, live): analyst +0.46 (consensus +0.57 / 110 ratings), insider −1.0
(one recent sell), congress −0.05 (dampened), news velocity 1.4 (neutral direction),
social 0 (disabled) → composite −0.04, confidence 0.75. ETF holdings show near-zero
(no analyst/insider/congress data for ETFs) — data-honest.

**Guardrails:** writes only the 3 crowd artifacts + the 2 SQLite tables;
`decision_plan.json` and all decision artifacts are untouched (test-asserted);
API keys never appear in stored events/artifacts/errors.

### Phase 2C — Portfolio tab integration (view-model)

`gui_v2/data/portfolio_presenter.py` composes a display-only view-model from existing
artifact data: **summary cards** (value/cash/drift/diversification), **Advisory Picks
with Context** (each pick gets 3 reasoning rows — Portfolio / Crowd / Catalyst-Risk —
a signal-strength bar, conviction band, and the note "Crowd input is research context
only"), a right-side **Crowd Overlay** panel (active governed sources, coverage %,
Agree/Inconclusive/Disagree summary, conviction legend, "View Crowd Details" link), and
a **Why These Picks** reasoning strip. Crowd is always subordinate: it never changes a
pick's action and an `Agree`/`Disagree` is classified from the crowd label vs the
(unchanged) decision direction — disagreement is surfaced, never auto-suppresses a pick.
Pure functions; no decision/scoring/allocation change. Mobile: cards stack, overlay
drops below the advisory grid.

## Phase 2B — GUI context + advisory enrichment + daily wiring (observe-only)

Surfaces the Phase-2A artifacts on the **Portfolio tab** advisory picks as
**context only**. Artifact-only: no FMP / HTTP / governor calls (test-asserted by a
source grep). Never changes recommendation generation, scoring, allocations, risk
caps, BUY/SELL/HOLD, or execution.

- **`context_loader.py`** — reads `crowd_intelligence.json` + `_status.json`; returns
  per-symbol context + `{available, stale, generated_at, social_disabled}`. Missing →
  `not_generated`; unreadable → `unreadable`; age > 30h → `stale`.
- **`advisory_context_enricher.py`** (pure) — context label
  (**Supportive / Neutral / Caution / High Attention / Insufficient Data** — never
  Bullish/Bearish/Buy/Sell) + explanation lines, all run through a **forbidden-phrase
  guard** (`assert_safe`) so buy/sell/confirm/privileged language cannot be emitted.
- **GUI** — `gui_v2/data/dash_crowd_context.py` feeds each advisory pick's
  `decision_card` a context block (label badge, composite score, confidence, enabled/
  disabled sources, freshness, ≤3 reasons, warnings, enrichment lines). The Portfolio
  tab shows a status banner: unavailable / stale / "Direct FMP social sentiment is
  unavailable on the current Starter plan."
- **Empty/stale:** missing artifact → "Crowd context unavailable — artifact not
  generated yet."; stale → "…may be stale — last generated at {ts}."; symbol absent →
  "No crowd context available for this symbol."
- **Daily wiring:** `run_daily_safe.sh` Stage 7d3 (`run_aux_stage "Crowd intelligence"`)
  runs `artifact_writer.run('.')` post-pipeline. Non-blocking — `run()` swallows all
  errors and returns a status dict, so a failure WARNs and never blocks the portfolio
  run; it never mutates `decision_plan` or advisory selection.

## Rerunning after a plan change

If you change FMP tiers, re-run the probe; `enabled_after_probe` / the canonical
`starter_safe` flags should be updated to match the new capability map, and Phase-2
adapters automatically pick up newly-AVAILABLE endpoints.
