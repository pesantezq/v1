---
description: Health + status review of the Research-Backed Strategy Lab. Runs the deterministic strategy_lab_health assessor over the sandbox lab artifacts (leaderboard, research catalog, walk-forward, factor attribution), triages GREEN/AMBER/RED, and emits a one-line heartbeat + structured body. Observe-only. Confirms the lab is running healthy after build/enable. Designed to run on demand and via the weekly cadence alongside the sim suite.
---

# Strategy Lab Analysis

Operational + health readout of the Research-Backed Strategy Lab
(`portfolio_automation/portfolio_sim/run_strategy_lab.py`). Working dir
`/opt/stockbot`. Observe-only — never edits code/scoring/decision_plan.

See `docs/RESEARCH_STRATEGY_LAB.md` for the full design.

## Step 1 — Run the deterministic assessor

```bash
.venv/bin/python -c "import json; from portfolio_automation.portfolio_sim.strategy_lab_health import assess_strategy_lab_health; print(json.dumps(assess_strategy_lab_health(root='.'), indent=2, default=str))"
```

Reads (all `outputs/sandbox/`): `strategy_leaderboard.json`,
`research_strategy_catalog.json`, `walk_forward_results.json`,
`factor_exposure_report.json`.

**Also check the Simulation Graphs artifact** (`outputs/latest/simulation_charts.json`,
the Strategy Lab dashboard's plain-English chart source — observe-only/sandbox, produced
by `run_daily_safe.sh` Stage 10b2; see `docs/SIMULATION_CHARTS.md`):
```bash
.venv/bin/python -c "import json,pathlib; p=pathlib.Path('outputs/latest/simulation_charts.json'); d=json.loads(p.read_text()) if p.exists() else {}; ch=d.get('charts',{}); av=[k for k,c in ch.items() if c.get('available')]; print('present:', bool(d), '| status:', d.get('status','ok' if d else 'absent'), '| charts available:', av or 'NONE', '| sources:', d.get('source_files_present'))"
```
- **AMBER content_liveness** — artifact present (`generated_at` set) but **every** chart
  `available:false` (looks-fresh-but-empty: it ran but found no usable upstream series →
  check that `strategy_comparison.json` / `portfolio_backtest.json` / `portfolio_projection.json`
  exist and are populated). Absent artifact is the inert pre-pipeline state (report, don't alert).
- It is sandbox/observe-only and **never RED** — it never feeds `decision_plan.json`.
  `allocation_drift` being empty is expected (no upstream composition series yet), not a finding.

## Step 2 — Triage

- **RED** — `looks_fresh_but_empty` (status `ok` but zero tactics scored → the lab
  ran but every tactic degraded; check `outputs/backtest/historical/*_5y.json`
  coverage for the holdings/benchmarks). The lab never blocks the decision core,
  but RED means its output is untrustworthy — do not act on the leaderboard.
- **AMBER** — `disabled` (inert steady state, report don't alert), `insufficient_data`,
  `stale` (>~8d, weekly cadence), `undocumented_tactics` (Strategy Documentation
  Requirement violated — add the `academic_basis`/rationale), `still_works_oos=false`
  tactic surfaced (walk-forward says it overfit — it is ranked down but flag it),
  `factor_data_unavailable` (run `scripts/fetch_factor_data.sh` to enable attribution).
- **GREEN** — ran, populated, documented, no failing-OOS tactic surfaced.

## Step 3 — Output

Heartbeat: `"Strategy-Lab: {status} · {tactic_count} tactics · top {top_tactic}
(score {top_score}, excess vs SPY {top_excess_vs_spy}) · coverage {complete|INCOMPLETE}
· factors {available|missing} · OOS-fail {n}"`.

For RED/AMBER, append the reasons. For coverage violations, name the undocumented
tactics. For a `still_works_oos=false` tactic, name it (it overfit in walk-forward).

## Step 4 — Dispatch (optional)

If RED `looks_fresh_but_empty` persists, the price archive likely lacks the
portfolio/benchmark tickers — recommend backfilling `outputs/backtest/historical/`.
No agent auto-dispatch; this is a research lane that never feeds `decision_plan`.
