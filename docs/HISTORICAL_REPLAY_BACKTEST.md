# Historical Replay / Backtest Calibration

Design status: planned only. Not implemented as of 2026-04-30.

## Purpose

Accelerate calibration and attribution when live resolved decision history is still sparse.

## What This Is

An offline historical replay path that uses approved FMP historical end-of-day data to reconstruct prior decision dates, replay the existing decision logic, and generate source-tagged historical outcome records for evaluation.

## What This Is Not

- not auto-trading
- not live execution
- not broker integration
- not blind ML training
- not automatic policy promotion
- not a replacement for live outcome history

## Why This Exists

Current live history is still limited. Confidence calibration, decision performance attribution, and triage analysis need at least 20 resolved decisions before they become directionally useful.

Historical replay is intended to:

- accelerate confidence calibration
- accelerate performance attribution
- give future agents a larger evaluation sample
- stay strictly downstream from live decision generation

## Proposed Module

- `portfolio_automation/historical_decision_replay.py`

Expected role:

- offline only
- deterministic only
- no LLM calls
- no trading or broker actions
- no scoring changes
- no threshold changes

## Data Sources

Approved initial data source:

- FMP stable historical EOD prices via the approved historical EOD endpoint

Initial replay universe:

- current holdings
- active watchlist universe

Initial data constraints:

- end-of-day only
- no premium endpoints unless explicitly enabled and documented
- no intraday assumptions

## Replay Flow

```text
FMP historical EOD prices
    -> reconstruct past scanner inputs
    -> replay decision logic for historical dates
    -> generate historical decision rows
    -> resolve 1d / 3d / 7d outcomes
    -> write backtest-tagged decision outcome records
    -> feed calibration / attribution summaries
```

Required source flags:

- live rows must remain `source = "live"`
- replay rows must use `source = "historical_replay"`

## Output Artifacts

Recommended replay artifacts:

- `outputs/policy/historical_decision_outcomes.jsonl`
- `outputs/policy/historical_decision_outcome_summary.json`
- `outputs/policy/historical_decision_outcome_summary.md`

Downstream summaries that should become source-aware:

- `outputs/policy/confidence_calibration.json`
- `outputs/policy/decision_performance_attribution.json`
- `outputs/policy/decision_triage.json`

Recommended source-aware metrics:

- `live_hit_rate`
- `historical_hit_rate`
- `combined_hit_rate`
- `by_decision`
- `by_strategy`
- `by_validation_status`
- `by_triage_bucket`

## Separation From Live History

Live and replay rows must not be mixed blindly.

Required rules:

- keep live and replay rows source-tagged
- do not overwrite live `decision_outcomes.jsonl`
- do not publish combined metrics without explicit source-aware breakdowns
- treat live metrics as the primary production signal
- treat replay metrics as calibration support only

## Safety Rules

- observe-only only
- replay is offline only
- no execution behavior
- no policy auto-promotion from replay results
- no scoring changes inside replay
- no threshold tuning during replay generation
- no backtest result should silently alter live recommendation behavior

## Initial Implementation Scope

- 90 trading days
- holdings + active watchlist universe
- FMP historical EOD only
- no LLM calls
- no broker actions
- no scoring changes
- no threshold changes
- no automatic promotion to live policy

## Future Extensions

- longer replay windows after the 90-day baseline
- source-aware comparison of live vs replay calibration quality
- strategy-specific replay slices
- replay-aware attribution by validation status and triage bucket
- explicit operator reports showing where replay and live outcomes diverge

## Next Implementation Step

Implement `portfolio_automation/historical_decision_replay.py` as a fully offline writer of source-tagged replay artifacts without changing the live daily pipeline.
