# Evaluation And Learning Loop

Last verified against `watchlist_scanner/performance_feedback.py`, `policy_evaluator/history_writer.py`, `policy_evaluator/evaluator.py`, `policy_evaluator/outcome_attributor.py`, and `profit_attribution/confidence_calibration.py`.

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
