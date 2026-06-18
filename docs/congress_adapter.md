# Congress Adapter

## Purpose

`portfolio_automation/crowd_intelligence/adapters/congress_adapter.py` is the
congressional-trade-disclosure category adapter for the Crowd Intelligence
builder (Lane B). It converts disclosed Senate/House trade activity for a symbol
into a **low-weight, context-only** directional score. The score is deliberately
dampened (×0.5) and hard-clamped to `±0.5`, and all explanations avoid any
causal or privileged-insight implication — it is public disclosure context only.

---

## Two-Lane Governance

Context-only and **simulation-active / production-gated**. Emits a
`CategoryResult` for composition into the observe-only `crowd_intelligence.*`
artifacts; never recomputes decisions, never writes `decision_plan.json`, and
never touches scoring. FMP access is through the governed client. Production
effect only via the human-approved `sim_governance` promotion workflow.

---

## Inputs / Outputs

- **Inputs:** `symbol`, governed `client`, `usable` endpoint-id set, `shared`.
- **FMP endpoints (`ENDPOINT_IDS`):** `senate_trading`, `house_trading`
  (per-symbol). The `*_by_name` member-keyed feeds are intentionally excluded
  (not per-symbol).
- **Output:** `CategoryResult` (`category="congress"`) with the dampened/capped
  `score`, a disclosure-count reason, `events`, endpoint lists, `has_data`, and
  `freshness`.

---

## Key Functions

- `run(symbol, *, client, usable, shared, now=None) -> CategoryResult` — counts
  purchase vs sale disclosures and sets
  `score = clamp(0.5 * (buys − sells) / n, -0.5, 0.5)`.
- `_direction(row) -> int` — `+1` for purchase/buy, `−1` for sale/sell, `0`
  otherwise (tolerant of FMP field aliases `type` / `transaction` /
  `transactionType`).

Module constants: `_DAMPEN = 0.5`, `_CAP = 0.5`.

---

## Tests

Covered with the crowd-intelligence adapter suite under `tests/`
(`python -m pytest -q tests -k crowd`).
