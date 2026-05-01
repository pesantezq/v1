# Output Artifact Contracts

Last verified against live files in `outputs/latest`, `outputs/portfolio`, `outputs/policy`, `outputs/performance`, `outputs/regime`, `outputs/backtest`, `outputs/sandbox/discovery/`, plus `gui_operator_data.py`, `watchlist_scanner/output_writers.py`, `portfolio_automation/ai_decision_validator.py`, `portfolio_automation/decision_outcome_tracker.py`, `portfolio_automation/historical_replay/replay_reports.py`, and `portfolio_automation/discovery/discovery_reports.py`.

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

## Artifact Health Severity

System summary, memo, and GUI health messaging should use severity-aware wording, not vague missing-artifact counts.

### `critical_missing`

- A truly required pipeline artifact is absent.
- It must count toward `missing_artifact_count`.
- It requires investigation because the expected producer step did not deliver a required output.
- Health messages should name the exact file path and producer step.

### `defaulting`

- A policy/config artifact is absent, but the system has a safe default behavior.
- It must not count toward `missing_artifact_count`.
- Current examples:
  - `outputs/performance/approved_ranking_config.json`
  - `outputs/performance/approved_allocation_policy.json`
- Expected meanings:
  - ranking weights source = `default`
  - allocation policy = `not_approved` / observe-only
- Health messages should say `defaulting` or equivalent, not `required artifact missing`.

### `optional_missing`

- A non-critical artifact is absent, but a valid fallback exists.
- It must not count toward `missing_artifact_count`.
- Current example:
  - `outputs/latest/theme_opportunities.json` when `outputs/latest/theme_signals.json` exists
- Health messages should say `optional artifact not present` or equivalent.

### Contract Rule

- `missing_artifact_count` is reserved for `critical_missing` only.
- Severity-aware detail lists may be additive fields.
- Downstream consumers must preserve exact artifact path and producer-step visibility.

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

Expected `data_health` behavior:

- `missing_artifacts`, `missing_artifact_details`, and `missing_artifact_count`
  Reserved for `critical_missing` artifacts only.
- `defaulting_artifact_details`
  Artifacts absent but safe defaults are active.
- `optional_artifact_details`
  Artifacts absent but a valid fallback source exists.

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
- additive structured explainability fields are allowed and now expected on production rows when present, including:
  - `decision_type`
  - `priority_score`
  - `capital_action`
  - `decision_reason`
  - `override_flags`
  - `allocation`
  - `decision_reason_structured`

### `outputs/latest/ai_decision_validation.json`

Required top-level fields:

- `generated_at`
- `observe_only`
- `available`
- `total_validated`
- `aligned_count`
- `caution_count`
- `contradiction_count`
- `insufficient_context_count`
- `ai_used`
- `summary_line`
- `validations`

Required validation row fields:

- `symbol`
- `decision`
- `validation_status`
- `plain_english_summary`
- `rule_alignment`
- `narrative_context`
- `contradictions`
- `watch_next`
- `ai_used`
- `model`
- `generated_at`

Contract notes:

- validation is downstream only
- validator must never change decisions, ranks, scores, or allocations
- deterministic rules run first
- optional LLM use must remain non-blocking
- missing or malformed `decision_plan.json` must degrade to:
  - `available: false`
  - zero counts
  - empty `validations`
  - a clear `summary_line`

### `outputs/policy/decision_outcomes.jsonl`

Required row fields:

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

- one JSON object per line
- snapshot step must be idempotent for the same `run_id`
- unresolved rows are valid and expected
- `direction_correct` may be:
  - `true`
  - `false`
  - `null` for neutral decisions such as `HOLD`

### `outputs/policy/decision_outcome_summary.json`

Required top-level fields:

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

Contract notes:

- summary is aggregated from `decision_outcomes.jsonl`
- `hit_rate` and `avg_return_pct` may be `null` when no resolved rows exist
- `by_validation_status` links decision outcome analysis to the AI validation layer
- this artifact is consumed by GUI read-only performance views

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

## Backtest / Historical Replay Artifacts

All replay artifacts are written to `outputs/backtest/` only. They are produced by
`portfolio_automation/historical_replay/replay_runner.py` and never mixed into
the live `outputs/policy/` directory.

### `outputs/backtest/decision_outcomes_historical.jsonl`

JSONL; one row per replay decision. All rows have `source="historical_replay"`.

Required row fields:

- `source` — always `"historical_replay"`
- `run_id` — `"historical_YYYY-MM-DD"`
- `date` — ISO date of simulated decision
- `symbol`
- `decision` — `BUY | SELL | WAIT | HOLD | SCALE | AVOID`
- `strategy` — `"historical_momentum_proxy"` in v1
- `band` — `"replay"` in v1
- `confidence` — float 0–1
- `price_at_decision` — float
- `priority` — float (0.0 in v1)
- `validation_status` — `"historical_replay"` in v1
- `reason` — plain-text explanation
- `lookback_features` — `{return_5d, sma20, above_sma20}`
- `resolved` — bool
- `resolved_at` — ISO date or null
- `days_elapsed` — int or null
- `price_at_resolution` — float or null
- `return_pct` — float or null
- `direction_correct` — bool or null
- `window_days` — 1 | 3 | 7 or null
- `outcome_price` — float or null

### `outputs/backtest/historical_calibration.json`

Required top-level fields:

- `generated_at`
- `source` — `"historical_replay"`
- `observe_only` — always `true`
- `total_resolved`
- `overall_hit_rate`
- `overall_avg_return`
- `by_confidence_bucket` — `{low, medium, high, unknown}` each with `{count, hit_rate, avg_return}`
- `by_decision` — keyed by decision type
- `by_strategy` — keyed by strategy name

### `outputs/backtest/historical_performance_attribution.json`

Required top-level fields:

- `generated_at`
- `source` — `"historical_replay"`
- `observe_only` — always `true`
- `total_decisions`
- `resolved_decisions`
- `hit_rate`
- `avg_return`
- `by_decision`
- `by_strategy`
- `best_decision` — `{symbol, date, decision, return_pct, direction_correct}` or null
- `worst_decision` — same shape or null

### `outputs/latest/confidence_calibration.json`

Enhanced confidence calibration report. Written by `write_confidence_calibration_report()` on every pipeline run.

Required top-level fields:

- `generated_at` — ISO timestamp string
- `observe_only` — always `true`
- `available` — bool
- `insufficient_data` — bool; `true` when fewer than `min_required` resolved decisions exist
- `total_resolved` — int
- `min_required` — int (default 20)
- `overall_hit_rate` — float or null
- `overall_average_confidence` — float or null
- `overall_calibration_gap` — float or null (`average_confidence - hit_rate`)
- `buckets_5` — **always an array of exactly 5 objects** regardless of `insufficient_data`; each has `{label, lower, upper, count, hit_rate, average_confidence, calibration_gap}`; labels are `very_low`, `low`, `medium`, `high`, `very_high`
- `signal_results` — array; may be empty; each has `{signal_id, known_in_registry, discovery_only, count, hit_rate, average_confidence, calibration_gap, overconfident, underconfident, suggested_review, note}`
- `dq_warnings` — array of strings; data quality warnings from `data_quality_report.json`
- `summary_line` — human-readable string

**Schema-stability guarantee:** `buckets_5` always has 5 entries. `insufficient_data=true` affects metric values (nulls, zeros), not the schema shape. Consumers may iterate all 5 buckets unconditionally.

### `outputs/policy/confidence_calibration.json`

Legacy confidence calibration report. Read by the GUI operator data layer. Written by `run_calibration()` on every pipeline run.

Required top-level fields:

- `generated_at` — ISO timestamp string
- `observe_only` — always `true`
- `available` — bool
- `insufficient_data` — bool
- `total_resolved` — int
- `min_required` — int
- `overall_hit_rate` — float or null
- `overall_avg_return` — float or null
- `confidence_buckets` — 3-bucket dict `{low, medium, high, unknown}` each with `{count, hit_rate, avg_return}`; may be empty dict when `insufficient_data=true`
- `validation_analysis` — dict keyed by validation status; may be empty
- `decision_analysis` — dict keyed by decision type; may be empty
- `insights` — array of strings (max 5); may be empty
- `summary_line` — human-readable string

## Discovery Engine Sandbox Artifacts

All four artifacts are written to `outputs/sandbox/discovery/` by `RunMode.DISCOVERY` only.
Official modes (DAILY, MANUAL_UPDATE, WEEKLY_REVIEW) cannot write these paths.

### `outputs/sandbox/discovery/emerging_candidates.json`

| Field | Type | Always present |
|---|---|---|
| `generated_at` | ISO timestamp | Yes |
| `run_id` | string | Yes |
| `observe_only` | `true` | Yes |
| `discovery_only` | `true` | Yes |
| `sandbox_only` | `true` | Yes |
| `disclaimer` | string | Yes |
| `total_candidates` | int | Yes |
| `watch_count` | int | Yes |
| `discovered_count` | int | Yes |
| `candidates` | array | Yes (may be empty) |

Each candidate object: `ticker`, `status` (discovered/watch/rejected), `score`, `mention_count`, `unique_source_count`, `event_type`, `event_confidence`, `risk_flag`, `rejection_reason`, `discovery_only`, `sandbox_only`, `corroboration_required` (true), `corroboration_met` (false), `corroboration_sources` ([]), `first_seen`, `last_seen`, `evidence_snippets`.

### `outputs/sandbox/discovery/rejected_candidates.json`

Same top-level shape as `emerging_candidates.json`. Contains only REJECTED candidates.
`total_rejected` instead of `total_candidates`.

### `outputs/sandbox/discovery/discovery_memory.json`

| Field | Type | Always present |
|---|---|---|
| `generated_at` | ISO timestamp | Yes |
| `discovery_only` | `true` | Yes |
| `sandbox_only` | `true` | Yes |
| `entry_count` | int | Yes |
| `entries` | array | Yes (may be empty) |

Each memory entry: `ticker`, `first_seen`, `last_seen`, `mention_count`, `source_count`, `seen_runs`, `status`, `last_score`, `last_event_type`, `rejected_reason`, `discovery_only`, `sandbox_only`.

### `outputs/sandbox/discovery/discovery_memo_section.md`

Markdown. Always contains the disclaimer: *"Discovery candidates are not buy/sell recommendations."*
Always states: *"Official watchlist and recommendations were not modified."*
