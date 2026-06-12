# Portfolio Simulation Suite — Complete Write-Up & Improvement Guide

> **Purpose of this doc:** a self-contained overview of the sandbox portfolio
> simulation suite for any engineer (or Claude session) picking it up to make
> improvements. It covers what exists, why it's shaped this way, how to run it,
> its limitations, and a concrete prioritized improvement backlog.
>
> **Status (2026-06-12):** built, tested (~103 tests), committed, **ships inert**
> (`config.json portfolio_sim.enabled=false`). Observe-only, sandbox-only, no
> auto-trading.

---

## 1. What it is

A sandbox layer that simulates the operator's **real portfolio** plus alternative
**tactics** so they can answer:

- **"Which strategy makes the most money vs the S&P 500?"** (historical backtest,
  ranked by excess return over SPY)
- **"How do outcomes scale with how much I contribute?"** (contribution-sensitivity)
- **"What could happen going forward?"** (Monte-Carlo projection: percentile bands,
  probability of hitting a target)

It is **not** a trading system. It never executes, recommends, or mutates the
official portfolio / `decision_plan.json` / `config.json` / `signal_registry.yaml`.

The operator objective is encoded in `config.json` → `portfolio_sim`:
`objective: maximize_excess_vs_sp500`, `anchor: actual_portfolio`, period windows
(YTD / quarterly / monthly / trailing), and contribution scenarios.

---

## 2. The core abstraction

Everything rests on one type — a **Tactic** = a named target-weight vector,
optionally **time-varying**:

```python
@dataclass
class Tactic:
    tactic_id: str
    name: str
    source: str                      # shadow | strategy_profile | benchmark | baseline | crowd
    target_weights: dict[str, float] # {ticker: weight}, sums to ~1
    metadata: dict                   # materialization map, caps, objective, horizon
    approximate: bool = False        # static stand-in for a rules-based tactic
    def target_weights_asof(date, ctx) -> dict[str, float]  # static = constant
```

Three things consume the same interface:
- **Backtest engine** — replays a tactic over historical prices.
- **Crowd tactic** — a `TimeVaryingTactic` whose weights depend on as-of date.
- **Projection engine** — Monte-Carlo forward simulation of a tactic's weights.

This is why adding sub-projects 2 and 3 was cheap: new tactic + same engines.

**Data flow:** read prices from the HISTORICAL archive
(`outputs/backtest/historical/<TICKER>_5y.json`) → write research artifacts to the
SANDBOX lane (`outputs/sandbox/`). This satisfies both namespace rules at once.

---

## 3. Module reference (`portfolio_automation/portfolio_sim/`)

| Module | Responsibility |
|---|---|
| `sim_base.py` | Observe-only envelope (`sim_envelope`), `SimStatus` enum, invariants. |
| `universe.py` | `resolve_simulable_universe(root)` → holdings ∪ proxy ETFs ∪ optional `universe_lists`. Dynamic, config-driven. |
| `prices.py` | `load_price_panel(tickers, root)` → `PricePanel` (calendar-aligned closes/volumes, forward-fill ≤5d, `monthly_returns()`). FMP fallback seam. |
| `metrics.py` | CAGR, vol, max-drawdown, Sharpe, Sortino, `excess_return`, `dca_terminal`. numpy. |
| `windows.py` | `resolve_windows(names, dates)` → trailing + intra-year calendar periods (YTD/quarter/month/explicit YYYY[-Qn\|-MM]). |
| `tactics.py` | `Tactic` / `TimeVaryingTactic`; materializers: `tactics_from_shadow_portfolios` (reuses `shadow_tracker`), `tactics_from_strategy_profiles` (8 SEED_PROFILES → capped tilt vectors), `benchmark_tactics`. `_clamp_caps` enforces concentration (0.60) + leverage (0.25). |
| `rebalance.py` | `RebalancePolicy`: `BuyAndHold`, `Periodic`, `ConfigRules` (real `rebalance_rules`). |
| `backtest_engine.py` | `run_backtest(tactic, policy, panel, window, ...)` → dual neutral (time-weighted) + DCA dollar paths, metrics, excess-vs-SPY, degraded handling. Look-ahead safe. |
| `crowd_tactic.py` | `build_crowd_sleeve` (priority-weighted sleeve + ×0.8 avoid-overlay), `CrowdTactic` (live/proxy), `proxy_pseudo_state` (volume-z + momentum → pseudo-state). |
| `crowd_forward_track.py` | `snapshot_sleeve` (paper positions → `social_signal_history.json`) + `resolve_records` (forward returns at 1/5/20/60d). |
| `projection_engine.py` | `project(...)` — numpy block-bootstrap of monthly return vectors → terminal percentiles, prob-reach-target, prob-loss, drawdown distribution, fan. Seeded. |
| `strategy_docs.py` | `build_strategy_catalog` + `render_strategy_catalog_md` — per-tactic documentation; `coverage_complete` gate. |
| `run_portfolio_backtest.py` | Orchestrator: tactics × policies × windows → leaderboard (by excess-vs-SPY) + contribution sensitivity + crowd proxy backtest + catalog → artifacts. |
| `run_portfolio_projection.py` | Orchestrator: project each tactic over config horizons → percentile artifact. |

---

## 4. Config reference (`config.json` → `portfolio_sim`)

```jsonc
"portfolio_sim": {
  "enabled": false,                          // master switch (ships OFF)
  "objective": "maximize_excess_vs_sp500",   // beat the S&P 500
  "primary_benchmark": "SPY",
  "secondary_benchmarks": ["QQQ"],
  "anchor": "actual_portfolio",
  "monthly_contribution": 1000,
  "contribution_scenarios": [500, 1000, 2000],
  "windows": ["ytd","trailing_1y","trailing_3y","trailing_5y","calendar_quarter","calendar_month"],
  "rebalance_policies": ["buy_and_hold","periodic"],
  "universe": { "proxy_etfs": ["BND","TLT","SCHD","USMV"], "include_universe_lists": false },
  "projection": { "n_paths": 5000, "seed": 12345, "block_months": 1, "horizons_years": [1,5,10,35] }
}
```

---

## 5. The three sub-projects

### 5.1 Historical backtest (the foundation)
Runs the operator's real portfolio + 6 shadow portfolios (`actual_baseline`,
`target_allocation_baseline`, `engine_followed`, `lower_risk`,
`discovery_enhanced`, `boom_bucket`) + 8 materialized strategy profiles
(`aggressive_growth`, `short_term_tactical` [approximate], `long_term_compounding`,
`tax_aware`, `defensive`, `income_dividend`, `balanced_core_satellite`,
`boom_bucket`) + SPY/QQQ benchmarks. Each runs under `buy_and_hold` + `periodic`
across all windows. Output: a leaderboard **ranked by excess return vs SPY** per
window, plus a contribution-sensitivity table.

**Profile materialization** (the one piece of real modeling): each profile's
declarative tilts (e.g. Defensive → zero leverage, raise gold/bonds/low-vol) are
applied as bounded multipliers to the actual-portfolio anchor over the resolved
universe, normalized, and clamped to caps. The exact tilt map is written into the
catalog (auditable). `short_term_tactical` is flagged `approximate` (a faithful
signal-driven version needs point-in-time signals = look-ahead risk = deferred).

### 5.2 Crowd-signal tactic
A capped sleeve (≤15% total, ≤5%/idea) toward useful Crowd Radar states
(`emerging_dd`, `crowd_validation`, `contrarian_neglect`), priority-weighted by
`crowd_research_priority_score`; an avoid-overlay excludes caution states
(`hype_acceleration`, `reflexive_squeeze_risk`, `crowd_exhaustion`) and trims
caution **core** holdings ×0.8 (flagged). Evaluated two ways:
- **Forward shadow-track (real):** paper-trade from today; resolve at 1/5/20/60d;
  feeds the sample-gated `social_signal_backtest.json`.
- **Proxy historical backtest (illustrative):** a volume-z + momentum stand-in for
  "attention," stamped `proxy: true` — NOT the real crowd signal's record.

### 5.3 Forward Monte-Carlo projection
Block-bootstraps historical monthly return *vectors* (preserves cross-asset
correlation + fat tails; no covariance/normality assumption), N paths over
1/5/10/35y. Reports terminal-balance percentiles (p5..p95), prob-reach-target
(vs config target CAGR), prob-loss, drawdown distribution, percentile fan. Seeded
→ reproducible. Labeled "illustration, not forecast."

---

## 6. Governance & safety invariants

- **Observe-only / sandbox-only.** All writes via `OutputNamespace.SANDBOX`,
  guarded by `assert_can_write_namespace` (DAILY/MANUAL/WEEKLY modes cannot write).
- Every artifact stamps `observe_only/sandbox_only/no_trade`.
- **Never** writes `decision_plan.json`, `config.json`, or `signal_registry.yaml`.
- **No trade verbs** anywhere (GUI tests assert this).
- **Default-disabled**; degrades to a status artifact (never crashes the pipeline).
- **Look-ahead safe:** engines read only data ≤ the simulation date.
- **Strategy Documentation Requirement** (CLAUDE.md): every tactic needs a
  catalog rationale or it doesn't surface in the Strategy Lab.

---

## 7. How to run / enable

```bash
# manual (sandbox, reads offline archive — no network needed when archives present)
.venv/bin/python -m portfolio_automation.portfolio_sim.run_portfolio_backtest  --root . --run-mode discovery
.venv/bin/python -m portfolio_automation.portfolio_sim.run_portfolio_projection --root . --run-mode discovery
```

- **Cadence:** 2 weekly stages in `scripts/run_weekly_safe.sh` (non-blocking).
- **Enable:** `config.json portfolio_sim.enabled=true`.
- **GUI:** Strategy Lab tab (`/dashboard/strategy-lab`) → Backtest + Projection
  sections (needs a dashboard restart to appear).
- **Health:** monthly-tool-analysis quant lens + content-liveness; `/strategy-catalog`
  for doc coverage.

**Artifacts** (`outputs/sandbox/`): `portfolio_backtest.json`,
`portfolio_backtest_summary.md`, `strategy_catalog.json`,
`portfolio_projection.json`, `crowd_tactic_backtest.json`, +
`docs/STRATEGY_CATALOG.md`.

---

## 8. Known limitations / caveats (read before trusting numbers)

1. **No transaction costs, taxes, slippage, or spreads** in the P&L. Results are
   gross. (config has `is_taxable_account: true` — taxes matter for this operator.)
2. **Profile materialization is heuristic.** The tilt multipliers are reasonable
   but not optimized/validated; documented in the catalog, not derived from theory.
3. **`short_term_tactical` is a static approximation** — flagged, not faithful.
4. **Crowd backtest has no real history** — the proxy measures volume/momentum, not
   actual crowd evidence. The honest signal is the forward track (matures over months).
5. **Projection bootstraps only ~5y of history** — underweights rare regimes
   (2008-style crashes). Past ≠ future; it's an illustration.
6. **Price archive coverage is partial.** Only some tickers have `*_5y.json`;
   missing ones are dropped + renormalized + recorded (`degraded`/`missing_price_history`).
   The operator's holdings (QQQ/GLD/QLD/etc.) may need backfill for a full real run.
7. **`reflexive_squeeze_risk` crowd state never fires** — needs a short-interest /
   options feed not yet wired.
8. **DCA `start_value` is a config default (10k)**, not the live portfolio value.
9. **Rebalancing ignores tax-lot / wash-sale reality** even under `config_rules`.

---

## 9. Improvement backlog (concrete, prioritized — hand these to Claude)

### High value
- **Add transaction-cost + tax modeling** to the P&L (esp. since the account is
  taxable). A `CostModel` injected into `backtest_engine` + `ConfigRules`;
  short-term vs long-term capital-gains on rebalance sells; surface after-tax
  return alongside gross. *Biggest fidelity gain for the "make the most money"
  objective.*
- **Backfill the 5y price archive for all holdings + proxy ETFs** (QQQ, GLD, QLD,
  VFH, VXUS, NASA, BND, TLT, SCHD, USMV) via the free FMP loader so real runs
  aren't degraded. Add a one-shot `scripts/` backfill or extend `historical_backfill`.
- **Seed the actual portfolio value as `start_value`** from the live snapshot
  (or broker_aware) instead of a flat 10k, so DCA dollar paths are real.
- **Validate / tune the profile tilt multipliers** — e.g. grid-search tilts that
  best separate the profiles' risk/return on historical data; or document a
  principled mapping. Currently hand-picked.

### Medium value
- **Faithful `short_term_tactical` + crowd tactic via point-in-time signal
  reconstruction** (mirror `backtesting/historical_signal_recon.py`) so they can be
  honestly backtested instead of approximated/proxied.
- **Sharpe/Sortino vs SPY (information ratio)** and **rolling-window** excess
  return (not just point-to-point) for more robust ranking.
- **Operator-defined custom tactics** in config (e.g. "60% QQQ / 40% GLD") — the
  Tactic interface already supports it; add a config reader + catalog entry.
- **Projection: parametric / regime-switching return models** and **fat-tail
  stress scenarios** (bootstrap from a crisis sub-window) as an alternative to
  pure historical resampling.
- **GUI: value-series charts** (the backtest already emits `value_series`; render a
  sparkline/line chart and the projection percentile fan as an actual chart).

### Lower value / polish
- **Wire a short-interest / options feed** to activate `reflexive_squeeze_risk` and
  enrich the crowd tactic.
- **Inflation-adjusted (real) returns** option in projection.
- **Per-tactic attribution** (which holdings drove excess vs SPY).
- **Cache the price panel** across the backtest + projection runs (currently each
  orchestrator reloads).
- **Sensitivity to rebalance frequency** (weekly/quarterly/annual) as a dimension.

---

## 10. Pointers

- **Specs:** `docs/superpowers/specs/2026-06-12-portfolio-tactic-backtest-design.md`,
  `-crowd-signal-tactic-design.md`, `-forward-projection-design.md`
- **Plan:** `docs/superpowers/plans/2026-06-12-portfolio-sim-suite.md`
- **Artifact contracts:** `docs/OUTPUT_ARTIFACT_CONTRACTS.md` (Portfolio Simulation Suite section)
- **Runbook:** `docs/PIPELINE_RUNBOOK.md` (Portfolio Simulation Suite section)
- **Auto-generated catalog:** `docs/STRATEGY_CATALOG.md` (after first enabled run)
- **Tests:** `tests/portfolio_sim/`, `tests/test_gui_strategy_lab_{backtest,projection}.py`
- **Rule:** CLAUDE.md → "Strategy Documentation Requirement"
- **Config:** `config.json` → `portfolio_sim`
- **State:** `.agent/project_state.yaml` (`portfolio_simulation_suite_built`),
  `.agent/phase_status.yaml` (`portfolio_simulation_suite`)

## 11. Suggested prompt for a future Claude session

> "Read `docs/PORTFOLIO_SIM_SUITE.md`. I want to improve the portfolio simulation
> suite — specifically [pick from §9, e.g. 'add transaction-cost + tax modeling to
> the backtest P&L' or 'backfill the price archive for all my holdings']. Keep it
> sandbox-only / observe-only, follow the Strategy Documentation Requirement, add
> tests, and don't touch decision_plan/scoring. Brainstorm the design with me first."
