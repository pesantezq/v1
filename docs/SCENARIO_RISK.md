# Risk & Scenario Comparison (Phase 11)

Status: **shipped** on `feat/complete-simulation-quant-governance-loop`.
Observe-only. **Illustrations, not forecasts** (`is_forecast=False`).

## What already existed
`risk_delta_advisor` (concentration/leverage/VaR vs caps) +
`correlation_risk_advisor` (effective bets, correlated pairs). Phase 11 adds the
**deterministic stress scenarios + pre/post-action risk + marginal
contribution** that were missing.

## Scenarios (`scenario_risk.SCENARIOS`)
Seven deterministic shocks: `broad_market_decline, nasdaq_growth_decline,
semiconductor_drawdown, volatility_spike, rate_shock, gold_decline,
liquidity_shock`. Each maps coarse asset classes (broad equity / nasdaq-growth /
semis / financials / international / gold) to a % return shock.

## API (pure)
- `apply_scenario(weights, scenario)` — deterministic shocked P&L overall +
  `by_position`.
- `pre_post_action_risk(before, after, scenario)` — scenario P&L before vs after
  a simulated action + `delta_pct` + per-position `marginal_contribution`.
- `marginal_contribution(weights, scenario)` — per-position contributions that
  **sum to the total** scenario P&L.
- `build_scenario_risk(root)` — runs all scenarios on the current risk_delta
  weights; `worst_case_scenario`; writes `outputs/latest/scenario_risk.json`.

## Honesty guards
- `is_forecast=False` — modeled illustrations, never presented as predictions.
- `etf_lookthrough_available=False` — no constituent data, so ETF look-through
  is **not fabricated**; coarse asset-class mapping is used and disclosed.
- `degraded=True` + null returns when holdings are unavailable.

## Pipeline + cadence
`run_daily_safe.sh` **Stage 7b2** (after the risk-delta panel it reads). Smoke:
7 positions, worst case `liquidity_shock`. Deeper scenario simulation stays in
the weekly pipeline.

## Tests
`tests/test_scenario_risk.py` (7) — scenario presence, deterministic weighted
P&L, gold-vs-broad targeting, pre/post + marginal, contributions sum to total,
graceful degrade, no fabricated ETF look-through.
