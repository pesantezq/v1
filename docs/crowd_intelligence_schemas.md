# Crowd Intelligence — Schemas

## Purpose

`portfolio_automation/crowd_intelligence/schemas.py` defines the normalized
record shapes for the Crowd Intelligence subsystem (Lane B — FMP crowd context).
It is a pure module (no I/O, no clock) so the category adapters, the builder, and
the artifact writer all agree on one contract. (One of two `schemas.py` modules;
see [`docs/schemas.md`](schemas.md) for disambiguation.)

---

## Two-Lane Governance

Pure data contract for observe-only context. The shapes here carry no decision,
allocation, or scoring authority; they describe crowd context only. Production
use of any crowd context flows through the human-approved `sim_governance`
promotion workflow.

---

## Data Model

- `CATEGORIES = ("news", "analyst", "insider", "congress", "attention",
  "social_sentiment")` — the canonical category order.
- `@dataclass NormalizedEvent` — one raw crowd event: `provider`, `endpoint_id`,
  `symbol`, `category`, `event_time`, `normalized_event_type`, `raw` (the
  trimmed source payload).
- `@dataclass CategoryResult` — one category's contribution for one symbol:
  `category`, `score` (clamped `[-1, 1]`), `reasons`, `warnings`, `events`,
  `enabled_endpoints`, `disabled_endpoints`, `has_data`, `freshness` (`[0, 1]`).
  `neutral()` is true when there is no data or the score is exactly `0.0`.
- `@dataclass CrowdSignal` — the per-symbol composite: `symbol`,
  `composite_crowd_score`, `confidence`, `category_scores`, `enabled_sources`,
  `disabled_sources`, `top_reasons`, `warnings`, `data_freshness`,
  `source_records_count`, plus the day-over-day `composite_trend` and
  `trend_label` (default `"building"` until ≥2 days of history exist).

---

## Tests

Covered under `tests/` with the crowd-intelligence suite
(`python -m pytest -q tests -k crowd`).
