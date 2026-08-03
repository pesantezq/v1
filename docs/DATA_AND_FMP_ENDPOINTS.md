# Data And FMP Endpoints

Last verified against `fmp_client.py`, `watchlist_scanner/scanner.py`, `watchlist_scanner/fundamentals_engine.py`, `universe/fmp_universe.py`, and endpoint tests on 2026-04-28.

## Data Source Split

- Watchlist scan
  Mixed-provider flow. The scanner still attempts Alpha Vantage OHLCV/overview paths, but when FMP is enabled it also prefetches stable quotes, profiles, historical prices, and ratios. For watchlist news, FMP is tried first and Alpha Vantage is the fallback.
- Broader-market scanner and universe
  FMP is primary.
- Theme engine
  RSS feeds plus LLM classification. No FMP dependency.

## Stable FMP Endpoints In Active Use

| Method | Endpoint | Status | Used by | Required fields |
| --- | --- | --- | --- | --- |
| `get_batch_quotes()` | `/stable/quote` | stable | watchlist fallback, market coverage, candidate refresh | `symbol`, `price`; commonly also `changesPercentage`, `volume`, `avgVolume`, `priceAvg50`, `priceAvg200`, `marketCap`, `pe`, `eps`, `yearHigh`, `yearLow` |
| `get_profile()` / `get_batch_profiles()` | `/stable/profile` | stable | watchlist fallback fundamentals | `symbol`; commonly `companyName`, `sector`, `industry`, `mktCap`, `beta`, `description` |
| `get_ratios()` | `/stable/ratios` | stable | watchlist fallback enrichment | `symbol`; commonly `netProfitMargin`, `revenueGrowth`, `epsGrowth` or `earningsGrowth`, `debtEquityRatio` or `debtToEquity`, `dividendYield`, `priceEarningsRatio` |
| `get_historical_prices()` | `/stable/historical-price-eod/full` | stable | backtesting, watchlist fallback historical prices | per-row `date`, `close`, usually `open`, `high`, `low`, `volume` |
| `get_stock_news()` | `/stable/news/stock` | stable | watchlist fallback news | article `symbol` or ticker coverage, `title`, `text` or summary-like content, timestamp |
| `get_income_statement()` | `/stable/income-statement` | stable | fundamentals bundle | `revenue`, `grossProfit`, `netIncome`, `operatingIncome`, `eps`, `ebitda` |
| `get_key_metrics()` | `/stable/key-metrics` | stable | fundamentals bundle | commonly `returnOnEquity`, `priceEarningsRatio`, other quality/valuation fields |

## Legacy Or Premium FMP Endpoints Still Used

These are intentionally still present for broader-market and universe workflows.

| Method | Endpoint | Status | Notes |
| --- | --- | --- | --- |
| `get_sp500_constituents()` | `/api/v3/sp500_constituent` | **RETIRED — 403 on this key** | FMP restricted the v3 legacy API to subscriptions predating 2025-08-31 (verified live 2026-08-03). `/stable/sp500-constituent` returns **402 Restricted** on the current plan. Callers must go through `universe/sp500.py::SP500Universe`, which falls back to a free public source and then a last-good cache. |
| `get_batch_profiles_v3()` | `/api/v3/profile/{sym1,sym2,...}` | **RETIRED — 403 on this key** | Same retirement. The method now catches the failure and falls back to `stable/profile` per symbol via `_stable_profiles_as_v3()`, which renames `marketCap` → `mktCap` because every downstream consumer reads the v3 field name. |
| `get_fundamentals_v3()` | `/api/v3/key-metrics/{symbol}` and `/api/v3/financial-growth/{symbol}` | legacy but active | Used when stable fundamentals are unavailable for broader-market scan flows. |
| `get_bulk_profiles()` | `/api/v4/profile/all` | premium | Used by premium universe/scanner flows. |
| `get_bulk_key_metrics()` | `/api/v4/key-metrics-bulk` | premium | Used by premium broader-market scan flows. |

## Scanner Data-Quality Model (added 2026-08-03)

### Constituent authority chain

`FMP → free public source → last-good cache → hard failure`

| Stage | "Available" means | Plausibility | Freshness | Degraded behaviour |
| --- | --- | --- | --- | --- |
| **FMP** (`get_sp500_constituents`) | the call returns a list clearing the plausibility floor | ≥ `MIN_PLAUSIBLE_CONSTITUENTS` (400) distinct symbols | live read ⇒ `fresh` by definition | 403 on this key today, so this stage always falls through |
| **Free public source** (`fetch_from_wikipedia`, stdlib `html.parser` + `requests`; **no new dependency**) | HTTP 200 **and** the `id="constituents"` table parses to ≥400 rows | same floor — a layout change that yields 3 rows is REJECTED, not published | live read ⇒ `fresh` | falls through to cache |
| **Last-good cache** (`data/universe/sp500_constituents.json`) | file parses, carries a `constituents` list, clears the floor, **and** clears the freshness gate | same floor | `fresh` ≤7d · `stale` ≤30d · `expired` >30d · `unknown` if the timestamp is missing/unparseable/in the future | `fresh`/`stale` → served with `degraded=true`; `expired`/`unknown` → **refused** |
| **Hard failure** | nothing plausible AND current | — | — | raises `ConstituentSourceError`. Caught by `main.py`'s scanner handler → static fallback watchlist → those names have no fundamentals, so screening coverage reads ~0 and the **speculative sleeve is suppressed** |

Freshness is judged from the **recorded `fetched_at`**, never from file mtime — a
touched, rsynced, or restored file would otherwise look new while its contents are
months old. **Existence is not evidence of currency.** All six cache outcomes
(absent / unreadable / implausible / unknown-age / stale / expired) stay
distinguishable in the raised error.

`CACHE_FRESH_MAX_DAYS = 7` deliberately equals `degraded_mode.DEFAULT_STALE_DAYS`.
`CACHE_USABLE_MAX_DAYS = 30` is a **policy default, not a validated constant** —
S&P 500 membership turns over a few names per quarter, and both live sources
failing for a month is itself the incident.

### Three independent scanner-quality dimensions

1. **constituent validity/freshness** — `scanner.constituent_resolution`
2. **screening coverage** — `scanner.screening_sufficiency`
3. **resulting candidate count** — `scanner.universe_sufficiency`

plus `scanner.ranking_quality` as pure observability (it gates nothing).

> **A healthy upstream API does not imply a trustworthy scanner dataset.**
> `fmp_succeeded: true` with `fallback_used: false` and 3 candidates was the live
> state for roughly two months while every health surface read GREEN.

> **A large candidate count does not imply a fully screened universe.**
> Under `v3_max_symbols: 100`, `full_scan` emitted 100 candidates of which only
> ~24 had fundamentals; the rest were admitted unscreened and formed an
> alphabetical tie tail.

**Screening coverage is measured by PRIMARY-FIELD PRESENCE, never by row count.**
`get_fundamentals_v3` appends a row for every requested symbol unconditionally —
even a bare `{'symbol': 'X'}` — so `len(bulk_metrics)` is 100% by construction.
Coverage = (eligible symbols whose `revenueGrowth` resolved to a usable number) /
(eligible symbols after the market-cap stage). Thresholds: `healthy ≥ 0.90`,
`degraded ≥ 0.50`, `unsafe < 0.50` — **policy defaults**, adjustable in
`degraded_mode.py`, not empirically optimal.

Coverage is a property of the **build**, so it is persisted with the watchlist
(`screening_sufficiency` in `top100_watchlist.json`) and read back on refresh-only
runs, which fetch no fundamentals and therefore cannot re-measure it. An
uncertified watchlist yields `screening_not_certified` and suppresses the sleeve
until the next rebuild — uncertified is not the same as coverage 0, and neither is
treated as healthy.

### Factor / filter liveness (added 2026-08-03)

Field coverage answers "did the input arrive?"; it cannot answer "can this
component change anything?". A fourth dimension measures that, from the exact
metrics/quote inputs handed to `CandidateScanner` (not from candidate rows, whose
missing values `_build_row` has already coerced to 0).

A scoring factor is **LIVE** only when its input is present, its transformation
executes, **and** its contribution VARIES across the candidate set. A constant
contribution is `low_information`, not live. A hard filter is LIVE only when its
input is present and the condition is actually evaluable.

States: `live` · `low_information` · `degraded` · `inert` · `not_applicable` ·
`unknown`.

Measured live 2026-08-03 over 503 eligible symbols:

| Component | Max pts | Input coverage | Non-zero | Variance | Status |
| --- | --- | --- | --- | --- | --- |
| revenue_growth | 30 | 0.996 | 91 | 42.9 | **live** |
| fcf_yield | 25 | 0.996 | 457 | 71.2 | **live** |
| roe | 20 | 0.996 | 443 | 47.8 | **live** |
| **pe** | **15** | **0.000** | **0** | **0.0** | **INERT** |
| trend | 10 | 1.000 | 344 | 21.6 | **live** |

| Hard filter | Evaluable | Rejections | Status |
| --- | --- | --- | --- |
| rev_growth_min | 501 / 503 | 410 | live |
| **pe_bubble_guard** | **0 / 503** | **0** | **INERT** |
| fcf_negative_guard | 501 / 503 | 44 | live |
| trend_200dma | 503 / 503 | 159 | live |

Factor liveness **never suppresses the speculative sleeve** (`suppresses_sleeve`
is hardcoded false). PE has been inert for the whole life of the scanner while the
sleeve was permitted; making it a suppression would retroactively change
production authority semantics. It is a DEGRADED observability finding.

`score_breakdown()` in `scanner/candidate_scanner.py` is a **read-only mirror** of
`_score`, not a refactor of it — production scoring is untouched, and
`test_score_breakdown_reconciles_exactly_to_production_score` pins
`min(100, sum(breakdown)) == _score(...)` so the two cannot drift.

### PE source authority (research only)

`stable/ratios` carries a **direct** PE as **`priceToEarningsRatio`** — and
`get_ratios` is already in `STABLE_METHOD_MAP`, so **no new endpoint is needed**.
Production misses it because `get_fundamentals_v3` reads `stable/key-metrics` and
looks for `peRatio`/`priceEarningsRatio`, neither of which key-metrics returns.
(Note `fmp_client`'s `get_ratios` docstring says `priceEarningsRatio`; the live
payload spells it `priceToEarningsRatio`.)

`earningsYield` is a **decimal** (AAPL 0.029). `1/earningsYield` reconciles tightly
for profitable names — AAPL 0.04%, NVDA 0.02%, XOM 0.00%, KO 0.13% — but **not
universally**: **BA diverged 15.07%** (87.20 direct vs 74.05 derived), INTC 7.12%.
So derived is a labelled fallback, never an equivalent. Period is **annual**, to
match the basis `get_fundamentals_v3` already uses; TTM diverges materially
(NVDA 37.8 vs 31.5, PLTR 257.6 vs 130.9).

**Negative earnings get their own state, never a number.** A negative PE (INTC
≈ −615) *passes* a `pe > 50` bubble guard while meaning loss-making — strictly
worse than expensive. `pe_resolver` returns `negative_earnings` with
`pe_ratio: None`, and the challenger injects nothing for it, so a missing PE
cannot become a fake `0` that `_score`'s `or 100` default would silently band.

Live research coverage over 503 eligible: **471 direct · 0 derived · 30
negative_earnings · 2 unavailable → 93.6% usable.**

#### Known inert guard (measured 2026-08-03, deliberately NOT fixed here)

`field_resolution` reports per-field resolution counts, which surfaced that
`peRatio` resolves for **0 of 503** eligible symbols: `stable/key-metrics` returns
`earningsYield` (the reciprocal) and no `peRatio`/`priceEarningsRatio`, while the
v3 fallback inside `get_fundamentals_v3` only runs when key-metrics returns
*nothing at all* — and it returns plenty. So the **PE > 50 bubble guard in
`_passes_hard_filters` has never bound on any symbol.** Repairing it would change
which candidates pass (i.e. scanner behaviour), which is out of scope for a
measurement task; it is recorded via `inert_fields` so it cannot be forgotten.

### FMP-call budget for a full scan

Measured live 2026-08-03: **≈3,700 calls** per full scan — ~503 `stable/profile`
+ ~503 `stable/key-metrics` (plus `stable/financial-growth` wherever key-metrics
lacks `revenueGrowth`) + ~503 `stable/quote`. Profiles cache 7 days, key-metrics
30 days. The `daily` run-mode budget is **uncapped** (`call_budget: 0` means
uncapped — not zero), and FMP is a flat subscription, so a monthly cadence is
safe. **A daily forced full scan is not** — do not shorten the cadence.

### Recovery model

* **Weekly self-heal.** `weekly_refresh()` is monotone (it only re-filters cached
  rows), so the repopulate trigger is a floor, not zero: when the cached watchlist
  drops below `MIN_TRUSTED_DATASET_SIZE` (5) the weekly run takes the full rebuild
  branch. The old `if not watchlist:` was an absorbing floor at zero that a
  decaying cache never reached.
* **Monthly membership refresh.** `scripts/run_monthly_universe_refresh.sh` (1st,
  06:30 UTC) forces a full rebuild even when the cache is healthy, because
  self-heal only fires on decay and never discovers *new* members. It uses
  `--run-mode weekly --force-universe-refresh` rather than `--run-mode monthly`,
  which would additionally apply theme boosts and switch the email path.
* **Safe mode.** Any failed mandatory guard suppresses the **speculative sleeve
  only** (`main.py`'s `not _scanner_safe_mode` gate) via a distinct reason —
  `empty_dataset`, `small_dataset`, `insufficient_screening_coverage`, or
  `screening_not_certified`. The broader advisory pipeline stays operational, and
  no decision, score, allocation, or approval semantics change.

## Scanner Screen Coverage (`scanner.v3_max_symbols`)

`v3_max_symbols` caps how many mktCap-qualifying symbols get per-ticker
fundamentals. It was a call-conservation setting from when `v3/profile/{batch}`
cost 5 calls for 500 symbols; now that every surviving endpoint is per-symbol,
that rationale is gone.

The cap is not harmless when it binds. `CandidateScanner._passes_hard_filters`
treats a missing `revenueGrowth` / `peRatio` / `freeCashFlowYield` as
**non-fatal by design** (so an unavailable fundamentals source cannot eject the
whole universe). Capped symbols therefore arrive with no fundamentals, pass the
screen by default, tie on score, and collapse the tail of the ranking to
alphabetical order.

Measured live on 2026-08-03 (503 S&P constituents, 503 passing the $5B mktCap floor):

| `v3_max_symbols` | Candidates | Actually screened | `rev_growth` rejections | FMP calls |
| --- | --- | --- | --- | --- |
| 100 (old) | 100 (top_k cap binds; 297 "passed") | ~24 | 67 | ~2,770 |
| 600 (current) | **55** | **55 (all)** | **410** | ~3,719 |

Raised to `600` on 2026-08-03 so the cap does not bind at the current index size.
`main.py` now logs a WARNING naming how many symbols would be admitted unscreened
whenever the cap does bind — a partial screen must never be silent again.

## Deprecated Or Avoided Patterns

- Do not regress stable quote/profile/ratios/historical/news/income/key-metrics methods back to v3 or v4.
- Do not batch stable profile or stable quote by comma-separated path. The implementation intentionally calls them per symbol.
- Do not add new core dependencies on undocumented FMP responses without contract tests.

## Fallback Order

### Watchlist Price And Technical Data

1. Alpha Vantage daily OHLCV
2. FMP stable quote or stable historical prices
3. Stale Alpha Vantage cache
4. Missing-data row with degraded confidence

### Watchlist Fundamentals

1. Alpha Vantage `OVERVIEW`
2. FMP stable `profile` plus optional `ratios` enrichment
3. Stale AV cache
4. Empty fundamentals dict

### Watchlist News

1. FMP stable stock news
2. Alpha Vantage news sentiment
3. Empty article list

### Broader-Market Universe

1. Premium: `v4/profile/all`
2. Free tier: free public constituent source (`universe/sp500_constituents.py`) plus
   `stable/profile` per symbol — `v3/sp500_constituent` and `v3/profile/{batch}` both
   403 on this key as of 2026-08-03
3. Last-good constituent cache (`data/universe/sp500_constituents.json`), flagged
   `degraded=True` so a stale universe is distinguishable from a live one
4. Hard failure (`ConstituentSourceError`) — deliberately NOT an empty list, because
   an empty universe reads downstream as "healthy, just empty"

## Rate-Limit And Budget Assumptions

- FMP client default daily budget: `230` calls.
- FMP client enforces a minimum `500 ms` gap between outbound requests.
- FMP client retries transient failures with exponential backoff.
- When the FMP daily budget would be exceeded, the client prefers stale cache over a live call.
- Watchlist Alpha Vantage default daily budget is treated as limited and cache-aware.
- `run_daily_pipeline.py` and `watchlist_scanner/__main__.py` assume cache-backed degraded operation is acceptable and preferable to hard failure.

## Required Field Expectations By Consumer

### Watchlist technical layer

- Minimum viable live row:
  `price`
- Full-quality row:
  `price`, `changesPercentage` or `changePercentage`, `volume`, `avgVolume`, `priceAvg50`, `priceAvg200`

### Watchlist fundamentals layer

- Minimum viable profile:
  `symbol`, `sector`
- Better-quality enrichment:
  `mktCap`, `beta`, `pe`, `netProfitMargin`, `revenueGrowth`, `debtEquityRatio`

### Candidate scanner

- Hard filters rely on:
  `mktCap`, `revenueGrowth`, `peRatio`, `freeCashFlowYield`, `price`, `priceAvg200`

### Universe filter

- Filtering relies on:
  `symbol`, `mktCap` or `marketCap`, `price`

## Contract Notes

- Stable endpoint compliance is enforced by tests such as `tests/test_fmp_endpoint_compliance.py`.
- Cache fallback is part of intended behavior, not an error path.
- Missing optional fields must lower confidence or degrade enrichment quality, not fabricate conviction.
