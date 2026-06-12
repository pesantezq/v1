# Research-Backed Strategy Lab — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development
> or superpowers:executing-plans. Steps use `- [ ]`. Spec:
> `docs/superpowers/specs/2026-06-12-research-backed-strategy-lab-design.md`.

**Goal:** Add a research-backed strategy lab on top of the Portfolio Simulation
Suite — academic strategy families as tactics, factor attribution, walk-forward
OOS validation, regime sims, vol-managed + Black-Litterman tactics, crowd event
studies — ranked by an after-cost, risk-adjusted master score vs SPY.

**Architecture:** New modules under `portfolio_automation/portfolio_sim/`, reusing
the existing Tactic/engine/metrics/windows/rebalance/prices/projection/crowd
infrastructure. Sandbox-only, observe-only, default-disabled. New orchestrator
`run_strategy_lab.py`.

**Tech stack:** Python 3.12, numpy (regression/optimization — no new deps),
Fama-French factor CSVs cached to `data/factors/`, existing FMP/archive, pytest.

**Invariants:** observe_only/sandbox_only/no_trade stamped; never writes
decision_plan/config/registry; look-ahead safe; run-mode-gated; every tactic
carries `academic_basis`.

**Test runner:** `.venv/bin/python -m pytest`. Restore `config/signal_registry.yaml`
after the full suite if mutated.

---

## PHASE A — Research Strategy Library + Master Score + Leaderboard

### Task A1: Research tactic library
**Files:** Create `portfolio_sim/research_library.py`, `tests/portfolio_sim/test_research_library.py`
- [ ] Test: `research_tactics(root)` returns tactics incl. `sixty_forty`,
  `risk_parity_lite`, `momentum_rotation`, `dual_momentum`, `mean_variance_frontier`,
  `factor_tilt`; each has non-empty `metadata["academic_basis"]`; weights normalized
  + cap-clamped (reuse `tactics._clamp_caps`).
- [ ] Test: `momentum_rotation` is a `TimeVaryingTactic` — at a date it holds the
  top-N tickers by trailing-K-month return computed from data ≤ date (look-ahead safe).
- [ ] Test: `mean_variance_frontier` produces max-Sharpe-ish weights from trailing
  mean/cov, clamped to caps (closed-form, numpy; degrade to equal-weight if singular).
- [ ] Implement the materializers (static ones return fixed vectors; momentum/
  dual-momentum/mean-variance are `TimeVaryingTactic` using `ctx["panel"]`). Record
  `academic_basis` (citation + claim) + `params` in metadata.
- [ ] Run tests → PASS. Commit `feat(strategy-lab): research strategy tactic library`.

### Task A2: Expanded risk metrics
**Files:** Modify `portfolio_sim/metrics.py`, `tests/portfolio_sim/test_metrics.py`
- [ ] Tests for `time_underwater`, `worst_window_return(values, k)`,
  `expected_shortfall(returns, q)`, `prob_beat(series_a, series_b)` on hand-checked
  inputs.
- [ ] Implement; keep pure/numpy. Commit `feat(strategy-lab): expanded risk metrics`.

### Task A3: Master strategy score
**Files:** Create `portfolio_sim/strategy_score.py`, `tests/portfolio_sim/test_strategy_score.py`
- [ ] Test: `score(components, weights)` = weighted sum of normalized bonuses minus
  penalties; higher excess/consistency → higher score; higher overfit/turnover/tax/
  concentration/leverage → lower. Missing `overfit_penalty` (SP-B not run) → treated
  as 0 + flagged `overfit_unknown`.
- [ ] Implement + a `rank(tactic_results)` helper. Config weights from
  `config.portfolio_sim.scoring` (documented defaults). Commit
  `feat(strategy-lab): master strategy score`.

### Task A4: Strategy-lab orchestrator + expanded dimensions + leaderboard
**Files:** Create `portfolio_sim/run_strategy_lab.py`, `tests/portfolio_sim/test_strategy_lab_e2e.py`
- [ ] e2e test (seed archive + config, enabled): writes
  `outputs/sandbox/strategy_leaderboard.json` + `_summary.md` +
  `research_strategy_catalog.json`; leaderboard ranked by `strategy_score`; every
  card has `academic_basis`; `observe_only True`; `decision_plan.json` untouched;
  daily run-mode cannot write.
- [ ] Implement: build suite tactics + research tactics; run each ×
  rebalance-frequency × contribution-scenario × window via `run_backtest`; compute
  metrics + master score; emit leaderboard + extended catalog. Gated by
  `assert_can_write_namespace`. Config `portfolio_sim.strategy_lab.enabled`.
- [ ] Commit `feat(strategy-lab): orchestrator + master leaderboard + expanded sims`.

---

## PHASE B — Walk-Forward Validation

### Task B1: Walk-forward engine
**Files:** Create `portfolio_sim/walk_forward.py`, `tests/portfolio_sim/test_walk_forward.py`
- [ ] Test: `walk_forward(tactic, panel, train_test=[(12,1),(24,3),(36,6)])` chooses
  params on each train window, evaluates on the next test window, rolls forward;
  returns OOS mean excess-vs-SPY, OOS hit-rate, `is_oos_gap`, `still_works_oos`,
  sample size. Param choice uses ONLY train-window data (look-ahead guard test).
- [ ] Test: a non-parameterized tactic returns `status: no_params`.
- [ ] Implement (reuse `run_backtest` per window; param grid from tactic metadata).
  Commit `feat(strategy-lab): walk-forward OOS validation engine`.

### Task B2: Wire overfit penalty + artifact
**Files:** Modify `run_strategy_lab.py`, `strategy_score.py`; Create `tests/portfolio_sim/test_walk_forward_e2e.py`
- [ ] e2e: orchestrator writes `walk_forward_results.json`; each parameterized
  tactic's `overfit_penalty` flows into `strategy_score`; `still_works_oos==false`
  tactics ranked down + flagged.
- [ ] Commit `feat(strategy-lab): walk-forward artifact + overfit penalty in score`.

---

## PHASE C — Factor Attribution

### Task C1: Factor data loader (offline-first)
**Files:** Create `portfolio_sim/factor_data.py`, `scripts/fetch_factor_data.sh`, `tests/portfolio_sim/test_factor_data.py`
- [ ] Test: `load_factors(root)` reads cached `data/factors/ff_monthly.csv` →
  {month: {Mkt-RF, SMB, HML, RMW, CMA, MOM, RF}}; absent cache → `{}` (no crash).
- [ ] Implement loader + a fetch script (Kenneth French library URLs) that writes
  the cache. Loader never fetches at runtime. Commit
  `feat(strategy-lab): Fama-French factor data loader + fetch script`.

### Task C2: Factor regression + report
**Files:** Create `portfolio_sim/factor_attribution.py`, `tests/portfolio_sim/test_factor_attribution.py`
- [ ] Test: `attribute(tactic_monthly_excess, factors)` → betas + alpha + R² via
  numpy lstsq on a synthetic factor set with a known loading. Absent factors →
  `status: factor_data_unavailable`.
- [ ] Test: a pure-SPY tactic loads ~1.0 on Mkt-RF, ~0 alpha.
- [ ] Implement + `factor_exposure_report.json` / `factor_attribution_summary.md`
  builders. Wire into `run_strategy_lab`. Commit
  `feat(strategy-lab): factor attribution (Fama-French regression) + report`.

---

## PHASE D — Regime-Specific Simulations

### Task D1: Regime classification + per-regime sim
**Files:** Create `portfolio_sim/regime_sim.py`, `tests/portfolio_sim/test_regime_sim.py`
- [ ] Test: `classify_months(panel)` labels each month bull/bear/sideways +
  high/low-vol from SPY trailing return + `vol_regime_advisor.realized_vol_annualised`.
- [ ] Test: `regime_excess(tactic, panel, regimes)` → per-regime excess-vs-SPY;
  a tactic strong in bull / weak in bear shows the split.
- [ ] Implement (reuse `market_regime`/`vol_regime_advisor`); emit
  `regime_performance_sim.json`; feed the consistency bonus. Commit
  `feat(strategy-lab): regime-specific simulations`.

---

## PHASE E — Vol-Managed Overlay + Black-Litterman

### Task E1: Volatility-managed tactic
**Files:** Create `portfolio_sim/vol_managed.py`, `tests/portfolio_sim/test_vol_managed.py`
- [ ] Test: `VolManagedTactic.target_weights_asof(date, ctx)` cuts the leverage
  sleeve when trailing realized vol > threshold and restores it when vol normalizes
  (look-ahead safe; data ≤ date). Academic_basis = Moreira & Muir 2017.
- [ ] Implement (reuse `realized_vol_annualised`). Commit
  `feat(strategy-lab): volatility-managed overlay tactic`.

### Task E2: Black-Litterman confidence blend
**Files:** Create `portfolio_sim/black_litterman.py`, `tests/portfolio_sim/test_black_litterman.py`
- [ ] Test: `bl_blend(market_prior, views, confidence)` returns weights between the
  prior and the view-tilted target, scaled by confidence (confidence 0 → prior;
  confidence cap respected); never produces an extreme/concentrated vector.
- [ ] Implement a simplified Idzorek-style confidence blend (numpy). Wire a
  `BlackLittermanTactic` taking crowd/news/factor views. Commit
  `feat(strategy-lab): Black-Litterman confidence-blend tactic`.

---

## PHASE F — Crowd Signal Event Studies

### Task F1: Event-study engine
**Files:** Create `portfolio_sim/crowd_event_study.py`, `tests/portfolio_sim/test_crowd_event_study.py`
- [ ] Test: `event_study(events, panel, pre, post)` aligns forward/backward returns
  around each event date (T-20..T+60); separates Emerging-DD vs Hype-Acceleration
  cohorts; reports mean cumulative abnormal return vs SPY per cohort + sample size.
- [ ] Implement (extends `crowd_forward_track`); emit `crowd_event_study.json` with
  the caution framing (Oxford WSB signal vs the risk-raising finding). Commit
  `feat(strategy-lab): crowd signal event studies`.

---

## PHASE G — GUI + wiring + docs

### Task G1: GUI master leaderboard + factor/OOS panels
**Files:** Modify `gui_v2/data/dash_next_stage.py`, `gui_v2/templates/dashboard/strategy_lab.html`; Create `tests/test_gui_strategy_lab_research.py`
- [ ] Test: loader reads `strategy_leaderboard.json` → ranked rows (score, excess,
  drawdown, consistency, evidence-quality, OOS badge); absent → empty no-crash;
  route renders 200, shows academic-basis + OOS badge, no trade verbs.
- [ ] Implement leaderboard table + factor-exposure panel + regime breakdown.
  Commit `feat(gui): research strategy-lab master leaderboard + factor/OOS panels`.

### Task G2: Registry + cron + monthly-analysis + docs + catalog rule
**Files:** Modify `artifact_registry.yaml`, `scripts/run_weekly_safe.sh`,
`scripts/preflight.sh`, `.claude/commands/monthly-tool-analysis.md`,
`.claude/commands/strategy-catalog.md`, `docs/OUTPUT_ARTIFACT_CONTRACTS.md`,
`docs/PIPELINE_RUNBOOK.md`, `docs/PORTFOLIO_SIM_SUITE.md`, `docs/roadmap.md`,
`.agent/{project_state,phase_status}.yaml`
- [ ] Register the 6 new artifacts (weekly). Add a `run_weekly_safe.sh`
  `run_strategy_lab` stage (non-blocking). Add modules to preflight. Extend
  monthly-tool-analysis (leaderboard top + OOS + factor alpha + content-liveness).
  Extend `/strategy-catalog` to verify `academic_basis` coverage. Docs + roadmap +
  state sync. Test: registry schema_invalid 0. Commit
  `feat(strategy-lab): register artifacts + cron + monthly-analysis + docs`.

---

## PHASE H — Integration validation
- [ ] `cp config/signal_registry.yaml /tmp/sr.before`.
- [ ] `.venv/bin/python -m pytest -q tests/portfolio_sim/ tests/test_gui_strategy_lab_*.py` → PASS.
- [ ] Full suite → only the 3 documented pre-existing failures.
- [ ] `diff` registry UNCHANGED; `run_artifact_registry` schema_invalid 0; live-run
  `run_strategy_lab --run-mode discovery` → artifacts, observe_only, no mutation.
- [ ] Regenerate `docs/STRATEGY_CATALOG.md`; commit state/doc sync.

---

## Self-Review (planner)
- **Spec coverage:** SP-A→A1-A4, SP-B→B1-B2, SP-C→C1-C2, SP-D→D1, SP-E→E1-E2,
  SP-F→F1, GUI/wiring→G1-G2, validation→H. Master score (A3) consumes overfit (B2),
  consistency (D1), factor (C2 informs research_support). Academic_basis enforced
  in A1 + catalog rule (G2).
- **Placeholders:** none — each task names files, test intent, impl approach, commit.
- **Type consistency:** reuses `Tactic`/`TimeVaryingTactic`/`PricePanel`/
  `run_backtest`/`sim_envelope` from the suite; new types `WalkForwardResult`,
  `FactorAttribution`, `StrategyScore` defined in their tasks and consumed by the
  orchestrator (A4) + GUI (G1) consistently.
- **Governance:** every orchestrator gated; observe_only; no-mutation tested (A4,H);
  no trade verbs (G1); default-disabled; Fama-French offline + degrade.
- **Sequencing:** A→B→C is the operator's highest-value trio; D/E/F independent;
  G/H last. Each phase ships working software.
