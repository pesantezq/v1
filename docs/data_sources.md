# Flock Intelligence — Data Sources

## Purpose

`portfolio_automation/flock_intelligence/data_sources.py` is the artifact-loading
layer for the simulation-only Flock Intelligence feature. It maps existing
upstream artifacts onto the pure flock-metric inputs (groups, crowd metrics,
returns, prior states). It introduces **no new paid data** — every input is a
reused free/already-paid artifact, and every loader is defensive (a missing or
malformed artifact degrades to an empty result and never raises).

---

## Two-Lane Governance

This is part of the **simulation-only** Flock Intelligence lane. It reads
upstream artifacts but only ever feeds the simulation producer; it never feeds
the decision engine and never writes production artifacts. Production behavior
changes only via the human-approved `sim_governance` promotion workflow.

---

## Inputs (reused artifacts)

- **Crowd velocity / breadth** — PREFERS the unified crowd bus
  (`outputs/latest/unified_crowd_intelligence.json` via
  `crowd_intelligence.unified_loader.read_unified_crowd`); when unavailable,
  falls back to the legacy ApeWisdom multi-source + public-knowledge velocity
  artifacts under `outputs/sandbox/discovery/`.
- **Theme grouping** — `outputs/latest/theme_signals.json` (`themes[].tickers`).
- **Sector grouping** — `data/fmp_cache/profile_stable_<TICKER>.json`
  (`data[0].sector`).
- **Price returns** — `outputs/performance/signal_outcomes.csv`
  (`outcome_return_1d`).
- **Prior flock states / volatility** —
  `outputs/simulation/flock_state_history.json` (written by the producer).

When the unified bus is the source, each per-ticker entry keeps the legacy
contract keys (`velocity` / `breadth` / `mentions`) and is additively enriched
with the unified cross-source fields (retail/fmp attention,
confirmation/divergence, news/analyst/insider/congress, crowd_state). This is
purely additive and observe-only.

---

## Key Functions

- `load_theme_groups(root) -> [(name, "theme", [tickers])]`.
- `load_sector_groups(root, universe) -> [(name, "sector", [tickers])]` — sectors
  with ≥2 resolvable tickers.
- `load_ticker_sector(root, ticker) -> str|None`.
- `load_universe(root) -> [tickers]` — union of config watchlist + holdings.
- `load_crowd_metrics(root) -> {ticker: {velocity, breadth, mentions, …}}` —
  unified-preferred, legacy-fallback.
- `load_returns(root, return_col="outcome_return_1d") -> {ticker: {date:
  return}}`.
- `aligned_group_returns(returns, tickers)` / `latest_returns(returns, tickers)`
  — date-aligned series and most-recent return for correlation/spread metrics.
- `load_prior_states(root) -> {group: {state, avg_correlation, volatility}}`.

The constant `_RETAIL_ATTENTION_VELOCITY_SCALE = 5.0` inverts the unified bus's
0..1 retail-attention normalization back onto the legacy mention-velocity
magnitude so the flock metrics see comparable values from either source.

---

## Tests

Covered under `tests/` with the flock-intelligence suite
(`python -m pytest -q tests -k flock`).
