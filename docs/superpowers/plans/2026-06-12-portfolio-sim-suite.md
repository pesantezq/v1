# Portfolio Simulation Suite — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a sandbox-only, observe-only portfolio-simulation suite: a historical backtest engine over the operator's real portfolio + alternative tactics (sub-project 1), a crowd-signal tactic (sub-project 2), and a forward Monte-Carlo projection engine (sub-project 3), with a strategy-documentation discipline.

**Architecture:** New package `portfolio_automation/portfolio_sim/`. Pure functions; reads the 5y price archive (`outputs/backtest/historical/*_5y.json`) and existing artifacts; writes only to `OutputNamespace.SANDBOX`. Reuses `data_governance`, `run_mode_governance`, `shadow_tracker`, `strategy.profiles`, `historical_replay.replay_data_loader`, `social_intelligence`. Never writes `decision_plan.json`/config/registry; no trade verbs.

**Tech Stack:** Python 3.12, numpy 2.4 (Monte-Carlo), existing FMP client (free), FastAPI/Jinja GUI v2, pytest.

**Invariants stamped in every artifact:** `observe_only: true`, `sandbox_only: true`, `no_trade: true`, `schema_version`, `source`, `run_id`, `created_at`, `warnings`.

**Test runner:** `.venv/bin/python -m pytest` (NOT bare `python`). Restore `config/signal_registry.yaml` if the full suite mutates it.

---

## File Structure

```
portfolio_automation/portfolio_sim/
  __init__.py              # exports run_portfolio_backtest, run_portfolio_projection
  sim_base.py              # observe-only envelope, shared dataclasses, status enum
  universe.py              # resolve_simulable_universe(root) -> dict[ticker, meta]
  prices.py                # load_price_panel(tickers, root) -> PricePanel (date×ticker close)
  metrics.py               # return/vol/max_drawdown/sharpe/sortino; TW + DCA path metrics
  tactics.py               # Tactic + materializers (shadow/profile/benchmark/baseline) + target_weights_asof
  rebalance.py             # RebalancePolicy: buy_and_hold | periodic | config_rules
  backtest_engine.py       # run_backtest(tactic, policy, panel, window, ...) -> BacktestResult
  crowd_tactic.py          # CrowdTactic (time-varying) + proxy pseudo-state mapper
  crowd_forward_track.py   # forward sleeve snapshot + resolution join (ledger)
  projection_engine.py     # Monte-Carlo block-bootstrap projection
  strategy_docs.py         # strategy catalog producer (json + md)
  run_portfolio_backtest.py   # orchestrator (sub-project 1 + 2 proxy)
  run_portfolio_projection.py # orchestrator (sub-project 3)

tests/portfolio_sim/        # one test module per source module + e2e
gui_v2/data/dash_strategy_lab.py  # MODIFY: add backtest+projection sections (or new loader fn)
gui_v2/templates/dashboard/strategy_lab.html  # MODIFY: backtest leaderboard + projection fan
.claude/commands/strategy-catalog.md  # CREATE: /strategy-catalog skill
docs/STRATEGY_CATALOG.md    # auto-generated catalog (created by strategy_docs)
CLAUDE.md                   # MODIFY: add Strategy Documentation Requirement rule
portfolio_automation/artifact_registry.yaml  # MODIFY: register sim artifacts
scripts/run_weekly_safe.sh  # MODIFY: add sim stages (weekly cadence)
scripts/preflight.sh        # MODIFY: compile + import smoke
.claude/commands/monthly-tool-analysis.md  # MODIFY: sim health checks
```

---

# PHASE 1 — Backtest Engine Foundation (sub-project 1)

### Task 1.1: Package skeleton + sim_base

**Files:** Create `portfolio_automation/portfolio_sim/__init__.py`, `sim_base.py`, `tests/portfolio_sim/__init__.py`, `tests/portfolio_sim/test_sim_base.py`

- [ ] **Step 1: Write failing test** (`test_sim_base.py`): assert `sim_envelope(run_id="r", run_mode="discovery")` returns dict with `observe_only is True`, `sandbox_only is True`, `no_trade is True`, `schema_version=="1"`, `source=="portfolio_sim"`, keys `run_id/created_at/warnings`.
- [ ] **Step 2:** Run `…pytest tests/portfolio_sim/test_sim_base.py -v` → FAIL (import error).
- [ ] **Step 3: Implement** `sim_base.py`: constants `OBSERVE_ONLY=True`, `SANDBOX_ONLY=True`, `NO_TRADE=True`, `SCHEMA_VERSION="1"`, `SOURCE="portfolio_sim"`; `class SimStatus(str,Enum)` = ok/insufficient_data/degraded/error/disabled; `utc_now_iso()`; `sim_envelope(*, run_id, run_mode, status="ok", warnings=None, created_at=None)->dict`. Mirror `social_intelligence/base.py:base_envelope`.
- [ ] **Step 4:** Run test → PASS.
- [ ] **Step 5: Commit** `feat(portfolio-sim): package skeleton + observe-only envelope`.

### Task 1.2: Universe resolver

**Files:** Create `universe.py`, `tests/portfolio_sim/test_universe.py`

- [ ] **Step 1: Failing test:** with a tmp_path config holding holdings `[QQQ,GLD]` + a `portfolio_sim.universe.proxy_etfs:[BND,SCHD]`, `resolve_simulable_universe(root)` returns a dict whose keys ⊇ `{QQQ,GLD,BND,SCHD}` and each value has `source ∈ {holding,proxy,universe_list}`. Empty/missing config → at least the holdings (or `{}` gracefully).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement:** read `config.json` `portfolio.holdings` (source=holding), `portfolio_sim.universe.proxy_etfs` (default `["BND","SCHD","USMV","TLT"]`, source=proxy), optional `config/universe_lists.yaml` broad/sector ETFs (source=universe_list, only if `include_universe_lists` flag). Return `{TICKER: {"source":..., "in_holdings":bool}}`. Fail-safe (try/except → holdings only).
- [ ] **Step 4:** PASS. **Step 5: Commit** `feat(portfolio-sim): dynamic simulable universe resolver`.

### Task 1.3: Price panel loader

**Files:** Create `prices.py`, `tests/portfolio_sim/test_prices.py`

- [ ] **Step 1: Failing test:** write two synthetic archive files `tmp/outputs/backtest/historical/AAA_5y.json` and `BBB_5y.json` (list of `{date,close,volume}` oldest-first, overlapping + disjoint dates). `load_price_panel(["AAA","BBB"], root=tmp)` returns a `PricePanel` with: sorted union calendar, per-ticker close aligned (forward-fill ≤5 trading days, NaN/None beyond), `.tickers`, `.dates`, `.close(ticker,date)`, `.monthly_returns()` (month-end pct change matrix). Missing ticker (no archive, no fmp) → recorded in `.missing`.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement:** read `<T>_5y.json` via `OutputNamespace.HISTORICAL` path (`outputs/backtest/historical/`); reuse `historical_replay.replay_data_loader.normalize_prices` shape. Optional FMP fallback param `fmp_client=None` (when provided + archive missing, call `get_historical_prices`); default None → mark missing. Build aligned numpy-friendly panel (use plain dict/list, numpy optional here). `monthly_returns()` resamples to month-end closes → returns matrix + month labels.
- [ ] **Step 4:** PASS. **Step 5: Commit** `feat(portfolio-sim): price panel loader + monthly returns`.

### Task 1.4: Metrics

**Files:** Create `metrics.py`, `tests/portfolio_sim/test_metrics.py`

- [ ] **Step 1: Failing tests** with known series: a constant +1%/period series → known CAGR; a series `[100,110,99,121]` → known max_drawdown (= (99-110)/110); `sharpe(returns, rf=0)` matches mean/std*sqrt(periods); `sortino` uses downside dev; `time_weighted_return(values)` and `dca_path(value_series, monthly_contribution)` produce expected terminal balance on a hand-checked 3-month example.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement:** pure functions `cagr(values, periods_per_year)`, `annual_vol(returns, ppy)`, `max_drawdown(values)`, `sharpe`, `sortino`, `time_weighted_return(values)`, `dca_terminal(value_series_per_contribution_unit, contributions)`. Document formulas in docstrings. Use numpy.
- [ ] **Step 4:** PASS. **Step 5: Commit** `feat(portfolio-sim): return/risk metrics (CAGR/DD/Sharpe/Sortino)`.

### Task 1.5: Tactic interface + materializers

**Files:** Create `tactics.py`, `tests/portfolio_sim/test_tactics.py`

- [ ] **Step 1: Failing tests:** `Tactic` dataclass with `tactic_id,name,source,target_weights,metadata,approximate=False` and method `target_weights_asof(date, ctx)` returning `target_weights` for static tactics. `tactics_from_shadow_portfolios(root)` → ≥6 tactics whose weights sum≈1. `tactics_from_strategy_profiles(root)` → 8 tactics; `defensive` has lower leveraged-ETF (QLD) weight than `aggressive_growth`; `short_term_tactical.approximate is True`; every materialized vector normalized + max weight ≤ concentration_cap (0.60) + leveraged-asset weight ≤ leverage_cap (0.25). `benchmark_tactics()` → SPY/QQQ 100%.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement:**
  - `Tactic` dataclass + `target_weights_asof` default returns static weights.
  - `tactics_from_shadow_portfolios`: import `shadow_tracker.build_shadow_portfolios(root, now_iso)`, wrap each portfolio's `weights` as a Tactic (source="shadow").
  - `tactics_from_strategy_profiles`: for each `strategy.profiles.SEED_PROFILES`, materialize weights from resolved universe via a deterministic `_apply_tilts(profile, universe, base_weights)` (bounded multipliers per §5 table: growth tilt, defensive de-risk, income/bond tilt, boom sleeve cap). Normalize + clamp to caps; record tilt map in `metadata["materialization"]`; set `approximate=True` for `short_term_tactical`.
  - `benchmark_tactics`.
- [ ] **Step 4:** PASS. **Step 5: Commit** `feat(portfolio-sim): Tactic interface + shadow/profile/benchmark materializers`.

### Task 1.6: Rebalance policies

**Files:** Create `rebalance.py`, `tests/portfolio_sim/test_rebalance.py`

- [ ] **Step 1: Failing tests:** `BuyAndHold.apply(holdings_value, target, date, cash_in)` leaves existing shares, routes `cash_in` to cash/pro-rata; after a price move the weights drift (not reset). `Periodic(freq="monthly").due(date, last)` True at month boundary; `.apply` resets to target weights. `ConfigRules(rebalance_rules).apply` only rebalances a position when |weight−target|>band (0.12), prefers cash/contributions before selling.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement:** `class RebalancePolicy` (interface: `due(date,last)->bool`, `apply(holdings_value: dict, target: dict, date, cash_in: float)->dict`). Three subclasses. `config_rules` reads `config.json rebalance_rules`. Keep deterministic.
- [ ] **Step 4:** PASS. **Step 5: Commit** `feat(portfolio-sim): rebalance policies (buy&hold/periodic/config-rules)`.

### Task 1.7: Backtest engine

**Files:** Create `backtest_engine.py`, `tests/portfolio_sim/test_backtest_engine.py`

- [ ] **Step 1: Failing tests** on a tiny 2-ticker synthetic panel (known closes): `run_backtest(tactic, policy, panel, window_years=1, start_value, monthly_contribution)` returns a `BacktestResult` with `metrics` (cagr/max_drawdown/sharpe/sortino/time_weighted_return/final_balance_dca/total_contributed/excess_vs_spy/excess_vs_qqq), a downsampled `value_series`, and `degraded:[...]` for any tactic ticker absent from the panel (dropped + renormalized). Look-ahead guard test: a spy-on engine never reads `panel.close` for a date > the current sim date (assert via a panel wrapper that raises on future access).
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement:** walk the panel's trading calendar within `[end-window, end]`; at t0 set shares from `tactic.target_weights_asof(t0)` × start_value / price; each day mark-to-market; on policy `due()` rebalance via `policy.apply`; inject `monthly_contribution` on month boundaries (DCA path) and run a parallel contribution-neutral growth-of-$1 series for `time_weighted_return`. Compute metrics via `metrics.py`. SPY/QQQ excess from benchmark single-asset runs. Drop+renormalize missing tickers, record in `degraded`.
- [ ] **Step 4:** PASS. **Step 5: Commit** `feat(portfolio-sim): historical backtest engine`.

### Task 1.8: Strategy-docs producer

**Files:** Create `strategy_docs.py`, `tests/portfolio_sim/test_strategy_docs.py`

- [ ] **Step 1: Failing tests:** `build_strategy_catalog(tactics, results, decisions)` → dict with one card per tactic containing `objective, universe, materialization, rebalance_assumptions, caps, metrics_by_window, rationale, explanation`; a tactic with empty `rationale` makes `coverage_complete` False (the rule, under test). `render_strategy_catalog_md(catalog)` contains each tactic name + "observe-only".
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement** the catalog builder + md renderer. `coverage_complete = all(card["rationale"] for card in cards)`.
- [ ] **Step 4:** PASS. **Step 5: Commit** `feat(portfolio-sim): strategy catalog producer (rule mechanism)`.

### Task 1.9: Backtest orchestrator + artifacts

**Files:** Create `run_portfolio_backtest.py`, `tests/portfolio_sim/test_run_backtest_e2e.py`

- [ ] **Step 1: Failing e2e test:** seed tmp `outputs/backtest/historical/{QQQ,GLD,SPY,QQQ}_5y.json` (a few tickers) + a minimal `config.json`; `run_portfolio_backtest(root=tmp, run_mode="discovery")` writes `outputs/sandbox/portfolio_backtest.json` + `portfolio_backtest_summary.md` + `outputs/sandbox/strategy_catalog.json` + `docs/STRATEGY_CATALOG.md`; asserts `observe_only True`, ≥1 tactic result, `decision_plan.json` untouched (write one first, diff after), run-mode `daily` cannot write sandbox (RunModeViolation caught → wrote_files False).
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement:** build tactics (shadow+profiles+benchmarks), resolve universe, load panel, run each tactic × {buy_and_hold, periodic} × {1y,3y,5y}, fill `shadow_portfolios.json:would_have_helped_portfolio` (best-effort), build catalog, write artifacts via `safe_write_json`/`safe_write_text` (SANDBOX) gated by `assert_can_write_namespace`. Never raises. `--root/--run-mode` CLI.
- [ ] **Step 4:** PASS. **Step 5: Commit** `feat(portfolio-sim): backtest orchestrator + sandbox artifacts`.

### Task 1.10: CLAUDE.md rule + /strategy-catalog skill

**Files:** Modify `CLAUDE.md`; Create `.claude/commands/strategy-catalog.md`

- [ ] **Step 1:** Add to `CLAUDE.md` a "## Strategy Documentation Requirement" section (text from spec §9): every Tactic ships a catalog entry; every tunable parameter records rationale; undocumented tactic must not surface in Strategy Lab; `/strategy-catalog` maintains it; doc-audit verifies coverage.
- [ ] **Step 2:** Create `.claude/commands/strategy-catalog.md`: front-matter `description`; body = read `strategy_catalog.json` + latest backtest/projection, regenerate `docs/STRATEGY_CATALOG.md`, write plain-language explanations, route prose findings to `portfolio-doc-writer`; observe-only.
- [ ] **Step 3:** `bash -n` n/a; `.venv/bin/python -c "import yaml"` n/a. Just verify files exist. **Commit** `docs(strategy-docs): add Strategy Documentation rule + /strategy-catalog skill`.

### Task 1.11: GUI Strategy Lab backtest section

**Files:** Modify `gui_v2/data/dash_strategy_lab.py` (or `dash_next_stage.py` — whichever feeds `/dashboard/strategy-lab`), `gui_v2/templates/dashboard/strategy_lab.html`; Create `tests/test_gui_strategy_lab_backtest.py`

- [ ] **Step 1: Failing tests:** loader fn reads `outputs/sandbox/portfolio_backtest.json`; with a populated fixture returns a `backtest` block (leaderboard rows: tactic/policy/window/cagr/max_dd/sharpe/final_balance); with absent artifact returns empty/none without crashing. Route `/dashboard/strategy-lab` renders 200 and contains "Backtest" + no trade verbs.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement** loader addition + template "Backtest" section (table + sparkline from downsampled series). Reuse `shared.card`/`_read_json`.
- [ ] **Step 4:** PASS. **Step 5: Commit** `feat(gui): Strategy Lab backtest leaderboard`.

### Task 1.12: Registry + pipeline wiring + preflight

**Files:** Modify `portfolio_automation/artifact_registry.yaml`, `scripts/run_weekly_safe.sh`, `scripts/preflight.sh`; Create `tests/portfolio_sim/test_registry_rows.py`

- [ ] **Step 1: Failing test:** assert registry contains `portfolio_backtest.json`, `portfolio_backtest_summary.md`, `strategy_catalog.json` with `cadence: weekly`, `role`, valid `lens`, `consumer_status`; `run_artifact_registry(root='.')` schema_invalid==0.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement:** add 3 yaml rows (lens market_discovery/meta_governance, role advisor/telemetry/narrative, weekly). Add a `run_weekly_safe.sh` stage `python -m portfolio_automation.portfolio_sim.run_portfolio_backtest --root "${REPO_ROOT}" --run-mode discovery` (non-blocking, before status stages). Add modules to `preflight.sh` py_compile + import smoke.
- [ ] **Step 4:** PASS + `bash -n scripts/run_weekly_safe.sh scripts/preflight.sh`. **Step 5: Commit** `feat(portfolio-sim): register artifacts + weekly cron wiring + preflight`.

### Task 1.13: Docs

**Files:** Modify `docs/OUTPUT_ARTIFACT_CONTRACTS.md`, `docs/PIPELINE_RUNBOOK.md`; `docs/STRATEGY_CATALOG.md` auto-generated.

- [ ] **Step 1:** Add contract entries for the 3 backtest artifacts (fields per spec §10). Add a PIPELINE_RUNBOOK weekly section for the backtest stage (enable/disable, degraded behavior).
- [ ] **Step 2: Commit** `docs(portfolio-sim): artifact contracts + runbook for backtest engine`.

---

# PHASE 2 — Crowd-Signal Tactic (sub-project 2)

### Task 2.1: Time-varying tactic context

**Files:** Modify `tactics.py`; Modify `backtest_engine.py`; Modify `tests/portfolio_sim/test_tactics.py`

- [ ] **Step 1: Failing test:** a `Tactic` subclass `TimeVaryingTactic` overriding `target_weights_asof(date, ctx)` returns date-dependent weights; the engine calls it at each rebalance date (assert via a stub tactic that records the dates it was asked for).
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement:** ensure `backtest_engine` calls `tactic.target_weights_asof(date, ctx)` at t0 and each rebalance (not just t0); `ctx` carries `panel` + `date`. Add `TimeVaryingTactic` base.
- [ ] **Step 4:** PASS. **Step 5: Commit** `feat(portfolio-sim): time-varying tactic support in engine`.

### Task 2.2: Crowd sleeve + overlay

**Files:** Create `crowd_tactic.py`, `tests/portfolio_sim/test_crowd_tactic.py`

- [ ] **Step 1: Failing tests:** `build_crowd_sleeve(core_weights, crowd_states, caps)`:
  - sleeve total ≤ 0.15, per-idea ≤ 0.05; names from `emerging_dd/crowd_validation/contrarian_neglect` only; weighted by `crowd_research_priority_score` (higher score → larger slice); top-N until filled.
  - avoid-overlay: a name in `hype_acceleration/reflexive_squeeze_risk/crowd_exhaustion` is excluded from the sleeve; a core holding in a caution state gets `×0.8` trim + an `underweight_flags` entry; freed weight to cash.
  - normalized result sums ≈ 1.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement** `build_crowd_sleeve` + `CrowdTactic(TimeVaryingTactic)` whose `target_weights_asof(date, ctx)` reads crowd states (live from `crowd_knowledge_state.json` for "today", or proxy pseudo-states for historical dates — see 2.4). Caps from config boom-bucket.
- [ ] **Step 4:** PASS. **Step 5: Commit** `feat(portfolio-sim): crowd sleeve construction + avoid-caution overlay`.

### Task 2.3: Forward shadow-track

**Files:** Create `crowd_forward_track.py`, `tests/portfolio_sim/test_crowd_forward_track.py`

- [ ] **Step 1: Failing tests:** `snapshot_sleeve(root, now_iso)` appends a record to `outputs/sandbox/discovery/social_signal_history.json` with `{ticker, crowd_state, signal_date, entry_price}` per sleeve name. `resolve_due(root, panel)` joins forward prices at 1/5/20/60d offsets → `raw_returns` + `returns.vs_spy/vs_qqq/vs_self_baseline`, updating resolved records; under-min-sample → `social_signal_backtest.build_social_signal_backtest` reports `insufficient_data`.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement** ledger append + resolution join (reuse `social_signal_backtest.SignalObservation` shape; prices from panel/archive). Idempotent per signal_date.
- [ ] **Step 4:** PASS. **Step 5: Commit** `feat(portfolio-sim): crowd tactic forward shadow-tracking ledger`.

### Task 2.4: Proxy historical backtest

**Files:** Modify `crowd_tactic.py` (add proxy mapper); Create `tests/portfolio_sim/test_crowd_proxy.py`

- [ ] **Step 1: Failing tests:** `proxy_pseudo_state(volume_z, momentum)` returns `emerging_dd` for rising-attention+moderate-momentum, `hype_acceleration` for extreme spike, `crowd_exhaustion` for high-attention+negative-momentum; `dormant_noise` otherwise. A `CrowdTactic` in `proxy=True` mode resolves states from the panel's `(volume_z, momentum)` at each date (data ≤ date only).
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement** `proxy_pseudo_state` + panel-driven proxy state resolution (volume z vs trailing 20d, momentum = trailing return). Look-ahead-safe.
- [ ] **Step 4:** PASS. **Step 5: Commit** `feat(portfolio-sim): labeled volume/momentum proxy pseudo-states`.

### Task 2.5: Crowd artifacts + wiring + catalog + GUI row

**Files:** Modify `run_portfolio_backtest.py` (add crowd tactic + proxy artifact), `artifact_registry.yaml`, `dash_strategy_lab.py`/template, `tests/portfolio_sim/test_crowd_e2e.py`

- [ ] **Step 1: Failing e2e test:** with a populated `crowd_knowledge_state.json` fixture + price panel, the orchestrator writes `outputs/sandbox/crowd_tactic_backtest.json` stamped `proxy: true` + `measures` note; the crowd tactic appears in `portfolio_backtest.json` marked `forward_maturing`; catalog has a crowd-tactic card with rationale; `observe_only True`.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement** orchestrator additions (snapshot forward + run proxy backtest + emit labeled artifact), registry row, Strategy Lab row (marked forward/proxy), catalog entry. Weekly stage already wired (Task 1.12) covers it.
- [ ] **Step 4:** PASS. **Step 5: Commit** `feat(portfolio-sim): crowd tactic artifacts + Strategy Lab + catalog`.

---

# PHASE 3 — Forward Monte-Carlo Projection (sub-project 3)

### Task 3.1: Projection engine

**Files:** Create `projection_engine.py`, `tests/portfolio_sim/test_projection_engine.py`

- [ ] **Step 1: Failing tests:** `project(tactic, monthly_return_matrix, month_labels, horizon_months, n_paths, start_value, monthly_contribution, seed, block=1)` returns `ProjectionResult` with terminal percentiles `p5≤p25≤p50≤p75≤p95`, `prob_reach_target(target)`, `prob_loss`, `max_drawdown_p50/p95`, `cagr_p5/p50/p95`, and a downsampled percentile fan. Same `seed` → identical output (reproducible). `block=3` samples contiguous 3-month spans. Single positive-drift asset → p95 terminal ≥ contributions.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement** numpy block-bootstrap: build per-month portfolio return = weights·R[month]; sample `horizon/block` random blocks per path (seeded `np.random.default_rng(seed)`); compound with monthly contribution injection (DCA) + a contribution-neutral growth-of-$1 series; collect terminal + path matrix; compute percentiles/probabilities. Drop missing tickers + renormalize (record `degraded`).
- [ ] **Step 4:** PASS. **Step 5: Commit** `feat(portfolio-sim): Monte-Carlo block-bootstrap projection engine`.

### Task 3.2: Projection orchestrator + artifacts

**Files:** Create `run_portfolio_projection.py`, `tests/portfolio_sim/test_run_projection_e2e.py`

- [ ] **Step 1: Failing e2e test:** seed archive + config; `run_portfolio_projection(root=tmp, run_mode="discovery")` writes `outputs/sandbox/portfolio_projection.json` (+ `_summary.md`) with `assumptions` block, `seed`, per tactic×horizon distributions, `observe_only True`; `decision_plan.json` untouched; daily run-mode cannot write (caught).
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement** orchestrator: tactics + universe + monthly-return panel; project each tactic over `[1y,5y,10y,full(config horizon)]`; horizons + n_paths(5000) + seed from `config.portfolio_sim.projection`; assumptions block; catalog entry; SANDBOX writes gated. Never raises. CLI.
- [ ] **Step 4:** PASS. **Step 5: Commit** `feat(portfolio-sim): projection orchestrator + sandbox artifacts`.

### Task 3.3: Projection GUI + registry + wiring + docs

**Files:** Modify `dash_strategy_lab.py`/template, `artifact_registry.yaml`, `scripts/run_weekly_safe.sh`, `scripts/preflight.sh`, `docs/OUTPUT_ARTIFACT_CONTRACTS.md`, `.claude/commands/monthly-tool-analysis.md`; Create `tests/test_gui_strategy_lab_projection.py`

- [ ] **Step 1: Failing tests:** loader reads `portfolio_projection.json` → projection block (per-tactic p50 balance, prob_reach_target, p95 maxDD); absent → empty no-crash; route renders 200 with "Projection" + "illustration" disclaimer + no trade verbs. Registry row present + schema_invalid 0.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement** GUI projection section (percentile fan + table), registry row (weekly), `run_weekly_safe.sh` projection stage, preflight imports, contract docs, monthly-tool-analysis sim health check (p50 CAGR plausibility + content-liveness all-degraded).
- [ ] **Step 4:** PASS + `bash -n`. **Step 5: Commit** `feat(portfolio-sim): projection GUI + registry + wiring + docs`.

---

# PHASE 4 — Integration validation

### Task 4.1: Full-suite + invariants

- [ ] **Step 1:** `cp config/signal_registry.yaml /tmp/sr.before`.
- [ ] **Step 2:** Run targeted: `.venv/bin/python -m pytest -q tests/portfolio_sim/ tests/test_gui_strategy_lab_backtest.py tests/test_gui_strategy_lab_projection.py` → all PASS.
- [ ] **Step 3:** Run full suite `.venv/bin/python -m pytest -q -p no:cacheprovider` → only the 3 documented pre-existing failures (test_run_loop oos, 2× test_tuning_proposals) remain; nothing new.
- [ ] **Step 4:** `diff /tmp/sr.before config/signal_registry.yaml` → UNCHANGED. `run_artifact_registry(root='.')` → schema_invalid 0, overall green/amber (no new critical-missing). Live-run each orchestrator (`--run-mode discovery`) → artifacts written, observe_only, no decision_plan mutation.
- [ ] **Step 5: Commit** any doc/state sync: update `.agent/project_state.yaml` + `phase_status.yaml` + `docs/roadmap.md`; regenerate `docs/STRATEGY_CATALOG.md`; `feat(portfolio-sim): integration validation + roadmap/state sync`.

---

## Self-Review (planner)

- **Spec coverage:** SP1 → Tasks 1.1–1.13 (universe/prices/metrics/tactics/rebalance/engine/docs/orchestrator/rule+skill/GUI/registry+wiring/docs). SP2 → 2.1–2.5 (time-varying/sleeve+overlay/forward-track/proxy/artifacts). SP3 → 3.1–3.3 (engine/orchestrator/GUI+wiring). Strategy-docs rule covered (1.8, 1.10, enforced in catalog tests). Cross-cutting (registry/cron/preflight/monthly-analysis/docs) covered per phase + Phase 4.
- **Placeholders:** none — each task has concrete files, test intent, impl outline, exact commit.
- **Type consistency:** `Tactic.target_weights_asof(date, ctx)` defined 1.5, extended 2.1, consumed by engine 1.7/2.1 and projection 3.1; `BacktestResult`/`ProjectionResult`/`PricePanel`/`RebalancePolicy` names consistent across tasks; `sim_envelope`/`SimStatus` from 1.1 used by all orchestrators.
- **Cadence/health coverage:** weekly cron (1.12, 3.3) + monthly-tool-analysis checks (3.3) + content-liveness — satisfies CLAUDE.md Analysis+Health requirement.
- **Governance:** every orchestrator gated by `assert_can_write_namespace(SANDBOX)`, observe_only stamped, no-mutation invariant tested (1.9, 3.2), no trade verbs (GUI tests).
