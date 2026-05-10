# News Intelligence Layer

## Overview

The News Intelligence layer (`portfolio_automation/news/`) is an **observe-only, rules-first** evidence foundation that ingests FMP news articles and emits structured evidence packets for official holdings, watchlist symbols, ETFs/sectors/themes, and sandbox discovery candidates.

**Safety invariants (hardcoded):**

- `observe_only: true` — never mutates official state
- `no_trade: true` — no broker/execution calls
- `not_recommendation: true` — no BUY/SELL/HOLD statuses
- No official portfolio, watchlist, allocation, recommendation, or scoring mutation
- No discovery candidate promotion
- No LLM or AI calls — deterministic keyword rules only

## Module Location

```
portfolio_automation/
  news/
    __init__.py
    fmp_news_intelligence.py
```

## Public API

```python
from portfolio_automation.news import run_fmp_news_intelligence

result = run_fmp_news_intelligence(
    raw_articles=[...],         # Raw FMP-style article dicts
    holdings=["NVDA", "MSFT"],  # Official holdings (observe-only)
    watchlist=["AAPL", "AMZN"], # Official watchlist (observe-only)
    discovery_candidates=["PLTR"],  # Sandbox candidates (not promoted)
    base_dir="outputs",
    run_mode="daily",
    write_files=True,
)
```

### Individual functions

| Function | Purpose |
|---|---|
| `normalize_news_articles(raw_articles)` | Normalize raw FMP dicts into `NormalizedArticle` objects |
| `dedupe_news_articles(articles)` | Remove duplicates; sort newest-first |
| `extract_news_entities(article)` | Deterministic ticker/entity extraction |
| `classify_news_themes(article)` | Keyword-based theme scoring |
| `build_news_evidence_packets(articles, holdings, watchlist, discovery_candidates)` | Group evidence by ticker/entity |
| `write_news_intelligence_report(base_dir, raw_articles, ...)` | Full pipeline + artifact writes |
| `run_fmp_news_intelligence(raw_articles, ...)` | Top-level orchestrator |

## Processing Pipeline

```
raw_articles
  → normalize_news_articles()     # schema normalization, missing-field handling
  → dedupe_news_articles()        # URL/title+date deduplication, newest-first order
  → build_news_evidence_packets() # entity extraction, theme classification,
                                  # risk/catalyst flags, lane assignment
  → write artifacts
```

## Entity Extraction

Deterministic, no AI:

1. **Source-provided** — `symbols`/`tickers` fields in raw article dict (highest reliability)
2. **Cashtag** — `$NVDA`, `$AAPL` patterns
3. **Parenthetical** — `NVIDIA (NVDA)`, `Microsoft (MSFT)` patterns
4. **Company alias map** — Lowercased name matching → canonical ticker (e.g., "nvidia" → NVDA)

The alias map is seeded with ~50 well-known companies, ETFs, and indices. Generic theme terms (e.g., "cloud") are NOT automatically mapped to tickers.

## Theme Classification

Deterministic keyword scoring across 16 themes:

| Theme | Example signals |
|---|---|
| `ai_infrastructure` | artificial intelligence, LLM, generative ai, gpu cluster |
| `semiconductors` | chip, wafer, fab, foundry |
| `cloud` | cloud computing, aws, azure, google cloud, saas |
| `earnings_guidance` | earnings, beat estimates, raised guidance, eps beat |
| `rates_inflation` | inflation, cpi, interest rate, yield curve |
| `fed_policy` | federal reserve, fomc, powell, monetary policy |
| `legal_regulatory_risk` | lawsuit, investigation, sec, fine, penalty |
| `mna` | merger, acquisition, takeover, definitive agreement |
| `geopolitical_risk` | tariff, trade war, sanctions, export controls |
| `energy` | oil price, crude, natural gas, energy transition |
| `financials` | bank earnings, net interest margin, stress test |
| `gold_safe_haven` | gold, precious metals, safe haven, gld |
| `sector_rotation` | sector rotation, defensive, cyclical, risk-off |
| `consumer_demand` | consumer spending, retail sales, consumer confidence |
| `valuation` | overvalued, pe ratio, stretched valuation |
| `market_sentiment` | market rally, volatility, vix, overbought |

## Evidence Lanes

| Lane | Assignment | Description |
|---|---|---|
| `official_monitoring` | Ticker in `holdings` or `watchlist` | Evidence for current portfolio and watchlist |
| `sandbox_discovery_research` | All other tickers | Research context only; not official |

## Artifacts Produced

| Artifact | Namespace | Path | Description |
|---|---|---|---|
| `news_intelligence.json` | LATEST | `outputs/latest/news_intelligence.json` | Full evidence payload with packets, counts, safety flags |
| `news_intelligence.md` | LATEST | `outputs/latest/news_intelligence.md` | Human-readable report with disclaimer |
| `news_candidate_evidence.json` | SANDBOX | `outputs/sandbox/discovery/news_candidate_evidence.json` | Sandbox-lane evidence only (written when sandbox packets exist) |

All artifacts include:

```json
{
  "observe_only": true,
  "no_trade": true,
  "not_recommendation": true,
  "source": "fmp_news_intelligence_layer",
  "disclaimer": "News Intelligence is observe-only research context..."
}
```

## FMP Integration Boundary

The module is designed to accept any list of raw article dicts — it does not call FMP directly. The caller (e.g., `main.py` or a future orchestrator) fetches news via `FMPClient.get_stock_news()` and passes the result to `run_fmp_news_intelligence()`.

This design keeps all tests mockable without live API calls.

## How Later Phases Consume This Layer

| Future phase | How it uses this layer |
|---|---|
| `discovery_news_integration` | Feeds evidence packets into discovery candidate scoring |
| `daily_weekly_monthly_ai_market_narratives` | Uses theme classifications and evidence as narrative context |
| `news_evidence_layer_for_decision_engine` | Attaches evidence packets to decision plan entries as context |

## Tests

File: `tests/test_fmp_news_intelligence.py`
Count: 91 tests across 8 test classes

Coverage includes: normalization, deduplication, entity extraction, alias mapping, theme classification, risk/catalyst flags, evidence packets, lane assignment, safety flags, governance namespace compliance, malformed inputs, determinism.
