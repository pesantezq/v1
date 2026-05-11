# Output Artifact Contracts

Last verified against live files in `outputs/latest`, `outputs/portfolio`, `outputs/policy`, `outputs/performance`, `outputs/regime`, `outputs/backtest`, `outputs/sandbox/discovery/`, plus `gui_operator_data.py`, `watchlist_scanner/output_writers.py`, `portfolio_automation/ai_budget.py`, `portfolio_automation/ai_decision_validator.py`, `portfolio_automation/decision_outcome_tracker.py`, `portfolio_automation/historical_replay/replay_reports.py`, and `portfolio_automation/discovery/discovery_reports.py`.

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

### `outputs/latest/data_quality_report.json`

Observe-only data quality report. Written with `safe_write_json(OutputNamespace.LATEST, ...)` by `write_data_quality_report()`.

Required top-level fields:

- `generated_at` — ISO timestamp string
- `observe_only` — always `true`
- `available` — bool; may be `false` when no records were available
- `total_symbols` — int
- `healthy_symbols` — int
- `warning_symbols` — int
- `critical_symbols` — int
- `missing_price_count` — int
- `missing_fundamentals_count` — int
- `missing_news_count` — int
- `stale_price_count` — int
- `fallback_count` — int
- `cached_count` — int
- `source_counts` — object keyed by source label
- `summary_line` — human-readable string
- `issues` — array of aggregate issue objects
- `symbols` — array of per-symbol data quality reports

Issue objects include `issue_type`, `severity`, `symbol`, `source`, `message`, and `metadata`.
Per-symbol reports include symbol-level source/status flags plus an `issues` array.

### `outputs/latest/data_quality_report.md`

Markdown companion to `data_quality_report.json`. Written with `safe_write_text(OutputNamespace.LATEST, ...)`.
It is operator-facing documentation only and must not be parsed as the machine contract.

### `outputs/latest/ai_budget_summary.json`

Observe-only AI usage budget summary. Written with `safe_write_json(OutputNamespace.LATEST, ...)` by `write_ai_budget_summary()`.

Required top-level fields:

- `generated_at` — ISO timestamp string
- `observe_only` — bool; `true` by default
- `enabled` — bool
- `daily_token_total` — int
- `daily_cost_total_usd` — float
- `monthly_cost_total_usd` — float
- `daily_cost_limit_usd` — float or null
- `monthly_cost_limit_usd` — float or null
- `warning` — bool
- `blocked` — bool
- `warnings` — array of strings
- `summary_line` — human-readable string
- `event_count` — int
- `events` — array of daily AI usage event objects

Contract note: the raw artifact does not include an `available` field. GUI loaders may add `available=True` at read time for display normalization, but producers are not required to write it.

### `outputs/latest/ai_budget_summary.md`

Markdown companion to `ai_budget_summary.json`. Written with `safe_write_text(OutputNamespace.LATEST, ...)`.
It is operator-facing documentation only and must not be parsed as the machine contract.

### `outputs/policy/ai_usage_events.jsonl`

Append-only AI usage event log. Written by `record_ai_usage_event()` after each real LLM call.
One JSON object per line; new lines are appended on each call (no full rewrite).

Required per-event fields:

- `timestamp` — ISO 8601 timestamp string (UTC) when the event was recorded
- `task_name` — string identifying the caller; currently `"ai_decision_validator"`
- `provider` — LLM provider string: `"anthropic"`, `"openai"`, `"ollama"`, or `"local"`
- `model` — model name string (e.g. `"gemma3:4b"`, `"claude-haiku-4-5-20251001"`)
- `run_id` — string or `null`; pipeline run identifier when available
- `prompt_tokens` — int; estimated input token count (see note on estimation below)
- `completion_tokens` — int; estimated output token count; `0` on error or empty response
- `total_tokens` — int; `prompt_tokens + completion_tokens`
- `estimated_cost_usd` — float; cost estimate in USD; `0.0` for free/unknown providers
- `allowed` — bool; `true` when budget policy allowed the call; always `true` in observe-only mode
- `blocked_reason` — string or `null`; set only when `allowed=false`
- `metadata` — object with optional observability fields:
  - `usage_source` — always `"estimated_from_length"` for `ai_decision_validator` events;
    token counts are derived from text length (`len(text) // 4`), not from the provider response
  - `status` — `"success"` or `"error"`
  - `output_accepted` — bool; `true` when the LLM response was long enough to use as an
    enhancement; `false` for empty/short responses that were silently discarded
  - `fallback_reason` — `"empty_response"` or `"short_response"` when `output_accepted=false`;
    absent when `output_accepted=true`
  - `error` — string (≤200 chars) describing the exception when `status="error"`; absent on success
  - `unknown_pricing` — bool; set by the budget layer when no pricing data exists for the model

Contract notes:

- Token counts for `ai_decision_validator` are **estimated** from text length because
  `call_provider()` returns plain text only, with no API response object carrying usage metadata.
  Events are tagged `usage_source="estimated_from_length"` to document this.
- A usage event is recorded for **every completed provider call** — including calls whose output
  was empty or too short to use (empty/short responses fall back silently but still incur cost).
- Provider exceptions produce an event with `status="error"` and `completion_tokens=0`.
- If `record_ai_usage_event` itself fails, a warning is logged and the pipeline continues.
  The event log may be incomplete under filesystem errors.
- This artifact is **observability and cost-control only**. It does not influence portfolio
  decisions, scoring, allocation, recommendations, or discovery behavior.
- The LLM call path is opt-in (`AI_VALIDATOR_USE_LLM=1`). No events are recorded when
  the validator runs in its default deterministic mode.

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

All four artifacts are written to `outputs/sandbox/discovery/` by sandbox-writable research modes.
Current allowed modes:

- `RunMode.DISCOVERY`
- `RunMode.BACKTEST`

Blocked modes:

- `RunMode.DAILY`
- `RunMode.MANUAL_UPDATE`
- `RunMode.WEEKLY_REVIEW`
- `RunMode.HISTORICAL_REPLAY`

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

Each candidate object:

| Field | Type | Notes |
|---|---|---|
| `ticker` | string | |
| `status` | string | `discovered`, `watch`, or `rejected` |
| `score` | float | Base relevance score |
| `mention_count` | int | |
| `unique_source_count` | int | |
| `event_type` | string | |
| `event_confidence` | float | |
| `risk_flag` | bool | |
| `rejection_reason` | string\|null | |
| `discovery_only` | bool | Always `true` |
| `sandbox_only` | bool | Always `true` |
| `corroboration_required` | bool | Always `true` |
| `corroboration_met` | bool | `true` when `corroboration_score >= 0.65` |
| `corroboration_score` | float | 0.0–1.0 composite; source_diversity 35%, mention 20%, event_strength 25%, persistence 20%, risk_penalty −0.20 |
| `corroboration_level` | string | `none` (<0.30), `weak` (0.30–0.50), `moderate` (0.50–0.65), `strong` (≥0.65) |
| `corroboration_sources` | array | Unique source names contributing evidence |
| `first_seen` | ISO timestamp | |
| `last_seen` | ISO timestamp | |
| `evidence_snippets` | array | Up to 3 text snippets |

### `outputs/sandbox/discovery/rejected_candidates.json`

Contains only REJECTED candidates. Carries `observe_only`, `discovery_only`, `sandbox_only`, and `disclaimer` like `emerging_candidates.json`, but has a different top-level shape: uses `total_rejected` (not `total_candidates`) and does **not** include `watch_count` or `discovered_count`.

Required top-level fields:

- `generated_at` - ISO timestamp
- `run_id` - string
- `observe_only` - always `true`
- `discovery_only` - always `true`
- `sandbox_only` - always `true`
- `disclaimer` - warning string
- `total_rejected` - int
- `candidates` - array of rejected candidate objects

Compatibility note: runtime writers use the top-level `candidates` key. GUI loaders also tolerate the older `rejected_candidates` key for backward-compatible fixture reads.

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

### `outputs/sandbox/discovery/approval_decisions.jsonl`

Append-only JSONL. One JSON object per line. Written by the GUI approval workflow via `approval_workflow.record_approval_decision()`.

Per-decision fields:

| Field | Type | Always present |
|---|---|---|
| `generated_at` | ISO timestamp | Yes |
| `symbol` | string (uppercase) | Yes |
| `company_name` | string | Yes (may be empty) |
| `candidate_status` | string | Yes |
| `corroboration_score` | float | Yes |
| `corroboration_level` | string | Yes |
| `decision` | string | Yes — one of four allowed values |
| `decision_reason` | string | Yes (may be empty) |
| `operator` | string | Yes |
| `source_artifact` | string | Yes |
| `run_id` | string | Yes |
| `observe_only` | `true` | Always |
| `sandbox_only` | `true` | Always |
| `no_trade` | `true` | Always |
| `no_official_promotion` | `true` | Always |

Allowed `decision` values: `approve_for_research_review`, `keep_watching`, `reject_candidate`, `needs_more_evidence`.
Forbidden `decision` values (never written): `buy`, `sell`, `actionable`, `promoted`, `validated`.

This file is never written outside `outputs/sandbox/discovery/`. Governance flags are validated before every append. Loaders skip malformed JSONL lines and semantically tampered lines, including forbidden decision values or governance flags that are missing or not strictly `true`.

No separate approval summary artifact is written. Approval summaries are computed in memory from valid `approval_decisions.jsonl` records by the GUI and approval workflow helpers.

### `outputs/sandbox/discovery/replay_results.json`

Written by `discovery_replay.write_discovery_replay_report()`. Sandbox-only. Produced in DISCOVERY or BACKTEST run modes only.

Top-level governance flags (always `true`): `observe_only`, `sandbox_only`, `no_trade`, `no_official_promotion`.

Required top-level fields:

| Field | Type | Description |
|---|---|---|
| `generated_at` | ISO timestamp | When the report was produced |
| `observe_only` | `true` | Always true |
| `sandbox_only` | `true` | Always true |
| `no_trade` | `true` | Always true |
| `no_official_promotion` | `true` | Always true |
| `insufficient_data` | bool | True when no candidates have price data |
| `disclaimer` | string | Sandbox-only warning |
| `methodology` | string | Explanation of replay methodology |
| `disclaimers` | list[string] | List of safety statements |
| `candidate_count` | int | Total candidates evaluated |
| `resolved_count` | int | Candidates with price data available |
| `insufficient_data_count` | int | Candidates without price data |
| `summary` | object | Counts by status |
| `window_metrics` | object | Per-window aggregate metrics |
| `status_comparison` | object | WATCH vs DISCOVERED vs REJECTED aggregates |
| `corroboration_comparison` | object | High vs low corroboration aggregates |
| `approval_decision_comparison` | object | Per-decision type aggregates |
| `risk_comparison` | object | Risk-flagged vs non-risk aggregates |
| `rejected_candidate_review` | object | Rejected candidate summary |

Never contains BUY/SELL/ACTIONABLE/PROMOTED/VALIDATED status keys.

### `outputs/sandbox/discovery/replay_results.md`

Markdown companion to `replay_results.json`. Contains disclaimer, executive summary, data coverage, outcome metrics table (when data available), WATCH vs DISCOVERED comparison, corroboration analysis, approval decision analysis, rejected/risk summary, insufficient data notes, and recommended future research thresholds.

Always includes: `"SANDBOX ONLY"` header, `"No official recommendation or watchlist change is made by this report."`, sandbox-only closing statement.

### `outputs/sandbox/discovery/replay_candidate_outcomes.jsonl`

One JSON object per evaluated candidate. **Overwritten** on each replay run (not append-only). Each record carries per-window metrics (`forward_return_pct`, `direction_correct`, `max_drawdown_pct`, `max_runup_pct`), candidate metadata, `insufficient_data` flag, and governance flags (`observe_only=true`, `sandbox_only=true`, `no_trade=true`, `discovery_only=true`).

Never contains candidates with forbidden statuses (buy/sell/actionable/promoted/validated).

### Daily Memo — Discovery Research Section

The daily memo (`outputs/latest/daily_memo.txt` and `outputs/latest/daily_memo.md`) includes a **DISCOVERY RESEARCH [Sandbox Only]** section when sandbox discovery artifacts are present. This section:

- Is produced by `generate_daily_memo()` in `watchlist_scanner/daily_memo.py`
- Reads sandbox artifacts (above four files) as **read-only inputs**; never writes to sandbox
- Is omitted (not an error) when no sandbox discovery artifacts exist
- Validates approval records via `is_valid_loaded_approval_record()` before rendering; tampered records silently excluded
- Never emits BUY/SELL/ACTIONABLE/PROMOTED/VALIDATED language
- Always includes the disclaimer: *"Discovery candidates are sandbox research only. They are not buy/sell recommendations and do not update the official watchlist or portfolio."*

The discovery section does **not** produce a new artifact — it is part of the standard memo outputs (`daily_memo.txt`, `daily_memo.md`) that already exist.

---

## Memo Email Delivery Artifacts

Produced by `portfolio_automation/memo_email_sender.py`.

**Feature is disabled by default** (`MEMO_EMAIL_ENABLED=0`).  No SMTP connections are ever made unless explicitly enabled.

### `outputs/latest/memo_delivery_status.json`

| Field | Type | Description |
|---|---|---|
| `generated_at` | string (ISO 8601) | Timestamp of delivery attempt |
| `observe_only` | bool | Always `true` |
| `no_trade` | bool | Always `true` |
| `available` | bool | `true` when memo files were found |
| `enabled` | bool | Whether MEMO_EMAIL_ENABLED was set |
| `dry_run` | bool | Whether dry-run mode was active |
| `attempted` | bool | Whether an SMTP connection was tried |
| `sent` | bool | Whether the message was sent |
| `skipped` | bool | Whether delivery was skipped (and why) |
| `reason` | string | `disabled`, `dry_run`, `sent`, `already_sent`, `memo_file_missing`, `missing_smtp_config`, `invalid_or_missing_recipients`, `smtp_error`, etc. |
| `run_id` | string | `YYYY-MM-DD_memo_delivery` or caller-supplied |
| `memo_date` | string | `YYYY-MM-DD` date of delivery attempt |
| `memo_source_txt` | string | Path to `daily_memo.txt` read |
| `memo_source_md` | string | Path to `daily_memo.md` read |
| `recipients_count` | int | Number of To recipients |
| `cc_count` | int | Number of CC recipients |
| `bcc_count` | int | Number of BCC recipients |
| `smtp_host_present` | bool | Whether MEMO_EMAIL_SMTP_HOST was set |
| `username_present` | bool | Whether MEMO_EMAIL_USERNAME was set |
| `error_class` | string | Exception class name on failure, else `null` |
| `error_message_sanitized` | string | Sanitized error (password/secret redacted), else `null` |

**Never contains**: SMTP password, raw credentials, full recipient list beyond count.

### `outputs/policy/memo_delivery_log.jsonl`

Append-only log of every delivery attempt.  One JSON object per line.

| Field | Description |
|---|---|
| `generated_at` | ISO 8601 timestamp |
| `run_id` | Run identifier |
| `memo_date` | `YYYY-MM-DD` |
| `enabled` | Feature enabled flag |
| `dry_run` | Dry-run flag |
| `attempted` | Whether SMTP connection was attempted |
| `sent` | Whether message was delivered |
| `skipped` | Whether delivery was skipped |
| `reason` | Reason string |
| `recipients_count` | To-recipient count |
| `error_class` | Exception class or `null` |
| `observe_only` | Always `true` |
| `no_trade` | Always `true` |

**Never contains**: SMTP password, raw credentials, or sensitive exception dumps.

---

## News Intelligence Artifacts

### `outputs/latest/news_intelligence.json`

Namespace: LATEST. Written by `portfolio_automation/news/fmp_news_intelligence.py`.

Top-level fields:

| Field | Type | Description |
|---|---|---|
| `generated_at` | string | ISO 8601 timestamp |
| `observe_only` | bool | Always `true` |
| `no_trade` | bool | Always `true` |
| `not_recommendation` | bool | Always `true` |
| `source` | string | `"fmp_news_intelligence_layer"` |
| `run_mode` | string | Run mode string |
| `article_count_raw` | int | Input article count |
| `article_count_normalized` | int | After normalization |
| `article_count_deduped` | int | After deduplication |
| `evidence_packet_count` | int | Total evidence packets |
| `official_monitoring_count` | int | Packets in official monitoring lane |
| `sandbox_count` | int | Packets in sandbox lane |
| `disclaimer` | string | Safety disclaimer |
| `evidence_packets` | array | List of evidence packet objects |

Evidence packet fields:

| Field | Type | Description |
|---|---|---|
| `entity_key` | string | Ticker symbol |
| `entity_type` | string | `"ticker"` |
| `related_tickers` | array | Associated tickers |
| `article_count` | int | Articles mentioning this entity |
| `source_count` | int | Unique source count |
| `latest_published_at` | string | Newest article timestamp |
| `themes` | array | Top theme names |
| `risk_flags` | array | Detected risk keywords |
| `catalyst_flags` | array | Detected catalyst keywords |
| `sentiment_hint` | string | `positive`, `negative`, `mixed`, or `neutral` |
| `article_refs` | array | Article title/url/date/source references (up to 10) |
| `summary_bullets` | array | Top 3 article titles as bullets |
| `evidence_lane` | string | `official_monitoring` or `sandbox_discovery_research` |
| `observe_only` | bool | Always `true` |
| `no_trade` | bool | Always `true` |
| `not_recommendation` | bool | Always `true` |

### `outputs/latest/news_intelligence.md`

Namespace: LATEST. Human-readable Markdown report. Contains disclaimer, official monitoring section, and sandbox research section. No BUY/SELL/HOLD language.

### `outputs/sandbox/discovery/news_candidate_evidence.json`

Namespace: SANDBOX. Written only when sandbox-lane evidence packets exist. Contains same evidence packet structure as above, filtered to `evidence_lane: sandbox_discovery_research`. Includes all safety flags.

---

## Discovery News Integration Artifacts

Sandbox-only discovery artifacts written by `portfolio_automation/discovery/news_integration.py`.
Both artifacts are written under `OutputNamespace.SANDBOX` and are research context only.
They do **not** mutate official watchlists, portfolio state, recommendations, allocation,
scoring, broker/API execution, or auto-trading behavior.

Top-level governance flags for the JSON artifact are always:

- `observe_only: true`
- `no_trade: true`
- `not_recommendation: true`
- `discovery_only: true`

### `outputs/sandbox/discovery/news_enriched_candidates.json`

Namespace: SANDBOX. Enriches sandbox discovery candidates using
`outputs/latest/news_intelligence.json` as read-only input. Also reads sandbox
discovery candidate artifacts as input. It does not write to `outputs/latest`,
`outputs/policy`, `outputs/portfolio`, or official state.

Top-level fields:

| Field | Type | Description |
|---|---|---|
| `generated_at` | string | ISO 8601 timestamp |
| `run_id` | string | Run identifier |
| `run_mode` | string | Run mode used for the write |
| `observe_only` | bool | Always `true` |
| `no_trade` | bool | Always `true` |
| `not_recommendation` | bool | Always `true` |
| `discovery_only` | bool | Always `true` |
| `source` | string | `"discovery_news_integration"` |
| `disclaimer` | string | Sandbox-only safety disclaimer |
| `total_enriched` | int | Total enriched records |
| `with_news_count` | int | Records with matched news evidence |
| `research_caution_count` | int | Records with risk-heavy news context |
| `research_supported_count` | int | Records with catalyst-supported news context |
| `news_only_count` | int | News-only tickers without existing discovery candidate records |
| `enriched_candidates` | array | Sandbox enriched candidate records |

Enriched candidate fields:

| Field | Type | Description |
|---|---|---|
| `ticker` | string | Candidate or news-only ticker |
| `candidate_status` | string | Original sandbox status or `news_only`; never BUY/SELL/ACTIONABLE/PROMOTED/VALIDATED |
| `discovery_only` | bool | Always `true` |
| `observe_only` | bool | Always `true` |
| `no_trade` | bool | Always `true` |
| `not_recommendation` | bool | Always `true` |
| `matched_news_count` | int | Total matched article count |
| `matched_evidence_packets` | int | Number of matched evidence packets |
| `source_diversity` | int | Aggregate source count from matched packets |
| `matched_themes` | array | News themes matched to the ticker |
| `catalyst_flags` | array | Catalyst flags from matched evidence |
| `risk_flags` | array | Risk flags from matched evidence |
| `news_relevance_score` | float | Deterministic news relevance score |
| `corroboration_news_score` | float | Deterministic news corroboration context score |
| `news_context` | string | `research_supported`, `research_caution`, `research_neutral`, or `no_news` |
| `latest_news_headlines` | array | Matched headlines, capped for readability |
| `integration_reason` | string | Human-readable evidence match explanation |
| `safety_disclaimer` | string | Sandbox-only disclaimer |
| `original_score` | number/null | Original sandbox discovery score when present |
| `original_mention_count` | int/null | Original mention count when present |
| `original_corroboration_score` | number/null | Original corroboration score when present |
| `first_seen` | string/null | Original first-seen timestamp when present |
| `last_seen` | string/null | Original last-seen timestamp when present |

### `outputs/sandbox/discovery/news_integration_summary.md`

Namespace: SANDBOX. Human-readable sandbox summary for Discovery News Integration.
It summarizes enriched candidate counts, news-supported research context,
risk-heavy research context, and news-only tickers. It includes a sandbox-only
disclaimer and does not emit BUY/SELL/HOLD recommendations or official promotion
instructions.

---

## Market Narrative Artifacts

All six narrative artifacts are written to `OutputNamespace.LATEST` by `portfolio_automation/market_narratives.py`. All are observe-only; none mutate scoring, allocation, recommendations, official watchlist, or portfolio state.

### Top-level fields (all six artifacts share this shape)

| Field | Type | Description |
|---|---|---|
| `narrative_period` | string | `"daily"`, `"weekly"`, or `"monthly"` |
| `generated_at` | string | ISO 8601 timestamp |
| `observe_only` | bool | Always `true` |
| `no_trade` | bool | Always `true` |
| `not_recommendation` | bool | Always `true` |
| `source` | string | `"market_narratives_layer"` |
| `data_available` | bool | Whether any input artifact was available |
| `top_headline` | string | One-line narrative headline |
| `executive_summary` | string | 2–4 sentence period summary |
| `key_themes` | array | Top themes (theme, signal_count, sources, description) |
| `portfolio_context` | string | Brief portfolio context from decision plan |
| `discovery_context` | object/null | Sandbox-only discovery research context |
| `risks_to_watch` | array | Risk signals (label, tickers, description) |
| `catalysts_to_watch` | array | Catalyst signals (label, tickers, description) |
| `data_quality_notes` | array | Data quality warnings |
| `confidence_notes` | array | Calibration context |
| `operator_watchlist` | array | Review items (no trading commands) |
| `inputs_used` | array | Per-artifact availability records |
| `missing_inputs` | array | List of unavailable input artifact names |
| `prohibited_actions_detected` | array | Safety validator output (should be empty) |
| `safety_disclaimer` | string | Mandatory safety disclaimer |

### `discovery_context` object

| Field | Type | Description |
|---|---|---|
| `candidate_count` | int | Total sandbox research candidates |
| `watch_count` | int | Candidates at WATCH status |
| `news_supported` | array | Tickers with positive news context |
| `risk_heavy` | array | Tickers with risk-heavy news context |
| `news_only` | array | News-only tickers needing corroboration |
| `top_themes` | array | Top sandbox themes |
| `disclaimer` | string | Sandbox-only disclaimer |

### Artifact paths

| Artifact | Path |
|---|---|
| `market_narrative_daily.json` | `outputs/latest/market_narrative_daily.json` |
| `market_narrative_daily.md` | `outputs/latest/market_narrative_daily.md` |
| `market_narrative_weekly.json` | `outputs/latest/market_narrative_weekly.json` |
| `market_narrative_weekly.md` | `outputs/latest/market_narrative_weekly.md` |
| `market_narrative_monthly.json` | `outputs/latest/market_narrative_monthly.json` |
| `market_narrative_monthly.md` | `outputs/latest/market_narrative_monthly.md` |

**Never contains**: BUY/SELL/HOLD trading instructions, official recommendations, broker/execution commands, or any modification of official portfolio/watchlist/allocation state.
