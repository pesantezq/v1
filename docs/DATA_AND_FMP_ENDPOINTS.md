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
| `get_sp500_constituents()` | `/api/v3/sp500_constituent` | legacy but active | Used to seed the free-tier broader universe. |
| `get_batch_profiles_v3()` | `/api/v3/profile/{sym1,sym2,...}` | legacy but active | Free-tier batch profile fallback for the scanner/universe. |
| `get_fundamentals_v3()` | `/api/v3/key-metrics/{symbol}` and `/api/v3/financial-growth/{symbol}` | legacy but active | Used when stable fundamentals are unavailable for broader-market scan flows. |
| `get_bulk_profiles()` | `/api/v4/profile/all` | premium | Used by premium universe/scanner flows. |
| `get_bulk_key_metrics()` | `/api/v4/key-metrics-bulk` | premium | Used by premium broader-market scan flows. |

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
2. Free tier: `v3/sp500_constituent` plus `v3/profile/{batch}`
3. Constituents-only fallback with weak profile data

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
