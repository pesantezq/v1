# Research-Backed Strategy Lab — Specs, Workflow & Strategies

> Sandbox-only · observe-only · no auto-trading. Extends the Portfolio Simulation
> Suite (`docs/PORTFOLIO_SIM_SUITE.md`). Design spec:
> `docs/superpowers/specs/2026-06-12-research-backed-strategy-lab-design.md`;
> plan: `docs/superpowers/plans/2026-06-12-research-backed-strategy-lab.md`.

## 1. Purpose

Rank strategies for the operator's real portfolio by **after-cost, risk-adjusted
excess return vs the S&P 500**, where each strategy is grounded in academic
finance research, validated out-of-sample (walk-forward), and explained by factor
attribution. The goal is to reduce guessing and curve-fitting — not guaranteed profit.

**Every strategy must declare:** academic basis, test period, benchmark,
risk-adjusted return, factor exposure, overfit risk, and whether it still works
out-of-sample.

## 2. Workflow (how a run flows)

```
run_strategy_lab.run_strategy_lab(root, run_mode="discovery")
  1. gate on config.portfolio_sim.enabled AND .strategy_lab.enabled
  2. build tactics = suite tactics (6 shadow + 8 profiles + SPY/QQQ)
                    + research_library tactics (~10 academic strategies)
  3. load_price_panel(all tickers + SPY) from outputs/backtest/historical/*_5y.json
  4. resolve windows (trailing 1/3/5y + YTD)
  5. walk_forward(momentum grid) → OOS overfit per parameterized tactic
  6. factor_report: regress each tactic's monthly returns on Fama-French factors
  7. per tactic: backtest across windows → metrics → master strategy_score
  8. rank by strategy_score → leaderboard
  9. write SANDBOX artifacts (+ research catalog) ; never touch decision_plan
```

Cadence: weekly (`run_weekly_safe.sh` stage) + on-demand CLI. Health:
`/strategy-lab-analysis` skill + monthly-tool-analysis quant lens.

## 3. Strategy catalog (the tactics)

**Suite tactics** (from the sim suite): `actual_baseline`, `target_allocation_baseline`,
`engine_followed`, `lower_risk`, `discovery_enhanced`, `boom_bucket` (shadow); the
8 strategy profiles (`aggressive_growth`, `short_term_tactical`*, `long_term_compounding`,
`tax_aware`, `defensive`, `income_dividend`, `balanced_core_satellite`, `boom_bucket`);
`benchmark_spy`, `benchmark_qqq`.

**Research-library tactics** (`research_library.py`):

| tactic_id | Strategy | Academic basis |
|---|---|---|
| `research_sixty_forty` | 60% SPY / 40% BND | Classic balanced benchmark |
| `research_factor_tilt` | Quality/value/dividend proxy tilt (SCHD/USMV/SPY) | Fama-French (1993) factors |
| `research_momentum_rotation` | Top-N ETFs by trailing 3/6/12m return | Jegadeesh & Titman (1993) |
| `research_dual_momentum` | Risk-on if abs+rel momentum>0 else defensive | Antonacci dual momentum |
| `research_risk_parity_lite` | Inverse-vol weights | Risk budgeting |
| `research_mean_variance` | Long-only max-Sharpe from trailing mean/cov | Markowitz (1952) |
| `research_vol_managed` | Cut leverage sleeve when realized vol high | Moreira & Muir (2017) |
| `research_black_litterman` | Confidence-weighted prior + view blend | Black-Litterman / Idzorek |

\* `short_term_tactical` is an approximate static stand-in (flagged). Parameterized
tactics (momentum lookback/top-N, mean-variance lookback, risk-parity vol window,
vol-managed threshold) are `TimeVaryingTactic`s computing weights from data ≤ the
rebalance date (look-ahead safe), and are subject to walk-forward param selection.

**Planned (deferred, documented):** `ensemble_top3` (blend of the top-3 by score),
regime-specific simulations (per bull/bear/sideways/high-low-vol — Phase D), crowd
signal event studies (Phase F). These are spec'd in the plan; the crowd tactic +
forward-track already exist in the suite.

## 4. Master strategy score

```
strategy_score =
    excess_return_vs_spy
  + probability_beat_spy_bonus      # fraction of windows beating SPY
  + drawdown_control_bonus          # less drawdown → higher
  + consistency_bonus               # stable beating across windows
  + research_support_bonus          # has academic_basis
  - turnover_penalty                # time-varying tactics turn over more
  - tax_drag_penalty                # flat proxy until cost model (flagged gross_until_cost_model)
  - concentration_penalty           # max single-name weight
  - leverage_penalty                # leveraged-asset weight
  - overfit_penalty                 # walk-forward IS−OOS gap (None → overfit_unknown)
```
Component weights in `config.portfolio_sim.strategy_lab.scoring`. The leaderboard
ranks by `strategy_score`, **not** ending balance — a strategy that only works
in-sample ranks below a humbler one that survives out-of-sample.

## 5. Walk-forward validation

`walk_forward.py` rolls train→test (default 24→3 months) across history, choosing
params on each train window and evaluating on the next test window. Reports
`oos_mean_excess`, `oos_hit_rate`, `is_oos_gap`, `overfit = max(0, gap)`, and
`still_works_oos`. The `overfit` value feeds the master score's penalty.

## 6. Factor attribution

`factor_attribution.py` regresses each tactic's monthly excess returns on
Fama-French factors (Mkt-RF, SMB, HML, RMW, CMA, MOM) → alpha + betas + R². Answers
"did it beat SPY from real value or just tech/growth overweight?" Factor data
(`factor_data.py`) is read offline from `data/factors/ff_monthly.csv`
(`scripts/fetch_factor_data.sh` populates it). Absent → `factor_data_unavailable`
(degrades, never blocks).

## 7. Artifacts (all `outputs/sandbox/`)

| Artifact | Contents |
|---|---|
| `strategy_leaderboard.json` + `_summary.md` | tactics ranked by strategy_score, per-window metrics, OOS flag |
| `research_strategy_catalog.json` | per-tactic academic_basis + coverage_complete gate |
| `walk_forward_results.json` | OOS validation per parameterized tactic |
| `factor_exposure_report.json` + `factor_attribution_summary.md` | factor betas + alpha |

## 8. Config (`config.json` → `portfolio_sim`)

```jsonc
"portfolio_sim": {
  "enabled": true,                 // suite + lab master switch
  "strategy_lab": {
    "enabled": true,
    "windows": ["trailing_1y","trailing_3y","trailing_5y","ytd"],
    "scoring": { /* optional component-weight overrides */ }
  }
}
```

## 9. Governance & safety

Sandbox-only writes (`OutputNamespace.SANDBOX`, run-mode-gated), observe-only,
no trade verbs, never writes `decision_plan`/`config`/`signal_registry`,
look-ahead safe, default-disabled. Every research tactic carries `academic_basis`
(extends the Strategy Documentation Requirement — every strategy needs an
explanation **and a citation**).

## 10. Health monitoring

- **`/strategy-lab-analysis` skill** + `portfolio_sim/strategy_lab_health.py`
  assessor: GREEN/AMBER/RED on artifact freshness, `coverage_complete`,
  all-degraded (looks-fresh-but-empty), walk-forward ran, factor status, and any
  tactic surfaced with `still_works_oos == false`.
- monthly-tool-analysis (quant lens) reads the leaderboard + walk-forward.

## 11. Known limitations

1. No transaction costs / taxes in P&L yet (tax/turnover penalties use flat proxies,
   flagged `gross_until_cost_model`). Highest-value next improvement.
2. Factor attribution needs the F-F cache (offline; degrades if absent).
3. Mean-variance is closed-form long-only max-Sharpe with cap projection (no optimizer).
4. Regime sims + crowd event studies + ensemble are deferred (documented in the plan).
5. Bootstrap/backtest history is ~5y — underweights rare regimes.

## 12. Pointers

Modules: `portfolio_automation/portfolio_sim/{research_library, strategy_score,
walk_forward, factor_data, factor_attribution, run_strategy_lab, strategy_lab_health}.py`.
Tests: `tests/portfolio_sim/test_{research_library, strategy_score, walk_forward,
factor_attribution, strategy_lab_e2e}.py`. Skill: `.claude/commands/strategy-lab-analysis.md`.
