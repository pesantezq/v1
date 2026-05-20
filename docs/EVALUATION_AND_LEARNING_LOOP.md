# Evaluation And Learning Loop

Last verified against `watchlist_scanner/performance_feedback.py`, `policy_evaluator/history_writer.py`, `policy_evaluator/evaluator.py`, `policy_evaluator/outcome_attributor.py`, `profit_attribution/confidence_calibration.py`, `watchlist_scanner/outcome_evaluator.py`, `ml_history.py`, and `portfolio_automation/decision_outcome_tracker.py`. Last updated 2026-05-20.

## Two Separate Learning Loops

### 1. Watchlist signal loop

Storage:

- `watchlist_signal_feedback`
- `watchlist_alert_outcomes`

Outputs:

- `outputs/performance/performance_summary.json`
- `outputs/regime/regime_performance.json`

Purpose:
measure whether watchlist signals, theme alignment, portfolio fit, and ranking features separate good from weak opportunities.

### 2. Finance recommendation loop

Storage:

- `outputs/policy/recommendation_history.jsonl`

Outputs:

- `outputs/policy/recommendation_evaluation.json`
- optional outcome attribution outputs from `policy_evaluator/outcome_writer.py`

Purpose:
measure whether scored finance recommendations are stable, calibrated, and appropriately confidence-adjusted over time.

## Recommendation History

Writer:
`policy_evaluator/history_writer.py:append_run_recommendations`

One JSONL record is appended per scored finance recommendation from `main.py`.

Important fields:

- run metadata
- degraded/data-mode context
- guardrail context
- drawdown context
- recommendation identity
- `score`, `raw_score`, `confidence`, `action_level`

Important note:
This history may be absent on fresh installs. Evaluation must degrade gracefully.

## Hit Rate

There are multiple hit-rate concepts.

### Watchlist hit rate

Derived from forward returns in `watchlist_signal_feedback`.

Typical summaries:

- by window (`1d`, `3d`, `7d`)
- by ticker
- by confidence bucket
- by regime
- by theme alignment bucket
- by portfolio fit bucket

### Finance recommendation hit rate

In `policy_evaluator/evaluator.py`, "resolution" is proxy-based:

- a recommendation is treated as resolved when its stable `rec_base_id` disappears in the next run

This is not a trade PnL metric. It measures whether the underlying condition persisted.

## Confidence Calibration

### Watchlist calibration

Artifacts:

- `performance_summary.json.global_metrics`
- `regime_performance.json`
- profit attribution confidence calibration modules

Goal:
higher-confidence signals should outperform lower-confidence signals more often.

### Finance recommendation calibration

Artifact:
`recommendation_evaluation.json.confidence_calibration`

Goal:
higher-confidence finance recommendations should resolve faster or more reliably.

## Stability

Artifact:
`recommendation_evaluation.json.recommendation_stability`

Meaning:
run-over-run churn of finance recommendations.

Why it matters:

- too much churn suggests noisy thresholds
- too little churn can mean stale or unresponsive scoring

## Feedback Loops That Are Observe-Only

Current learning outputs inform humans and future tuning, but do not auto-activate live behavior.

Examples:

- `weight_tuning_suggestions.json`
- `allocation_policy_preview.json`
- `allocation_policy_simulation.json`
- `performance_summary.json.future_activation.enabled = false`

## What The Learning Loop Must Preserve

- clear provenance from source score to measured outcome
- separate attractiveness from trustworthiness
- small-sample warnings instead of overconfident claims
- additive evaluation artifacts rather than hidden in-place score rewrites

## Common Empty-State Behavior

Fresh or sparse data can produce:

- `tracked_signals = 0`
- `resolved_signals = 0`
- empty `recommendation_history.jsonl`
- null or empty hit-rate/calibration outputs

That is valid behavior and should remain non-fatal.

## Resolver Data Flow (2026-05-19 hardening)

Three coordinated changes pushed the resolved-outcome history past the
volume needed to power downstream learning consumers:

- `watchlist_scanner/outcome_evaluator.py` — `load_next_available_close`
  composite. AV daily cache stays primary; FMP `get_historical_prices` is the
  secondary path when the cache is empty. This unsticks weekend resolutions
  and symbols outside the scanner's daily universe.
- `ml_history.auto_resolve_pending_records` — natural-resolution path that
  marks ml records resolved once their `rec_key` no longer surfaces in
  today's adjustments. Also fixes a latent argument-order TypeError in
  `update_record_resolution`.
- `portfolio_automation/decision_outcome_tracker._augment_price_map_with_fmp` —
  fills `price_at_decision` for non-watchlist decision symbols via
  `FMPClient.get_batch_quotes`, so decision rows on tickers the scanner never
  scored have an entry/exit pair available for resolution.

## ml_advisor Status

`ml_advisor` is enabled in `config.json` (and `config/base.json`) as of
2026-05-18. Combined with the resolver fixes above, the resolved-decisions
history now exceeds the advisor's `MIN_RECORDS_FOR_HIGH_CONFIDENCE = 30`
threshold, so it produces real pattern outputs in
`outputs/latest/ml_pattern_advisor.{json,md}` instead of the previous
`status=insufficient_data` degraded state. The advisor remains observe-only;
it does not mutate decisions, scores, or allocations.

## Resolution-Due Probe

`portfolio_automation/resolution_due_probe.py` (Stage 7e in the safe wrapper)
surfaces rows in `outputs/performance/signal_outcomes.csv` whose 1d/3d/7d
windows have elapsed but whose outcome columns are still null. The artifact
`outputs/latest/decisions_due_for_resolution.{json,md}` should report a near-
zero `stuck_count` on a healthy run. Persistent non-zero stuck counts are the
operator's primary signal that the resolver has regressed.
