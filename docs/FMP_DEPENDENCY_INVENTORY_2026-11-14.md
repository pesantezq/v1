# FMP Dependency Inventory — 2026-11-14 (FMP-1, research intake)

**Status: NOT_READY.** This is a static, offline inventory of `main@2a6cb16` to
guide the 2026-11-27 FMP Starter → Basic downgrade. It does **not** show that
Basic/Free entitlement, rights, or data parity are in place. No live FMP call, probe, secret
read, or network access was made. This is separate from draft PR #77
(offline audit) and issue #78.

Reproduce (read-only, writes only to stdout):

```bash
python tools/fmp_dependency_inventory.py --repo-root .            # JSON, sorted
python -m pytest -q tests/test_fmp_dependency_inventory.py
```

All `file:line` anchors below were checked by text search on this checkout.
The key anchors are pinned in `test_real_repo_read_only_smoke`.

## 1. Quota: Basic/Free daily allowance vs documented consumption

| Item | Evidence | Note |
|---|---|---|
| Free/Basic plan allowance | **250 calls/day** (from FMP public plan pages; **not re-verified offline**). No repo file mentions it (grep for `250 calls` / `Basic plan` finds nothing). | UNVERIFIED |
| Vendor reset time | FMP publicly documents a **3 PM EST** daily reset (official FAQ, independently checked 2026-11-14); account-specific cutoff/quota telemetry remains unverified. | UNVERIFIED |
| Local counter reset | `fmp_client.py:117-132` `_CallCounter._load` keys on `date.today().isoformat()` — **local calendar midnight**, not 3 PM EST. | Local and vendor windows don't line up. A run spanning 3 PM can use two vendor days' worth of budget against one local count, or the reverse. |
| Governor rate limits | `config.json:209-211` `monthly_bandwidth_gb: 20`, `rate_per_min: 240`, `burst: 300` | Sized for Starter (per-minute, not per-day) |
| Run-mode budgets | `config.json:213-218` gui_refresh 30, **daily 0 (=UNCAPPED)**, weekly_review 800, monthly 1500, discovery 650, historical_replay 0 + cache_only | With a 250/day cap, `daily` as written has no cap at all. weekly/monthly/discovery are each above 250 on their own. |
| Legacy budget knob | `config.json:205` `fmp_daily_calls_budget: 0` | Unbounded |
| Monthly universe scan | `scripts/run_monthly_universe_refresh.sh:20-27`: ~503 stable/profile + ~503 key-metrics (+financial-growth) + ~503 stable/quote ≈ **3,700 calls**, "measured live 2026-08-03". Comment says it is safe only because of the flat subscription. | **~15× a 250/day allowance.** A single run can't finish on Basic/Free. |

## 2. Call-site / endpoint / workflow matrix

Risk categories are the tool's `risk` field.

| Risk | Client method → endpoint (fmp_client.py) | Production call sites (non-test) | Workflow | Fallback readiness |
|---|---|---|---|---|
| quote | `get_batch_quotes` → `stable/quote` per symbol (:934, :1009); `get_quote_short` → `quote-short` (:1071) | `market_data.py:40` (holdings prices; factory `market_data.py:33`), `watchlist_scanner/scanner.py:478`, `main.py:990,1063,1082,1150,1557`, `portfolio_automation/decision_outcome_tracker.py:181,216` | Daily decision pipeline, watchlist scan, outcome tracking | Stale-cache fallback when budget runs out (:993-1001). There's **no second price vendor** (market_data.py docstring: "FMP is the system's sole price provider"). |
| universe | `get_sp500_constituents` → **v3** `sp500_constituent` (:461-464); `get_bulk_profiles` → **v4** `profile/all` (:476); `get_batch_profiles_v3` → **v3** `profile/{batch}` (:669) with stable/profile fallback (:677-720) | `universe/fmp_universe.py:183,200,220`; `main.py:946,1034` | Universe build, monthly refresh | v3 constituent list has "no confirmed stable equivalent" (`fmp_endpoint_registry.py:263`). v4 bulk is `premium_optional` (:267-268). |
| fundamentals | `get_key_metrics`/`get_financial_growth`/`get_ratios`/`get_income_statement` → stable (:762, :811, :614, :862); `get_fundamentals_v3` stable-first + **v3** `key-metrics/{sym}` fallback (:906); `get_bulk_key_metrics` → **v4** (:490) | `main.py:947,983,1035,1056,1071,1073`; `scripts/diag_weekly_refresh_zero.py:66` | Weekly/monthly refresh, scanner metrics | Per-symbol stable calls: N symbols → ≥N calls |
| eod_history | `get_historical_prices` → `stable/historical-price-eod/full` (:1209); `get_full_bars_strict_live` (:1392) | `watchlist_scanner/scanner.py:497`, `performance_feedback.py:184`, `outcome_evaluator.py:97`, `portfolio_automation/{correlation_risk_advisor.py:375, vol_regime_advisor.py:248, tax_harvest_advisor.py:284, exit_advisor.py:401, historical_backfill.py:230, historical_replay/replay_data_loader.py:96, portfolio_sim/prices.py:100}`, `backtesting/fmp_backtester.py:146,350` | Advisors, backtests, replay, Strategy Lab | One call per symbol; `ttl_days=0` sites (`performance_feedback.py:184`, `outcome_evaluator.py:97`) skip the cache |
| adjusted_history_vs002 | `get_historical_prices_dividend_adjusted` (:1275) / `get_dividend_adjusted_bars_strict_live` (:1342) → `historical-price-eod/dividend-adjusted` (`_EP_HISTORICAL_DIVADJ`, :54) | `portfolio_automation/vs002_evidence/production_runner.py:169` (22 adjusted + 1 benchmark `/full` witness, :78, :132); `vs002_evidence/builder.py:216` | VS-002 evidence (Phase 0C) | **The adjustment semantics are fixed by the endpoint itself** (:46-53). Entitlement on the current plan is "UNVERIFIED (deferred B3)". If the endpoint is missing it **fails closed**: no fallback to `/full`, the archive, or another vendor. |
| news | `get_stock_news` → `stable/news/stock` (:1137) | `watchlist_scanner/scanner.py:547`, `portfolio_automation/news/run_news_intelligence.py:165` | Daily news intelligence | Registry says "Starter-confirmed" (`fmp_endpoint_registry.py:229`). **Basic has not been checked.** |
| unknown | `get_json(path)` generic (:1076-1093) | `portfolio_automation/crowd_intelligence/adapters/__init__.py:31` (path read from a registry row at runtime), `portfolio_automation/intraday_lab/providers.py:121` | Crowd intelligence, intraday lab | **The endpoint can't be resolved statically.** The tool lists these as `dynamic_endpoint` and doesn't claim them as covered. |

Direct `FMPClient()` constructions that bypass the governor factory:
`backtesting/fmp_backtester.py:10`, `backtesting/poc_simulation_harness.py:206,213`,
`backtesting/run_loop.py:194`, `portfolio_automation/historical_replay/replay_runner.py:42`,
`scripts/diag_weekly_refresh_zero.py:64`, `scripts/validate_pnl_advisors.py:86`.
The governed paths are `portfolio_automation/data_budget/factory.py:28,100` and `governor.py:174`.

## 3. Legacy v3/v4 surface

Base URL `fmp_client.py:35` (`/api`) next to `:36` (`/stable`). Constants at
`:67-72`: `v3/sp500_constituent`, `v3/profile`, `v3/key-metrics`,
`v3/financial-growth`, `v4/profile/all`, `v4/key-metrics-bulk`. The registry
classifies them at `fmp_endpoint_registry.py:263-268` (legacy_optional /
premium_optional) and marks v4 social endpoints PLAN_LOCKED on Starter (:254-255).
FMP publicly states Free/Basic accounts cannot access the discontinued Legacy APIs; do not assume a grandfathered paid-account exception survives the downgrade (see official FAQ).

## 4. Preflight / shell dependencies

- `scripts/preflight.sh:133-134`: hard-fails if `FMP_API_KEY` is missing. This checks the key is present, not which plan it belongs to.
- `scripts/preflight.sh:164-171`: requires `python -m fmp_endpoint_compliance` to output `RESULT: COMPLIANT`. Compliance is checked against the registry, which says "Starter-plan-safe" (`fmp_endpoint_registry.py:2`). It doesn't consider Basic.
- `scripts/preflight.sh:178`: FMP pytest subset. `:115-116, :187-191, :255`: compile/import checks.

## 5. Unresolved (blocking NOT_READY → READY)

1. **Basic entitlement** for each stable endpoint above (quote, profile, key-metrics, financial-growth, ratios, income-statement, news/stock, historical-price-eod/full, **dividend-adjusted**). None has been checked against Basic.
2. **Rights/licensing**: whether Basic terms allow the current use (stored caches, derived artifacts, GUI display). Not reviewed.
3. **Data parity**: Starter vs Basic history depth, adjustment, delay and coverage. Not measured. VS-002 relies on the dividend-adjusted endpoint, so if it's missing, VS-002 evidence collection **stops**; it does not degrade.
4. **Quota fit**: the 3,700-call monthly scan, the uncapped `daily` mode, and 800/1500/650 run-mode budgets are all above 250/day. No re-budgeting has been designed.
5. **Reset-window mismatch**: local-midnight counter vs vendor ~3 PM EST reset.
6. **Dynamic paths**: the `get_json` call sites, whose endpoints come from registries at runtime.
7. **Static-analysis limits**: matching is by method name, so receivers aren't type-resolved (e.g. `vs002_evidence/builder.py:216` calls a wrapper). The tool sets `match="method_name"`.


## 6. Official source verification and secret-safe output

FMP published plan details checked independently on 2026-11-14:
- Basic: 250 requests/day, end-of-day data only, no real-time data: https://site.financialmodelingprep.com/pricing-plans
- Basic reset: 3 PM EST; Free users lose Legacy v3/v4 access: https://site.financialmodelingprep.com/faqs
- Paid-to-Free stored data rights are not automatically retained: https://site.financialmodelingprep.com/insights/platform/can-you-downgrade-or-return-to-the-free-fmp-plan-later

The source-inventory scanner never emits raw URL query strings, shell
expressions, dynamic dispatch expressions, or receiver/argument literals that
might contain API keys. Its synthetic security regression tests include planted
credentials in Python and shell source. A static inventory is NOT a substitute
for repository secret scanning, live plan-entitlement verification or independent
source-rights review. No provider fetch, new subscription or runtime change
occurred as part of FMP-1.
