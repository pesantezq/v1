# Analyst Adapter

## Purpose

`portfolio_automation/crowd_intelligence/adapters/analyst_adapter.py` is one of the
five FMP-sourced category adapters that feed the Crowd Intelligence builder
(Lane B — FMP market/institutional context). It turns sell-side analyst
consensus and recent grade-action direction into a single bounded directional
score in `[-1, 1]` for one symbol. Consensus is the backbone of the score;
recent up/down grade momentum nudges it.

---

## Two-Lane Governance

This adapter is **context-only**. It produces a `CategoryResult` that the crowd
builder composes into the observe-only `crowd_intelligence.*` artifacts. It is
**simulation-active / production-gated**: it never recomputes a BUY/SELL/HOLD,
never writes to `decision_plan.json`, and never touches scoring semantics. Any
production effect of crowd context only happens through the human-approved
`sim_governance` promotion workflow. All FMP access flows through the governed
client; any exception inside the adapter is caught by the builder and converted
to a neutral result with a warning.

---

## Inputs / Outputs

- **Inputs:** `symbol`, a governed FMP `client`, the set of `usable` endpoint
  ids (from the Phase-1 capability map), and the shared pre-fetched payloads.
- **FMP endpoints (`ENDPOINT_IDS`):** `ratings_snapshot`, `ratings_historical`,
  `stock_grades`, `grades_consensus`.
- **Output:** a `CategoryResult` (`category="analyst"`) carrying `score`,
  `reasons`, `events` (`NormalizedEvent` records), `enabled_endpoints` /
  `disabled_endpoints`, `has_data`, and `freshness`. No file is written by the
  adapter itself — the builder/`artifact_writer` persists the composed result.

---

## Key Functions

- `run(symbol, *, client, usable, shared, now=None) -> CategoryResult` — adapter
  entrypoint called once per symbol by `crowd_signal_builder`.
- `_consensus_score(row) -> (score|None, total_ratings)` — maps a consensus row
  (`strongBuy/buy/hold/sell/strongSell`, with FMP alias fallbacks) to a clamped
  `(strongBuy + buy − sell − strongSell) / total`.

Scoring combination:

- with consensus: `score = clamp(0.8 * consensus + 0.2 * grade_direction)`
- without consensus: `score = clamp(0.5 * grade_direction)`

`grade_direction` is `clamp((upgrades − downgrades) / (upgrades + downgrades))`
over the five most-recent `stock_grades` rows. `freshness` is `1.0` when any
data was found, else `0.0`.

---

## Tests

Covered with the rest of the crowd-intelligence adapters under
`tests/` (run `python -m pytest -q tests -k crowd`).
