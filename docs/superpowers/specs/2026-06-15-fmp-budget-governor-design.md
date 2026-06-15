# Budget-Aware FMP Data Orchestrator — Design Spec

- **Date:** 2026-06-15
- **Status:** Approved (design); pending implementation plan
- **Author:** Claude Code (brainstormed with operator)
- **Roadmap:** Operator-authorized active step; supersedes the standing
  `observe_and_iterate` hold for this work (mirrors prior governance-layer steps
  that were operator-approved). `next_official_step` otherwise unchanged.

## 1. Goal

A single guarded FMP access layer that maximizes useful data while minimizing
API calls and bandwidth. **All** existing FMP calls route through this layer.
No trading behavior change, no buy/sell execution, observe-only preserved.

## 2. Non-Goals

- No replacement of the existing file-based `_DiskCache` (it already does
  cache-first + stale-while-revalidate).
- No change to `decision_engine.py`, scoring, or any protected score semantics.
- No new third-party dependencies (stdlib `sqlite3` + `urllib` only).
- `data_request_queue` table is **deferred** (YAGNI) — added only if a concrete
  deferred-fetch need appears.

## 3. Existing infrastructure reused (do NOT rebuild)

| Capability | Where it already lives |
|---|---|
| Cache-first + stale-while-revalidate | `fmp_client._DiskCache.get / get_stale / set` |
| Daily call counter + budget guard + stale fallback on exceed | `fmp_client._CallCounter`, `_raw_get`, `would_exceed` (`budget <= 0` = uncapped) |
| Batch/bulk endpoints | `get_batch_quotes`, `get_bulk_profiles`, `get_batch_profiles`, `get_bulk_key_metrics`, `get_historical_prices`, `get_sp500_constituents` |
| Endpoint registry/compliance | `fmp_endpoint_registry.py`, `fmp_endpoint_compliance.py` (must NOT be bypassed) |
| Budget telemetry artifact | `fmp_budget_telemetry.py` → `fmp_budget_status.json` (kept; daily-check depends on it) |
| Output namespace governance | `portfolio_automation.data_governance.OutputNamespace` |
| Per-request rate spacing (~500ms) | `fmp_client._rate_limit` |

## 4. Architecture (wrap + extend)

The governor is the single factory every call site uses. The kill-switch falls
back to today's exact behavior.

```
governor.client(run_mode) ──► enabled?  ─yes─► GovernedFMPClient(wraps FMPClient)
                                          └no─► FMPClient(...)   # current proven path
```

`GovernedFMPClient` proxies the existing FMPClient methods **by the same names
and signatures**, so output artifact contracts are unchanged. Before each real
(cache-missing) call it: (a) consults the token bucket, (b) checks the run-mode
soft budget, (c) checks the monthly bandwidth guard, then (d) records a ledger
row. Cache hits and stale-serves are recorded but consume no token/budget.

## 5. Package layout

```
portfolio_automation/data_budget/
  __init__.py          # exports FMPBudgetGovernor, RunMode constants
  governor.py          # FMPBudgetGovernor (factory, enable/kill-switch) + GovernedFMPClient
  scheduler.py         # run-mode budget table; skip/defer/stale decisions; priority tiers
  request_manifest.py  # endpoint strategy (batch/quote-short/EOD-bulk/profile-bulk selection)
  cache.py             # adapter over existing _DiskCache for hit-rate/stale reporting + symbol_data_policy
  usage_ledger.py      # SQLite api_usage_ledger writer/reader, aggregation, monthly bandwidth, retention
```

Each unit is independently testable: governor (orchestration), scheduler (pure
budget/priority logic), request_manifest (pure endpoint selection), cache
(adapter + policy), usage_ledger (persistence + aggregation).

## 6. Token bucket + guards

- **Token bucket** (in-process, per run): **240/min sustained** refill (4 tok/s),
  **300 burst** capacity. Empty bucket → high-priority **waits** (capped sleep,
  e.g. ≤2s); low-priority (discovery) **skips** → serves cache/stale.
  Rationale: a single pipeline run is the burst unit; cross-run guards below
  persist.
- **Daily guard:** existing `_CallCounter` (`call_counter.json`).
- **Monthly bandwidth guard:** `api_usage_ledger` byte-sum for the calendar
  month vs **20 GB** (config). At threshold → **low-priority run modes
  (discovery, live historical_replay) disabled**; portfolio/decision data is
  never blocked (observe-only safety). Soft-warn at 80% / 90%.
- **Real bytes:** additive `self._last_response_bytes = len(raw)` captured in
  `fmp_client._raw_get` before `json.loads` (backward-compatible; no behavior
  change). The governor reads it after each call into the ledger.

## 7. Run modes (config-driven, with rationale)

| Run mode | Priority | Default soft call budget | Notes |
|---|---|---|---|
| `gui_refresh` | high | small (~30) | cache-first; prefer `quote-short` |
| `daily` | high | large (effective soft cap; honors uncapped `0`) | main pipeline |
| `weekly_review` | medium | larger window | |
| `monthly` | medium | larger window | |
| `discovery` | low | bounded | **first skipped** under pressure |
| `historical_replay` | low | 0 live by default | **cache-only**; live only if cache missing |

Defaults live in `config.json` under a `data_budget` block, each with a
rationale comment per CLAUDE.md's tunable-param rule.

## 8. Storage — `data/fmp_budget.db` (portfolio.db untouched)

- `api_usage_ledger` — per call: `ts, run_mode, endpoint, symbols, cache_hit,
  bytes, skipped_reason`.
- `symbol_data_policy` — `symbol → ttl_seconds, priority_tier`.
- `data_request_queue` — **deferred** (not built initially).

Isolated DB keeps the high-churn append-only ledger out of portfolio.db's
decision/holdings state and its backup/migration machinery. Independent
retention/pruning in `usage_ledger.py`.

## 9. Endpoint strategy (`request_manifest.py`)

- Portfolio/watchlist quotes → **batch-quote** (`get_batch_quotes`).
- Single-symbol lightweight GUI → **quote-short** (thin `get_quote_short`; add
  registry entry if absent).
- Daily price updates → **EOD bulk / light EOD** (`/stable/historical-price-eod/full`,
  already registered).
- Profile metadata → **profile bulk** (`get_bulk_profiles` / `get_batch_profiles`).
- Per-symbol full-history → **only when cache missing**.

All selections validated against `fmp_endpoint_registry`; registry entries for
`quote-short` / EOD-bulk added if missing (registry honored, never bypassed).

## 10. Artifacts (observe_only:true, `OutputNamespace.LATEST`)

- `fmp_usage_status.json` — calls this run by run_mode, token-bucket state,
  daily count vs budget, per-endpoint tally.
- `fmp_cache_status.json` — cache hit rate (run + rolling), file count/size,
  stale-served count, fresh/stale per portfolio symbol.
- `data_budget_status.json` — monthly bandwidth vs 20 GB, per-run-mode
  utilization, discovery/backtest-skipped-due-to-budget flags,
  enabled/kill-switch state, `overall_status` (ok/near_cap/constrained).

Existing `fmp_budget_status.json` is kept unchanged (additive trio).

## 11. Migration (single chokepoint + guard test)

Replace the ~8 direct `FMPClient(...)` constructions with
`governor.client(run_mode=…)`:

| Call site | run_mode |
|---|---|
| `main.py` (×3 pipeline) | `daily` |
| `watchlist_scanner/performance_feedback.py` | `daily` |
| `portfolio_automation/decision_outcome_tracker.py` | `daily` |
| `portfolio_automation/news/run_news_intelligence.py` | `daily` |
| `market_data.py` (holdings prices) | caller-supplied (default `daily`) |
| `portfolio_automation/historical_backfill.py` | `historical_replay` |
| discovery pulse paths | `discovery` |
| `gui_v2` data loaders | `gui_refresh` |

**Guard test:** AST/grep test asserting no module outside `data_budget/` (+ a
small sanctioned list: backtests, diag scripts) constructs `FMPClient` directly.
Mirrors the existing Schwab no-trade AST-enforcement test.

## 12. GUI

New **System-tab panel** (gui_v2) reading the trio: calls used this run, cache
hit rate, bandwidth estimate, portfolio data stale/fresh, discovery/backtest
skipped-due-to-budget. Observe-only; never feeds `decision_plan`. Loader-only
change (no route/contract break).

## 13. Error handling (non-blocking)

- Governor init failure or any governor-level error → fall back to plain
  `FMPClient` (= kill-switch). Pipeline never sees a new exception class.
- Ledger writes wrapped in try/except (telemetry must never break a run).
- Token-bucket waits capped; on timeout → skip + serve stale.
- FMPClient's existing budget-exceed → stale fallback preserved verbatim.

## 14. Kill-switch / enablement

Ships **enabled** (operator preference: prod-ready features ship enabled).
Disabled via any of: `config.json data_budget.enabled=false`,
`STOCKBOT_FMP_GOVERNOR_DISABLED=1`, or `config/fmp_governor.DISABLED` file.
Disabled = instant revert to current proven behavior.

## 15. Testing

Spec-required:
1. token bucket respects call limits (240 sustained / 300 hard)
2. cache hit avoids API call (no token/budget consumed on hit)
3. stale data refreshes only when TTL expired
4. batch quote preferred over per-symbol
5. monthly bandwidth guard disables low-priority discovery (≥20 GB)
6. historical replay uses local cache by default
7. existing decision artifacts unchanged except metadata

Additional:
8. governed factory returns plain FMPClient when kill-switch active
9. no-direct-construction guard test
10. ledger writes one row/call; artifacts aggregate correctly
11. health-check degraded-fixture test (Analysis+Health requirement)

## 16. Validation + docs

- Full `pytest` suite (mind the signal_registry test-isolation gotcha:
  restore `default_weight 0.4947` + drop fresh snapshots before committing).
- **Analysis + Health Coverage (mandatory):** extend `/daily-tool-analysis`
  (daily cadence) to read the trio; dispatch `portfolio-discovery-health`
  (developer lens — it already owns FMP headroom/budget) on bandwidth-near-cap
  or discovery-skipped; add content_liveness for looks-fresh-but-empty.
- Docs: `docs/OUTPUT_ARTIFACT_CONTRACTS.md` (+3 artifacts),
  `docs/PIPELINE_RUNBOOK.md` (governor + run-modes + kill-switch),
  `.agent/project_state.yaml` / roadmap entry.

## 17. Definition of done

The app runs daily, weekly, GUI-refresh, discovery, and backtest modes without
wasting FMP calls: prefers cached + batch/bulk data, logs every call to the
ledger, and exposes API budget health in the dashboard. Kill-switch reverts to
current behavior instantly. All tests pass; docs + roadmap updated.
