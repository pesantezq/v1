---
description: Regenerate and review the Strategy Catalog for the portfolio-simulation suite. Reads outputs/sandbox/strategy_catalog.json + portfolio_backtest.json + portfolio_projection.json, regenerates docs/STRATEGY_CATALOG.md with plain-language explanations + decision rationale for every tactic, flags any undocumented tactic (the Strategy Documentation Requirement), and routes prose-quality findings to portfolio-doc-writer. Observe-only — docs only, never changes runtime/scoring.
---

# Strategy Catalog

Maintain the explanation + decision-rationale documentation for every simulation
tactic (backtest / crowd-signal / projection). This is the maintenance mechanism
for the **Strategy Documentation Requirement** in CLAUDE.md.

Working dir: `/opt/stockbot`. Observe-only: this skill never edits runtime code,
scoring, or `decision_plan.json` — only the catalog docs.

## Step 1 — Read

- `outputs/sandbox/strategy_catalog.json` → `coverage_complete`, `undocumented[]`,
  `cards[]` (objective, universe, materialization, caps, metrics_by_window,
  rationale, explanation).
- `outputs/sandbox/portfolio_backtest.json` → per-tactic leaderboard (excess vs
  SPY, CAGR, maxDD, Sharpe) + `contribution_sensitivity`.
- `outputs/sandbox/portfolio_projection.json` (if present) → per-tactic forward
  percentile distributions.
- If `strategy_catalog.json` is absent, run
  `python -m portfolio_automation.portfolio_sim.run_portfolio_backtest --root . --run-mode discovery`
  first (it regenerates the catalog).

## Step 2 — Coverage gate

- If `coverage_complete == false` (any tactic in `undocumented[]`): RED. The
  Strategy Documentation Requirement is violated — a tactic is surfaced without
  a rationale. Add the rationale in `portfolio_sim/strategy_docs.py:_RATIONALE`
  (or via `extra_rationale`) and re-run; do not let an undocumented tactic ship.

## Step 3 — Regenerate + explain

- Regenerate `docs/STRATEGY_CATALOG.md` from the catalog (the orchestrator writes
  it; this skill verifies it is current and readable).
- For each tactic, confirm the plain-language explanation answers: *what is this
  strategy, how are its weights derived, what did the backtest say, what are the
  caveats* (approximate flag, proxy label, insufficient-data, degraded tickers).
- Where the auto-generated prose is thin or unclear, dispatch the
  `portfolio-doc-writer` agent to improve the narrative (docs only).

## Step 4 — Output

Emit a one-line heartbeat: `"Strategy-catalog: {tactic_count} tactics · coverage
{complete|INCOMPLETE: <ids>} · best 3y excess-vs-SPY {name} {value}"` plus any
RED coverage violation and the doc-writer dispatch result.
