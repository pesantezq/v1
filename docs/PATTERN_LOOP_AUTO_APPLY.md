# Pattern-Loop Auto-Apply (sub-project E)

> **Status: INERT.** Ships with `config.json backtesting.auto_apply.enabled=false` and
> cannot fire until the walk-forward OOS window matures (`oos_window.folds_possible`,
> ≈2027). This is the single operator-approved exception (2026-06-05) to the owner-gated
> Step 5 path. Every weight change it makes is surfaced and routed for review.

## What it does

`backtesting/auto_apply.py::maybe_auto_apply` — when (and only when) every gate clears —
authors `config/approved_weight_changes.json` and invokes the existing reversible protected
apply (`backtesting/registry_apply.py`), removing the human from the registry-weight apply
path. A GPT approver sits **on top of** the deterministic gates: it may only **veto** or
**approve the pre-bounded delta** — it can never widen a bound, change the magnitude, or
pick a different signal.

This is the one sanctioned **mutating** path in the loop (`observe_only: false` in its
output). Everything else in the Pattern-Loop remains observe-only.

## Gate sequence (fail-closed; first failure wins → no apply)

| Gate | Condition | Status on fail |
|---|---|---|
| G0 | `enabled` is true (`config.json backtesting.auto_apply.enabled`) | `disabled` |
| G1 | kill-switch absent | `kill_switched` |
| G2 | `oos_window.folds_possible` is true (OOS matured) | `oos_immature` |
| G3 | ≥1 proposal with non-zero `proposed_delta` and an actionable status | `no_actionable_proposal` |
| G4 | this-month drift + Δ ≤ `max_monthly_drift` | `drift_capped` |
| G5 | pre-apply score-invariance gate == GREEN | `score_gate_blocked` |
| G6 | AI budget allows the approver call (real LLM path only) | `budget_exceeded` |
| G7 | GPT approver verdict == approve (within bounds) | `gpt_vetoed` |
| apply | `registry_apply.apply_approved_changes` succeeds | `apply_failed` |
| post | post-apply score-invariance gate != RED, else auto-revert | `rolled_back` |
| ✓ | all clear | `applied` |

Any uncertainty (unparseable verdict, LLM unreachable, exception) → veto / no-op.

## Kill-switch (immediate hard-disable, regardless of `enabled`)

- File: create `config/auto_apply.DISABLED` (any contents), **or**
- Env: set `STOCKBOT_AUTO_APPLY_DISABLED=1`

Either makes `maybe_auto_apply` return `kill_switched` before any apply.

## Rollback

Every apply snapshots the registry byte-for-byte under `config/history/` first. A post-apply
score-invariance regression auto-reverts. To manually revert the last apply:

```
python -m backtesting.registry_apply --rollback   # or registry_apply.revert_last(...)
```

## Audit + oversight

- Every terminal decision (disabled/vetoed/applied/rolled_back/…) is appended to
  `outputs/policy/auto_apply_audit.json` with full provenance (gate states, GPT verdicts).
- `backtesting/backtest_health.assess_backtest_health` surfaces it: RED
  `auto_apply_rolled_back` on a rollback, AMBER `auto_apply_active` on an apply.
- The daily and monthly tool-analysis skills dispatch `portfolio-backtest-health`
  (+ `portfolio-attribution-analyst` monthly) on any `applied`/`rolled_back` event — so
  every autonomous weight change is reviewed.

## Activation runbook (operator only — all four required)

1. Confirm the OOS window is mature (`oos_window.folds_possible == true`) and the weight
   proposals show a real, significant edge.
2. Review recent `signal_weight_proposals.json` + `poc_simulation_results.json`.
3. Set `config.json backtesting.auto_apply.enabled = true`.
4. Ensure no kill-switch (`config/auto_apply.DISABLED` absent, env unset).

Until all four hold, auto-apply is inert. Deactivate at any time via the kill-switch.
