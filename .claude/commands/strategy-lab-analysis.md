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

**Also check the active-strategy divergence artifact** (`outputs/sandbox/strategy_divergence.json`,
WS5 — see `.superpowers/audit/ws-04-05-14-18-health.md`; producer
`portfolio_automation/strategy/strategy_divergence.py`, observe-only/sandbox, compares the
operator-approved active strategy against the leaderboard's #1-ranked tactic):
```bash
.venv/bin/python -c "import json; from portfolio_automation.strategy.strategy_divergence import compute_strategy_divergence; print(json.dumps(compute_strategy_divergence(root='.'), indent=2, default=str))"
```
- Read `classification` (one of `EXPECTED_POLICY_DIVERGENCE`, `PENDING_REVIEW`,
  `INSUFFICIENT_EVIDENCE`, `STALE_ACTIVE_STRATEGY`, `UNEXPLAINED_DIVERGENCE`),
  `rank_difference`, `top_tactic_oos.state`, and `structural_unpromotability`.
- **`INSUFFICIENT_EVIDENCE`** (today's real, non-regression state: the top-ranked
  tactic is `OOS_NOT_TESTED`) — report, don't alert. Do not treat this as something
  to "fix" by loosening the classifier; it is the honest answer given 25/26
  leaderboard tactics have never been walk-forward tested.
- **`structural_unpromotability.blocked: true`** — the top tactic is a Strategy-Lab
  research/shadow tactic, not one of the 8 fixed `SEED_PROFILES` in
  `strategy_review_queue.json`; a human cannot currently approve it via the
  existing GUI decide-route. Always surface this fact verbatim regardless of
  classification — it is the structural reason "why hasn't the top tactic just
  been promoted" has no simple answer today.
- **`UNEXPLAINED_DIVERGENCE`** — the top tactic IS `OOS_SUPPORTED`, is promotable,
  and nothing explains the gap; flag for operator attention (still no auto-dispatch
  — this artifact only ever reports, it never re-anchors anything).
- **`STALE_ACTIVE_STRATEGY`** — matches `strategy_lab_health`'s existing
  `stale_active_strategy_selection` signal; resolve that first before trusting any
  rank comparison.
- Absent artifact (producer not yet run this cycle) is the inert pre-pipeline
  state — report, don't alert.

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

**WS14 update (2026-07-28, .superpowers/audit/ws-04-05-14-18-health.md):**
`ranking_credibility` and `oos_validity` are now ALSO downgraded (GREEN→AMBER,
with a stated `regime_concentration` reason appended either way) whenever
`portfolio_automation.regime_coverage.assess_regime_coverage()` — reading
`outputs/regime/regime_performance.json` — reports `REGIME_CONCENTRATED`
(a single regime holds ≥80% of resolved evidence, by count or return-weighted)
or `RISK_OFF_UNPROVEN` (the risk_off regime label is absent or has <30
full-quality observations). Confirmed live (2026-07-28): both fire —
~98% of resolved evidence is `neutral`, and `risk_off` carries only n=27
effective observations. Read `result["signals"]["regime_coverage"]` for the
full state. This is DISTINCT from the daily check's regime-degeneracy guard
(`daily-tool-analysis.md` item 26/6m) — that one only catches a
producer-ordering bug collapsing the column to a SINGLE value and explicitly
allows a legitimately calm single-`"neutral"` window; this measures SHARE of
evidence and fires even with 2-3 labels present. `REGIME_DATA_INSUFFICIENT`
alone (no regime artifact yet, or <30 resolved signals) never triggers this
downgrade — do not treat a small/fixture-only run's clean GREEN as a
regression when the artifact simply hasn't run yet.

## Step 3 — Output

Heartbeat: `"Strategy-Lab: {status} · {tactic_count} tactics · top {top_tactic}
(score {top_score}, excess vs SPY {top_excess_vs_spy}) · coverage {complete|INCOMPLETE}
· factors {available|missing} · OOS states {oos_state_counts} · active-strategy
{active_strategy_id|none} ({strategy_decisions_count} decisions) · divergence
{classification|absent}"`.

For RED/AMBER, append `blocking_reasons` verbatim. For coverage violations,
name the undocumented tactics. For an `OOS_FAILED` tactic, name it (it overfit
in walk-forward). If `oos_validity` is AMBER solely on `no_credible_oos_test`,
say so explicitly — that is the case where legacy tooling would have reported
GREEN.

## Step 4 — Dispatch (optional)

If RED `looks_fresh_but_empty` persists, the price archive likely lacks the
portfolio/benchmark tickers — recommend backfilling `outputs/backtest/historical/`.
No agent auto-dispatch; this is a research lane that never feeds `decision_plan`.

If the divergence artifact's `classification` is `UNEXPLAINED_DIVERGENCE`, name
the top tactic and its score/rank gap in the body so the operator can decide
whether to widen the review queue and route a human promotion decision — this
skill never does so itself.
