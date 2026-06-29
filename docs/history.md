# Social Sentiment History Tracker

## Purpose

`portfolio_automation/social_sentiment/history.py` keeps a bounded, append-only
daily ledger of social sentiment per `(ticker, source)` and classifies each
ticker's rolling trend. It lets the lane distinguish a one-day spike from a
sustained rise/fall, and supplies the `social_quality_state` (trend) field on the
unified crowd-bus extension artifact.

---

## Two-Lane Governance

This module is part of the **simulation-active / production-gated / sandbox-only**
social-sentiment lane. Its history and trend labels never feed
`outputs/latest/decision_plan.json` and never touch any score semantics
(`feeds_decision_engine=false`, `sandbox_only=true`).

---

## Data Substrate

- **Ledger:** a JSONL file (the pipeline uses
  `data/social_sentiment_history.jsonl`). Each line is one daily entry for one
  `(ticker, source)`: `{date, ticker, source, sentiment_score, confidence,
  sample_size}`.
- **Idempotent:** `record_daily(...)` skips writing if the same
  `(ticker, source, date)` already exists.
- **Bounded:** `_prune()` keeps only the last `MAX_HISTORY_DAYS = 30` distinct
  dates per `(ticker, source)`.

---

## Trend States

`compute_trend_state(ticker)` aggregates across sources (mean sentiment per day),
needs `MIN_HISTORY_DAYS = 5` points, then classifies the recent window:

- `building_history` — fewer than 5 daily points
- `mixed` — variance > 0.1 (no clear direction)
- `neutral` — `|mean| < 0.1` and `|slope| < 0.02`
- `positive_rising` / `positive_stable` — mean > 0.1, slope above / below threshold
- `negative_falling` / `negative_stable` — mean < -0.1, slope below / at threshold

Tuning constants: `_NEUTRAL_BAND = 0.1`, `_SLOPE_THRESHOLD = 0.02`. Slope is a
simple linear regression over the recent window.

---

## Key API

- `class SentimentHistoryTracker(ledger_path)`
  - `record_daily(ticker, source, sentiment_score, confidence, sample_size,
    date=None) -> None` — append one daily entry (idempotent, prunes after write).
  - `get_ticker_history(ticker) -> list[dict]` — all rows for a ticker, sorted by
    date ascending.
  - `compute_trend_state(ticker) -> str` — current trend label.
  - `get_summary() -> dict` — `{ledger_path, total_rows, unique_tickers,
    unique_sources, date_range}` for health/status reporting.

---

## Related Modules

`pipeline` (calls `record_daily` per scored source, `compute_trend_state` per
ticker) · `aggregator` (supplies the per-source sentiment recorded here).

---

## Tests

Covered under `tests/` with the social-sentiment suite
(`python -m pytest -q tests -k history`).
