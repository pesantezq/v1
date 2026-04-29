# Output Artifact Contracts

Last verified against live files in `outputs/latest`, `outputs/portfolio`, `outputs/policy`, `outputs/performance`, `outputs/regime`, plus `gui_operator_data.py` and `watchlist_scanner/output_writers.py`.

## Contract Policy

These artifacts are consumed by:

- `gui/app.py`
- `gui_operator_data.py`
- `agent/bundle_builder.py`
- system-summary and evaluation layers
- tests

Backward-compatible additions are preferred. Renames, removals, and meaning changes are high risk.

## Top-Level Rules

- Keep file paths stable.
- Keep top-level object-vs-list shape stable.
- Do not silently rename required fields.
- Additive nested fields are allowed.
- If data is missing, keep the artifact and degrade values to empty/null/defaults instead of deleting the contract.

## Current Stable JSON Artifacts

### `outputs/latest/watchlist_signals.json`

Required top-level fields:

- `run_date`
- `generated_at`
- `calls_used`
- `scan_summary`
- `results`
- `alerts`

Important nested contracts:

- `scan_summary`
  Must include status, data-mode, cooldown/action suppression counts, conviction summary, portfolio construction summary, and market regime summary when available.
- `results[]`
  Must preserve core fields such as `ticker`, `signal_score`, `confidence_score`, `confidence_band`, `data_quality`, `alert_priority`, `priority_score`, `final_rank_score`, `effective_score`, `conviction_score`, `conviction_band`, `watchlist_source`, `notification_status`.
- `alerts[]`
  Same row shape as results, with outcome-tracking fields populated when surfaced.

### `outputs/latest/theme_signals.json`

Required top-level fields:

- `generated_at`
- `run_date`
- `themes`
- `theme_source`
- `no_update`

Expected theme item fields:

- `name`
- `confidence`
- `rationale`
- `evidence_items`
- `direct_mentions`
- `tickers`

### `outputs/latest/watch_candidates.json`

Required top-level fields:

- `generated_at`
- `run_date`
- `watch_candidates`
- `theme_source`
- `no_update`

Expected item fields:

- `ticker`
- `sources`
- `themes`
- `confidence`
- `rationale`
- `timestamp`

### `outputs/portfolio/portfolio_snapshot.json`

Required top-level fields:

- `enabled`
- `observe_only`
- `summary_label`
- `summary_line`
- `total_suggested_allocation`
- `total_normalized_allocation`
- `warnings`
- `groupings`
- `rows`

Expected row fields:

- `ticker`
- `sector`
- `themes`
- `market_cap_bucket`
- `conviction_score`
- `conviction_band`
- `suggested_allocation`
- `normalized_allocation`
- `allocation_capped`
- `allocation_cap_reason`

### `outputs/policy/policy_recommendation.json`

Required top-level fields:

- `generated_at`
- `formula`
- `current_context`
- `recommendation`
- `alternatives`
- `policy_rankings`
- `profile_rankings`

Required nested recommendation fields:

- `recommended_policy`
- `recommended_profile`
- `recommendation_score`
- `recommendation_confidence`
- `recommendation_reasoning`
- `recommendation_inputs`
- `recommendation_data_quality`
- `recommendation_source`

### `outputs/policy/recommendation_evaluation.json`

Required top-level fields:

- `generated_at`
- `history_path`
- `total_records`
- `total_runs`
- `date_range`
- `hit_rate_by_regime`
- `hit_rate_by_mode`
- `confidence_calibration`
- `recommendation_stability`
- `best_vs_recommended_gap`

This file may legitimately contain empty dicts when history is missing or too sparse.

### `outputs/policy/profit_attribution.json`

Required top-level fields:

- `generated_at`
- `metrics`
- `by_strategy`
- `by_score_band`
- `by_regime`
- `trade_ledger`
- `exit_summary`
- `missed_opportunities`
- `total_opportunity_cost`
- `data_quality_notes`
- `execution`

### `outputs/latest/agent_bundle.json`

Required top-level fields:

- `run_mode`
- `generated_at`
- `sources`
- `config`
- `drawdown`
- `drawdown_regime`
- `portfolio_value`
- `cash_available`
- `holdings_snapshot`
- `guardrails`
- `policy_recommendation`
- `data_health`

This is an AI-facing summary contract and should stay broad but stable.

### `outputs/latest/agent_llm_metadata.json`

Required top-level fields:

- `generated_at`
- `run_id`
- `started_at`
- `completed_at`
- `mode`
- `degraded_mode`
- `data_sources_used`
- `tasks`

### `outputs/latest/theme_engine_llm_metadata.json`

Required top-level fields:

- `generated_at`
- `run_id`
- `started_at`
- `completed_at`
- `llm_metadata`

### `outputs/latest/scraped_intel_run_summary.json`

Required top-level fields:

- `timestamp`
- `run_mode`
- `degraded_mode`
- `data_sources_used`
- `data_mode`
- `scanner`
- `scraped_intel`
- `market_regime`
- `market_coverage`

### `outputs/latest/scraped_intel_comparison.json`

Required top-level fields:

- `generated_at`
- `mode`
- `blend_weights`
- `max_signal_boost`
- `max_conf_boost`
- `symbols_total`
- `symbols_with_soft_signals`
- `symbols_rank_changed`
- `comparison`

### `outputs/latest/system_decision_summary.json`

Required top-level fields:

- `generated_at`
- `schema_version`
- `top_theme`
- `top_opportunity`
- `best_portfolio_fit`
- `system_state`
- `capital_preview`
- `policy_insight`
- `data_health`
- `changes`

### `outputs/latest/decision_plan.json`

Required top-level fields:

- `generated_at`
- `run_mode`
- `observe_only`
- `total_decisions`
- `decisions`

Required decision row fields:

- `symbol`
- `decision`
- `priority`
- `urgency`
- `source`
- `recommended_action`
- `recommended_amount`
- `recommended_allocation_pct`
- `reason`
- `risk_flags`
- `confidence`
- `inputs_used`

Contract notes:

- this is the decision source of truth for downstream consumers
- downstream layers must not mutate this file
- downstream layers must not recompute decision order or actions

### `outputs/latest/decision_explanations.json`

Required top-level fields:

- `generated_at`
- `available`
- `observe_only`
- `summary_line`
- `source_artifacts`
- `explanations`

Required explanation row fields:

- `decision_id`
- `symbol`
- `action`
- `priority`
- `urgency`
- `source`
- `source_attribution`
- `concise_explanation`
- `risks`
- `what_to_watch_next`
- `explanation_basis`
- `ai_validation`

Contract notes:

- additive downstream artifact only
- must not feed back into decision generation
- must preserve input decision order for the top explanation set
- `explanations` is capped at `5`
- `risks` is capped at `3`
- `what_to_watch_next` is capped at `3`
- `ai_validation` is a closed set:
  - `boost`
  - `neutral`
  - `caution`
- missing or malformed `decision_plan.json` should degrade to an available artifact with an empty explanation list and a clear status line, not a pipeline failure

### `outputs/latest/market_opportunities.json`

Required top-level fields:

- `enabled`
- `promoted`
- `event_summary`
- `symbols_scanned`
- `symbols_with_price`
- `portfolio_review`
- `decision_layer`

### `outputs/performance/performance_summary.json`

Required top-level fields:

- `generated_at`
- `windows`
- `primary_window_days`
- `tracked_signals`
- `resolved_signals`
- `by_window`
- `by_ticker`
- `global_metrics`
- `regime_performance`
- `theme_alignment_performance`
- `portfolio_fit_performance`
- `final_rank_performance`

### `outputs/performance/weight_tuning_suggestions.json`

Required top-level fields:

- `generated_at`
- `observe_only`
- `primary_window_days`
- `current_weights`
- `recommended_candidate`
- `candidates`

### `outputs/performance/allocation_policy_preview.json`

Required top-level fields:

- `generated_at`
- `observe_only`
- `not_applied`
- `candidate_count`
- `opportunities`

### `outputs/performance/allocation_policy_simulation.json`

Required top-level fields:

- `generated_at`
- `observe_only`
- `not_applied`
- `primary_window_days`
- `sample_size`
- `baseline`
- `rank_aware`
- `delta`
- `details`

### `outputs/regime/regime_performance.json`

Required top-level fields:

- `generated_at`
- `primary_window_days`
- `resolved_signals`
- `by_regime`
- `observability`

## What Must Never Change Without A Coordinated Migration

- `watchlist_signals.json` being a dict with `results` and `alerts`
- `portfolio_snapshot.json` row-based portfolio construction shape
- `policy_recommendation.json` top-level `recommendation` object and its score/confidence fields
- `recommendation_evaluation.json` top-level metric family names
- file locations under `outputs/latest`, `outputs/portfolio`, `outputs/policy`, `outputs/performance`, and `outputs/regime`

## Safe Change Pattern

- Add new fields.
- Keep old fields intact.
- Document meaning changes explicitly.
- Update GUI/tests in the same change if a consumer depends on new fields.
