# Simulation Charts

**Status:** shipped 2026-06-16. Module: `portfolio_automation/simulation_charts.py`.
Loader: `gui_v2/data/dash_simulation_charts.py`. Components: `gui_v2/templates/components/_charts.html`.
Rendered in the **Strategy Lab** dashboard (`/dashboard/strategy-lab`) as the
"Simulation Graphs" section.

## What it is

A human-readable, **sandbox / observe-only** visualization of existing backtest and
simulation results — built for a non-quant reader. It aggregates artifacts the pipeline
already produces into one normalized artifact and renders six plain-English charts, each
with a one-line "what it shows", a "takeaway", and a Source & safety disclosure.

It is research/evidence context only. It **does not** create trades, emit buy/sell/hold
instructions, modify `decision_plan.json`, change any recommendation, or promote sandbox
results to production. Official advisory actions come only from `decision_plan.json`.

## Data flow

```
outputs/sandbox/strategy_comparison.json   (daily)   ─┐
outputs/sandbox/portfolio_backtest.json    (weekly)  ─┼─► simulation_charts.run_simulation_charts()
outputs/sandbox/portfolio_projection.json  (weekly)  ─┘        │ pure read + aggregate
                                                               ▼
                                              outputs/latest/simulation_charts.json
                                                               │
                                       gui_v2/data/dash_simulation_charts.py (view + SVG geometry)
                                                               │
                                  Strategy Lab template → components/_charts.html (inline SVG)
```

- Producer wired as **Stage 10b2** of `scripts/run_daily_safe.sh` (after the next-stage
  lane writes `strategy_comparison.json`; before the daily-run-status stage). Non-blocking.
- The producer is **pure** (no network/LLM). The GUI loader pre-computes all SVG geometry
  so the template only draws — no JS, no external chart library.

## The six charts (human-readable names)

| Chart | Source | Notes |
|---|---|---|
| **Growth Over Time** | `portfolio_projection.json:anchor_fan` | Median (p50) growth of $10k with a cautious–optimistic (p5–p95) band. |
| **How Deep the Losses Got** | `strategy_comparison.json:max_drawdown_estimate` | Per-strategy worst drawdown, shown as positive depth. |
| **Risk vs Return** | `strategy_comparison.json` | Scatter: return (y) vs volatility (x) per strategy. |
| **Was Performance Consistent?** | `portfolio_backtest.json:leaderboard[*].excess_vs_spy` | Excess-vs-SPY across look-back windows; above zero = ahead of SPY. |
| **How Contributions Change the Outcome** | `portfolio_backtest.json:contribution_sensitivity` | Ending value by monthly-addition amount. |
| **How the Portfolio Shifted Over Time** | — none yet — | Honest empty state: no artifact tracks per-sleeve composition over time. |

## Degraded / fallback behavior

- **No persisted artifact** → the loader builds a *limited* view live from
  `strategy_comparison.json` (at minimum Risk vs Return + drawdown), marked `status="limited"`.
- **Nothing available** → empty state: *"Simulation charts are not available yet. Run the
  simulation/backtest pipeline…"*
- **A specific chart has no source data** → *"Not enough simulation data to draw this chart yet."*
- **Stale** (> 14 days) → a non-blocking *"Simulation data may be stale. Last generated: …"* note.
- **Malformed JSON** → safe empty state, never a 500.

## Safety invariants (enforced by tests)

- `observe_only: true`, `sandbox_only: true`, `safety.can_execute_trades: false`,
  `safety.official_advisory_source: "decision_plan.json"` in the artifact.
- No forbidden language anywhere in the artifact or rendered page (`buy/sell/hold/execute`,
  `execute trade`, `place order`, `rebalance now`, `promotion approved`,
  `official recommendation`, …).
- `run_simulation_charts` never touches `decision_plan.json` (byte-identical after a run).

## Health / analysis pairing

The `/strategy-lab-analysis` skill includes a content-liveness check for
`simulation_charts.json` (looks-fresh-but-empty: artifact present but every chart
`available:false`). Cadence matches the lab's weekly review.

## Tests

`tests/test_simulation_charts.py` (producer + loader) and the Simulation-Graphs cases in
`tests/test_dashboard_strategy_lab.py` (route render, missing/malformed safety, sandbox
labelling, no-trade language, responsive grid).
