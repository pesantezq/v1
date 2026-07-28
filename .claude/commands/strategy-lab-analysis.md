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

**WS4 update (2026-07-28, .superpowers/audit/ws-02-03-oos-selection.md):** the
assessor now rolls up **9 independent dimensions** (`runtime_health`,
`artifact_completeness`, `documentation_coverage`, `data_admissibility`,
`statistical_sufficiency`, `oos_validity`, `ranking_credibility`,
`governance_compliance`, `presentation_consistency`) fail-closed — worst
dimension wins. Read `result["dimensions"]` for the per-dimension breakdown and
`result["blocking_reasons"]` for what is keeping it off GREEN. This ships
behind a gate that defaults ON (`portfolio_sim.strategy_lab.health.strict_oos_rollup_enabled`,
kill-switch file `config/strategy_lab_strict_health.DISABLED`, or env
`STOCKBOT_STRATEGY_LAB_STRICT_HEALTH_DISABLED=1` — disabling it reproduces the
pre-WS4 verdict exactly, bug included, for rollback only). The legacy
top-level `status`/`reasons`/`signals` keys still resolve.

**Known, intended, non-regression fact**: today's real leaderboard carries 25/26
tactics with `still_works_oos: null` (never walk-forward tested — classified
`OOS_NOT_TESTED`) and 1 tactic that is genuinely OOS-tested and passing
(`OOS_SUPPORTED`, but ranked LAST of 26). Under the strict rollup this is
AMBER (`statistical_sufficiency` + `ranking_credibility`), not GREEN — do not
treat this AMBER as something to "fix" by loosening thresholds; it is the
fix.

- **RED** — `looks_fresh_but_empty` (status `ok` but zero tactics scored → the lab
  ran but every tactic degraded; check `outputs/backtest/historical/*_5y.json`
  coverage for the holdings/benchmarks), an unparsable `created_at` on a
  present leaderboard, or a confirmed governance-invariant breach
  (`observe_only`/`sandbox_only`/`no_trade` explicitly false). The lab never
  blocks the decision core, but RED means its output is untrustworthy — do
  not act on the leaderboard.
- **AMBER** — `disabled` (inert steady state, report don't alert), `insufficient_data`,
  `stale` (>~8d, weekly cadence), `undocumented_tactics` (Strategy Documentation
  Requirement violated — add the `academic_basis`/rationale), any tactic
  surfaced as `OOS_FAILED`, **zero tactics reaching `OOS_SUPPORTED`**
  (`oos_validity`'s `no_credible_oos_test` — this is the WS4 headline fix:
  "nothing failed" is no longer read as "OOS-valid"), `<50%` of tactics with
  sufficient-fold OOS evidence (`statistical_sufficiency`), a top-ranked tactic
  that isn't `OOS_SUPPORTED` (`ranking_credibility` — see WS3 selection-bias
  finding), `factor_data_unavailable` (run `scripts/fetch_factor_data.sh` to
  enable attribution), `stale_active_strategy_selection` (the operator-approved
  active strategy `active_strategy_id` no longer appears in the current
  `strategy_review_queue.json` — the selection re-anchors the sandbox
  projection on a profile that's gone; re-approve a current profile or it
  falls back to the baseline anchor).
- **GREEN** — ALL 9 dimensions GREEN: ran, populated, documented, no
  `OOS_FAILED` tactic, AND at least one tactic reaches `OOS_SUPPORTED` with
  positive evidence (folds ≥ 4, no single fold dominating the aggregate).

## Step 3 — Output

Heartbeat: `"Strategy-Lab: {status} · {tactic_count} tactics · top {top_tactic}
(score {top_score}, excess vs SPY {top_excess_vs_spy}) · coverage {complete|INCOMPLETE}
· factors {available|missing} · OOS states {oos_state_counts} · active-strategy
{active_strategy_id|none} ({strategy_decisions_count} decisions)"`.

For RED/AMBER, append `blocking_reasons` verbatim. For coverage violations,
name the undocumented tactics. For an `OOS_FAILED` tactic, name it (it overfit
in walk-forward). If `oos_validity` is AMBER solely on `no_credible_oos_test`,
say so explicitly — that is the case where legacy tooling would have reported
GREEN.

## Step 4 — Dispatch (optional)

If RED `looks_fresh_but_empty` persists, the price archive likely lacks the
portfolio/benchmark tickers — recommend backfilling `outputs/backtest/historical/`.
No agent auto-dispatch; this is a research lane that never feeds `decision_plan`.
