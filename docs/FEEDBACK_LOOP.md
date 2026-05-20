# Feedback Loop

Last verified against `portfolio_automation/decision_outcome_tracker.py`, `main.py`, `gui/app.py`, `watchlist_scanner/outcome_evaluator.py`, `ml_history.py`, and `portfolio_automation/resolution_due_probe.py` on 2026-05-20.

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

## Live vs Historical Replay

Historical Replay v1 is implemented and extends the feedback loop without replacing it.

Required source-aware rules:

- live rows stay `source = "live"`
- replay rows use `source = "historical_replay"`
- do not blend live and replay outcomes into one metric without source-aware reporting

Why this matters:

- live conditions include real timing, state, and degraded-mode behavior
- replay conditions are useful, but reconstructed
- replay can accelerate calibration sooner than waiting for live history alone
- replay cannot perfectly reproduce live execution conditions

Recommended source-aware analysis:

- `live_hit_rate`
- `historical_hit_rate`
- `combined_hit_rate`
- by decision
- by strategy
- by validation status
- by triage bucket

Guardrail:

- avoid overfitting thresholds or confidence behavior to backtest results alone

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

## Historical Replay Subsystem

Historical Replay v1 is implemented at `portfolio_automation/historical_replay/`.

It runs offline (operator-triggered, never from `main.py`) and writes to
`outputs/backtest/`, not `outputs/policy/`.

Source separation:

| Source | File | Written by |
|--------|------|------------|
| `"live"` | `outputs/policy/decision_outcomes.jsonl` | decision_outcome_tracker.py |
| `"historical_replay"` | `outputs/backtest/decision_outcomes_historical.jsonl` | replay_runner.py |

See `docs/HISTORICAL_REPLAY_BACKTEST.md` for full documentation.

## Resolver Hardening (2026-05-19)

Three coordinated fixes plug long-standing gaps where the resolver was leaving
large fractions of history unresolved:

1. **FMP fallback in `watchlist_scanner/outcome_evaluator.py`.** A new helper
   `_load_next_available_close_fmp` and a composite `load_next_available_close`
   keep Alpha Vantage as primary, fall back to `FMPClient.get_historical_prices`
   when the AV daily cache is empty. This unsticks weekend resolutions and
   symbols outside the scanner's daily universe.
2. **Natural-resolution in `ml_history.auto_resolve_pending_records`.** Marks
   ml records resolved when their `rec_key` no longer surfaces in today's
   adjustments. Also fixes a latent argument-order TypeError in
   `update_record_resolution`.
3. **FMP price augmentation in
   `portfolio_automation/decision_outcome_tracker._augment_price_map_with_fmp`.**
   Fills in `price_at_decision` for non-watchlist decision symbols via
   `FMPClient.get_batch_quotes`. Without this, decisions on tickers the scanner
   never scored landed in `decision_outcomes.jsonl` with `price_at_decision=None`
   and could never resolve.

Net effect: resolved-row counts in `outputs/policy/decision_outcomes.jsonl` and
`outputs/performance/signal_outcomes.csv` climb significantly. ml_advisor (now
enabled — see `docs/CHANGELOG_DECISIONS.md`) crosses its
`MIN_RECORDS_FOR_HIGH_CONFIDENCE = 30` threshold and emits real pattern outputs
instead of the previous `insufficient_data` status.

The new `portfolio_automation/resolution_due_probe.py` advisor is the
forward-looking guardrail: any window that stays stuck past its expected
resolution date (`1d`, `3d`, `7d` × calendar-day multiplier) appears in
`outputs/latest/decisions_due_for_resolution.json` so the operator notices
silent resolver failures. A healthy run leaves `stuck_count` at or near zero.
