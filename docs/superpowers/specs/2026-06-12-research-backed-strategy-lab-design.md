# Design — Research-Backed Strategy Lab (Sandbox Sub-Suite)

> Status: design (planned 2026-06-12 from operator vision). Sandbox-only ·
> observe-only · no auto-trading. **Extends** the Portfolio Simulation Suite
> (`docs/PORTFOLIO_SIM_SUITE.md`); reuses its Tactic interface, engines, metrics,
> windows, rebalance policies, prices, strategy catalog, and GUI tab.

## 1. Thesis

Turn the sim suite from "cool backtests" into a legitimate decision-support lab:
**take proven strategy families from academic finance research, express each as a
sandbox Tactic, backtest them against the operator's real portfolio, then rank by
after-cost, risk-adjusted excess return vs SPY — with factor attribution and
walk-forward out-of-sample validation so wins are explained and overfitting is
penalized.** Not guaranteed profit; the goal is to reduce guessing and curve-fitting.

Every strategy must declare: academic basis, test period, benchmark, risk-adjusted
return, factor exposure, overfit risk, and whether it still works out-of-sample.

## 2. What it reuses (do NOT rebuild)

`portfolio_sim/`: `Tactic`/`TimeVaryingTactic`, `tactics.py` materializers,
`backtest_engine.run_backtest`, `metrics.py`, `windows.py`, `rebalance.py`,
`prices.PricePanel` (+ `monthly_returns`), `projection_engine`, `crowd_tactic` +
`crowd_forward_track`, `strategy_docs`, the Strategy Lab GUI tab.
Repo: `market_regime.py` / `vol_regime_advisor.py` (`realized_vol_annualised`,
`classify_regime`), `outputs/regime/regime_performance.json`.

## 3. Hard boundaries (unchanged from the suite)

Sandbox-only writes (`OutputNamespace.SANDBOX`), observe-only, default-disabled,
no trade verbs, never writes `decision_plan`/`config`/`signal_registry`,
run-mode-gated, look-ahead safe. Black-Litterman / vol-managed / crowd tactics
produce *sandbox weight vectors for simulation only* — never real allocation changes.

## 4. Decomposition (sub-projects → phases)

Highest-value first (operator's stated priority = **A + B + C**):

- **SP-A — Research Strategy Library + Master Score + Leaderboard** (foundation)
- **SP-B — Walk-Forward Validation** (OOS framework + overfit penalty)
- **SP-C — Factor Attribution** (Fama-French regression: explain WHY a tactic won)
- **SP-D — Regime-Specific Simulations**
- **SP-E — Volatility-Managed Overlay + Black-Litterman Confidence Blend**
- **SP-F — Crowd Signal Event Studies**

Each is its own spec→plan→build cycle; this doc covers all six at design depth and
sequences them. Expanded contribution / rebalance / risk dimensions fold into SP-A's
orchestrator (they're new loops + metrics, not new subsystems).

---

## 5. SP-A — Research Strategy Library

New `portfolio_sim/research_library.py`: a catalog of research strategy families,
each materialized to a Tactic with an `academic_basis` (citation + one-line claim).

| # | Tactic | Academic basis | Materialization |
|---|---|---|---|
| 1 | actual_baseline / target | (operator) | reuse shadow tactics |
| 2 | spy_only / qqq_only | benchmark | reuse benchmark tactics |
| 3 | sixty_forty | classic | 60% SPY / 40% BND |
| 4 | risk_parity_lite | risk budgeting | inverse-vol weights across equity/gold/bond/tech (trailing vol) |
| 5 | momentum_rotation | Jegadeesh & Titman 1993 | hold top-N ETFs by trailing 3/6/12m return (param) |
| 6 | dual_momentum | Antonacci | risk-on asset if its abs+rel momentum positive, else defensive |
| 7 | mean_variance_frontier | Markowitz 1952 | constrained max-Sharpe weights from trailing mean/cov (capped) |
| 8 | factor_tilt | Fama-French 1993 | tilt to quality/value/profitability proxy ETFs |
| 9 | vol_managed_qld | Moreira & Muir 2017 | (SP-E) vol-target the QLD leverage sleeve |
| 10 | black_litterman_blend | Black-Litterman / Idzorek | (SP-E) market prior + confidence-weighted views |
| 11–14 | crowd sleeves/overlays | (SP-F) | reuse crowd_tactic variants |
| 15 | ensemble_top3 | model averaging | equal-weight blend of the top-3 tactics by master score |

Parameterized tactics (momentum lookback, top-N, vol threshold, mean-variance
risk-aversion) expose params so SP-B can choose them on a train window.

**Master strategy score** (`portfolio_sim/strategy_score.py`):
```
strategy_score =
    excess_return_vs_spy
  + probability_beat_spy_bonus
  + drawdown_control_bonus
  + consistency_bonus            # stability of excess across windows/regimes
  + research_support_bonus       # has academic_basis
  - turnover_penalty
  - tax_drag_penalty             # from cost model (suite improvement #1)
  - concentration_penalty
  - leverage_penalty
  - overfit_penalty              # IS−OOS gap from SP-B
```
Each component normalized; weights configurable in `config.portfolio_sim.scoring`.
Leaderboard ranks by `strategy_score`, not ending balance. Artifact:
`outputs/sandbox/strategy_leaderboard.json` + `_summary.md`.

**Expanded simulation dimensions** (config-driven loops in the orchestrator):
contribution scenarios (300/500/1000/2000, biweekly, buy-the-dip, vol-scaled,
cash-first); rebalance frequencies (monthly/quarterly/semiannual/annual + ±5/7/10/12%
threshold + cash-only + tax-aware); risk metrics (time-underwater, worst 1/3/12m,
expected shortfall, tail loss, P(beat SPY), P(reach goal)) added to `metrics.py`.

## 6. SP-B — Walk-Forward Validation

New `portfolio_sim/walk_forward.py`. For each parameterized tactic:
```
for (train_months, test_months) in [(12,1),(24,3),(36,6)]:
    roll: choose params on train window → evaluate on next test window → record
aggregate OOS test results; compute IS vs OOS excess-vs-SPY gap → overfit_score
```
Output `walk_forward_results.json`: per tactic, OOS mean excess, OOS hit-rate,
IS−OOS degradation, `still_works_oos` flag, sample size. Feeds `overfit_penalty`
in the master score. Non-parameterized tactics report "n/a (no params to fit)".
Look-ahead safe (params chosen only from train ≤ test start).

## 7. SP-C — Factor Attribution

New `portfolio_sim/factor_attribution.py`. Regress each tactic's monthly excess
returns on factor returns (Mkt-RF, SMB, HML, RMW, CMA, + MOM) → factor betas +
alpha + R². Answers: "did it beat SPY from true tactic value or just tech/growth
overweight?" Output `factor_exposure_report.json` + `factor_attribution_summary.md`.

**Data dependency (the only external one):** Kenneth French monthly factor returns.
Plan: a one-shot fetch (`scripts/fetch_factor_data.sh` or a `data_fetch` helper)
caches CSVs to `data/factors/` (offline thereafter). `factor_attribution` degrades
to `status: factor_data_unavailable` if the cache is absent — never blocks. Numpy
least-squares regression (no new dependency).

## 8. SP-D — Regime-Specific Simulations

New `portfolio_sim/regime_sim.py` (reuses `market_regime`/`vol_regime_advisor`).
Classify each historical month into regimes (bull/bear/sideways, high/low-vol, and
— where macro series available — rising/falling-rate, inflation-shock, tech-led,
gold-led risk-off, meme-mania). Re-run each tactic restricted to each regime's
months → per-regime excess-vs-SPY. Output `regime_performance_sim.json`. Surfaces
"beats SPY in bull, fails in drawdown" — feeds the consistency bonus.

## 9. SP-E — Vol-Managed Overlay + Black-Litterman

- `portfolio_sim/vol_managed.py` — a `TimeVaryingTactic`: when trailing realized
  vol > threshold, scale the leverage sleeve (QLD) down and bonds/gold/cash up;
  restore gradually as vol normalizes (Moreira & Muir). Reuses
  `vol_regime_advisor.realized_vol_annualised`.
- `portfolio_sim/black_litterman.py` — base = market/target prior; views from
  crowd/news/factor signals with explicit confidence (0–1); output a *small*
  confidence-scaled tilt (Idzorek). Fits the observe-only system: signals become
  bounded views, never direct allocation changes. Confidence cap in config.

## 10. SP-F — Crowd Signal Event Studies

New `portfolio_sim/crowd_event_study.py` (extends `crowd_forward_track`). Event
studies around crowd-state spikes: T-20..T+60 around mention-velocity spike,
T-5..T+20 around DD spike; Emerging-DD vs Hype-Acceleration; Crowd-Validation vs
Known-News-Echo; Crowd-Exhaustion reversal; Contrarian-Neglect forward return.
Honest framing: real data accrues forward (proxy for history). Cite Oxford WSB
(Granger-causal forum signal) AND the caution finding (WSB attention raised risk,
lowered holding-period return) — which is exactly why the classifier separates
Emerging-DD from Hype-Acceleration. Output `crowd_event_study.json`.

## 11. GUI

Extend the Strategy Lab tab: a **master leaderboard** ranked by `strategy_score`
with columns Return / Risk-adjusted / Drawdown / Consistency / Excess-vs-SPY /
Evidence-quality / Implementation-risk; a factor-exposure panel; an OOS / overfit
badge per tactic; a regime breakdown. Reuses the existing loader + macro library.

## 12. Artifacts (all SANDBOX)

`research_strategy_catalog.json` (extends strategy_catalog with academic_basis /
overfit_risk / oos_status), `strategy_leaderboard.json` + `_summary.md`,
`factor_exposure_report.json` + `factor_attribution_summary.md`,
`walk_forward_results.json`, `regime_performance_sim.json`, `crowd_event_study.json`.

## 13. Governance & documentation

Every research tactic carries an `academic_basis` field (extends the Strategy
Documentation Requirement — now: every strategy needs an explanation **and a
citation**). Observe-only/sandbox-only/no-trade unchanged. Default-disabled
(`config.portfolio_sim.strategy_lab.enabled`). Weekly cadence (own stage) +
on-demand. Health: monthly-tool-analysis quant lens reads the leaderboard +
walk-forward (flag any tactic surfaced with `still_works_oos == false` or missing
`academic_basis`).

## 14. Cadence + health coverage (CLAUDE.md requirement)

New artifacts registered (weekly). monthly-tool-analysis: leaderboard top tactic +
its OOS status + factor alpha; content-liveness (lab ran but zero tactics scored).
`/strategy-catalog` extended to verify `academic_basis` coverage.

## 15. Dependencies / risks

- **Fama-French data** is the one external need — cached offline, degrades gracefully.
- **Survivorship / look-ahead** — strictly enforced in walk-forward + parameter choice.
- **Overfitting** — the whole point of SP-B; the master score penalizes IS−OOS gap.
- **After-cost realism** depends on the suite's cost/tax model (improvement #1) — if
  not yet built, `tax_drag_penalty`/`turnover_penalty` use a documented flat estimate
  and are flagged `gross_until_cost_model`.

## 16. Decomposition / sequencing (the plan)

1. **SP-A** Research Strategy Library + master score + leaderboard + expanded sims.
2. **SP-B** Walk-forward validation (feeds overfit penalty).
3. **SP-C** Factor attribution (Fama-French).
4. **SP-D** Regime sims · **SP-E** vol-managed + Black-Litterman · **SP-F** crowd event studies.
5. GUI master leaderboard + factor panel; registry + cron + monthly-analysis wiring.

## 17. Roadmap placement (operator-specified)

```
portfolio_sim_suite
→ portfolio_sim_suite_production_readiness   (cost/tax model, archive backfill, real start_value)
→ research_backed_strategy_lab               (SP-A + SP-C)
→ walk_forward_strategy_validation           (SP-B)
→ sandbox_strategy_leaderboard               (master score + GUI)
→ optional_weekly_strategy_lab_enablement
```

## 18. Open decisions (resolve at build time; sensible defaults chosen)

- Factor data: fetch from Kenneth French library to `data/factors/` (default) vs
  ship a small cached fixture. **Default: fetch script + cache, degrade if absent.**
- Mean-variance solver: closed-form max-Sharpe with cap projection (default) vs an
  optimizer dependency. **Default: numpy closed-form + clamp (no new dep).**
- Regime macro series (rates/inflation): use only price-derivable regimes first
  (bull/bear/sideways/high-low-vol); add macro regimes when a data source is wired.
