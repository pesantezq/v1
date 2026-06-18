# Crowd Intelligence — Capability Store

## Purpose

`portfolio_automation/crowd_intelligence/capability_store.py` is the SQLite
persistence layer for the Crowd Intelligence subsystem (Lane B). It owns a
dedicated database, `data/crowd_intelligence.db`, isolated from `portfolio.db`
and `fmp_budget.db`. It stores FMP endpoint capability-probe results plus the
raw crowd events and per-day composite crowd signals.

---

## Two-Lane Governance

Observe-only data substrate, **simulation-active / production-gated**. It writes
only into its own database; it never reads or mutates `decision_plan.json`,
allocations, or scoring. Any production use of crowd data flows through the
human-approved `sim_governance` promotion workflow.

---

## Schema (data substrate)

`data/crowd_intelligence.db` — three tables created idempotently on open:

- `fmp_endpoint_capabilities` — `endpoint_id` PK, `status`, `http_status`,
  `response_bytes`, `sample_fields` (JSON), `last_checked_at`, `error_summary`.
- `crowd_raw_events` — `provider`, `endpoint_id`, `symbol`, `category`,
  `event_time`, `normalized_event_type`, `raw_json`, `fetched_at` (indexed by
  `symbol`).
- `crowd_signal_daily` — per `(symbol, signal_date)` unique row: the five
  category scores plus `social_sentiment_score`, `composite_crowd_score`,
  `confidence`, enabled/disabled source JSON, `explanation_json`, `created_at`.

---

## Key API — `class CapabilityStore`

- `CapabilityStore(db_path)` — opens/creates the DB and applies the DDL.
- `upsert(records) -> int` — upsert endpoint-capability rows (conflict on
  `endpoint_id`).
- `all_rows() -> list[dict]` — capability rows ordered by `endpoint_id`
  (`sample_fields` decoded back to a list).
- `record_events(events) -> int` — append raw crowd events.
- `upsert_daily(rows) -> int` — upsert per-`(symbol, signal_date)` composite
  signal rows.
- `daily_rows() -> list[dict]` — all daily rows ordered by `(symbol,
  signal_date)` (used to compute day-over-day trend).
- `raw_event_count() -> int` — count of stored raw events.

---

## Tests

Covered under `tests/` with the crowd-intelligence suite
(`python -m pytest -q tests -k crowd`).
