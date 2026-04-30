# Feedback Loop

Last verified against `portfolio_automation/decision_outcome_tracker.py`, `main.py`, and `gui/app.py` on 2026-04-29.

## Purpose

The feedback loop tracks how observe-only decisions performed after they were generated. It is a learning layer, not a trading or execution layer.

System role:

- snapshot decisions
- resolve outcomes after time passes
- aggregate hit-rate and return metrics
- expose learning signals for later calibration work

## Execution Order

```text
decision_plan.json
    + ai_decision_validation.json
    + watchlist_signals.json
    -> portfolio_automation/decision_outcome_tracker.py
    -> decision_outcomes.jsonl
    -> decision_outcome_summary.json
    -> decision_outcome_summary.md
```

The tracker runs after AI validation. It never changes the original decision plan.

## Design

The tracker has three jobs:

1. Snapshot current decisions into history
2. Resolve older decisions when enough time has elapsed
3. Aggregate performance summaries for operators and future tuning

Current windows:

- 1 day
- 3 days
- 7 days

Current `WAIT` correctness threshold:

- `abs(return_pct) < 0.03`

## JSONL History Contract

Primary history file:

- `outputs/policy/decision_outcomes.jsonl`

Expected row fields:

- `run_id`
- `date`
- `symbol`
- `decision`
- `priority`
- `source`
- `strategy`
- `band`
- `confidence`
- `validation_status`
- `price_at_decision`
- `timestamp`
- `resolved`
- `resolved_at`
- `days_elapsed`
- `price_at_resolution`
- `return_pct`
- `direction_correct`

Contract notes:

- one row per snapshotted decision
- snapshot step is idempotent for the same `run_id`
- rows are capped by the tracker's history-size guardrails
- missing prices should degrade to unresolved rows, not crashes

## Outcome Resolution Logic

Directional correctness is deterministic:

- `SELL` and `AVOID`
  Correct when price goes down
- `BUY` and `SCALE`
  Correct when price goes up
- `WAIT`
  Correct when the absolute move stays below the threshold
- `HOLD`
  Neutral; excluded from hit-rate judgment

Examples:

- `SELL` with `return_pct = -0.05`
  `direction_correct = true`
- `BUY` with `return_pct = 0.04`
  `direction_correct = true`
- `WAIT` with `return_pct = 0.01`
  `direction_correct = true`
- `WAIT` with `return_pct = 0.05`
  `direction_correct = false`

## Aggregation Logic

Summary metrics are computed from history rows:

- `hit_rate`
- `avg_return_pct`
- `resolved`
- `unresolved`
- by-decision breakdown
- by-validation-status breakdown
- recent resolved decisions
- best decision
- worst decision

Important behavior:

- `HOLD` does not count toward hit rate
- unresolved rows remain in history and are summarized separately
- aggregation is read-only relative to the original decision artifacts

## Output Artifacts

- `outputs/policy/decision_outcomes.jsonl`
- `outputs/policy/decision_outcome_summary.json`
- `outputs/policy/decision_outcome_summary.md`

Summary JSON fields:

- `generated_at`
- `total_decisions`
- `resolved`
- `unresolved`
- `hit_rate`
- `avg_return_pct`
- `by_decision`
- `by_validation_status`
- `last_10_resolved`
- `best_decision`
- `worst_decision`

## GUI Integration

The GUI consumes `decision_outcome_summary.json` in the `Decision Performance` section.

Boundaries:

- GUI is read-only
- GUI does not recompute outcomes
- GUI does not modify history
- tracker remains a downstream learning layer

## Why This Matters

This system now supports:

```text
Decide -> Explain -> Validate -> Track Outcomes
```

That gives future agents a stable base for:

- confidence calibration
- validation-quality review
- threshold tuning
- strategy-specific feedback

None of that changes the live advisory decision path today.

## Invariants

- observe-only only
- no trade execution
- no scoring changes
- no decision mutation
- no same-run feedback into ranking or allocation
- tracker failure is non-fatal

## Next Implementation Step

Use the tracked outcome history for later calibration and optimization work, but keep the feedback loop analytically downstream from live decision generation.
