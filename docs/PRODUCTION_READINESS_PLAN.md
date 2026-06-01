# Production-Readiness Plan — Quant & Engineering

**Date:** 2026-06-01 · **Author scope:** advisory analysis + a runnable POC.
**Boundaries:** This plan and its POC are **additive and observe-only**. They do
**not** modify or override the protected scoring, decision, or allocation logic
(`decision_engine.py`, `scoring.py`, `allocation_engine.py`, the six protected
scores), introduce broker/execution behavior, or write to the live namespace.
Anything here that *would* touch protected logic is called out as
"requires owner approval."

---

## 1. Where the system stands

The roadmap state (`.agent/project_state.yaml`) puts the project at
`next_official_step: observe_and_iterate` — i.e., feature-complete enough to run
daily and accumulate outcome history. The pieces a production-grade quant system
needs are largely present: a deterministic decision engine, a signal catalogue
(`config/signal_registry.yaml`), an outcome tracker, a confidence-calibration
layer, an offline backtester (`backtesting/fmp_backtester.py`), and strict
output governance.

What separates "feature-complete" from "production-ready" here is **evidence and
hardening**: proving the signals/patterns carry a real edge out-of-sample, doing
it with statistical rigor, and closing the engineering gaps (test coverage,
silent-failure handling, the read-only-ops end state) identified in
[`TECH_DEBT_AUDIT.md`](TECH_DEBT_AUDIT.md). This plan addresses both lenses and
ships a POC that demonstrates the measurement layer.

---

## 2. Quant lens — proving signal & pattern efficacy

### 2.1 The metrics that matter

The existing backtester reports hit-rate, average forward return, win/loss
ratio, max single-signal drawdown, and a calibration slope. To reach a
production-grade evidence bar, layer on:

| Metric | Why it matters | Status |
|---|---|---|
| Hit rate, avg return, win/loss | Baseline efficacy | In `FMPBacktester` |
| **Confidence calibration slope** | Does higher confidence → better outcome? | In `FMPBacktester` |
| **Sharpe-like / risk-adjusted return** | Return per unit of variability, not raw return | Added in the POC |
| **Edge vs. random-entry baseline** | Is the signal better than a dart-throw? | Added in the POC |
| **Per-pattern efficacy** | Which signal types actually work? | Added in the POC |
| Sortino / downside deviation | Penalize downside, not upside, variance | Recommended next |
| Information ratio vs. benchmark | Edge net of a market/sector benchmark | Recommended next |
| Regime-conditioned performance | Do signals hold in drawdown vs. normal regimes? | Recommended next |
| Signal decay / half-life | How fast does the edge erode after the signal? | Recommended next |

### 2.2 Statistical rigor (the part that's easy to get wrong)

- **Out-of-sample / walk-forward.** Never tune and evaluate on the same window.
  Use rolling train/validate splits; report only out-of-sample numbers.
- **Sample size & significance.** A 60% hit rate on 25 signals is noise. Attach
  counts and confidence intervals; suppress conclusions below a minimum N.
- **Multiple-comparisons discipline.** Testing many signal types/parameters
  inflates false positives; correct for it (or hold out a final test set).
- **Look-ahead & survivorship bias.** Only use data available at signal time;
  use a point-in-time universe, not today's survivors.
- **Costs & slippage.** Even advisory output should model realistic frictions so
  a "paper" edge isn't an artifact of zero-cost assumptions.
- **A baseline always.** Every efficacy claim is reported against a control
  (random entry, buy-and-hold, or a benchmark). The POC ships this as
  `edge_vs_random_baseline_pct`.

### 2.3 What the POC already demonstrates

The included harness (section 4) computes risk-adjusted return, the
edge-vs-baseline control, per-pattern efficacy, and reuses the engine's
calibration metric — and it includes a **pure-noise control** (`--edge 0.0`) so
you can see the metrics correctly report "no edge" when none exists. That noise
control is the single most important habit for not fooling yourself.

---

## 3. Engineering lens — path to production

Drawing on [`TECH_DEBT_AUDIT.md`](TECH_DEBT_AUDIT.md):

- **Close the test-coverage gaps** on the largest untested modules
  (`gui_operator_data.py` first), then add a coverage gate to CI.
- **Tame silent failures.** Classify the 562 `except Exception` sites into
  intentional pipeline guards (must log a reason) vs. silent swallows (narrow or
  log). Silent zeros are the enemy of a learning loop.
- **Verify the data layer.** The 13 SQLite tables are empty in this checkout and
  there are no live artifacts — run the pipeline once and confirm producers wire
  up before trusting any historical aggregate.
- **Determinism & reproducibility.** Seed every stochastic step (the POC does),
  pin dependencies, and make runs idempotent so a backtest is repeatable to the
  byte.
- **Observability.** Emit structured logs/metrics for run health, FMP budget,
  data freshness, and "looks-fresh-but-empty" detection; alert on anomalies.
- **Reach the `read_only_ops` end state** on the VPS (see `CLAUDE.md` /
  `CLAUDE_VPS_MODES.md`): once the advisory layers are stable, lock the
  production filesystem so the cron pipeline is ground truth.
- **Respect FMP compliance & budgets** for any new endpoint work; keep
  backtests on the approved stable historical endpoint.

---

## 4. The delivered POC simulation harness

**File:** `backtesting/poc_simulation_harness.py` ·
**Test:** `tests/test_poc_simulation_harness.py` (5 tests, fully offline).

It builds on `FMPBacktester` and adds the risk-adjusted, baseline, and
per-pattern metrics above. It runs **fully offline by default** via a
deterministic synthetic price provider duck-typed to `FMPClient` (no network, no
API keys), and writes results to the **HISTORICAL** namespace
(`outputs/backtest/poc_simulation_results.{json,md}`) through the governed safe
writers, with `observe_only: true` hardcoded.

```bash
# offline (default) — reproducible, no keys
python -m backtesting.poc_simulation_harness
python -m backtesting.poc_simulation_harness --seed 7 --signals 250
python -m backtesting.poc_simulation_harness --edge 0.0     # pure-noise control
python -m backtesting.poc_simulation_harness --live          # real FMPClient
```

**Illustrative offline output (synthetic data; not the live strategy):** with an
embedded edge (`--edge 0.7`, seed 42), the harness reports ~58% hit-rate, a
positive risk-adjusted ratio, a small positive edge vs. the random baseline, and
a **clearly positive calibration slope (~+4)** — and `STRONG_MOVE_UP` is the
strongest pattern in that synthetic run. Re-run with `--edge 0.0` and the
calibration slope collapses (goes negative) while the confidence-independent
metrics stay flat — exactly what a correct measurement layer should show when
confidence carries no information. **These numbers describe generated data; the
point is the measurement, not the result.**

### How to evolve it toward production evidence

1. **Feed it real signals.** Point it at `outputs/latest/watchlist_signals.json`
   (read-only) once the pipeline has been run, instead of synthetic signals.
2. **Add walk-forward windows** and report only out-of-sample numbers.
3. **Add regime tagging** (reuse the drawdown/regime classifier) and break every
   metric down by regime.
4. **Add Sortino + information-ratio-vs-benchmark** alongside the Sharpe-like
   proxy.
5. **Replace the per-trade Sharpe proxy** with a proper portfolio-level series
   once position sizing is simulated (observe-only replay).

---

## 5. A simulation-testing strategy (five complementary harnesses)

| # | Harness | Question it answers | Builds on |
|---|---|---|---|
| A | **Historical backtest** | Did signals have a forward edge, out-of-sample, by regime? | `FMPBacktester` + this POC |
| B | **Monte-Carlo / synthetic stress** | How do decisions behave across many simulated regimes? | the POC's synthetic engine |
| C | **Calibration tracking** | Is confidence honest over time? | `confidence_calibration` + POC slope |
| D | **Pattern-efficacy** | Which `signal_registry.yaml` patterns actually work? | POC per-pattern breakdown |
| E | **Decision↔outcome replay** | Does the decision plan's *ranking* predict realized outcomes? | `decision_outcome_tracker` (read-only) |

All five are **observe-only**: they replay or simulate against the engine's
public outputs and never mutate scoring/decision logic. Replay/backtest writes
go to the HISTORICAL namespace only.

---

## 6. Phased roadmap

**Phase 1 — Evidence (quant).** Wire the POC to real signals; add walk-forward,
regime buckets, Sortino/IR, and significance/min-N gating. Establish the
out-of-sample efficacy baseline per pattern.

**Phase 2 — Hardening (engineering).** Close top test-coverage gaps + CI
coverage gate; classify and fix silent `except` sites; verify DB producer
wiring; add freshness/silent-zero alerts.

**Phase 3 — Operations.** Finish `gui_v2` and retire the legacy GUI; complete
the `read_only_ops` VPS lockdown; document the runbook for the simulation
harnesses.

**Phase 4 — Continuous validation.** Schedule periodic backtests/calibration
checks; track metric drift over time; feed findings back into tuning proposals
(proposals only — protected-logic changes require owner approval).

---

## 7. Repo-discipline follow-up (required pairing)

`CLAUDE.md` requires every new feature to be paired with an analysis-and-health
check at the matching cadence. This harness is backtest/lifetime-cadence under
the **Quant lens**, so the correct home is the yearly review
(`.claude/commands/yearly-tool-analysis.md`) and/or a new
`portfolio-backtest-health` agent that reads `outputs/backtest/` and flags stale
or degenerate results. **This pairing is recommended as the immediate next step
and was intentionally not auto-wired**, since editing the analysis skills/agents
is a separate, owner-scoped change. Flagging it here keeps the feature honest
against the repo's own rules.

---

## 8. Boundaries respected (summary)

- No changes to `decision_engine.py`, `scoring.py`, `allocation_engine.py`, or
  the six protected scores.
- No broker, execution, or auto-trading behavior.
- New code is additive; the harness is observe-only and writes only to the
  HISTORICAL namespace via governed safe writers.
- Synthetic-mode results are clearly labeled as generated data, not a strategy
  claim.

> **Execution-ready build plan:** [`PATTERN_LOOP_IMPLEMENTATION_SPEC.md`](PATTERN_LOOP_IMPLEMENTATION_SPEC.md)
> turns sections 4–6 into concrete, step-by-step tasks (exact files, functions,
> tests, and the observe-only vs. protected boundary).
>
> Companion documents: [`ARCHITECTURE_MAP.md`](ARCHITECTURE_MAP.md) and
> [`TECH_DEBT_AUDIT.md`](TECH_DEBT_AUDIT.md).
