# Design — Forward Monte-Carlo Projection (Sandbox Sub-Project 3)

> Status: design (brainstorm 2026-06-12; decisions made autonomously under the
> "spec + plan everything" goal). Sandbox-only · observe-only · no auto-trading.
> Sub-project 3 of the portfolio-simulation workstream; depends on **sub-project
> 1** (Tactic interface + metrics + price panel).

## 1. Goal

Answer the forward-looking question for the operator's real portfolio + each
tactic: **"what is the *distribution* of outcomes over the next 1/5/10/35 years —
balance percentiles, probability of hitting my target, drawdown risk?"**

Where sub-project 1 says "what *did* happen," this says "what *could* happen,"
with explicit uncertainty bands rather than a single number.

## 2. Approach (decisions made)

- **Return model: block bootstrap of historical monthly return *vectors*.** Each
  Monte-Carlo step samples a real historical month's per-asset return vector (all
  assets from the same month) from the 5y archive. Sampling whole vectors
  **preserves cross-asset correlation and fat tails naturally** — no covariance
  matrix to estimate, no normality assumption. Block size configurable (default
  1 month; optional 3-month blocks to retain short-horizon autocorrelation).
  *Rationale: non-parametric, robust, and honest about tails; documented as a
  key assumption.* A parametric multivariate-normal mode is a documented future
  option, not v1.
- **Paths:** `n_paths` (default 5,000) × horizon. Horizons from config:
  `1y / 5y / 10y / full` (full = `investor.investment_horizon_years`, 35y).
- **Contributions:** model the `$1,000/mo` DCA injection (config
  `monthly_contribution`) so terminal balances are realistic dollars; also report
  a contribution-neutral growth-of-$1 percentile fan.
- **Reproducible:** a seeded numpy RNG (`config.crowd_radar`-style block →
  `portfolio_sim.projection.seed`, default fixed) so a run is deterministic and
  testable. The seed is recorded in the artifact.

## 3. Module layout (extends sub-project 1 package)

```
portfolio_sim/
  projection_engine.py        # Monte-Carlo engine: (tactic, return_panel, horizon, n_paths) → path matrix
  run_portfolio_projection.py # orchestrator → artifacts
```

Reuses sub-project 1: `tactics.py` (Tactic + materializers — same tactics get
projected), `prices.py` (build the monthly-return panel from the archive),
`metrics.py` (drawdown/return helpers applied per path), `universe.py`.

## 4. Engine

`project(tactic, monthly_return_panel, horizon_months, n_paths, start_value,
monthly_contribution, seed, block=1)`:

1. Build the universe monthly-return matrix `R` (months × tickers) from the 5y
   archive (reuse `prices.py`); drop tickers without history (renormalize tactic
   weights, record `degraded`).
2. For each of `n_paths`: draw `horizon_months / block` random historical blocks,
   concatenate → a synthetic monthly path; apply the tactic's weights (held;
   optionally periodic-rebalanced reusing sub-project 1's `RebalancePolicy`);
   compound value, injecting the monthly contribution.
3. Collect terminal values + full value paths (downsampled) across all paths.

Vectorized with numpy for speed (5,000 paths × 420 months is trivial).

## 5. Outputs (metrics per tactic × horizon)

- Terminal balance percentiles: `p5 / p25 / p50 / p75 / p95` (DCA dollars) and
  growth-of-$1 percentiles (contribution-neutral).
- `prob_reach_target`: P(terminal ≥ target), where target = config target-CAGR
  implied balance and/or an operator $ goal.
- `prob_loss`: P(terminal < total contributed).
- Drawdown distribution: median + p95 max-drawdown across paths.
- `cagr_p50`, `cagr_p5`, `cagr_p95`.
- Downsampled percentile **fan** (p5/p50/p95 over time) for charting.

Artifacts (SANDBOX):
- `outputs/sandbox/portfolio_projection.json` — per tactic×horizon distribution
  + percentile fan + assumptions block + seed + `degraded` + observe-only envelope.
- `outputs/sandbox/portfolio_projection_summary.md` — operator-readable.
- Strategy Catalog entry per sub-project 1's documentation rule (model,
  assumptions, seed, caveats, decisions).

## 6. Surfacing

- GUI **Strategy Lab** tab: a "Projection" section — per-tactic percentile fan
  (p5/p50/p95 balance over the horizon) + a small table (p50 balance,
  prob-reach-target, p95 max-drawdown). Reuses the tab.
- Daily/weekly memo: optional one-line ("Projection: p50 balance in 10y = $X,
  P(reach 9% CAGR) = Y%") — research-framed, never a guarantee.

## 7. Governance + assumptions labeling

Observe-only, sandbox-only, OutputNamespace.SANDBOX, no trade verbs, never writes
`decision_plan.json` / config. The artifact carries a prominent `assumptions`
block: *historical-return resampling (past ≠ future), no regime-shift / structural
break modeling, no fees/taxes, contributions assumed constant.* A projection is
labeled a **probabilistic illustration, not a forecast.**

## 8. Cadence + health coverage

- Weekly + on-demand (reuses the weekly sim cadence).
- Register `portfolio_projection.json` in `artifact_registry.yaml` (weekly).
- Extend `monthly-tool-analysis` (quant lens): sanity-check that p50 CAGR is in a
  plausible band and percentiles are monotone; content-liveness: engine ran but
  all tactics degraded (no return panel).

## 9. Tests

- percentile monotonicity (p5 ≤ p25 ≤ p50 ≤ p75 ≤ p95).
- reproducibility: same seed → identical output; different seed → different paths.
- DCA: terminal balance ≥ total contributed in the p95 path of a positive-drift asset.
- single-asset degenerate panel → projection ≈ that asset's resampled distribution.
- contribution-neutral vs DCA paths differ as expected.
- degraded: tactic ticker missing history → dropped + renormalized + recorded.
- no-mutation invariant; `observe_only=True`; assumptions block present.
- block bootstrap: block=3 preserves 3-month contiguity (sampled months are contiguous).

## 10. Deferred (YAGNI)

- Parametric / GARCH / regime-switching return models.
- Fees/taxes/inflation-adjusted (real) returns.
- Correlated contribution/withdrawal scenarios (retirement decumulation).

## 11. Risks

- Bootstrap from only 5y of history underweights rare regimes — mitigated by the
  explicit assumptions label and offering 3-month blocks.
- Users may read percentiles as guarantees — mitigated by "illustration, not
  forecast" framing in every artifact + GUI.
