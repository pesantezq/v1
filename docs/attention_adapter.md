# Attention Adapter

## Purpose

`portfolio_automation/crowd_intelligence/adapters/attention_adapter.py` is the
market-attention category adapter for the Crowd Intelligence builder (Lane B).
It derives context from the market-wide gainers / losers / most-active lists and
sector/industry performance snapshots. Gainer/loser membership is **directional**;
most-active membership is **attention only** (it raises confidence, not
direction). It is explicitly attention, never a recommendation.

---

## Two-Lane Governance

Context-only and **simulation-active / production-gated**. The adapter emits a
`CategoryResult` the builder composes into the observe-only `crowd_intelligence.*`
artifacts; it never recomputes decisions, never writes `decision_plan.json`, and
never touches scoring. All of its inputs are SHARED (non per-symbol) lists
fetched once by the builder through the governed FMP client, so it adds no
per-symbol FMP calls. Production effect only via the human-gated promotion lane.

---

## Inputs / Outputs

- **Inputs:** `symbol`, the governed `client`, the `usable` endpoint-id set, and
  the `shared` dict of pre-fetched market lists.
- **FMP endpoints (`SHARED_IDS` / `ENDPOINT_IDS`):** `biggest_gainers`,
  `biggest_losers`, `most_active`, `sector_performance_snapshot`,
  `industry_performance_snapshot`.
- **Output:** `CategoryResult` (`category="attention"`) with `score`, `reasons`,
  `events`, endpoint enable/disable lists, `has_data`, and `freshness`.

---

## Key Functions

- `run(symbol, *, client, usable, shared, now=None) -> CategoryResult` —
  +0.6 if the symbol is in biggest-gainers, −0.6 if in biggest-losers, an
  attention reason (no score change) if in most-active, plus a non-directional
  sector-performance context note. Final `score` is clamped to `[-1, 1]`.
- `_index_by_symbol(rows) -> dict[str, dict]` — builds an uppercase-symbol index
  over a shared list for O(1) membership tests.

---

## Tests

Covered with the crowd-intelligence adapter suite under `tests/`
(`python -m pytest -q tests -k crowd`).
