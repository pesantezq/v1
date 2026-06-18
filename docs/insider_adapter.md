# Insider Adapter

## Purpose

`portfolio_automation/crowd_intelligence/adapters/insider_adapter.py` is the
insider-trading category adapter for the Crowd Intelligence builder (Lane B). It
turns net insider buy/sell pressure for a symbol into a bounded directional
score in `[-1, 1]`. Notional values are **winsorized** so a single large filing
cannot dominate the score.

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
  (carries the market-wide `latest_insider_trading` feed pre-fetched once).
- **FMP endpoints (`ENDPOINT_IDS`):** `latest_insider_trading` (shared),
  `search_insider_trades` (per-symbol), `insider_trade_statistics` (per-symbol
  fallback).
- **Output:** `CategoryResult` (`category="insider"`) with `score`, a net-buy/
  sell reason, `events`, endpoint lists, `has_data`, and `freshness`.

---

## Key Functions

- `run(symbol, *, client, usable, shared, now=None) -> CategoryResult` —
  accumulates winsorized buy/sell notionals and sets
  `score = clamp((buy_sum − sell_sum) / (buy_sum + sell_sum))`. When no
  transactions resolve, it falls back to `insider_trade_statistics`
  (`buySellRatio`) mapped via `(r − 1)/(r + 1)`.
- `_is_buy(row) -> bool|None` — interprets `acquisitionOrDisposition` /
  `transactionType` (`A…` = buy, `D…`/SALE/SELL = sell).

---

## Tests

Covered with the crowd-intelligence adapter suite under `tests/`
(`python -m pytest -q tests -k crowd`).
