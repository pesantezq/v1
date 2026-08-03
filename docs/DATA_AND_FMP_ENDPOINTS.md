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
