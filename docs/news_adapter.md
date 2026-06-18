# News Adapter

## Purpose

`portfolio_automation/crowd_intelligence/adapters/news_adapter.py` is the news
category adapter for the Crowd Intelligence builder (Lane B). It contributes
**velocity / attention** context, not direction: the directional score is
**neutral by design** (`0.0`) because FMP's Starter tier carries no sentiment
field and RSS-sentiment is `PLAN_LOCKED`. Risk-event keywords surface as
**warnings only** — never as buy/sell signals.

---

## Two-Lane Governance

Context-only and **simulation-active / production-gated**. Emits a
`CategoryResult` for the observe-only `crowd_intelligence.*` artifacts; never
recomputes decisions, never writes `decision_plan.json`, never touches scoring.
FMP access is through the governed client. Production effect only via the
human-approved `sim_governance` promotion workflow.

---

## Inputs / Outputs

- **Inputs:** `symbol`, governed `client`, `usable` endpoint-id set, `shared`
  (shared news pools pre-fetched once), optional `now` for deterministic tests.
- **FMP endpoints:** `stock_news_search` (per-symbol) plus the shared pools
  `stock_news_latest`, `fmp_articles`, `general_news`, `crypto_news`,
  `forex_news`.
- **Output:** `CategoryResult` (`category="news"`) with `score=0.0`, a velocity
  reason (`N articles (cnt_24h in 24h / cnt_7d in 7d); velocity V`), optional
  risk-keyword `warnings`, `events`, endpoint lists, `has_data`, and `freshness`.

---

## Key Functions

- `run(symbol, *, client, usable, shared, now=None) -> CategoryResult` —
  gathers per-symbol and shared articles, counts 24h/7d mentions, computes
  `velocity = cnt_24h / max(1, cnt_7d/7)`, and decays `freshness` over a 7-day
  window from the latest article.
- `_parse_dt(s)` — tolerant article-date parser.

Risk keywords (`_RISK_KEYWORDS`) such as `bankruptcy`, `sec investigation`,
`fraud`, `halted`, `delist`, `default` only ever populate `warnings`.

---

## Tests

Covered with the crowd-intelligence adapter suite under `tests/`
(`python -m pytest -q tests -k crowd`).
