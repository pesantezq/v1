# State Schema

Last verified against `state_store.py`, `watchlist_scanner/state.py`, `theme_engine/theme_store.py`, and live `data/portfolio.db` schema on 2026-04-28.

## Primary Database

Path:
`data/portfolio.db`

Owner:
`state_store.py`

Design:
single SQLite database used for run history, cooldown state, evaluation memory, and supporting system health.

## Core Tables

### `run_history`

Purpose:
one row per attempted run; idempotency anchor for `main.py`

Columns:

- `run_id` PK
- `run_date`
- `mode`
- `status`
- `started_at`
- `completed_at`

### `snapshots`

Purpose:
end-of-run portfolio summary metrics

Columns:

- `id` PK
- `run_id` FK to `run_history.run_id`
- `total_value`
- `cash`
- `max_drift`
- `recorded_at`
- `drawdown_regime`

### `email_history`

Purpose:
digest deduplication

Columns:

- `digest_hash`
- `mode`
- `sent_at`

Composite primary key:
`(digest_hash, mode)`

### `portfolio_peaks`

Purpose:
persist drawdown reference peaks for SQL access

Columns:

- `peak_key` PK
- `peak_value`
- `updated_at`

## Theme And Alert Memory

### `theme_signals`

Purpose:
persist daily theme detections used by theme history and stale fallback

Columns:

- `id` PK
- `run_date`
- `theme_name`
- `confidence`
- `rationale`
- `evidence_items`
- `direct_mentions`
- `recorded_at`

### `alert_events`

Purpose:
cooldown and repeat-alert suppression state

Columns:

- `fingerprint` PK
- `first_seen`
- `last_seen`
- `last_emailed`
- `times_seen`
- `severity`
- `state_hash`
- `alert_tier`
- `reason_code`
- `last_signal_score`
- `last_confidence_score`
- `last_action_taken`

### `watchlist_alert_outcomes`

Purpose:
lifecycle tracking for surfaced alerts

Columns:

- `id` PK
- `fingerprint`
- `state_hash`
- `ticker`
- `watchlist_source`
- `surfaced_at`
- `last_seen_at`
- `notification_status`
- `alert_priority`
- `alert_quality_tier`
- `confirmation_count`
- `evidence_breadth`
- `portfolio_priority`
- `overlap_penalty`
- `diversification_bonus`
- `existing_position_relevance`
- `budget_fit`
- `baseline_price`
- `baseline_signal_score`
- `baseline_confidence_score`
- `evaluation_window`
- `evaluation_price`
- `return_pct`
- `evaluated_at`
- `outcome_label`
- `outcome_status`
- `outcome_pending`
- `resolved_at`

Relationship:
many alert surfaces can share a fingerprint over time if state changes produce new lifecycle rows.

### `watchlist_signal_feedback`

Purpose:
forward-outcome tracking for every tracked watchlist signal row

Columns:

- `id` PK
- `signal_key` unique
- `ticker`
- `signal_time`
- `watchlist_source`
- `signal_score`
- `confidence_score`
- `effective_score`
- `price_at_signal`
- `prediction_intent`
- `data_mode`
- `degraded_mode`
- `outcome_return_1d`
- `outcome_success_1d`
- `direction_correct_1d`
- `outcome_price_1d`
- `evaluated_at_1d`
- `outcome_return_3d`
- `outcome_success_3d`
- `direction_correct_3d`
- `outcome_price_3d`
- `evaluated_at_3d`
- `outcome_return_7d`
- `outcome_success_7d`
- `direction_correct_7d`
- `outcome_price_7d`
- `evaluated_at_7d`
- `conviction_score`
- `conviction_band`
- `normalized_allocation`
- `regime_label`
- `regime_confidence`
- `regime_data_quality`
- `theme_alignment_score`
- `theme_top_name`
- `theme_type`
- `portfolio_fit_score`
- `portfolio_fit_label`
- `final_rank_score`
- `augmented_signal_score`

Relationship:
this is the main watchlist learning table used to summarize future performance.

## Safety And Health Tables

### `subsystem_health`

Purpose:
circuit breaker state for fragile subsystems such as FMP

Columns:

- `subsystem` PK
- `consecutive_failures`
- `disabled_until`
- `last_error`
- `last_success`

### `structural_violations`

Purpose:
persist duration and escalation level for unresolved guardrail violations

Columns:

- `violation_key` PK
- `first_seen`
- `last_seen`
- `days_active`
- `escalation_level`
- `last_emailed`

### `cash_ledger`

Purpose:
persistent cash ledger used to seed or override config cash balance

Columns:

- `id` PK
- `timestamp`
- `type`
- `amount`
- `note`

### `extended_watchlist`

Purpose:
temporary theme-promoted watchlist membership state

Columns:

- `symbol` PK
- `is_active`
- `promoted_at`
- `expires_at`
- `last_reinforced`
- `theme_name`
- `theme_names`
- `theme_confidence`
- `mention_count`
- `scan_count`
- `alert_count`
- `outcome`
- `drop_reason`

## JSONL State

### `outputs/policy/recommendation_history.jsonl`

Writer:
`policy_evaluator/history_writer.py`

Purpose:
append-only history of scored finance recommendations for evaluation

Normalized fields include:

- `run_id`
- `timestamp`
- `run_mode`
- `regime`
- `degraded_mode`
- `degraded_reason`
- `degraded_confidence_penalty`
- `data_mode`
- `has_guardrail_violations`
- `guardrail_violation_types`
- `growth_mode`
- `drawdown_pct`
- `drawdown_regime`
- `rec_id`
- `rec_base_id`
- `impact_area`
- `title`
- `score`
- `raw_score`
- `action_level`
- `severity`
- `persistence_score`
- `impact_score`
- `priority`
- `confidence`
- `trigger`

## Lifecycle Relationships

- `run_history -> snapshots`
  One run can produce one snapshot row.
- `alert_events -> watchlist_alert_outcomes`
  Shared fingerprint and state-hash logic ties cooldown history to surfaced-alert outcome rows.
- `watchlist_signal_feedback`
  Independent from surfaced alerts; tracks all results eligible for learning.
- `theme_signals -> watch_candidates/theme_signals.json`
  SQLite persists history while JSON artifacts expose latest run state.

## Invariants

- Table additions should be additive migrations whenever possible.
- Existing column names are part of the de facto contract for GUI, summaries, and tests.
- State tables must support degraded operation; absence of some rows should reduce certainty, not crash the pipeline.
